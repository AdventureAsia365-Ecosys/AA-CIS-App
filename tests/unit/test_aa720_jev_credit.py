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

@pytest.mark.asyncio
async def test_alert_inserts_when_none_recent():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)        # no unread alert in 24h
    conn.execute = AsyncMock()
    assert await d.maybe_alert_jev_credit(conn, source="decide") is True
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_alert_throttled_when_recent_exists():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)           # an unread alert already exists
    conn.execute = AsyncMock()
    assert await d.maybe_alert_jev_credit(conn, source="canary") is False
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_swallows_db_error():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("db down"))
    assert await d.maybe_alert_jev_credit(conn, source="402_sweep") is False


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
