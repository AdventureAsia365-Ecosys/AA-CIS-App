"""AA-1025 H2 (capacity S12) — on real PostgreSQL (migrations 348 / 349 applied by conftest), as cis_app where the
code runs as cis_app:

- the Done-when: a prod guard circuit that opens moves a running (open) wave to paused, with one admin bell, and the
  job claim of the bulk and prod LLM kinds is postponed (the jobs stay queued); half-open lets the prod kind be
  claimed while the bulk kind and the wave still wait; an answer on the connection resumes the wave and the bulk job
  is claimed;
- the tables refuse what the code refuses (348 / 349 CHECKs, the FK cascade);
- the admin writes (capacity.put / delete) are audited and checked against the registry;
- the wave check does not end a paused wave; the staff pause / resume SQL of the waves router.
Every test runs in one transaction that is rolled back.
"""
import asyncio
import json
import os
import sys

import asyncpg
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_waves  # noqa: E402
from services.waves import report as wave_report  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.llm_client import app_profile, capacity  # noqa: E402

CONN = "zz-h2-guard"
TOUR = "00000000-0000-4000-8000-0000000a1025"
CAPS = {"s1_rewrite": 6, "t9_write": 4, "llm_connection_probe": 1}


class _OneConn:
    """A pool over one connection (the code acquires from a pool)."""

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

    async def fetchval(self, sql, *args):
        return await self.conn.fetchval(sql, *args)

    async def fetchrow(self, sql, *args):
        return await self.conn.fetchrow(sql, *args)


def _run(fn):
    async def go():
        conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        try:
            ready = await conn.fetchval(
                "SELECT to_regclass('shared.llm_capacity_limit') IS NOT NULL AND EXISTS (SELECT 1 FROM "
                "information_schema.columns WHERE table_schema = 'shared' AND table_name = 'rerun_wave' "
                "AND column_name = 'paused_at') AS ok")
            if not ready:
                pytest.skip("migrations 348 / 349 are not applied in this DB")
            tr = conn.transaction()
            await tr.start()
            try:
                return await fn(conn)
            finally:
                await tr.rollback()
        finally:
            await conn.close()
            capacity.reset()
            app_profile.invalidate()
    return asyncio.run(go())


async def _model(conn) -> str:
    key = await conn.fetchval("SELECT model_key FROM shared.llm_model_catalog ORDER BY model_key LIMIT 1")
    if key is None:
        pytest.skip("no model catalog row in this DB")
    return key


async def _setup(conn) -> str:
    """A connection with a prod guard, every other due job pushed out of the way, as cis_app. Returns a model key."""
    model = await _model(conn)
    await conn.execute("UPDATE shared.job SET run_after = now() + interval '1 day' WHERE status = 'queued'")
    await conn.execute("UPDATE shared.rerun_wave SET status = 'closed' WHERE status = 'open'")
    await conn.execute(
        "INSERT INTO shared.llm_provider_connection (connection_key, kind, auth_mode, label, data_scope, enabled, "
        "updated_by) VALUES ($1, 'mock', 'none', 'zz h2', 'aws_only', true, 'test')", CONN)
    await conn.execute("INSERT INTO shared.llm_capacity_limit (connection_key, model_key, prod_guard, updated_by) "
                       "VALUES ($1, '*', true, 'test')", CONN)
    if not await conn.fetchval("SELECT 1 AS ok FROM pg_roles WHERE rolname = 'cis_app'"):
        pytest.skip("role cis_app is not in this DB")
    await conn.execute("GRANT USAGE ON SCHEMA shared, acp_shared TO cis_app")
    await conn.execute("GRANT SELECT, INSERT, UPDATE ON shared.job, shared.rerun_wave, shared.notifications, "
                       "shared.llm_provider_health TO cis_app")
    await conn.execute("GRANT SELECT ON shared.llm_app_profile, shared.llm_provider_connection, "
                       "shared.llm_model_deployment TO cis_app")
    await conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.llm_capacity_limit TO cis_app")
    await conn.execute("GRANT SELECT, INSERT ON acp_shared.audit_log TO cis_app")
    await conn.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA shared, acp_shared TO cis_app")
    await conn.execute("SET LOCAL SESSION AUTHORIZATION cis_app")
    return model


async def _job(conn, kind: str) -> str:
    return await conn.fetchval(
        "INSERT INTO shared.job (kind, payload, run_after) VALUES ($1, $2::jsonb, now() - interval '1 second') "
        "RETURNING id::text", kind, json.dumps({"tour_id": TOUR}))


async def _hold(pool):
    capacity.invalidate()
    app_profile.invalidate()
    return await capacity.claim_hold(pool)


async def _claim(pool, hold):
    return await queue.claim(pool, "zz-h2-worker", capacity.without_held(CAPS, hold.lines()), 60)


