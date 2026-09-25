"""AA-637 — live writing progress (services/acp_shared/writing_progress.py, stream_sink).

Pure logic, no Redis/Bedrock: partial-JSON shaping, tag hiding, step transitions, and the sink
contract (only writer stages stream text; a provider restart drops partial text)."""
import asyncio
import json

import pytest

from services.acp_shared import writing_progress as wp
from shared.llm_client import stream_sink


# ── display shaping ────────────────────────────────────────────────────────────

def test_scan_partial_json_includes_the_string_being_written():
    raw = '```json\n{"name": "Silk Road", "summary": "Three days along the old car'
    vals = wp.scan_partial_json(raw)
    assert vals == [(("name",), "Silk Road"), (("summary",), "Three days along the old car")]


def test_scan_partial_json_arrays_objects_and_escapes():
    raw = ('{"highlights": ["Hike to \\"Tiger\\" Nest", "Punakha Dzong"], '
           '"itineraries": [{"day": 1, "title": "Paro", "body": "Arrive.\\nRest"}, {"day": 2, "tit')
    vals = wp.scan_partial_json(raw)
    assert (("highlights", 0), 'Hike to "Tiger" Nest') in vals
    assert (("highlights", 1), "Punakha Dzong") in vals
    assert (("itineraries", 0, "day"), "1") in vals
    assert (("itineraries", 0, "body"), "Arrive.\nRest") in vals
    # a key still being written is never reported as a value
    assert all(p[-1] != "tit" for p, _ in vals)


def test_scan_partial_json_never_raises_on_garbage():
    assert wp.scan_partial_json("no json here") == []
    assert wp.scan_partial_json('{"a": "x\\u00') == [(("a",), "x")]


def test_tour_sections_are_labelled_and_ordered():
    raw = ('{"name": "Bhutan Unhurried", "subtitle": "Seven days", "highlights": ["A", "B"], '
           '"itineraries": [{"day": 1, "title": "Paro", "body": "Arrive in Paro."}], '
           '"seo_title": "Bhutan tour", "trip_type": "cultural"}')
    secs = wp.build_sections(raw, "tour_json")
    assert [s["label"] for s in secs] == ["Title", "Subtitle", "Highlights", "Itinerary", "SEO title"]
    assert secs[2]["text"] == "• A\n• B"
    assert secs[3]["text"] == "Day 1 — Paro\n\nArrive in Paro."
    # trip_type is internal — not shown
    assert all(s["key"] != "trip_type" for s in secs)


def test_blog_sections_show_body_and_hide_summary_and_tags():
    raw = '{"body": "## Why go\\nThimphu has no traffic lights [R:atom_12ab] and'
    secs = wp.build_sections(raw, "blog_json")
    assert secs == [{"key": "body", "label": "", "text": "## Why go\nThimphu has no traffic lights and"}]
    raw2 = raw + ' more", "summary": "internal", "seo_title": "Bhutan'
    secs2 = wp.build_sections(raw2, "blog_json")
    assert [s["key"] for s in secs2] == ["body", "seo_title"]


@pytest.mark.parametrize("text,expected", [
    ("Temples of Paro [R:atom_1] are", "Temples of Paro are"),
    ("ends with a half tag [R:atom", "ends with a half tag"),
    ("ends with [", "ends with"),
    ("ends with [F", "ends with"),
    ("fact [F:fact_9] here", "fact here"),
])
def test_strip_tags_streaming(text, expected):
    assert wp.strip_tags_streaming(text) == expected


def test_plain_text_view_hides_summary_and_partial_marker():
    assert wp.plain_text_view("Post body\n===SUMMARY===\ninternal") == "Post body"
    assert wp.plain_text_view("Post body\n===SUM") == "Post body"
    assert wp.plain_text_view("```text\nHello") == "Hello"


# ── tracker ────────────────────────────────────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self.store = {}

    async def set(self, k, v, ex=None):
        self.store[k] = v

    async def get(self, k):
        return self.store.get(k)


def _tracker(redis=None):
    return wp.WritingProgress(
        redis or FakeRedis(), tenant_id="t1", kind="piece", job_id="p1",
        steps=[("prepare", "Preparing"), ("write", "Writing"), ("check", "Checking"),
               ("revise", "Revising"), ("save", "Saving")],
        stream_stages={"t9_write"}, display="text",
    )


