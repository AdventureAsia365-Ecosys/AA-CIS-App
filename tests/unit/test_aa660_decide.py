"""AA-660 — Jev decision seam: zones, enforce-only actions, tenant guard, fail-open, logging.
Mock-only: no TypeSafe call, no DB."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.llm_client import decide as d

PLATFORM = d.PLATFORM_TENANT_ID
TEST_TENANT = "a1b2c3d4-0001-4000-8000-000000000001"
REAL_TENANT = "11111111-2222-4333-8444-555555555555"


def _q(key="kw_belongs", kind="noul", mode="enforce", accept=0.8, reject=0.2, criteria=None):
    return d.Question(key=key, stage="a3_research", kind=kind, instructions="Is it about this place?",
                      criteria=criteria, mode=mode, accept_floor=accept, reject_ceiling=reject,
                      threshold_version=1)


# ── zones ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("p,zone", [(0.9, "accept"), (0.8, "accept"), (0.5, "grey"), (0.2, "reject"), (0.05, "reject")])
def test_noul_zones(p, zone):
    assert d.zone_for(_q(), {"type": "noul", "noul": p}).zone == zone


def test_uncalibrated_question_is_always_grey():
    q = _q(accept=None, reject=None, mode="shadow")
    assert d.zone_for(q, {"type": "noul", "noul": 0.99}).zone == "grey"
    assert d.zone_for(q, {"type": "noul", "noul": 0.01}).zone == "grey"


def test_choice_accepts_only_above_the_confidence_floor():
    q = _q(key="row_kind", kind="choice", accept=0.9, reject=None, criteria={"tour": None, "poi": None})
    hi = d.zone_for(q, {"type": "choice", "choice": "poi", "confidence": 0.95,
                        "probabilities": {"tour": 0.05, "poi": 0.95}})
    lo = d.zone_for(q, {"type": "choice", "choice": "poi", "confidence": 0.6,
                        "probabilities": {"tour": 0.4, "poi": 0.6}})
    assert (hi.zone, hi.choice) == ("accept", "poi")
    assert lo.zone == "grey"


def test_answer_type_mismatch_is_an_error():
    assert d.zone_for(_q(), {"type": "choice", "choice": "x", "confidence": 1}).zone == "error"


def test_only_enforced_confident_verdicts_are_actionable():
    dec = d.Decision(verdicts={
        "a": d.Verdict("a", "enforce", "reject", probability=0.1),
        "b": d.Verdict("b", "shadow", "reject", probability=0.1),
        "c": d.Verdict("c", "enforce", "grey", probability=0.5),
        "e": d.Verdict("e", "enforce", "error"),
    })
    assert dec.rejected("a") and not dec.accepted("a")
    assert not dec.rejected("b") and not dec.rejected("c") and not dec.rejected("e")
    assert not dec.rejected("missing")


def test_wire_question_shape():
    q = _q(kind="choice", criteria={"tour": "a multi-day tour", "poi": None})
    assert d.wire_question(q) == {"type": "choice", "instructions": "Is it about this place?",
                                  "criteria": {"tour": "a multi-day tour", "poi": None}}
    assert "criteria" not in d.wire_question(_q())


# ── the call, with a fake connection ──────────────────────────────────────────────────────────

def _conn(questions, allowlist=(), cached=()):
    conn = MagicMock()

    async def fetch(sql, *args):
        if "decision_question" in sql:
            return [{"question_key": q.key, "stage": q.stage, "kind": q.kind, "instructions": q.instructions,
                     "criteria": q.criteria, "mode": q.mode, "accept_floor": q.accept_floor,
                     "reject_ceiling": q.reject_ceiling, "threshold_version": q.threshold_version,
                     "question_hash": "h-" + q.key}
                    for q in questions]
        if "FROM shared.decision_log" in sql:          # verdict cache lookup
            return list(cached)
        return [{"t": t} for t in allowlist]

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.executemany = AsyncMock()
    return conn


@pytest.fixture(autouse=True)
def _fresh_cache():
    d._questions_loaded_at = 0.0
    d._allowlist_loaded_at = 0.0
    yield


@pytest.mark.asyncio
async def test_success_parses_answers_prices_input_and_logs():
    conn = _conn([_q(), _q(key="idea_ok", mode="shadow")])
    answer = {"model": "jev-1.13.0", "usage": {"input_tokens": 1000, "output_tokens": 3},
              "answers": {"kw_belongs": {"type": "noul", "noul": 0.1}, "idea_ok": {"type": "noul", "noul": 0.95}}}
    with patch.object(d, "_call_jev", new=AsyncMock(return_value=answer)) as m_call, \
         patch.object(d, "_price_in_per_mtok", return_value=0.042), \
         patch.object(d, "record_call_with_pool", new=AsyncMock()) as m_log:
        dec = await d._decide(d._SingleConn(conn), "a3_research", "kw:US:peninsula", "peninsula",
                              ["kw_belongs", "idea_ok"], None)
    assert dec.rejected("kw_belongs")                           # enforce + confident no
    assert not dec.accepted("idea_ok")                          # shadow never acts
    assert dec.verdicts["idea_ok"].zone == "accept"             # but its zone is still recorded
    assert dec.cost_usd == pytest.approx(1000 * 0.042 / 1_000_000)
    sent = m_call.await_args.args[1]
    assert set(sent) == {"kw_belongs", "idea_ok"} and sent["kw_belongs"]["type"] == "noul"
    rows = conn.executemany.await_args.args[1]
    assert {r[1] for r in rows} == {"kw_belongs", "idea_ok"} and {r[6] for r in rows} == {"reject", "accept"}
    log = m_log.await_args.kwargs
    assert log["provider"] == "typesafe" and log["role"] == "validate" and log["tokens_in"] == 1000


@pytest.mark.asyncio
async def test_non_allowlisted_tenant_is_skipped_without_calling_jev():
    conn = _conn([_q()], allowlist=[TEST_TENANT])
    with patch.object(d, "_call_jev", new=AsyncMock()) as m_call, \
         patch.object(d, "record_call_with_pool", new=AsyncMock()):
        dec = await d._decide(d._SingleConn(conn), "t3", "piece:1", "text", ["kw_belongs"], REAL_TENANT)
    m_call.assert_not_awaited()
    assert dec.verdicts["kw_belongs"].zone == "skipped" and not dec.rejected("kw_belongs")
    assert conn.executemany.await_args.args[1][0][6] == "skipped"   # still visible in the ledger


@pytest.mark.asyncio
async def test_allowlisted_tenant_and_platform_are_asked():
    for tenant in (TEST_TENANT, PLATFORM, None):
        conn = _conn([_q()], allowlist=[TEST_TENANT])
        answer = {"model": "jev", "usage": {"input_tokens": 1},
                  "answers": {"kw_belongs": {"type": "noul", "noul": 0.9}}}
        with patch.object(d, "_call_jev", new=AsyncMock(return_value=answer)) as m_call, \
             patch.object(d, "_price_in_per_mtok", return_value=0.042), \
             patch.object(d, "record_call_with_pool", new=AsyncMock()):
            dec = await d._decide(d._SingleConn(conn), "t3", "piece:1", "text", ["kw_belongs"], tenant)
        m_call.assert_awaited_once()
        assert dec.accepted("kw_belongs")


@pytest.mark.asyncio
async def test_jev_down_fails_open_as_error():
    conn = _conn([_q()])
    with patch.object(d, "_call_jev", new=AsyncMock(side_effect=RuntimeError("503"))), \
         patch.object(d, "record_call_with_pool", new=AsyncMock()) as m_log:
        dec = await d._decide(d._SingleConn(conn), "a3_research", "kw:x", "x", ["kw_belongs"], None)
    v = dec.verdicts["kw_belongs"]
    assert v.zone == "error" and not dec.rejected("kw_belongs") and not dec.accepted("kw_belongs")
    m_log.assert_not_awaited()                                  # nothing billed
    assert conn.executemany.await_args.args[1][0][6] == "error"


@pytest.mark.asyncio
async def test_off_and_unknown_questions_are_not_sent():
    conn = _conn([_q(mode="off")])
    with patch.object(d, "_call_jev", new=AsyncMock()) as m_call, \
         patch.object(d, "record_call_with_pool", new=AsyncMock()):
        dec = await d._decide(d._SingleConn(conn), "a3_research", "kw:x", "x", ["kw_belongs", "nope"], None)
    m_call.assert_not_awaited()
    assert dec.verdicts["kw_belongs"].zone == "skipped" and dec.verdicts["nope"].zone == "error"


@pytest.mark.asyncio
async def test_config_unavailable_fails_open():
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=Exception('relation "shared.decision_question" does not exist'))
    dec = await d._decide(d._SingleConn(conn), "a3_research", "kw:x", "x", ["kw_belongs"], None)
    assert dec.verdicts["kw_belongs"].zone == "error"


@pytest.mark.asyncio
async def test_concurrent_decides_on_a_tiny_pool_do_not_deadlock():
    """S203 regression: holding a connection across the Jev call and then acquiring a second one for
    llm_call_log deadlocked a small pool under concurrency. Now at most one is held, briefly."""
    import asyncio
    from contextlib import asynccontextmanager

    conn = _conn([_q()])
    slots = asyncio.Semaphore(1)                       # a pool with ONE connection

    class TinyPool:
        @asynccontextmanager
        async def acquire(self):
            async with slots:
                yield conn

    async def slow_jev(state, questions):
        await asyncio.sleep(0.05)
        return {"model": "jev", "usage": {"input_tokens": 1}, "answers": {"kw_belongs": {"type": "noul", "noul": 0.9}}}

    async def fake_log(pool, **kw):
        async with pool.acquire():                   # the real helper acquires from the same pool
            pass

    with patch.object(d, "_call_jev", new=slow_jev), patch.object(d, "_price_in_per_mtok", return_value=0.042), \
         patch.object(d, "record_call_with_pool", new=fake_log):
        results = await asyncio.wait_for(asyncio.gather(*[
            d.decide("a3_research", f"kw:{i}", "x", ["kw_belongs"], pool=TinyPool()) for i in range(4)
        ]), timeout=5)
    assert all(r.accepted("kw_belongs") for r in results)


@pytest.mark.asyncio
async def test_cached_verdict_is_reused_with_todays_floors_and_no_call():
    # stored p=0.25; today's reject ceiling 0.30 → reject, without asking Jev again
    conn = _conn([_q(accept=0.95, reject=0.30)],
                 cached=[{"question_key": "kw_belongs", "probability": 0.25, "choice": None, "probabilities": None}])
    with patch.object(d, "_call_jev", new=AsyncMock()) as m_call, \
         patch.object(d, "record_call_with_pool", new=AsyncMock()) as m_log:
        dec = await d._decide(d._SingleConn(conn), "a3_research", "kw:x", "x", ["kw_belongs"], None)
    m_call.assert_not_awaited()
    m_log.assert_not_awaited()                                  # nothing billed
    v = dec.verdicts["kw_belongs"]
    assert v.cached and v.zone == "reject" and dec.rejected("kw_belongs")
    row = conn.executemany.await_args.args[1][0]
    assert row[13] == 0.0 and row[15] == "h-kw_belongs" and row[16] is True   # cost, hash, cached


@pytest.mark.asyncio
async def test_cache_miss_asks_jev_and_logs_the_wording_hash():
    conn = _conn([_q()])
    answer = {"model": "jev", "usage": {"input_tokens": 10}, "answers": {"kw_belongs": {"type": "noul", "noul": 0.9}}}
    with patch.object(d, "_call_jev", new=AsyncMock(return_value=answer)) as m_call, \
         patch.object(d, "_price_in_per_mtok", return_value=0.042), \
         patch.object(d, "record_call_with_pool", new=AsyncMock()):
        dec = await d._decide(d._SingleConn(conn), "a3_research", "kw:x", "x", ["kw_belongs"], None)
    m_call.assert_awaited_once()
    assert not dec.verdicts["kw_belongs"].cached
    row = conn.executemany.await_args.args[1][0]
    assert row[15] == "h-kw_belongs" and row[16] is False


def test_cached_choice_answer_keeps_pick_and_confidence():
    q = _q(key="row_kind", kind="choice", accept=0.9, reject=None)
    ans = d.cached_answer(q, {"probability": 0.95, "choice": "poi", "probabilities": '{"poi": 0.95}'})
    v = d.zone_for(q, ans)
    assert (v.zone, v.choice) == ("accept", "poi")


# ── only the gateway may talk to TypeSafe ─────────────────────────────────────────────────────

def test_no_typesafe_endpoint_outside_the_gateway():
    repo = Path(__file__).resolve().parents[2]
    hits = [str(p.relative_to(repo)) for root in ("services", "api") for p in (repo / root).rglob("*.py")
            if "typesafe.ai" in p.read_text(encoding="utf-8") or "/v1/systemone" in p.read_text(encoding="utf-8")]
    assert not hits, f"TypeSafe must be called through shared/llm_client/decide.py: {hits}"
