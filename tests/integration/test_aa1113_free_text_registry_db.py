"""AA-1113 (CIS 449) — the gateway's stage read carries the free-text mark against a real Postgres.

conftest applies every migration on main. With 449 applied, a stage row marked ``free_text`` reads as marked through
``role_config._fetch_one`` and ``list_stage_configs`` (the admin GET), and every seeded tenant stage is marked. Before
449 (the code PR's own CI may run before the migration PR lands), the read falls back without the column: no error,
``free_text`` False, the ``t<n>_`` floor still scrubs. The store connects without TLS, like test_aa642_a1_stage_switch_db.
Invented stage names only; the inserted row is deleted at teardown.

The ledger (review S284): ``LLMClient.generate`` on ``t9_write`` with a planted e-mail and phone and a model key the
catalog does not hold (the $0 live probe's path) writes its row through the real ``call_log`` writer: the row is 0 USD,
``quality_signal.pii_scrub`` says scrubbed with the scrubbed request's ``scrubbed_hash``, and no column holds a planted
value. The probe rows (model ``zz-aa1113-ledger-no-model``) are deleted before and after.
"""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.llm_client import free_text_scrub, role_config  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "zz_aa1113_chat"


@pytest_asyncio.fixture
async def pool(monkeypatch):
    real_connect = asyncpg.connect

    async def connect(*a, **k):                 # CI Postgres has no SSL; the code asks for it
        k.pop("ssl", None)
        return await real_connect(DSN, **{key: v for key, v in k.items() if key == "timeout"})
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(role_config, "get_database_url", lambda: DSN)
    monkeypatch.setattr(role_config, "_free_text_column_missing_at", 0.0)
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    yield p
    await p.execute("DELETE FROM shared.llm_role_config WHERE stage = $1", STAGE)
    role_config.invalidate()
    await p.close()


async def _has_column(pool) -> bool:
    return bool(await pool.fetchval(
        "SELECT count(*) AS ok FROM information_schema.columns WHERE table_schema = 'shared' "
        "AND table_name = 'llm_role_config' AND column_name = 'free_text'"))


@pytest.mark.asyncio
async def test_the_stage_read_carries_the_mark(pool):
    has = await _has_column(pool)
    cols = ", free_text" if has else ""
    vals = ", true" if has else ""
    await pool.execute(
        "INSERT INTO shared.llm_role_config (stage, role, provider, model_id, is_active, updated_at, updated_by, "
        f"fallback_model_ids, shadow_sample_pct{cols}) VALUES ($1, 'writer', 'claude', 'zz-writer', true, now(), "
        f"'test', '{{}}', 0{vals})", STAGE)
    cfg = await role_config._fetch_one(STAGE)
    assert cfg is not None and cfg.free_text is has
    assert free_text_scrub.is_free_text_stage(STAGE, cfg) is has
    rows = {r["stage"]: r for r in await role_config.list_stage_configs()}
    assert rows[STAGE]["free_text"] is (True if has else None)


@pytest.mark.asyncio
async def test_every_seeded_tenant_stage_is_marked_once_449_is_applied(pool):
    if not await _has_column(pool):
        pytest.skip("migration 449 not applied on this database")
    rows = await pool.fetch("SELECT stage AS s, free_text AS f FROM shared.llm_role_config "
                            "WHERE stage ~ '^t[0-9]+_' AND pii_class <> 'pii'")
    assert rows and all(r["f"] for r in rows)
    for r in rows:
        cfg = await role_config._fetch_one(r["s"])
        assert cfg.free_text is True


# ── the ledger row of a scrubbed call, written by the real call_log writer ──────────────────────────────────────────

PROBE_KEY = "zz-aa1113-ledger-no-model"          # not in the catalog: refused after the scrub, before any attempt
PLANTED_EMAIL = "lantern.guest@example.test"      # invented values only
PLANTED_PHONE = "+84 900 000 222"


@pytest.fixture
def real_ledger(monkeypatch):
    """The gateway and the call_log writer on the test database (no TLS here), every cache cold."""
    from unittest.mock import patch

    from shared.llm_client import call_log, catalog
    from shared.llm_client.client import LLMClient
    real_connect = asyncpg.connect

    async def connect(*a, **k):
        k.pop("ssl", None)
        return await real_connect(DSN, **{key: v for key, v in k.items() if key == "timeout"})
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.delenv("SECRET_DB_ARN", raising=False)
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setattr(role_config, "get_database_url", lambda: DSN)
    monkeypatch.setattr(call_log, "get_database_url", lambda: DSN)
    role_config.invalidate()
    catalog.invalidate()
    _delete_probe_rows()
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        client = LLMClient()
    yield client
    call_log.wait_for_pending_writes()
    _delete_probe_rows()
    role_config.invalidate()
    catalog.invalidate()


def _sql(query: str, *args):
    import asyncio

    async def run():
        conn = await asyncpg.connect(DSN)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(run())


def _delete_probe_rows():
    _sql("DELETE FROM shared.llm_call_log WHERE model = $1", PROBE_KEY)


def test_a_free_text_call_lands_in_the_ledger_scrubbed(real_ledger):
    import json

    from shared.llm_client import call_log
    from shared.llm_client.free_text_scrub import scrub_request, scrubbed_hash
    from shared.llm_client.models import LLMRequest
    req = LLMRequest(system_prompt="Probe: write one line about the Lantern Valley.",
                     user_prompt=f"Guest note: write to {PLANTED_EMAIL} or call {PLANTED_PHONE}.",
                     stage="t9_write", model_tier=PROBE_KEY, max_tokens=16)
    with pytest.raises(Exception, match=PROBE_KEY):
        real_ledger.generate(req)
    call_log.wait_for_pending_writes()
    rows = _sql("SELECT status, error_class, cost_usd::float8 AS cost, quality_signal::text AS qs, "
                "row_to_json(l)::text AS whole FROM shared.llm_call_log l WHERE model = $1", PROBE_KEY)
    assert len(rows) == 1, "the refusal row was not written (see the llm_call_log_write_failed warning)"
    row = rows[0]
    # An unknown key fails as a gateway error before any provider attempt (the $0 probe path): nothing billed.
    assert (row["status"], row["error_class"], row["cost"]) == ("error", "other", 0.0)
    receipt = json.loads(row["qs"])["pii_scrub"]
    assert receipt["status"] == "scrubbed" and receipt["redacted"] == {"[email]": 1, "[phone]": 1}
    assert receipt["scrubbed_hash"] == scrubbed_hash(scrub_request(req)[0])
    for value in (PLANTED_EMAIL, PLANTED_PHONE, "900 000 222"):
        assert value not in row["whole"]
