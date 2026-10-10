"""AA-720 — Jev credit monitoring: 402 billing_error detection, circuit breaker, throttled alert.
Mock-only: no TypeSafe call, no DB."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.llm_client import decide as d


@pytest.fixture(autouse=True)
def _reset_breaker():
    d.reset_jev_breaker()
    d._questions_loaded_at = 0.0
    yield
    d.reset_jev_breaker()


# ── is_billing_error ─────────────────────────────────────────────────────────────────────────

def test_is_billing_error_true_on_402_billing_error():
    assert d.is_billing_error(402, {"detail": {"error_type": "billing_error", "message": "no credits"}})


def test_is_billing_error_bare_402_treated_as_credit_problem():
    # A 402 whose body we could not parse (None) or without a detail dict is still a credit problem.
    assert d.is_billing_error(402, None)
    assert d.is_billing_error(402, {"detail": "flat string"})


def test_is_billing_error_false_on_other_status():
    assert not d.is_billing_error(422, {"detail": {"error_type": "billing_error"}})
    assert not d.is_billing_error(400, {"detail": "schema error"})


def test_is_billing_error_false_on_402_non_billing():
    assert not d.is_billing_error(402, {"detail": {"error_type": "something_else"}})


# ── circuit breaker state ──────────────────────────────────────────────────────────────────────

def test_breaker_opens_on_trip_and_resets():
    assert d.jev_breaker_open() is False
    d._trip_jev_breaker()
    assert d.jev_breaker_open() is True
    d.reset_jev_breaker()
    assert d.jev_breaker_open() is False


def test_breaker_closes_after_cooldown_elapses():
    with patch.object(d.time, "monotonic", return_value=1000.0):
        d._trip_jev_breaker()                       # deadline = 1000 + cooldown
    # still within cooldown
    with patch.object(d.time, "monotonic", return_value=1000.0 + d._JEV_COOLDOWN_MIN * 60.0 - 1):
        assert d.jev_breaker_open() is True
    # past cooldown
    with patch.object(d.time, "monotonic", return_value=1000.0 + d._JEV_COOLDOWN_MIN * 60.0 + 1):
        assert d.jev_breaker_open() is False


# ── alert throttle (mirrors AA-627 _maybe_alert_low_balance) ──────────────────────────────────

class _AlertConn:
    """A fake asyncpg connection for maybe_alert_jev_credit: transaction() is an async context
    manager, the advisory-lock SELECT is a no-op, fetchval returns the configured `recent` value,
    and INSERTs are counted. AA-756 wraps the check + insert in one locked transaction."""

    def __init__(self, recent, raise_on_fetchval=False):
        self._recent = recent
        self._raise = raise_on_fetchval
        self.insert_calls = 0

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *exc):
                return False

        return _Tx()

    async def execute(self, sql, *args):
        if "INSERT INTO shared.notifications" in sql:
            self.insert_calls += 1
        return "OK"

    async def fetchval(self, sql, *args):
        if self._raise:
            raise RuntimeError("db down")
        return self._recent


def _alert_conn(recent, raise_on_fetchval=False):
    return _AlertConn(recent, raise_on_fetchval)


@pytest.mark.asyncio
async def test_alert_inserts_when_none_recent():
    conn = _alert_conn(recent=None)
    assert await d.maybe_alert_jev_credit(conn, source="decide") is True
    # advisory lock + the INSERT (the check-then-insert now runs inside one locked transaction).
    assert conn.insert_calls == 1


@pytest.mark.asyncio
async def test_alert_throttled_when_recent_exists():
    conn = _alert_conn(recent=1)
    assert await d.maybe_alert_jev_credit(conn, source="canary") is False
    assert conn.insert_calls == 0


@pytest.mark.asyncio
async def test_alert_swallows_db_error():
    conn = _alert_conn(recent=None, raise_on_fetchval=True)
    assert await d.maybe_alert_jev_credit(conn, source="402_sweep") is False


# ── AA-756: concurrent maybe_alert_jev_credit → exactly one insert ────────────────────────────
# On 10/10 04:09 UTC three identical exhausted alerts landed in the same millisecond: several
# decide() coroutines each read "no unread alert in 24h" before any inserted. The fix wraps the
# check + insert in one transaction holding pg_advisory_xact_lock. This fake serialises callers on
# that lock and shares one notifications store, so N concurrent callers insert exactly once.

class _SerialisingConn:
    """A fake asyncpg connection sharing one in-memory 'notifications' list + one advisory lock.
    conn.transaction() acquires the shared asyncio.Lock on __aenter__ (released on __aexit__); the
    pg_advisory_xact_lock SELECT is a no-op because the transaction already holds the lock. This
    reproduces the real guarantee: the check-then-insert of one caller completes before the next
    caller's check runs."""

    def __init__(self, store: list, lock: "asyncio.Lock"):
        self._store = store
        self._lock = lock
        self.inserts = 0

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_):
                await conn._lock.acquire()
                return self_

            async def __aexit__(self_, *exc):
                conn._lock.release()
                return False

        return _Tx()

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if "INSERT INTO shared.notifications" in sql:
            self._store.append(args)
            self.inserts += 1
            return "INSERT 0 1"
        return "OK"

    async def fetchval(self, sql, *args):
        # mirrors _JEV_ALERT_RECENT_SQL: 1 if any notification already recorded, else None.
        return 1 if self._store else None