def test_key_is_tenant_scoped():
    assert wp.progress_key("tenant-a", "piece", "p1") == "wp:tenant-a:piece:p1"
    with pytest.raises(ValueError):
        wp.WritingProgress(FakeRedis(), tenant_id="t", kind="nope", job_id="x", steps=[],
                           stream_stages=[], display="text")


def test_only_writer_stages_stream_and_restart_drops_partial_text():
    t = _tracker()
    assert t.wants("t9_write") and not t.wants("s1_judge")
    t.on_call_start("t9_write")
    t.on_delta("t9_write", "Half a draft from a provider that then failed")
    t.on_restart("t9_write")
    t.on_delta("t9_write", "Clean draft")
    t.on_delta("s1_judge", '{"score": 3}')  # never shown
    snap = t.snapshot()
    assert snap["sections"][0]["text"] == "Clean draft"


def test_second_writer_call_is_a_revision_and_replaces_the_draft():
    t = _tracker()
    t.step("prepare")
    t.on_call_start("t9_write")
    t.on_delta("t9_write", "first draft")
    t.step("check")
    t.on_call_start("t9_write")
    snap = t.snapshot()
    states = {s["key"]: s["state"] for s in snap["steps"]}
    assert states == {"prepare": "done", "write": "done", "check": "done", "revise": "active", "save": "pending"}
    assert snap["sections"] == [] and snap["revision"] == 2


def test_unused_steps_are_skipped_and_finish_writes_final_snapshot():
    redis = FakeRedis()
    t = _tracker(redis)

    async def run():
        t.start()
        t.step("prepare")
        t.on_call_start("t9_write")
        t.on_delta("t9_write", "Final text")
        t.step("check")
        t.step("save")
        await t.finish(True)

    asyncio.run(run())
    snap = json.loads(redis.store["wp:t1:piece:p1"])
    states = {s["key"]: s["state"] for s in snap["steps"]}
    assert snap["status"] == "done"
    assert states["revise"] == "skipped" and states["save"] == "done"
    assert snap["sections"][0]["text"] == "Final text"


def test_failed_finish_marks_active_step_failed():
    redis = FakeRedis()
    t = _tracker(redis)

    async def run():
        t.step("write")
        await t.finish(False, "Something went wrong")

    asyncio.run(run())
    snap = json.loads(redis.store["wp:t1:piece:p1"])
    assert snap["status"] == "failed" and snap["message"] == "Something went wrong"
    assert {s["key"]: s["state"] for s in snap["steps"]}["write"] == "failed"


def test_redis_errors_never_propagate():
    class Broken:
        async def set(self, *a, **k):
            raise ConnectionError("redis down")

    t = _tracker(Broken())
    asyncio.run(t.finish(True))  # must not raise


# ── sink plumbing ──────────────────────────────────────────────────────────────

def test_delta_callback_none_without_sink_or_for_unwanted_stage():
    assert stream_sink.delta_callback("t9_write") is None
    t = _tracker()
    with stream_sink.bind(t):
        assert stream_sink.delta_callback("s1_judge") is None
        cb = stream_sink.delta_callback("t9_write")
        assert cb is not None
    assert stream_sink.current() is None


def test_sink_reaches_worker_threads_via_to_thread():
    t = _tracker()

    def worker():
        stream_sink.emit_call_start("t9_write")
        stream_sink.delta_callback("t9_write")("from a thread")

    async def run():
        with stream_sink.bind(t):
            await asyncio.to_thread(worker)

    asyncio.run(run())
    assert t.snapshot()["sections"][0]["text"] == "from a thread"


def test_sink_errors_are_swallowed():
    class Exploding:
        def wants(self, stage):
            return True

        def on_call_start(self, stage):
            raise RuntimeError("boom")

        def on_restart(self, stage):
            raise RuntimeError("boom")

        def on_delta(self, stage, text):
            raise RuntimeError("boom")

        def on_call_end(self, stage, ok):
            raise RuntimeError("boom")

    with stream_sink.bind(Exploding()):
        stream_sink.emit_call_start("x")
        stream_sink.emit_restart("x")
        stream_sink.delta_callback("x")("text")
        stream_sink.emit_call_end("x", True)


# ── provider wiring ────────────────────────────────────────────────────────────

