"""AA-1025 B3 / B5 — the SQL of shared/provider_circuit.py and shared/provider_credit.py on real PostgreSQL (migrations
342 / 343 applied by conftest), run as cis_app where the code runs as cis_app.

- B3: the row-locked transition writes shared.provider_health and the snapshot reads it back (what every task sees);
  the opening alert writes one bell and one ops_alert_email job, and a second opening within 24 h writes neither;
  the Providers page read of the circuits.
- B5: a balance read and a top-up go in, the spend since the anchor comes from shared.llm_call_log (connection_key
  or account), the estimate and the low-credit bell + e-mail job follow; the throttle holds; read_all and the
  Providers balance block read the same numbers.
Every test cleans its rows (shared state goes back, the S263 lesson).
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared import ops_alerts  # noqa: E402
from shared import provider_circuit as pc  # noqa: E402
from shared import provider_credit as pcr  # noqa: E402

KEY = "openai-direct"
STAGE = "zz_aa1025_test"


def _run(fn):
    async def go():
        conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        try:
            ready = await conn.fetchval("SELECT to_regclass('shared.provider_health') IS NOT NULL "
                                        "AND to_regclass('shared.provider_credit_topup') IS NOT NULL AS ok")
            if not ready:
                pytest.skip("migration 342 is not applied in this DB")
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


async def _as_cis_app(conn):
    """Run the rest of the transaction as cis_app (SET LOCAL SESSION AUTHORIZATION: the X1 lesson), with a temporary
    schema USAGE grant that the transaction's rollback takes back."""
    if not await conn.fetchval("SELECT 1 AS ok FROM pg_roles WHERE rolname = 'cis_app'"):
        pytest.skip("role cis_app is not in this DB")
    await conn.execute("GRANT USAGE ON SCHEMA shared, acp_shared TO cis_app")
    # The grants cis_app holds on Dev (db-schema-reference) that the local replay may lack (a failed X1 file there).
    await conn.execute("GRANT SELECT, INSERT ON shared.notifications, shared.job, shared.llm_call_log TO cis_app")
    await conn.execute("GRANT SELECT ON shared.llm_provider_connection TO cis_app")
    await conn.execute("GRANT USAGE ON SEQUENCE shared.notifications_id_seq TO cis_app")
    await conn.execute("SET LOCAL SESSION AUTHORIZATION cis_app")


async def _cleanup(conn):
    await conn.execute("RESET SESSION AUTHORIZATION")
    await conn.execute("DELETE FROM shared.provider_health WHERE connection_key IN ('typesafe', 'dataforseo', "
                       "'geoapify')")
    await conn.execute("DELETE FROM shared.provider_credit_topup WHERE note LIKE 'zz-aa1025%'")
    await conn.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
    await conn.execute("DELETE FROM shared.notifications WHERE event_type IN ($1, $2)", pc.ALERT_EVENT, pcr.LOW_EVENT)
    await conn.execute("DELETE FROM shared.job WHERE kind = 'ops_alert_email' AND created_by IN "
                       "('provider_circuit', 'provider_credit')")


@pytest.fixture
def mail_on(monkeypatch):
    monkeypatch.setattr(ops_alerts, "sender_config", lambda: object())


# ── B3 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_transition_writes_the_shared_row_and_the_snapshot_reads_it():
    async def body(conn):
        await _cleanup(conn)
        if not await conn.fetchval("SELECT 1 AS ok FROM shared.llm_provider_connection WHERE connection_key = "
                                   "'dataforseo'"):
            pytest.skip("the 322 seed (dataforseo) is not in this DB")
        now = datetime.now(timezone.utc)
        try:
            async with conn.transaction():
                await _as_cis_app(conn)
                for i in range(5):
                    before, after = await pc.apply_transition(
                        conn, "dataforseo", lambda cur: pc.after_failure(cur, "other", f"HTTP 503 #{i}", now))
                assert after.circuit_open_until is not None and after.consecutive_failures == 5
                rows = {h.connection_key: h for h in await pc.fetch_health(conn)}
                assert rows["dataforseo"].last_error_class == "other" and rows["dataforseo"].open_streak == 1
                views = await pc.read_views(conn, now + timedelta(seconds=1))
                assert views["dataforseo"]["state"] == "open"
                _, closed = await pc.apply_transition(conn, "dataforseo", lambda cur: pc.after_success(cur, now))
                assert closed.circuit_open_until is None
                raise _Rollback()
        except _Rollback:
            pass
        finally:
            await _cleanup(conn)
    _run(body)


class _Rollback(Exception):
    pass