@pytest.mark.asyncio
async def test_concurrent_alert_inserts_exactly_once():
    import asyncio

    store: list = []
    lock = asyncio.Lock()
    conns = [_SerialisingConn(store, lock) for _ in range(8)]
    results = await asyncio.gather(
        *[d.maybe_alert_jev_credit(c, source="decide") for c in conns]
    )
    assert sum(conns_i.inserts for conns_i in conns) == 1   # exactly one row inserted
    assert sum(1 for r in results if r is True) == 1        # exactly one caller reports True
    assert len(store) == 1


# ── _decide: breaker open → TypeSafe is not called ────────────────────────────────────────────

def _conn_for_decide():
    """A fake connection that serves one enforce-mode question and swallows log writes."""
    q = d.Question(key="kw_belongs", stage="a3_research", kind="noul", instructions="?",
                   criteria=None, mode="enforce", accept_floor=0.8, reject_ceiling=0.2,
                   threshold_version=1, question_hash="h-kw")
    conn = MagicMock()

    async def fetch(sql, *args):
        if "decision_question" in sql:
            return [{"question_key": q.key, "stage": q.stage, "kind": q.kind,
                     "instructions": q.instructions, "criteria": q.criteria, "mode": q.mode,
                     "accept_floor": q.accept_floor, "reject_ceiling": q.reject_ceiling,
                     "threshold_version": q.threshold_version, "question_hash": q.question_hash}]
        return []        # no allowlist rows, no cache hits

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.executemany = AsyncMock()
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_decide_skips_typesafe_when_breaker_open():
    conn = _conn_for_decide()
    d._trip_jev_breaker()
    with patch.object(d, "_call_jev", new=AsyncMock()) as mock_call:
        dec = await d._decide(d._SingleConn(conn), "a3_research", "kw:US:x", "state",
                              ["kw_belongs"], tenant_id=None)
    mock_call.assert_not_awaited()                      # breaker open → no TypeSafe call
    assert dec.verdicts["kw_belongs"].zone == "error"
    assert "credit_exhausted" in (dec.verdicts["kw_belongs"].error or "")


@pytest.mark.asyncio
async def test_decide_trips_breaker_and_alerts_on_billing_error():
    conn = _conn_for_decide()
    assert d.jev_breaker_open() is False
    with patch.object(d, "_call_jev", new=AsyncMock(side_effect=d.JevBillingError("402"))), \
         patch.object(d, "maybe_alert_jev_credit", new=AsyncMock(return_value=True)) as mock_alert:
        dec = await d._decide(d._SingleConn(conn), "a3_research", "kw:US:x", "state",
                              ["kw_belongs"], tenant_id=None)
    assert d.jev_breaker_open() is True                 # breaker tripped
    assert dec.verdicts["kw_belongs"].zone == "error"
    assert dec.verdicts["kw_belongs"].error == "credit_exhausted"
    mock_alert.assert_awaited_once()
