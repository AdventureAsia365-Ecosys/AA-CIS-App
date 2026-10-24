"""AA-645 (S250) — a failed atomize read is retried, first inside run_a3_atomize, then by the job.

S250 Thailand wave: one day of tour 7dbbc234 came back as JSON that did not parse ("Expecting property name
enclosed in double quotes: line 183"). run_a3_atomize returned status "failed", but the a3_atomize job still ended
"succeeded", so max_attempts=2 never applied and the day kept no atoms.

* _ask_atoms: a second gateway call for a reply that raised or did not parse; a reply cut at the token ceiling
  is re-asked with more room; every billed unusable reply gets its own llm_call_log row (AA-927).
* run_a3_atomize: a day still failed keeps no fingerprint row, the other days are kept; the result names the day.
* _run_a3_atomize_background(retry_failed_days=True) stops before Segment/Score/Route; the job raises the
  retryable AtomizeIncomplete on every attempt that still has a failed day (on the last one after the later steps).
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import a3_atomize
from services.acp_contract.a3_atomize import AtomizeIncomplete
from services.export import handler
from services.jobs import a3_atomize_job
from shared.jobs import queue
from shared.jobs.registry import JobContext, NonRetryable

TOUR_ID = "44444444-4444-4444-4444-444444444444"
VERSION_ID = "77777777-7777-7777-7777-777777777777"
TWO_DAYS = ("Day 1 — Arrival in Hanoi\nWalk through the Old Quarter.\n\n"
            "Day 2 — Halong Bay Cruise\nKayak through limestone caves.")
CUT = '{"atoms": [{"place": "Old Quarter", "action": "walk"}, {place'


def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.model_used, self.input_tokens, self.output_tokens, self.cost_usd = "m", 10, 5, 0.001
        self.stop_reason, self.satellite_account, self.fallback_used, self.provider = stop_reason, "acc3", False, "x"


def _atoms(place, action):
    return json.dumps({"atoms": [{"place": place, "action": action, "activity_type": "culture"}]})


def _run(*replies):
    """Patches the gateway (replies: _Resp or an Exception, in call order), the cost log and Jev."""
    client = MagicMock()
    client.generate = MagicMock(side_effect=list(replies))
    record = AsyncMock()
    unusable = AsyncMock()
    patches = [
        patch.object(a3_atomize, "LLMClient", return_value=client),
        patch.object(a3_atomize, "get_stage_config",
                     AsyncMock(return_value=MagicMock(model_id="m", role="writer"))),
        patch.object(a3_atomize, "record_call_with_pool", record),
        patch.object(a3_atomize, "record_unusable", unusable),
        patch.object(a3_atomize, "ground_day_atoms", AsyncMock(side_effect=lambda atoms, *a, **k: atoms)),
    ]
    return patches, client.generate, record, unusable


async def _per_day(conn, *replies):
    patches, gen, record, unusable = _run(*replies)
    for p in patches:
        p.start()
    try:
        out = await a3_atomize.run_a3_atomize(TOUR_ID, {"name": "T", "itineraries": TWO_DAYS}, _pool(conn),
                                              version_id=VERSION_ID)
    finally:
        for p in patches:
            p.stop()
    return out, gen, record, unusable


def _fingerprint_days(conn):
    return [c.args[2] for c in conn.execute.call_args_list if "atomize_day_fingerprint" in c.args[0]]


def _conn():
    conn = AsyncMock()
    conn.fetch.return_value = []          # no fingerprint yet: both days are read
    conn.execute.return_value = "UPDATE 0"
    return conn


# ── in-call retry ────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_malformed_day_is_asked_again_and_kept():
    conn = _conn()
    out, gen, record, unusable = await _per_day(
        conn, _Resp(_atoms("Old Quarter", "walk")), _Resp("{not json"), _Resp(_atoms("Halong Bay", "kayak")))
    assert out["status"] == "success" and out["days_read"] == 2 and "days_failed" not in out
    assert gen.call_count == 3
    assert _fingerprint_days(conn) == [1, 2]
    # The unusable reply: one billed_unusable row (attempt 1); the kept reply: the normal row (attempt 2).
    unusable.assert_awaited_once()
    assert unusable.await_args.kwargs["quality_signal_extra"] == {"day_number": 2, "attempt": 1}
    signals = [c.kwargs["quality_signal"] for c in record.await_args_list]
    assert [(s["day_number"], s["attempt"]) for s in signals] == [(1, 1), (2, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["max_tokens", "length"])
async def test_a_reply_cut_at_the_ceiling_is_retried_with_more_room(stop_reason):
    out, gen, _, _ = await _per_day(
        _conn(), _Resp(CUT, stop_reason=stop_reason), _Resp(_atoms("Old Quarter", "walk")),
        _Resp(_atoms("Halong Bay", "kayak")))
    assert out["status"] == "success"
    sizes = [c.args[0].max_tokens for c in gen.call_args_list]
    assert sizes == [4096, a3_atomize._TRUNCATION_RETRY_MAX_TOKENS, 4096]
    assert all(c.args[0].stage == "a3_atomize" and c.args[0].model_tier is None for c in gen.call_args_list)


@pytest.mark.asyncio
async def test_a_malformed_reply_that_was_not_cut_keeps_the_same_ceiling():
    _, gen, _, _ = await _per_day(
        _conn(), _Resp("{not json"), _Resp(_atoms("Old Quarter", "walk")), _Resp(_atoms("Halong Bay", "kayak")))
    assert [c.args[0].max_tokens for c in gen.call_args_list] == [4096, 4096, 4096]


@pytest.mark.asyncio
async def test_a_gateway_error_is_asked_again():
    out, gen, _, unusable = await _per_day(
        _conn(), RuntimeError("throttled"), _Resp(_atoms("Old Quarter", "walk")), _Resp(_atoms("Halong Bay", "kayak")))
    assert out["status"] == "success" and gen.call_count == 3
    unusable.assert_not_awaited()          # nothing billed for a raised call


@pytest.mark.asyncio
async def test_atoms_that_are_not_a_list_count_as_a_parse_failure():
    out, gen, _, unusable = await _per_day(
        _conn(), _Resp('{"atoms": null}'), _Resp(_atoms("Old Quarter", "walk")), _Resp(_atoms("Halong Bay", "kayak")))
    assert out["status"] == "success" and gen.call_count == 3
    unusable.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_day_still_failing_keeps_no_fingerprint_and_the_other_day_is_kept():
    conn = _conn()
    out, gen, record, unusable = await _per_day(
        conn, _Resp("{bad"), _Resp("{bad again"), _Resp(_atoms("Halong Bay", "kayak")))
    assert gen.call_count == 3
    assert out["status"] == "failed" and out["days_failed"] == [1] and out["days_read"] == 1
    assert "invalid atom JSON" in out["day_errors"]["1"]
    assert _fingerprint_days(conn) == [2]  # day 1 is re-asked on the next run
    # Both billed unusable replies are logged, each with its attempt.
    assert [c.kwargs["quality_signal_extra"]["attempt"] for c in unusable.await_args_list] == [1, 2]
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_path_retries_too():
    conn = AsyncMock()
    conn.fetchval.return_value = None
    conn.execute.return_value = "UPDATE 0"
    patches, gen, record, unusable = _run(_Resp(CUT, stop_reason="max_tokens"), _Resp(_atoms("Old Quarter", "walk")))
    for p in patches:
        p.start()
    try:
        out = await a3_atomize.run_a3_atomize(TOUR_ID, {"name": "T", "itineraries": "free text"}, _pool(conn))
    finally:
        for p in patches:
            p.stop()
    assert out["status"] == "success" and out["atom_count"] == 1
    assert [c.args[0].max_tokens for c in gen.call_args_list] == [4096, a3_atomize._TRUNCATION_RETRY_MAX_TOKENS]
    unusable.assert_awaited_once()
    assert record.await_args.kwargs["quality_signal"]["attempt"] == 2


@pytest.mark.asyncio
async def test_legacy_path_fails_after_two_bad_replies():
    conn = AsyncMock()
    conn.fetchval.return_value = None
    patches, gen, record, unusable = _run(_Resp("{bad"), RuntimeError("down"))
    for p in patches:
        p.start()
    try:
        out = await a3_atomize.run_a3_atomize(TOUR_ID, {"name": "T", "itineraries": "free text"}, _pool(conn))
    finally:
        for p in patches:
            p.stop()
    assert out == {"status": "failed", "error": "RuntimeError: down"}
    assert gen.call_count == 2 and unusable.await_count == 1
    record.assert_not_awaited()


# ── the job retries what is still failed ─────────────────────────────────────────────────────────────────────

FAILED = {"status": "failed", "atom_count": 3, "days_total": 2, "days_read": 1, "days_failed": [1],
          "day_errors": {"1": "invalid atom JSON from model: x"}, "error": "day(s) [1] failed to atomize"}


def _downstream():
    return [patch("services.recompute.run_stages", AsyncMock(return_value={})),
            patch("services.export.handler.tour_segment_ids", AsyncMock(return_value=[])),
            patch("services.export.handler.clear_brand_fit_cache", AsyncMock(return_value=0)),
            patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock(return_value=("job-9", True))),
            patch("services.places.enqueue.after_atomize", AsyncMock(return_value={}))]


async def _background(result, **kw):
    pool = AsyncMock()
    patches = [patch.object(handler, "open_job_pool", AsyncMock(return_value=pool)),
               patch("services.acp_contract.a3_atomize.run_a3_atomize", AsyncMock(return_value=result)),
               *_downstream()]
    for p in patches:
        p.start()
    try:
        return await handler._run_a3_atomize_background(TOUR_ID, {}, "", VERSION_ID, reraise=True, **kw)
    finally:
        for p in patches:
            p.stop()
        pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_stops_before_the_later_steps_when_days_failed_and_a_retry_follows():
    with pytest.raises(AtomizeIncomplete) as exc:
        await _background(FAILED, retry_failed_days=True)
    assert exc.value.result["days_failed"] == [1]
    assert not isinstance(exc.value, NonRetryable)


@pytest.mark.asyncio
async def test_background_runs_the_later_steps_on_the_final_attempt():
    out = await _background(FAILED, retry_failed_days=False)
    assert out["segment_score_route"] == "ok"
    assert out["atomize"]["status"] == "failed" and out["atomize"]["days_failed"] == [1]
    assert out["atomize"]["day_errors"] == {"1": "invalid atom JSON from model: x"}


def _ctx(attempt):
    job = queue.Job(id="j1", kind=a3_atomize_job.KIND,
                    payload={"tour_id": TOUR_ID, "version_id": VERSION_ID, "country": "",
                             "rewritten": {"name": "T", "itineraries": TWO_DAYS}},
                    status="running", attempt=attempt, max_attempts=2, progress={}, cost_usd=0.0)
    ctx = JobContext(MagicMock(), job, {})
    ctx.progress = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_job_first_attempt_asks_the_chain_to_stop_early_and_raises_retryable():
    async def chain(**kw):
        assert kw["retry_failed_days"] is True
        raise AtomizeIncomplete(FAILED)
    with patch("services.export.handler._run_a3_atomize_background", chain):
        with pytest.raises(AtomizeIncomplete) as exc:
            await a3_atomize_job.run(_ctx(attempt=1))
    assert not isinstance(exc.value, NonRetryable)


@pytest.mark.asyncio
async def test_job_final_attempt_runs_the_chain_and_still_fails_with_its_result():
    seen = {}

    async def chain(**kw):
        seen.update(kw)
        return {"segment_score_route": "ok", "atomize": FAILED}
    ctx = _ctx(attempt=2)
    with patch("services.export.handler._run_a3_atomize_background", chain):
        with pytest.raises(AtomizeIncomplete):
            await a3_atomize_job.run(ctx)
    assert seen["retry_failed_days"] is False
    assert ctx.result["atomize"]["days_failed"] == [1] and ctx.result["segment_score_route"] == "ok"


@pytest.mark.asyncio
async def test_job_succeeds_when_every_day_was_read():
    async def chain(**kw):
        return {"segment_score_route": "ok", "atomize": {"status": "success", "atom_count": 4}}
    with patch("services.export.handler._run_a3_atomize_background", chain):
        out = await a3_atomize_job.run(_ctx(attempt=1))
    assert out["atomize"]["status"] == "success"