def _ev(obj):
    return {"chunk": {"bytes": json.dumps(obj).encode()}}


def _stream_events(texts, usage_in=None, out_tokens=7, stop="end_turn"):
    evs = [_ev({"type": "message_start", "message": {"usage": usage_in or {"input_tokens": 11}}})]
    for t in texts:
        evs.append(_ev({"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}))
    evs.append(_ev({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": out_tokens}}))
    return evs


def test_satellite_streams_only_when_given_a_callback(monkeypatch):
    from unittest.mock import MagicMock
    from shared.llm_client import bedrock_satellite as bs

    rt = MagicMock()
    rt.invoke_model_with_response_stream.return_value = {"body": _stream_events(["Hel", "lo"])}
    session = MagicMock()
    session.client.return_value = rt
    monkeypatch.setattr(bs, "_get_satellite_session", lambda account="acc1": session)

    got = []
    res = bs.invoke_claude("p", model="haiku", account="acc3", on_delta=got.append)
    assert got == ["Hel", "lo"] and res.text == "Hello"
    assert res.usage["input_tokens"] == 11 and res.usage["output_tokens"] == 7 and res.stop_reason == "end_turn"
    rt.invoke_model.assert_not_called()

    # no callback -> the old non-streaming request, untouched
    rt.invoke_model.return_value = {"body": MagicMock(read=lambda: json.dumps(
        {"content": [{"text": "plain"}], "usage": {}, "stop_reason": "end_turn"}).encode())}
    res2 = bs.invoke_claude("p", model="haiku", account="acc3")
    assert res2.text == "plain"
    assert rt.invoke_model.called


def test_llm_client_reports_call_and_streams_writer_stage(monkeypatch):
    from unittest.mock import MagicMock
    from shared.llm_client import client as cl
    from shared.llm_client.models import LLMRequest

    c = cl.LLMClient.__new__(cl.LLMClient)
    c._bedrock = MagicMock()
    c._bedrock.invoke_model_with_response_stream.return_value = {"body": _stream_events(["Draft ", "text"])}
    monkeypatch.setattr(cl, "get_stage_config_sync", lambda stage: None)

    t = _tracker()
    with stream_sink.bind(t):
        resp = c.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="t9_write", model_tier="haiku"))
    assert resp.content == "Draft text"
    snap = t.snapshot()
    assert snap["sections"][0]["text"] == "Draft text"
    assert {s["key"]: s["state"] for s in snap["steps"]}["write"] == "active"


def test_llm_client_without_sink_is_unchanged(monkeypatch):
    from unittest.mock import MagicMock
    from shared.llm_client import client as cl
    from shared.llm_client.models import LLMRequest

    c = cl.LLMClient.__new__(cl.LLMClient)
    c._bedrock = MagicMock()
    c._bedrock.invoke_model_with_response_stream.return_value = {"body": _stream_events(["ok"])}
    monkeypatch.setattr(cl, "get_stage_config_sync", lambda stage: None)
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="t9_write", model_tier="haiku")
    assert c.generate(req).content == "ok"


# ── endpoint ───────────────────────────────────────────────────────────────────

def test_progress_endpoint_reads_only_the_callers_tenant_key():
    from unittest.mock import MagicMock
    from api.routers import v1_progress

    redis = FakeRedis()
    redis.store["wp:tenant-a:piece:p1"] = json.dumps({"status": "running", "steps": [], "sections": []})
    req = MagicMock()
    req.app.state.redis = redis

    own = asyncio.run(v1_progress.get_progress("piece", "p1", req, tenant={"sub": "tenant-a"}))
    other = asyncio.run(v1_progress.get_progress("piece", "p1", req, tenant={"sub": "tenant-b"}))
    assert own["found"] is True and own["status"] == "running"
    assert other == {"found": False}


def test_progress_endpoint_rejects_unknown_kind_and_survives_redis_errors():
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    from api.routers import v1_progress

    req = MagicMock()
    with pytest.raises(HTTPException):
        asyncio.run(v1_progress.get_progress("admin", "x", req, tenant={"sub": "t"}))

    class Down:
        async def get(self, k):
            raise ConnectionError("down")
    req.app.state.redis = Down()
    assert asyncio.run(v1_progress.get_progress("tour", "v1", req, tenant={"sub": "t"})) == {"found": False}
