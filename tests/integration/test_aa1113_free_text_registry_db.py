"""AA-1113 (CIS 449) — the gateway's stage read carries the free-text mark against a real Postgres.

conftest applies every migration on main. With 449 applied, a stage row marked ``free_text`` reads as marked through
``role_config._fetch_one`` and ``list_stage_configs`` (the admin GET), and every seeded tenant stage is marked. Before
449 (the code PR's own CI may run before the migration PR lands), the read falls back without the column: no error,
``free_text`` False, the ``t<n>_`` floor still scrubs. The store connects without TLS, like test_aa642_a1_stage_switch_db.
Invented stage names only; the inserted row is deleted at teardown.
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
