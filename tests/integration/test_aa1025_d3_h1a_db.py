"""AA-1025 D3 / H1a — the SQL of the app profile (shared/llm_client/app_profile.py), the geocode ledger writer
(services/places/store.log_geocode_calls) and the admin readers (Calls tab, Providers stats) on real PostgreSQL
(migrations 345 / 346 applied by conftest), run as cis_app where the code runs as cis_app.

- H1a: an admin edit switches AI off for app cis and writes its audit row in the same transaction; the gateway's
  async read sees it; the bulk re-queue then holds every stage; a bad value is refused by the table too.
- D3: a geocode batch is written with its status / latency / app / stage; the Calls tab's geocoder SQL filters on them
  and reads them back; the Providers stats compute p50 / p95 from them; a Jev ask refused before an answer reads as
  refused on the Calls tab.
Every test runs in one transaction that is rolled back (shared state goes back, the S263 lesson).
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.services import ai_gateway as gw  # noqa: E402
from api.services import ai_gateway_calls as calls  # noqa: E402
from services.places import store  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.llm_client import app_profile  # noqa: E402

JOB = "00000000-0000-4000-8000-00000000d3a1"
STAGE = "zz_aa1025_d3"


class _OneConn:
    """A pool over one connection (the rest of the code acquires from a pool)."""

    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _A()

    async def fetch(self, sql, *args):
        return await self.conn.fetch(sql, *args)


def _run(fn):
    async def go():
        conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        try:
            ready = await conn.fetchval(
                "SELECT to_regclass('shared.llm_app_profile') IS NOT NULL AND EXISTS (SELECT 1 FROM "
                "information_schema.columns WHERE table_schema = 'geo' AND table_name = 'geocode_call' "
                "AND column_name = 'stage') AS ok")
            if not ready:
                pytest.skip("migrations 345 / 346 are not applied in this DB")
            tr = conn.transaction()
            await tr.start()
            try:
                return await fn(conn)
            finally:
                await tr.rollback()
        finally:
            await conn.close()
            app_profile.invalidate()
    return asyncio.run(go())


async def _as_cis_app(conn):
    """The rest of the transaction as cis_app (SET LOCAL SESSION AUTHORIZATION: the X1 lesson), with the temporary
    grants the local replay may lack; the rollback takes them back."""
    if not await conn.fetchval("SELECT 1 AS ok FROM pg_roles WHERE rolname = 'cis_app'"):
        pytest.skip("role cis_app is not in this DB")
    await conn.execute("GRANT USAGE ON SCHEMA shared, acp_shared, geo TO cis_app")
    await conn.execute("GRANT SELECT, INSERT ON acp_shared.audit_log TO cis_app")
    await conn.execute("GRANT SELECT ON shared.llm_role_config TO cis_app")
    await conn.execute("GRANT SELECT, INSERT ON geo.geocode_call, shared.decision_log TO cis_app")
    await conn.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA geo, shared TO cis_app")
    await conn.execute("SET LOCAL SESSION AUTHORIZATION cis_app")


# ── H1a ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_switching_ai_off_is_saved_audited_and_seen_by_the_gateway():
    async def body(conn):
        await _as_cis_app(conn)
        pool = _OneConn(conn)
        app_profile.invalidate()
        assert await app_profile.ai_off_async(pool) is None
        out = await app_profile.update(pool, "cis", {"ai_enabled": False, "disabled_reason": "zz drill"},
                                       actor="admin:zz-aa1025")
        assert out["ai_enabled"] is False and out["disabled_reason"] == "zz drill"
        assert out["updated_by"] == "admin:zz-aa1025" and out["updated_at"]
        row = await conn.fetchrow("SELECT action, resource_id, details FROM acp_shared.audit_log "
                                  "WHERE actor = 'admin:zz-aa1025' ORDER BY created_at DESC LIMIT 1")
        assert (row["action"], row["resource_id"]) == ("llm_app_ai_switched_off", "cis")
        assert '"ai_enabled": [true, false]' in row["details"]
        app_profile.invalidate()
        off = await app_profile.ai_off_async(pool)
        assert off is not None and off.app == "cis" and off.degrade_mode == "queue_retry"
        stages = await queue.disabled_stages(pool)
        assert "any_stage" in stages                          # the bulk re-queue holds every stage meanwhile
        back = await app_profile.update(pool, "cis", {"ai_enabled": True}, actor="admin:zz-aa1025")
        assert back["ai_enabled"] is True and back["disabled_reason"] is None
        app_profile.invalidate()
        assert "any_stage" not in await queue.disabled_stages(pool)
    _run(body)


def test_other_fields_and_the_four_rows():
    async def body(conn):
        await _as_cis_app(conn)
        pool = _OneConn(conn)
        out = await app_profile.update(pool, "tripplanner", {"bulk_paused": True, "default_daily_budget_usd": 2.5,
                                                             "degrade_mode": "last_saved_text"}, actor="admin:zz")
        assert (out["bulk_paused"], out["default_daily_budget_usd"], out["degrade_mode"]) == \
            (True, 2.5, "last_saved_text")
        table = await app_profile.read_all(conn)
        assert set(table) == set(app_profile.APPS)
        assert table["tripplanner"].bulk_paused is True and table["tripplanner"].ai_enabled is True
        with pytest.raises(LookupError):
            await app_profile.update(pool, "portal", {"bulk_paused": True}, actor="admin:zz")
    _run(body)


# ── D3 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_geocode_batch_carries_its_ledger_facts_and_the_calls_tab_reads_them():
    async def body(conn):
        await _as_cis_app(conn)
        await store.log_geocode_calls(conn, "geoapify", 4, 0.0, JOB, failed_requests=4, stage="place_geocode",
                                      latency_ms=800)
        await store.log_geocode_calls(conn, "geoapify", 2, 0.0, JOB, stage="place_shape", latency_ms=100)
        rows = await conn.fetch("SELECT status, latency_ms, app, stage FROM geo.geocode_call WHERE job_id = $1 "
                                "ORDER BY stage", JOB)
        assert [tuple(r) for r in rows] == [("error", 800, "cis", "place_geocode"), ("ok", 100, "cis", "place_shape")]
        now = datetime.now(timezone.utc)
        f = calls.parse_filters(stage="place_geocode", app="cis", status="error", job_id=JOB)
        sql, args = calls.source_sql("geocoder", f, since=now - timedelta(hours=1), until=now + timedelta(minutes=1),
                                     cursor=None, limit=10)
        found = [calls.cis_row("geocoder", dict(r)) for r in await conn.fetch(sql, *args)]
        assert len(found) == 1
        assert (found[0]["stage"], found[0]["status"], found[0]["latency_ms"], found[0]["app"]) == \
            ("place_geocode", "error", 800, "cis")
        f = calls.parse_filters(status="refused", job_id=JOB)
        sql, args = calls.source_sql("geocoder", f, since=now - timedelta(hours=1), until=now + timedelta(minutes=1),
                                     cursor=None, limit=10)
        assert await conn.fetch(sql, *args) == []
        stats = await conn.fetchrow(gw.GEO_STATS_SQL, now - timedelta(hours=24), now - timedelta(days=7),
                                    now + timedelta(minutes=1), "geoapify")
        assert stats["p95_24h"] is not None and stats["p50_24h"] >= 50.0       # 800 / 4 and 100 / 2 ms per request
    _run(body)


def test_a_refused_jev_ask_reads_as_refused():
    async def body(conn):
        await _as_cis_app(conn)
        for zone, error in (("error", app_profile.refusal_text("cis")), ("error", "budget_exceeded"),
                            ("error", "ReadTimeout"), ("accept", None)):
            await conn.execute("INSERT INTO shared.decision_log (stage, question_key, subject_key, job_id, mode, "
                               "zone, error, cached) VALUES ($1, 'zz_q', 'zz:subject', $2, 'shadow', $3, $4, false)",
                               STAGE, JOB, zone, error)
        now = datetime.now(timezone.utc)
        out = {}
        for status in ("refused", "error", "ok"):
            f = calls.parse_filters(status=status, stage=STAGE, job_id=JOB)
            sql, args = calls.source_sql("jev", f, since=now - timedelta(hours=1), until=now + timedelta(minutes=1),
                                         cursor=None, limit=10)
            out[status] = [calls.cis_row("jev", dict(r))["outcome"] for r in await conn.fetch(sql, *args)]
        assert sorted(out["refused"]) == ["refused", "refused"]
        assert out["error"] == ["error"] and out["ok"] == ["accept"]
    _run(body)