# ── the Done-when ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_open_prod_circuit_pauses_the_wave_and_postpones_the_claims():
    async def body(conn):
        model = await _setup(conn)
        pool = _OneConn(conn)
        wave = await conn.fetchval("INSERT INTO shared.rerun_wave (name, expected_tour_ids) "
                                   "VALUES ('zz h2 wave', $1::uuid[]) RETURNING id::text", [TOUR])
        bulk, prod = await _job(conn, "s1_rewrite"), await _job(conn, "t9_write")

        # 1. Nothing refuses: no hold, the wave stays running.
        hold = await _hold(pool)
        assert hold.lines() == frozenset()
        assert await capacity.sync_waves(pool, hold) == {"paused": [], "resumed": []}

        # 2. The circuit opens (a credential refusal on the guarded connection, written as the breaker writes it).
        await conn.execute(
            "INSERT INTO shared.llm_provider_health (connection_key, model_key, last_error_at, last_error_class, "
            "last_error, consecutive_failures, circuit_open_until, open_streak, opened_at) "
            "VALUES ($1, $2, now(), 'credential', 'zz key unreadable', 1, now() + interval '2 minutes', 1, now())",
            CONN, model)
        hold = await _hold(pool)
        assert hold.prod_open == (f"{CONN} (credential)",) and hold.lines() == {"prod", "bulk"}
        out = await capacity.sync_waves(pool, hold)
        assert out["paused"] == [wave]
        row = await conn.fetchrow("SELECT status, paused_at, paused_by, pause_reason FROM shared.rerun_wave "
                                  "WHERE id = $1::uuid", wave)
        assert row["status"] == "open" and row["paused_at"] is not None and row["paused_by"] == "capacity"
        assert CONN in row["pause_reason"]
        bells = await conn.fetchval("SELECT count(*)::int AS n FROM shared.notifications WHERE event_type = $1 "
                                    "AND entity_id = $2", capacity.WAVE_PAUSED_EVENT, wave)
        assert bells == 1
        assert (await capacity.sync_waves(pool, hold))["paused"] == []          # once: no second bell
        assert await _claim(pool, hold) is None                                 # both jobs postponed
        states = {r["id"]: r["status"] for r in await conn.fetch(
            "SELECT id::text AS id, status FROM shared.job WHERE id = ANY($1::uuid[])", [bulk, prod])}
        assert states == {bulk: "queued", prod: "queued"}                       # waiting, not failed
        assert wave_report._wave_dict(await wave_report.fetch_wave(conn, wave))["paused"] is True

        # 3. Half-open: the prod job is claimed (its call probes the circuit); the bulk job and the wave still wait.
        await conn.execute("UPDATE shared.llm_provider_health SET circuit_open_until = now() - interval '1 second' "
                           "WHERE connection_key = $1", CONN)
        hold = await _hold(pool)
        assert hold.prod_open == () and hold.lines() == {"bulk"}
        assert (await capacity.sync_waves(pool, hold))["resumed"] == []
        got = await _claim(pool, hold)
        assert got is not None and got.id == prod
        assert await _claim(pool, hold) is None

        # 4. The probe answered: the circuit closes, the wave resumes by itself and the bulk job is claimed.
        await conn.execute("UPDATE shared.llm_provider_health SET last_ok_at = now(), consecutive_failures = 0, "
                           "circuit_open_until = NULL, opened_at = NULL WHERE connection_key = $1", CONN)
        hold = await _hold(pool)
        assert hold.lines() == frozenset()
        assert (await capacity.sync_waves(pool, hold))["resumed"] == [wave]
        assert await conn.fetchval("SELECT paused_at IS NULL AS ok FROM shared.rerun_wave WHERE id = $1::uuid", wave)
        got = await _claim(pool, hold)
        assert got is not None and got.id == bulk
    _run(body)


def test_the_bulk_pause_of_the_app_profile_holds_bulk_and_pauses_waves():
    async def body(conn):
        await _setup(conn)
        pool = _OneConn(conn)
        wave = await conn.fetchval("INSERT INTO shared.rerun_wave (name, expected_tour_ids) "
                                   "VALUES ('zz h2 bulk', $1::uuid[]) RETURNING id::text", [TOUR])
        bulk, prod = await _job(conn, "s1_rewrite"), await _job(conn, "t9_write")
        await conn.execute("UPDATE shared.llm_app_profile SET bulk_paused = true WHERE app = 'cis'")
        hold = await _hold(pool)
        assert hold.bulk_paused and hold.lines() == {"bulk"}
        assert (await capacity.sync_waves(pool, hold))["paused"] == [wave]
        assert (await _claim(pool, hold)).id == prod
        assert await _claim(pool, hold) is None
        await conn.execute("UPDATE shared.llm_app_profile SET bulk_paused = false WHERE app = 'cis'")
        hold = await _hold(pool)
        assert (await capacity.sync_waves(pool, hold))["resumed"] == [wave]
        assert (await _claim(pool, hold)).id == bulk
    _run(body)


