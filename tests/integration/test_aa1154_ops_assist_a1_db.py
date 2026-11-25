"""AA-1154 (A1) — the assistant's two database reads on a real Postgres (conftest replays every migration): the
preflight before any model call (the ops_assist stage row, its hard-stop month cap and whether the Deep analysis model
is an enabled catalog chat model, read as cis_app)
and the alarm / service-drop rows of the look-up panel. Runs in a transaction that is rolled back; invented values."""
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services.ops_assist import explain, lookup  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _as_cis_app(conn) -> None:
    # The test database has no Dev grants (Dev: cis_app SELECT on these tables, db-schema-reference); give them inside
    # the rolled-back transaction only.
    await conn.execute("GRANT USAGE ON SCHEMA shared, acp_shared TO cis_app")
    await conn.execute("GRANT SELECT ON shared.llm_role_config, shared.spend_budget, shared.llm_model_catalog, "
                       "acp_shared.ops_event TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")


async def _seed(conn) -> None:
    """The CIS 487 rows (kept when the migration already wrote them), so the test runs before and after 487."""
    await conn.execute(
        "INSERT INTO shared.llm_role_config (stage, role, provider, model_id, is_active, fallback_model_ids, "
        "shadow_sample_pct, allowed_scopes, route_deadline_s, degrade_mode, updated_by) VALUES "
        "($1, 'writer', 'claude', 'haiku-5-5', true, ARRAY[]::text[], 0, ARRAY['aws_only', 'vendor_direct'], 25, "
        "'basic_narration', 'zz-test') ON CONFLICT (stage) DO NOTHING", explain.STAGE)
    await conn.execute(
        "INSERT INTO shared.spend_budget (provider, scope, per_month_usd, hard_stop, alert_pct, updated_by) VALUES "
        "('llm', $1, 10, true, 80, 'zz-test') ON CONFLICT (provider, scope) DO NOTHING", explain.BUDGET_SCOPE)


async def _preflight(conn, model=None) -> dict:
    row = await conn.fetchrow(explain.PREFLIGHT_SQL, explain.STAGE, explain.BUDGET_SCOPE, model)
    return dict(row)


@pytest.mark.asyncio
async def test_the_preflight_allows_the_seeded_stage_and_stops_on_the_kill_switch_or_a_missing_cap(conn):
    await _seed(conn)
    await _as_cis_app(conn)
    assert explain.preflight_error(await _preflight(conn)) is None
    await conn.execute("RESET ROLE")
    await conn.execute("UPDATE shared.llm_role_config SET is_active = false WHERE stage = $1", explain.STAGE)
    assert explain.preflight_error(await _preflight(conn)) == "switched_off"
    await conn.execute("UPDATE shared.llm_role_config SET is_active = true WHERE stage = $1", explain.STAGE)
    await conn.execute("UPDATE shared.spend_budget SET hard_stop = false WHERE provider = 'llm' AND scope = $1",
                       explain.BUDGET_SCOPE)
    assert explain.preflight_error(await _preflight(conn)) == "no_cap"
    await conn.execute("DELETE FROM shared.llm_role_config WHERE stage = $1", explain.STAGE)
    assert explain.preflight_error(await _preflight(conn)) == "not_configured"


@pytest.mark.asyncio
async def test_the_preflight_says_whether_the_deep_model_is_an_enabled_chat_model(conn):
    await _seed(conn)
    await _as_cis_app(conn)
    assert (await _preflight(conn))["model_enabled"] is False          # quick: no model asked
    assert (await _preflight(conn, "haiku-5-5"))["model_enabled"] is True
    assert (await _preflight(conn, "zz-no-such-model"))["model_enabled"] is False
    await conn.execute("RESET ROLE")
    await conn.execute("UPDATE shared.llm_model_catalog SET enabled = false WHERE model_key = 'haiku-5-5'")
    assert (await _preflight(conn, "haiku-5-5"))["model_enabled"] is False


@pytest.mark.asyncio
async def test_the_lookup_reads_alarms_and_service_drops_of_the_window(conn):
    now = datetime.now(timezone.utc)
    for n, (kind, sev, minutes) in enumerate((("alarm", "sev2", 10), ("service_down", "sev1", 20),
                                              ("deploy", "info", 5), ("alarm", "sev3", 200))):
        await conn.execute(
            "INSERT INTO acp_shared.ops_event (occurred_at, kind, severity, app, subject, title, details, dedupe_key) "
            "VALUES ($1, $2, $3, 'cis', $4, $5, '{}'::jsonb, $6)",
            now - timedelta(minutes=minutes), kind, sev, f"zz-subject-{n}", f"zz title {n}", f"zz-aa1154-a1-{n}")
    await _as_cis_app(conn)
    rows = [dict(r) for r in await conn.fetch(lookup.EVENTS_SQL, now - timedelta(minutes=60), now)]
    ours = [r for r in rows if r["subject"].startswith("zz-subject-")]
    assert [(r["kind"], r["severity"]) for r in ours] == [("alarm", "sev2"), ("service_down", "sev1")]
    shaped = lookup.event_rows(ours)
    assert shaped[0]["type"] == "alarm" and shaped[0]["title"] == "zz title 0" and shaped[0]["id"].startswith("event-")
