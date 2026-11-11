"""AA-1076 — the `llm_batch` job, the consumers' hand-off and the on-demand fallback, without a database.

- the runner: a handler's `Reschedule` puts its own row back (queue.reschedule, the attempt not counted);
- the job: prepare -> guard -> submit (fenced by phase 'submitting') -> poll (rescheduled, growing delay) -> ingest
  (ledger once, then the consumer's hand-off); a cap refusal sends nothing; a dry run stops at the estimate;
- the fallback (Done-when): a failed / expired / cancelled item reaches its normal job WITHOUT a reply, and the S1
  writer and the A3 day read then run ON DEMAND; a usable reply answers attempt 1 only, never a retry, never a
  stale prompt;
- the admin view's shape (spend, standard price, saved).
No AWS, no vendor, no DB, $0.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.jobs import llm_batch_job as lbj
from shared.jobs import queue, registry
from shared.jobs.registry import JobContext, NonRetryable, Reschedule
from shared.llm_client import batch_lane
from shared.llm_client.batch_lane import BatchCapExceeded, BatchItem, BatchTarget, ItemResult

TARGET = BatchTarget(stage="s1_generate", role="writer", model_key="sonnet-5-5", connection_key="anthropic-direct",
                     provider="anthropic", wire_id="claude-sonnet-5-5")
T1, T2, T3 = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
              "33333333-3333-3333-3333-333333333333")


def _ctx(payload, progress=None, pool=None):
    job = queue.Job(id="job-1", kind="llm_batch", payload=payload, status="running", attempt=1, max_attempts=3,
                    progress=dict(progress or {}), cost_usd=0.0)
    ctx = JobContext(pool or MagicMock(), job, {})
    ctx.progress = AsyncMock()
    return ctx


def _payload(**kw):
    p = {"batch_id": "B1", "consumer": "s1_attempt1", "cap_usd": 0.2, "tour_ids": [T1, T2, T3], "args": {}}
    p.update(kw)
    return p


def _items():
    return [BatchItem(custom_id=f"s1-{i}", system="SYS", user=f"USER {i}", max_tokens=16000, tour_id=t)
            for i, t in enumerate((T1, T2, T3))]


def _ok(cid, text='{"name": "Lake Verano walk"}', **kw):
    base = dict(custom_id=cid, outcome="succeeded", text=text, stop_reason="end_turn", input_tokens=1000,
                output_tokens=400)
    base.update(kw)
    return ItemResult(**base)


# ── the runner: Reschedule ───────────────────────────────────────────────────────────────────────────────────────

def test_reschedule_clamps_its_delay():
    assert Reschedule(0).delay_seconds == 1
    assert Reschedule(10_000).delay_seconds == registry.RESCHEDULE_MAX_DELAY_SECONDS
    assert Reschedule(90, {"a": 1}).progress == {"a": 1}


@pytest.mark.asyncio
async def test_the_worker_reschedules_the_same_row(monkeypatch):
    from shared.jobs.worker import Worker
    monkeypatch.setattr(registry, "_KINDS", {})

    @registry.job_kind("poll_kind")
    async def poll_kind(ctx):
        raise Reschedule(120, {"phase": "polling", "provider_batch_id": "p1"})

    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=0.0)
    w = Worker(pool, worker_id="w1", heartbeat_seconds=0.05)
    job = queue.Job(id="00000000-0000-0000-0000-000000000009", kind="poll_kind", payload={}, status="running",
                    attempt=1, max_attempts=3, progress={}, cost_usd=0.0)
    resched = AsyncMock(return_value="queued")
    with patch.object(queue, "reschedule", resched), patch.object(queue, "complete", AsyncMock()) as complete, \
            patch.object(queue, "fail", AsyncMock()) as fail:
        await w._execute_job(job)
    resched.assert_awaited_once()
    args, kw = resched.await_args
    assert args[1:5] == (job.id, "w1", 120, {"phase": "polling", "provider_batch_id": "p1"})
    assert kw == {"max_count": registry.RESCHEDULE_MAX_COUNT}
    complete.assert_not_awaited()
    fail.assert_not_awaited()


def test_llm_batch_is_registered_on_the_bulk_line():
    from shared.llm_client.capacity import BULK, KIND_LINES
    k = registry.get_kind("llm_batch")
    assert k is not None and k.max_attempts == 3 and not k.deploy_blocking
    assert KIND_LINES["llm_batch"] == BULK


def test_poll_delays_grow_to_ten_minutes():
    assert [lbj.poll_delay(n) for n in range(9)] == [60, 60, 120, 120, 300, 300, 600, 600, 600]


# ── the job ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def lane(monkeypatch):
    prepare = AsyncMock(return_value=("s1_generate", _items(), {"skipped": []}))
    ingest = AsyncMock(return_value={"kind": "s1_rewrite", "enqueued": 3})
    monkeypatch.setattr("services.llm_batch.consumers.CONSUMERS", {"s1_attempt1": (prepare, ingest)})
    monkeypatch.setattr(batch_lane, "resolve_target", lambda stage: BatchTarget(**TARGET.as_dict()))
    monkeypatch.setattr(batch_lane, "history_basis", AsyncMock(return_value={"n": 20, "p95": 0.08}))
    monkeypatch.setattr(batch_lane, "guard", lambda t, items, cap, basis=None: batch_lane.Estimate(
        usd=0.12, worst_case_usd=0.25, basis="ledger_p95", basis_calls=basis["n"]))
    submit = MagicMock(return_value="msgbatch_1")
    monkeypatch.setattr(batch_lane, "submit", submit)
    return SimpleNamespace(prepare=prepare, ingest=ingest, submit=submit)


@pytest.mark.asyncio
async def test_prepare_guard_submit_then_reschedule(lane):
    ctx = _ctx(_payload())
    with pytest.raises(Reschedule) as exc:
        await lbj.run(ctx)
    assert exc.value.delay_seconds == 60
    assert exc.value.progress["phase"] == "submitted" and exc.value.progress["provider_batch_id"] == "msgbatch_1"
    lane.submit.assert_called_once()
    # 'submitting' (with the item list, never a prompt) is written BEFORE the provider call
    phases = [c.kwargs.get("phase") for c in ctx.progress.await_args_list]
    assert phases == ["preparing", "submitting"]
    submitting = ctx.progress.await_args_list[1].kwargs
    assert [i["custom_id"] for i in submitting["items"]] == ["s1-0", "s1-1", "s1-2"]
    assert all(len(i["prompt_sha"]) == 32 and "system" not in i for i in submitting["items"])
    assert submitting["estimate_usd"] == 0.12 and submitting["target"]["connection_key"] == "anthropic-direct"
    assert submitting["estimate"] == {"usd": 0.12, "worst_case_usd": 0.25, "basis": "ledger_p95", "basis_calls": 20}


@pytest.mark.asyncio
async def test_a_run_found_in_submitting_never_sends_again(lane):
    with pytest.raises(NonRetryable, match="submit outcome unknown"):
        await lbj.run(_ctx(_payload(), progress={"phase": "submitting", "target": TARGET.as_dict()}))
    lane.submit.assert_not_called()


@pytest.mark.asyncio
async def test_a_cap_refusal_sends_nothing(lane, monkeypatch):
    def refuse(t, items, cap, basis=None):
        raise BatchCapExceeded("estimate 0.30 USD is above its cap 0.20 USD")
    monkeypatch.setattr(batch_lane, "guard", refuse)
    ctx = _ctx(_payload())
    with pytest.raises(NonRetryable, match="refused before submit"):
        await lbj.run(ctx)
    lane.submit.assert_not_called()
    assert ctx.progress.await_args_list[-1].kwargs["phase"] == "refused"


@pytest.mark.asyncio
async def test_a_dry_run_stops_at_the_estimate(lane):
    out = await lbj.run(_ctx(_payload(dry_run=True)))
    assert out["phase"] == "estimated" and out["estimate_usd"] == 0.12 and out["requests"] == 3
    lane.submit.assert_not_called()


def _sent(polls=0):
    return {"phase": "submitted", "provider_batch_id": "msgbatch_1", "submitted_at": "2026-11-11T00:00:00+00:00",
            "polls": polls, "target": TARGET.as_dict(), "estimate_usd": 0.12, "cap_usd": 0.2,
            "items": [{"custom_id": f"s1-{i}", "tour_id": t, "prompt_sha": f"sha{i}"}
                      for i, t in enumerate((T1, T2, T3))]}


@pytest.mark.asyncio
async def test_poll_reschedules_while_the_provider_works(lane, monkeypatch):
    monkeypatch.setattr(batch_lane, "status", lambda t, p: SimpleNamespace(ended=False, processing_status="in_progress",
                                                                          counts={"processing": 3}))
    monkeypatch.setattr(lbj, "_now", lambda: lbj._parse_ts("2026-11-11T01:00:00+00:00"))
    with pytest.raises(Reschedule) as exc:
        await lbj.run(_ctx(_payload(), progress=_sent(polls=4)))
    assert exc.value.delay_seconds == 300 and exc.value.progress["polls"] == 5
    assert exc.value.progress["provider_status"] == "in_progress"


@pytest.mark.asyncio
async def test_past_the_deadline_the_batch_is_cancelled(lane, monkeypatch):
    monkeypatch.setattr(batch_lane, "status", lambda t, p: SimpleNamespace(ended=False, processing_status="in_progress",
                                                                          counts={}))
    cancel = MagicMock()
    monkeypatch.setattr(batch_lane, "cancel", cancel)
    monkeypatch.setattr(lbj, "_now", lambda: lbj._parse_ts("2026-11-12T02:00:00+00:00"))
    with pytest.raises(Reschedule) as exc:
        await lbj.run(_ctx(_payload(), progress=_sent()))
    cancel.assert_called_once()
    assert exc.value.progress["cancel_sent_at"]


@pytest.mark.asyncio
async def test_an_ended_batch_is_ledgered_once_then_handed_off(lane, monkeypatch):
    monkeypatch.setattr(batch_lane, "status", lambda t, p: SimpleNamespace(ended=True, processing_status="ended",
                                                                          counts={}))
    results = {"s1-0": _ok("s1-0"), "s1-1": ItemResult(custom_id="s1-1", outcome="expired"),
               "s1-2": ItemResult(custom_id="s1-2", outcome="errored", error="overloaded")}
    monkeypatch.setattr(batch_lane, "results", lambda *a, **k: results)
    seen = iter([{"rows": 0, "usd": 0.0, "billed": 0}, {"rows": 3, "usd": 0.006, "billed": 1}])
    monkeypatch.setattr(batch_lane, "ledgered", AsyncMock(side_effect=lambda *a: next(seen)))
    record = AsyncMock()
    monkeypatch.setattr(lbj.call_log, "record_calls", record)
    out = await lbj.run(_ctx(_payload(), progress=_sent()))
    rows = record.await_args.args[0]
    assert len(rows) == 3 and {r["quality_signal"]["batch_outcome"] for r in rows} == {"succeeded", "expired",
                                                                                        "errored"}
    lane.ingest.assert_awaited_once()
    assert out["outcomes"] == {"usable": 1, "expired": 1, "errored": 1}
    assert out["spend_usd"] == 0.006 and out["standard_usd"] == pytest.approx(0.012)


@pytest.mark.asyncio
async def test_an_ingest_re_run_does_not_write_the_ledger_twice(lane, monkeypatch):
    monkeypatch.setattr(batch_lane, "status", lambda t, p: SimpleNamespace(ended=True, processing_status="ended",
                                                                          counts={}))
    monkeypatch.setattr(batch_lane, "results", lambda *a, **k: {"s1-0": _ok("s1-0")})
    monkeypatch.setattr(batch_lane, "ledgered", AsyncMock(return_value={"rows": 3, "usd": 0.006, "billed": 3}))
    record = AsyncMock()
    monkeypatch.setattr(lbj.call_log, "record_calls", record)
    await lbj.run(_ctx(_payload(), progress=_sent()))
    record.assert_not_awaited()
    lane.ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_spread_out_provider_errors_never_fail_the_job(lane, monkeypatch):
    """Review S277: a provider error after submit reschedules (attempt not counted) instead of failing the attempt;
    every good status read resets the count, so errors far apart never add up to a failed, unledgered batch."""
    from shared.llm_client.adapters.anthropic_messages import AnthropicAPIError
    monkeypatch.setattr(lbj, "_now", lambda: lbj._parse_ts("2026-11-11T01:00:00+00:00"))
    replies = iter([AnthropicAPIError("overloaded", status_code=529), "working",
                    ConnectionError("connection reset"), "working",
                    AnthropicAPIError("unauthorized", status_code=401), "ended"])

    def status(t, p):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(ended=r == "ended", processing_status=r, counts={})
    monkeypatch.setattr(batch_lane, "status", status)
    monkeypatch.setattr(batch_lane, "results", lambda *a, **k: {"s1-0": _ok("s1-0")})
    monkeypatch.setattr(batch_lane, "ledgered", AsyncMock(return_value={"rows": 0, "usd": 0.0, "billed": 0}))
    monkeypatch.setattr(lbj.call_log, "record_calls", AsyncMock())
    prog, errors_seen = _sent(), []
    for _ in range(5):
        with pytest.raises(Reschedule) as exc:
            await lbj.run(_ctx(_payload(), progress=prog))
        prog = {**prog, **exc.value.progress}
        errors_seen.append(prog["poll_errors"])
    assert errors_seen == [1, 0, 1, 0, 1]
    assert prog["last_error"].startswith("AnthropicAPIError: unauthorized")
    out = await lbj.run(_ctx(_payload(), progress=prog))
    assert out["phase"] == "done"
    lane.ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_poll_that_raises_three_times_then_ends_is_ingested(lane, monkeypatch):
    from shared.llm_client.adapters.openai_batches import OpenAIBatchError
    calls = {"n": 0}

    def status(t, p):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise OpenAIBatchError("502 bad gateway")
        return SimpleNamespace(ended=True, processing_status="ended", counts={})
    monkeypatch.setattr(batch_lane, "status", status)
    monkeypatch.setattr(batch_lane, "results", lambda *a, **k: {"s1-0": _ok("s1-0")})
    monkeypatch.setattr(batch_lane, "ledgered", AsyncMock(return_value={"rows": 0, "usd": 0.0, "billed": 0}))
    record = AsyncMock()
    monkeypatch.setattr(lbj.call_log, "record_calls", record)
    prog, delays = _sent(), []
    for _ in range(3):
        with pytest.raises(Reschedule) as exc:
            await lbj.run(_ctx(_payload(), progress=prog))
        prog = {**prog, **exc.value.progress}
        delays.append(exc.value.delay_seconds)
    assert prog["poll_errors"] == 3 and delays == [60, 120, 120]
    out = await lbj.run(_ctx(_payload(), progress=prog))
    assert out["phase"] == "done"
    record.assert_awaited_once()
    lane.ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_results_error_reschedules_and_the_next_run_ingests(lane, monkeypatch):
    monkeypatch.setattr(batch_lane, "status", lambda t, p: SimpleNamespace(ended=True, processing_status="ended",
                                                                          counts={}))
    calls = {"n": 0}

    def results(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("read timed out")
        return {"s1-0": _ok("s1-0")}
    monkeypatch.setattr(batch_lane, "results", results)
    monkeypatch.setattr(batch_lane, "ledgered", AsyncMock(return_value={"rows": 0, "usd": 0.0, "billed": 0}))
    monkeypatch.setattr(lbj.call_log, "record_calls", AsyncMock())
    with pytest.raises(Reschedule) as exc:
        await lbj.run(_ctx(_payload(), progress=_sent()))
    assert exc.value.progress["phase"] == "ingesting" and exc.value.progress["poll_errors"] == 1
    lane.ingest.assert_not_awaited()
    out = await lbj.run(_ctx(_payload(), progress={**_sent(), **exc.value.progress}))
    assert out["phase"] == "done"
    lane.ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_errors_in_a_row_fail_the_job_and_a_retry_polls_again(lane, monkeypatch):
    def down(t, p):
        raise ConnectionError("no route to host")
    monkeypatch.setattr(batch_lane, "status", down)
    ctx = _ctx(_payload(), progress={**_sent(), "poll_errors": lbj.POLL_ERROR_LIMIT - 1})
    with pytest.raises(NonRetryable, match="provider errors in a row after submit") as exc:
        await lbj.run(ctx)
    assert "msgbatch_1" in str(exc.value) and "nothing is sent again" in str(exc.value)
    # the counter is cleared so a Retry starts polling the same provider batch afresh
    assert ctx.progress.await_args_list[-1].kwargs["poll_errors"] == 0
    lane.submit.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_deadline_cancel_reschedules_and_is_sent_again(lane, monkeypatch):
    monkeypatch.setattr(batch_lane, "status", lambda t, p: SimpleNamespace(ended=False, processing_status="in_progress",
                                                                          counts={}))

    def cancel(t, p):
        raise ConnectionError("connection reset")
    monkeypatch.setattr(batch_lane, "cancel", cancel)
    monkeypatch.setattr(lbj, "_now", lambda: lbj._parse_ts("2026-11-12T02:00:00+00:00"))
    with pytest.raises(Reschedule) as exc:
        await lbj.run(_ctx(_payload(), progress=_sent()))
    assert exc.value.progress["poll_errors"] == 1 and "cancel_sent_at" not in exc.value.progress


# ── the S1 hand-off: failed / expired items go on demand ─────────────────────────────────────────────────────────

class _Pool:
    def __init__(self):
        self.enqueued = []


@pytest.mark.asyncio
async def test_s1_hand_off_seeds_usable_items_and_sends_the_rest_on_demand(monkeypatch):
    from services.llm_batch import consumers
    calls = []

    async def fake_enqueue(pool, kind, payload, **kw):
        calls.append((kind, payload, kw))
        return f"job-{len(calls)}", True
    monkeypatch.setattr("shared.jobs.registry.enqueue", fake_enqueue)
    items = [{"custom_id": f"s1-{i}", "tour_id": t, "prompt_sha": f"sha{i}"} for i, t in enumerate((T1, T2, T3))]
    results = {"s1-0": _ok("s1-0"), "s1-1": ItemResult(custom_id="s1-1", outcome="expired"),
               "s1-2": _ok("s1-2", stop_reason="refusal")}
    out = await consumers.ingest_s1(None, _payload(args={"batch_id": T3}), TARGET, items, results, batch_id="B1")
    assert out["seeded"] == 1 and out["on_demand"] == 2
    by_tour = {p["tour_id"]: (p, kw) for _k, p, kw in calls}
    assert all(k == "s1_rewrite" for k, _p, _kw in calls)
    assert len(by_tour[T1][0]["batch_replies"]) == 1 and by_tour[T1][0]["batch_replies"][0]["prompt_sha"] == "sha0"
    assert by_tour[T2][0]["batch_replies"] == [] and by_tour[T3][0]["batch_replies"] == []
    # the same job key as POST /admin/run-tour-async (tour + the run's batch): never a second rewrite
    assert by_tour[T1][1]["idempotency_key"] == f"s1_rewrite:{T1}:{T3}"
    assert by_tour[T1][0]["batch_id"] == T3 and by_tour[T1][0]["tenant_id"].startswith("00000000")


@pytest.mark.asyncio
async def test_the_drill_withholds_one_usable_reply(monkeypatch):
    from services.llm_batch import consumers
    calls = []

    async def fake_enqueue(pool, kind, payload, **kw):
        calls.append(payload)
        return "j", True
    monkeypatch.setattr("shared.jobs.registry.enqueue", fake_enqueue)
    items = [{"custom_id": f"s1-{i}", "tour_id": t, "prompt_sha": f"sha{i}"} for i, t in enumerate((T1, T2))]
    out = await consumers.ingest_s1(None, _payload(args={"drill_withhold": 1}), TARGET, items,
                                    {"s1-0": _ok("s1-0"), "s1-1": _ok("s1-1")}, batch_id="B1")
    assert out["drill_withheld"] == [T1] and out["seeded"] == 1 and calls[0]["batch_replies"] == []


def _v2_state():
    return {"tour": {"name": "Lake Verano walk", "itineraries": "Day 1 — Arrive\nWalk by the lake.", "country": "X"},
            "generate_stage": "s1_generate", "brand_forbidden_words": []}


def test_a_failed_item_falls_back_to_the_on_demand_writer(monkeypatch):
    """No reply bound (the item failed / expired / was cancelled): the writer calls the gateway live."""
    from services.content_generation import s1_writer_v2_live as live
    client = MagicMock()
    client.generate.return_value = SimpleNamespace(content='{"days": []}', model_used="anthropic-api-sonnet-5-5",
                                                   cost_usd=0.05, fallback_used=False, satellite_account=None,
                                                   input_tokens=10, output_tokens=5, stop_reason="end_turn",
                                                   cache_read_tokens=0, cache_write_tokens=0)
    monkeypatch.setattr(live, "record_call_sync", MagicMock())
    wp = live.writer_prompts(_v2_state())
    with batch_lane.bind_replies([]):
        out = live._call(client, _v2_state(), call="write", system=wp["system"], user=wp["user"], max_tokens=16000,
                         prefetch=True)
    client.generate.assert_called_once()
    assert out["ok"] and not out.get("batch_seeded")
    live.record_call_sync.assert_called_once()                # the live call is ledgered by the caller as before


def test_a_usable_reply_answers_attempt_one_without_a_call_or_a_second_row(monkeypatch):
    from services.content_generation import s1_writer_v2_live as live
    client = MagicMock()
    monkeypatch.setattr(live, "record_call_sync", MagicMock())
    wp = live.writer_prompts(_v2_state())
    rec = batch_lane.reply_record(TARGET, _ok("s1-0", text='{"days": []}'),
                                  batch_lane.prompt_sha("s1_generate", wp["system"], wp["user"]), batch_id="B1")
    with batch_lane.bind_replies([rec]):
        out = live._call(client, _v2_state(), call="write", system=wp["system"], user=wp["user"], max_tokens=16000,
                         prefetch=True)
        assert out["ok"] and out["batch_seeded"] and out["parsed"] == {"days": []} and out["cost_usd"] > 0
        client.generate.assert_not_called()
        live.record_call_sync.assert_not_called()
        # a retry (prefetch False: validate feedback present) always writes live
        client.generate.return_value = SimpleNamespace(content='{"days": []}', model_used="m", cost_usd=0.01,
                                                       fallback_used=False, satellite_account=None,
                                                       cache_read_tokens=0, cache_write_tokens=0)
        live._call(client, _v2_state(), call="write", system=wp["system"], user=wp["user"] + " FEEDBACK",
                   max_tokens=16000, prefetch=False)
        client.generate.assert_called_once()


def test_a_reply_that_is_not_json_is_not_used(monkeypatch):
    from services.content_generation import s1_writer_v2_live as live
    rec = batch_lane.reply_record(TARGET, _ok("s1-0", text="not json"), batch_lane.prompt_sha("s1_generate", "S", "U"),
                                  batch_id="B1")
    with batch_lane.bind_replies([rec]):
        assert live._batch_reply("s1_generate", "S", "U") is None


@pytest.mark.asyncio
async def test_a3_day_read_takes_the_reply_then_falls_back_live(monkeypatch):
    from services.acp_contract import a3_atomize as a3
    reply = '{"atoms": [{"place": "Lake Verano", "action": "walk", "evidence": "Walk by the lake."}]}'
    rec = batch_lane.reply_record(BatchTarget(**{**TARGET.as_dict(), "stage": "a3_atomize"}), _ok("a3-x-1",
                                                                                                    text=reply),
                                  batch_lane.prompt_sha("a3_atomize", a3._SYSTEM_PROMPT, "DAY PROMPT"), batch_id="B1")
    live = MagicMock(side_effect=AssertionError("no live call for a batched day"))
    monkeypatch.setattr(a3, "LLMClient", lambda: SimpleNamespace(generate=live))
    with batch_lane.bind_replies([rec]):
        atoms, resp, attempt, err = await a3._ask_atoms("DAY PROMPT", "validate", event="t", log_fields={},
                                                        prefetch=True)
    assert atoms and atoms[0]["place"] == "Lake Verano" and attempt == 1 and err is None
    assert batch_lane.already_ledgered(resp)
    # an expired day (no reply bound): read live
    live_resp = SimpleNamespace(content=reply, stop_reason="end_turn")
    monkeypatch.setattr(a3, "LLMClient", lambda: SimpleNamespace(generate=lambda req: live_resp))
    with batch_lane.bind_replies([]):
        atoms, resp, attempt, err = await a3._ask_atoms("DAY PROMPT", "validate", event="t", log_fields={},
                                                        prefetch=True)
    assert resp is live_resp and not batch_lane.already_ledgered(resp)


def test_the_jobs_bind_the_replies_of_their_payload():
    import inspect
    from services.jobs import a3_atomize_job, s1_rewrite_job
    assert 'bind_replies(payload.get("batch_replies"))' in inspect.getsource(s1_rewrite_job.run)
    assert 'bind_replies(p.get("batch_replies"))' in inspect.getsource(a3_atomize_job.run)


@pytest.mark.asyncio
async def test_the_binding_reaches_a_thread_and_a_nested_loop():
    """The A3 job runs its chain in a thread with its own event loop (asyncio.to_thread + asyncio.run)."""
    rec = batch_lane.reply_record(TARGET, _ok("x"), batch_lane.prompt_sha("s1_generate", "S", "U"), batch_id="B")

    def in_thread():
        async def inner():
            return batch_lane.prefetched("s1_generate", "S", "U")
        return asyncio.run(inner())
    with batch_lane.bind_replies([rec]):
        got = await asyncio.to_thread(in_thread)
    assert got is not None


@pytest.mark.asyncio
async def test_a3_enqueue_with_replies_is_keyed_on_the_batch(monkeypatch):
    from services.jobs import a3_atomize_job
    enq = AsyncMock(return_value=("j", True))
    monkeypatch.setattr(a3_atomize_job, "enqueue", enq)
    await a3_atomize_job.enqueue_a3_atomize(None, tour_id=T1, version_id=T2, country="X", rewritten={},
                                            created_by="t", batch_replies=[{"prompt_sha": "s"}], batch_id="B1")
    args, kw = enq.await_args
    assert kw["idempotency_key"] == f"a3_atomize_batch:{T2}:B1" and args[2]["batch_replies"] == [{"prompt_sha": "s"}]
    await a3_atomize_job.enqueue_a3_atomize(None, tour_id=T1, version_id=T2, country="X", rewritten={}, created_by="t")
    args, kw = enq.await_args
    assert kw["idempotency_key"] == f"a3_atomize:{T2}" and "batch_replies" not in args[2]


# ── consumers: S1 refuses without writer v2; ad hoc needs an adhoc_ stage ────────────────────────────────────────

@pytest.mark.asyncio
async def test_s1_batches_need_the_writer_v2(monkeypatch):
    from services.content_generation import s1_writer_v2_live
    from services.llm_batch import consumers
    monkeypatch.setattr(s1_writer_v2_live, "enabled", lambda: False)
    with pytest.raises(batch_lane.BatchUnavailable, match="writer v2"):
        await consumers.prepare_s1(None, _payload())


@pytest.mark.asyncio
async def test_adhoc_and_tour_list_rules():
    from services.llm_batch import consumers
    with pytest.raises(batch_lane.BatchUnavailable, match="adhoc_"):
        await consumers.prepare_adhoc(None, {"args": {"stage": "s1_generate", "items": [{"custom_id": "a",
                                                                                         "user": "u"}]}})
    stage, items, extra = await consumers.prepare_adhoc(None, {"args": {
        "stage": "adhoc_aa1076", "route_stage": "s1_judge", "items": [{"custom_id": "a", "user": "u"}]}})
    assert stage == "s1_judge" and extra == {"ledger_stage": "adhoc_aa1076"} and items[0].max_tokens == 4096
    with pytest.raises(batch_lane.BatchUnavailable):
        consumers._tour_ids({"tour_ids": []})
    assert consumers._tour_ids({"tour_ids": [T1.upper(), T1]}) == [T1]
    with pytest.raises(batch_lane.BatchUnavailable, match="at most"):
        consumers._tour_ids({"tour_ids": [f"{i:08d}-0000-0000-0000-000000000000" for i in range(201)]})


# ── admin view ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_admin_shape_states_spend_standard_price_and_saving():
    from api.services import ai_gateway_batches as agb
    row = {"id": "job-1", "status": "succeeded", "payload": {"batch_id": "B1", "consumer": "s1_attempt1",
                                                              "cap_usd": 0.2, "tour_ids": [T1, T2, T3]},
           "progress": {"phase": "done", "target": TARGET.as_dict(), "provider_batch_id": "msgbatch_1",
                        "estimate_usd": 0.12, "cap_usd": 0.2, "submitted_at": "2026-11-11T00:00:00+00:00"},
           "result": {"outcomes": {"usable": 2, "expired": 1}, "requests": 3,
                      "handoff": {"kind": "s1_rewrite", "enqueued": 3, "seeded": 2, "on_demand": 1, "jobs": []}},
           "error": None, "created_at": None, "finished_at": None, "run_after": None, "created_by": "admin:x"}
    out = agb.shape(row, {"rows": 3, "usd": 0.05, "billed": 2})
    assert out["spend_usd"] == 0.05 and out["standard_usd"] == 0.1 and out["saved_usd"] == 0.05
    assert out["provider"] == "anthropic" and out["model_key"] == "sonnet-5-5" and out["tours"] == 3
    assert out["handoff"] == {"kind": "s1_rewrite", "enqueued": 3, "seeded": 2, "on_demand": 1}
    assert out["outcomes"] == {"usable": 2, "expired": 1} and out["ledger_rows"] == 3


def test_the_page_send_payload():
    from api.services import ai_gateway_batches as agb
    p = agb.payload_for(consumer="s1_attempt1", tour_ids=[T1], cap_usd=0.2, dry_run=False, run_batch_id=T2,
                        seo_mode="standard", cache_system=None, drill_withhold=1)
    assert p["tour_ids"] == [T1] and p["args"] == {"seo_mode": "standard", "batch_id": T2, "drill_withhold": 1}
    with pytest.raises(ValueError):
        agb.payload_for(consumer="adhoc", tour_ids=[T1], cap_usd=1, dry_run=False, run_batch_id=None,
                        seo_mode="standard", cache_system=None, drill_withhold=0)


# ── admin routes (called directly, mocked pool) ──────────────────────────────────────────────────────────────────

class _Acq:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_post_batch_enqueues_one_llm_batch_job_and_audits(monkeypatch):
    from api.routers import admin_ai_gateway as r
    enq = AsyncMock(return_value=("job-9", True))
    monkeypatch.setattr("shared.jobs.registry.enqueue", enq)
    audit = AsyncMock()
    monkeypatch.setattr(r, "write_audit_log", audit)
    pool = MagicMock()
    pool.acquire = lambda: _Acq(MagicMock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    body = r.BatchSendBody(consumer="s1_attempt1", tour_ids=[T1, T2, T3], cap_usd=0.2)
    out = await r.post_batch(body, request, x_admin_user_id="u1")
    assert out["job_id"] == "job-9" and out["poll_url"] == "/admin/ai-gateway/batches/job-9"
    args, kw = enq.await_args
    assert args[1] == "llm_batch" and args[2]["tour_ids"] == [T1, T2, T3] and args[2]["cap_usd"] == 0.2
    assert kw["idempotency_key"] == f"llm_batch:{out['batch_id']}" and kw["created_by"] == "admin:u1"
    assert audit.await_args.kwargs["action"] == "llm_batch.sent"


@pytest.mark.asyncio
async def test_post_batch_is_refused_while_the_lane_is_off(monkeypatch):
    from fastapi import HTTPException
    from api.routers import admin_ai_gateway as r
    monkeypatch.setenv(batch_lane.SWITCH_ENV, "off")
    body = r.BatchSendBody(consumer="a3_atomize", tour_ids=[T1], cap_usd=0.2)
    with pytest.raises(HTTPException) as e:
        await r.post_batch(body, SimpleNamespace(app=None), x_admin_user_id=None)
    assert e.value.status_code == 503


def test_the_send_body_validates_its_fields():
    from pydantic import ValidationError
    from api.routers import admin_ai_gateway as r
    for bad in ({"consumer": "adhoc", "tour_ids": [T1], "cap_usd": 1}, {"consumer": "s1_attempt1", "tour_ids": [],
                                                                       "cap_usd": 1},
                {"consumer": "s1_attempt1", "tour_ids": [T1], "cap_usd": 0},
                {"consumer": "s1_attempt1", "tour_ids": [T1], "cap_usd": 101},
                {"consumer": "s1_attempt1", "tour_ids": ["not-a-uuid"], "cap_usd": 1},
                {"consumer": "s1_attempt1", "tour_ids": [T1], "cap_usd": 1, "drill_withhold": 2}):
        with pytest.raises(ValidationError):
            r.BatchSendBody(**bad)