def test_a_staff_paused_wave_holds_bulk_and_is_not_resumed_by_the_worker():
    async def body(conn):
        await _setup(conn)
        pool = _OneConn(conn)
        wave = await conn.fetchval("INSERT INTO shared.rerun_wave (name, expected_tour_ids) "
                                   "VALUES ('zz h2 staff', $1::uuid[]) RETURNING id::text", [TOUR])
        row = await conn.fetchrow(admin_waves._PAUSE_ONE_SQL, wave, "admin:zz", "zz check the writer first")
        assert row["paused_by"] == "admin:zz"
        bulk = await _job(conn, "s1_rewrite")
        hold = await _hold(pool)
        assert hold.waves_paused_by_staff == 1 and hold.lines() == {"bulk"}
        assert await capacity.sync_waves(pool, hold) == {"paused": [], "resumed": []}   # the worker leaves it
        assert await _claim(pool, hold) is None
        # A paused wave is not ended, although its only job is queued and no tour is terminal (the report reads the
        # pipeline tables, which this replay grants to the owner only: read them as the session user).
        await conn.execute("RESET SESSION AUTHORIZATION")
        await conn.execute("UPDATE shared.job SET status = 'cancelled' WHERE id = $1::uuid", bulk)
        assert (await wave_report.check(conn, wave))["status"] == "open"
        await conn.execute("UPDATE shared.job SET status = 'queued' WHERE id = $1::uuid", bulk)
        await conn.execute("SET LOCAL SESSION AUTHORIZATION cis_app")
        await conn.fetchrow(admin_waves._RESUME_ONE_SQL, wave)
        hold = await _hold(pool)
        assert (await _claim(pool, hold)).id == bulk
    _run(body)


# ── the tables and the admin writes ─────────────────────────────────────────────────────────────────────────────────

def test_the_tables_refuse_what_the_code_refuses():
    async def body(conn):
        await _setup(conn)
        for sql in ("INSERT INTO shared.llm_capacity_limit (connection_key, model_key) VALUES ($1, 'm1')",
                    "INSERT INTO shared.llm_capacity_limit (connection_key, model_key, tpm_limit, bulk_pct) "
                    "VALUES ($1, 'm2', 10, 0)",
                    "INSERT INTO shared.llm_capacity_limit (connection_key, model_key, tpm_limit) "
                    "VALUES ($1, 'Bad Key', 10)",
                    "INSERT INTO shared.llm_capacity_limit (connection_key, model_key, tpm_limit) "
                    "VALUES ($1, 'm3', 0)"):
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(sql, CONN)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.execute("INSERT INTO shared.rerun_wave (name, expected_tour_ids, paused_at) "
                                   "VALUES ('zz', $1::uuid[], now())", [TOUR])
    _run(body)


def test_admin_put_and_delete_are_audited_and_checked():
    async def body(conn):
        model = await _setup(conn)
        pool = _OneConn(conn)
        out = await capacity.put(pool, CONN, "*", {"tpm_limit": 2000, "bulk_pct": 30, "prod_guard": True},
                                 actor="admin:zz-h2")
        assert (out["tpm_limit"], out["bulk_pct"], out["prod_guard"], out["updated_by"]) == (2000, 30, True,
                                                                                            "admin:zz-h2")
        rows = await capacity.read_all(conn)
        assert [(r.connection_key, r.model_key, r.tpm_limit) for r in rows if r.connection_key == CONN] == [
            (CONN, "*", 2000)]
        audit = await conn.fetchrow("SELECT action, resource_id FROM acp_shared.audit_log WHERE actor = 'admin:zz-h2' "
                                    "ORDER BY created_at DESC LIMIT 1")
        assert (audit["action"], audit["resource_id"]) == ("llm_capacity_limit_set", f"{CONN}/*")
        with pytest.raises(LookupError, match="not deployed"):
            await capacity.put(pool, CONN, model, {"tpm_limit": 10}, actor="admin:zz-h2")
        with pytest.raises(LookupError, match="no such LLM connection"):
            await capacity.put(pool, "zz-nowhere", "*", {"tpm_limit": 10}, actor="admin:zz-h2")
        with pytest.raises(ValueError):
            await capacity.put(pool, CONN, "*", {}, actor="admin:zz-h2")
        assert await capacity.delete(pool, CONN, "*", actor="admin:zz-h2") is True
        assert await capacity.delete(pool, CONN, "*", actor="admin:zz-h2") is False
        assert [r for r in await capacity.read_all(conn) if r.connection_key == CONN] == []
    _run(body)