def test_alert_writes_one_bell_and_one_mail_job_per_opening_window(mail_on):
    async def body(conn):
        await _cleanup(conn)
        now = datetime.now(timezone.utc)
        h = pc.Health(connection_key="geoapify", model_key="*", last_error_at=now, last_error_class="other",
                      last_error="HTTP 503", consecutive_failures=5, circuit_open_until=now + timedelta(seconds=60),
                      open_streak=1, opened_at=now)
        try:
            async with conn.transaction():
                await _as_cis_app(conn)
                written, job_id = await pc.alert_open(conn, h)
                assert written and job_id
                assert await pc.alert_open(conn, h) == (False, None)          # unread bell within 24 h
                bells = await conn.fetch("SELECT entity_type, entity_id, payload, target_roles FROM "
                                         "shared.notifications WHERE event_type = $1", pc.ALERT_EVENT)
                assert len(bells) == 1 and bells[0]["entity_id"] == "provider_connection:geoapify"
                assert list(bells[0]["target_roles"]) == ["admin"]
                payload = json.loads(bells[0]["payload"])
                assert payload["label"] == "Geoapify" and payload["error_class"] == "other"
                jobs = await conn.fetch("SELECT payload->>'template' AS template, idempotency_key FROM shared.job "
                                        "WHERE kind = 'ops_alert_email' AND created_by = 'provider_circuit'")
                assert [j["template"] for j in jobs] == ["provider_circuit_open"]
                raise _Rollback()
        except _Rollback:
            pass
        finally:
            await _cleanup(conn)
    _run(body)


# ── B5 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _spend(conn, cost, at, *, by_account=False):
    await conn.execute(
        "INSERT INTO shared.llm_call_log (id, stage, role, model, quality_signal, created_at, cost_usd, provider, "
        "account, connection_key) VALUES ($1, $2, 'judge', 'invented-model', '{}'::jsonb, $3, $4, 'openai', $5, $6)",
        uuid.uuid4(), STAGE, at, cost, KEY if by_account else None, None if by_account else KEY)


def test_ledger_estimate_and_low_credit_alert(mail_on):
    async def body(conn):
        await _cleanup(conn)
        row = await conn.fetchrow("SELECT balance_source, low_balance_usd FROM shared.llm_provider_connection "
                                  "WHERE connection_key = $1", KEY)
        if row is None:
            pytest.skip("the 304 seed (openai-direct) is not in this DB")
        assert row["balance_source"] == "topups_minus_spend"                 # migration 343
        now = datetime.now(timezone.utc)
        try:
            async with conn.transaction():
                await conn.execute("UPDATE shared.llm_provider_connection SET low_balance_usd = 5 "
                                   "WHERE connection_key = $1", KEY)
                await _spend(conn, 7.0, now - timedelta(days=3))             # before the anchor: not counted
                await _spend(conn, 0.15, now - timedelta(minutes=30))
                await _spend(conn, 0.05, now - timedelta(minutes=20), by_account=True)   # pre-S6 row
                await _as_cis_app(conn)
                assert KEY in {c.connection_key for c in await pcr.ledger_connections(conn)}
                assert "typesafe" not in {c.connection_key for c in await pcr.ledger_connections(conn)}
                await pcr.add_entry(conn, KEY, kind="topup", amount_usd=50, effective_at=now - timedelta(days=10),
                                    note="zz-aa1025 old top-up", actor="admin:test")
                opening = await pcr.add_entry(conn, KEY, kind="opening_balance", amount_usd=4.2,
                                              effective_at=now - timedelta(hours=1), note="zz-aa1025 console read",
                                              actor="admin:test")
                assert opening["kind"] == "opening_balance" and opening["amount_usd"] == 4.2
                est = await pcr.estimate(conn, KEY)
                assert est["recorded_usd"] == 4.2 and abs(est["spent_usd"] - 0.2) < 1e-9
                assert abs(est["estimated_left_usd"] - 4.0) < 1e-9
                c = next(x for x in await pcr.ledger_connections(conn) if x.connection_key == KEY)
                first = await pcr.check_connection(conn, c, source="test")
                assert first["below_threshold"] and first["alerted"] and first["email_job_id"]
                again = await pcr.check_connection(conn, c, source="test")
                assert again["below_threshold"] and not again["alerted"]      # one bell per connection per 24 h
                bells = await conn.fetch("SELECT entity_id FROM shared.notifications WHERE event_type = $1",
                                         pcr.LOW_EVENT)
                assert [b["entity_id"] for b in bells] == [f"llm_connection:{KEY}"]
                listing = {x["connection_key"]: x for x in await pcr.read_all(conn)}[KEY]
                assert listing["below_threshold"] and listing["last_low_alert_at"] is not None
                assert [e["kind"] for e in listing["entries"]] == ["opening_balance", "topup"]   # newest first
                block = (await pcr.balances(conn))[KEY]
                assert block["below_threshold"] and abs(block["usd"] - 4.0) < 1e-9
                gone = await pcr.delete_entry(conn, opening["id"])
                assert gone["id"] == opening["id"] and await pcr.delete_entry(conn, opening["id"]) is None
                raise _Rollback()
        except _Rollback:
            pass
        finally:
            await _cleanup(conn)
    _run(body)
