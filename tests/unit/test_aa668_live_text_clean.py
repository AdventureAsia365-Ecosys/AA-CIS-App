"""AA-668 — the tenant live view (GET /v1/progress/...) shows piece text only.

Live Dev run S239: during a T9 write the Writer panel showed the sentence-repair model reply as raw
JSON ({"replacements": [...]}) and the final snapshot held that 165-char JSON instead of the post.
The repair call runs on the writer stage (`t9_write`), so the stream sink took its reply as a new
draft. These tests pin the fix: the repair reply never streams, the job publishes the text it keeps
(`progress_show`), and the final snapshot is the saved text — for the T9 post and the T2 tour."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import generate
from services.acp_content_writing import service
from services.acp_shared import writing_progress as wp
from shared.llm_client import stream_sink

REPAIR_REPLY = '{"replacements": ["Uran Togoo is a dormant volcano in Bulgan Province."]}'


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def set(self, k, v, ex=None):
        self.store[k] = v


def _piece_tracker(display="text", redis=None):
    return wp.WritingProgress(
        redis or FakeRedis(), tenant_id="t1", kind="piece", job_id="p1",
        steps=[("prepare", "Preparing"), ("write", "Writing"), ("check", "Checking"),
               ("revise", "Revising"), ("save", "Saving")],
        stream_stages={"t9_write"}, display=display,
    )


class _StreamingLLM:
    """Stands in for LLMClient: reports the call and streams its reply through the bound sink,
    the way the real client does for a writer stage (see test_aa637 provider wiring)."""
    reply = ""

    def generate(self, request):
        stream_sink.emit_call_start(request.stage)
        cb = stream_sink.delta_callback(request.stage)
        if cb is not None:
            for i in range(0, len(self.reply), 7):
                cb(self.reply[i:i + 7])
        stream_sink.emit_call_end(request.stage, True)
        return SimpleNamespace(content=self.reply, cost_usd=0.001, model_used="haiku",
                               input_tokens=1, output_tokens=1, stop_reason="end_turn")


def _all_text(snap: dict) -> str:
    return "\n".join(s["text"] for s in snap["sections"])


# ── stream_sink.suppressed ─────────────────────────────────────────────────────

def test_suppressed_hides_the_sink_and_restores_it():
    t = _piece_tracker()
    with stream_sink.bind(t):
        with stream_sink.suppressed():
            assert stream_sink.current() is None
            assert stream_sink.delta_callback("t9_write") is None
        assert stream_sink.current() is t


def test_suppressed_reaches_worker_threads():
    t = _piece_tracker()

    def worker():
        with stream_sink.suppressed():
            stream_sink.emit_call_start("t9_write")
            assert stream_sink.delta_callback("t9_write") is None

    async def run():
        with stream_sink.bind(t):
            await asyncio.to_thread(worker)

    asyncio.run(run())
    assert t.snapshot()["revision"] == 0


# ── the T9 sentence repair never streams ───────────────────────────────────────

def test_sentence_repair_reply_never_reaches_the_live_view(monkeypatch):
    llm = _StreamingLLM()
    monkeypatch.setattr(generate, "LLMClient", lambda: llm)
    monkeypatch.setattr(generate, "record_call_sync", lambda **k: None)
    t = _piece_tracker()
    with stream_sink.bind(t):
        # the attempt-1 post streams as usual (sanity: the fake does stream on t9_write)
        llm.reply = "Uran Togoo last erupted thousands of years ago."
        llm.generate(SimpleNamespace(stage="t9_write"))
        assert _all_text(t.snapshot()) == "Uran Togoo last erupted thousands of years ago."
        # the repair runs on the same writer stage, with a JSON reply
        llm.reply = REPAIR_REPLY
        reps, _cost = generate.repair_sentences(
            sentences=[{"sentence": "Uran Togoo last erupted thousands of years ago.", "problem": "x"}],
            content_seed="seed", channel="linkedin")
    assert reps == ["Uran Togoo is a dormant volcano in Bulgan Province."]
    snap = t.snapshot()
    assert "replacements" not in json.dumps(snap)
    assert _all_text(snap) == "Uran Togoo last erupted thousands of years ago."
    assert snap["revision"] == 1  # the repair is not a new draft


def test_plain_text_view_never_shows_a_json_reply():
    assert wp.plain_text_view(REPAIR_REPLY) == ""
    assert wp.plain_text_view('```json\n{"replacements": ["a"') == ""
    assert wp.build_sections(REPAIR_REPLY, "text") == []
    assert wp.plain_text_view("A post that mentions {braces} later") == "A post that mentions {braces} later"


# ── progress_show: the text the job keeps ──────────────────────────────────────

def test_progress_show_replaces_text_without_a_new_revision():
    t = _piece_tracker()
    t.on_call_start("t9_write")
    t.on_delta("t9_write", "Draft with a bad sentence.")
    with stream_sink.bind(t):
        wp.progress_show("Draft with a fixed sentence.")
    snap = t.snapshot()
    assert _all_text(snap) == "Draft with a fixed sentence." and snap["revision"] == 1


def test_progress_show_is_a_noop_without_a_tracker():
    wp.progress_show("nothing bound")  # must not raise


def test_progress_show_blog_drops_null_fields():
    t = _piece_tracker(display="blog_json")
    with stream_sink.bind(t):
        wp.progress_show({"seo_title": None, "meta_description": "Meta.", "body": "## Why go\nBody"})
    secs = t.snapshot()["sections"]
    assert [s["key"] for s in secs] == ["meta_description", "body"]
    assert "null" not in _all_text(t.snapshot())


@pytest.mark.parametrize("channel,display", [("linkedin", "text"), ("blog", "blog_json")])
def test_show_live_text_matches_the_channel_display(channel, display):
    t = _piece_tracker(display=display)
    with stream_sink.bind(t):
        service._show_live_text("Final post body.", {"seo_title": "A title", "meta_description": None}, channel)
    secs = t.snapshot()["sections"]
    body = next(s for s in secs if s["key"] == "body")
    assert body["text"] == "Final post body."
    if channel == "blog":
        assert [s["key"] for s in secs] == ["seo_title", "body"]


def test_final_snapshot_is_the_saved_text_after_a_repair(monkeypatch):
    """The live-run sequence: write → check → repair (JSON) → splice → save → finish. Every
    snapshot the flusher could write is piece text, and the last one is the spliced post."""
    llm = _StreamingLLM()
    monkeypatch.setattr(generate, "LLMClient", lambda: llm)
    monkeypatch.setattr(generate, "record_call_sync", lambda **k: None)
    redis = FakeRedis()
    t = _piece_tracker(redis=redis)
    seen = []

    async def run():
        with stream_sink.bind(t):
            t.step("prepare")
            llm.reply = "Bad sentence. Good sentence."
            await asyncio.to_thread(llm.generate, SimpleNamespace(stage="t9_write"))
            seen.append(t.snapshot())
            wp.progress_step("check")
            llm.reply = '{"replacements": ["Fixed sentence."]}'
            wp.progress_step("revise")
            await asyncio.to_thread(
                generate.repair_sentences, sentences=[{"sentence": "Bad sentence.", "problem": "x"}],
                content_seed="seed", channel="linkedin")
            seen.append(t.snapshot())
            service._show_live_text("Fixed sentence. Good sentence.", {}, "linkedin")
            seen.append(t.snapshot())
            wp.progress_step("save")
        await t.finish(True)

    asyncio.run(run())
    final = json.loads(redis.store["wp:t1:piece:p1"])
    assert all("replacements" not in json.dumps(s) for s in seen + [final])
    assert final["status"] == "done"
    assert _all_text(final) == "Fixed sentence. Good sentence."
    states = {s["key"]: s["state"] for s in final["steps"]}
    assert states["revise"] == "done" and states["save"] == "done"


# ── T2: the final tour snapshot is the saved version ───────────────────────────

def _tour_tracker(redis):
    return wp.WritingProgress(
        redis, tenant_id="t1", kind="tour", job_id="v1", steps=wp.TOUR_STEPS,
        stream_stages={"t2_generate", "s1_generate"}, display="tour_json",
        stage_steps=wp.TOUR_STAGE_STEPS,
    )


@pytest.mark.asyncio
async def test_t2_final_snapshot_is_the_saved_version():
    """T3 can keep an earlier draft and the em-dash fix runs after the last streamed draft: the live
    view must end on what was saved, not on the last draft the model streamed."""
    from services.jobs import t2_rewrite_job as t2

    streamed_last = {"name": "Draft two name", "summary": "Draft two summary"}
    chosen = {"name": "Sapa Trek — Reimagined", "subtitle": "New subtitle", "summary": "Kept summary",
              "highlights": ["h1", "h2"], "itineraries": [{"day": 1, "title": "Sapa", "body": "Walk."}],
              "seo_title": "st", "seo_meta": "sm", "trip_type": "trek"}

    async def fake_rewrite(*a, **k):
        # the second draft streams into the live view through the writer stage
        stream_sink.emit_call_start("t2_generate")
        stream_sink.delta_callback("t2_generate")(json.dumps(streamed_last))
        return {"status": "success", "generated": streamed_last, "quality_score": 8.0}

    qa = {"result": {"status": "success", "generated": chosen, "quality_score": 8.0},
          "passed": True, "attempts": 1, "structural_issues": [], "grounding_issues": []}
    conn = AsyncMock()
    conn.fetchrow.return_value = None   # no admin seo_context row
    conn.fetchval.return_value = 7.5    # source quality score
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)

    redis = FakeRedis()
    tracker = _tour_tracker(redis)
    pt = {"tour_id": "tour-1"}
    with patch("api.routers.v1_pipeline._rewrite_tour", side_effect=fake_rewrite), \
         patch("services.acp_produce.tenant_pipeline.run_t3_qa_gate", AsyncMock(return_value=qa)), \
         patch("services.seo_intelligence.s1_prefetch.tenant_market_seo", AsyncMock(return_value=("US", None))):
        with stream_sink.bind(tracker):
            await t2._rewrite_and_save(pool, pt, {"name": "Sapa Trek", "itineraries": ""}, {}, "t1", "v1", "p1")
        await tracker.finish(True)

    final = json.loads(redis.store["wp:t1:tour:v1"])
    by_key = {s["key"]: s["text"] for s in final["sections"]}
    saved = json.loads(next(c.args[1] for c in conn.execute.call_args_list
                            if "rewritten_content" in c.args[0]))
    assert by_key["name"] == saved["name"] != streamed_last["name"]
    assert "—" not in by_key["name"]  # the em-dash fix ran before the view was set
    assert by_key["summary"] == "Kept summary"
    assert by_key["highlights"] == "• h1\n• h2"
    assert by_key["itineraries"] == "Day 1 — Sapa\n\nWalk."
    assert "trip_type" not in by_key and "status" not in by_key


# ── T9 end to end: the job path itself (review S239) ───────────────────────────

_T9_PIECE = ("The trail to Tiger's Nest starts manageable — a dirt track through pine forest. [R:a1]\n\n"
             "Tiger's Nest is among the most sacred sites in Vajrayana Buddhism. "
             "The hike is steep and worth it. Book with us.")
_T9_F9 = {"gate": "F9_cta_fact", "passed": False, "blocking": True, "repairable": True, "violations": [
    'audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "The trail to Tiger\'s Nest starts '
    'manageable — a dirt track through pine forest." (claims: the start is a pine forest track; source: no '
    'pine forest); "Tiger\'s Nest is among the most sacred sites in Vajrayana Buddhism." (claims: most sacred; '
    'source: not stated)']}


class _HistoryRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.history = []

    async def set(self, k, v, ex=None):
        self.history.append(json.loads(v))
        await super().set(k, v, ex)


def _t9_outcome(ledger):
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


@pytest.mark.asyncio
async def test_t9_job_live_view_ends_on_the_saved_post_after_a_repair(monkeypatch):
    """Drives the real job path (t9_write_job._run_write_with_progress → run_write_background) with
    a bound tracker: attempt 1 streams the post, the gates ask for a sentence repair, the real
    repair_sentences call streams a JSON patch on t9_write, the job splices it, code edits the
    text again after the splice, and the piece is saved. The live view must never carry the
    patch, the repair must not count as a new draft, the spliced text must show before the
    re-check, and the final snapshot must be exactly the saved content_text."""
    from services.jobs import t9_write_job as t9

    llm = _StreamingLLM()
    llm.reply = '{"replacements": ["Fixed one.", "Fixed two."]}'
    monkeypatch.setattr(generate, "LLMClient", lambda: llm)
    monkeypatch.setattr(generate, "record_call_sync", lambda **k: None)

    redis = _HistoryRedis()
    tracker = wp.WritingProgress(redis, tenant_id="t1", kind="piece", job_id="p1", steps=t9.T9_STEPS,
                                 stream_stages={"t9_write"}, display="text")
    seen = []  # the view at points the flusher could write it

    def fake_write_content(**_k):
        stream_sink.emit_call_start("t9_write")
        stream_sink.delta_callback("t9_write")(_T9_PIECE)
        stream_sink.emit_call_end("t9_write", True)
        return _T9_PIECE, 0.01, {}, None

    edits = iter(range(1, 10))

    def fake_autofix(text, _ctx):
        # code edits after the last LLM step of each attempt: the saved text differs from the splice
        return f"{text} Edit number {next(edits)} here.", []

    def fake_gates(**kw):
        seen.append(tracker.snapshot())
        return _t9_outcome([_T9_F9]) if len(seen) == 1 else _t9_outcome([])

    finalize = AsyncMock(side_effect=lambda *a, **k: seen.append(tracker.snapshot()))
    conn = AsyncMock()
    conn.fetchval.return_value = "approved"
    acq = AsyncMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    ctx = {"atom_text": "seed", "goal": {"key": "promotion"}, "channel_style": {"channel": "linkedin"},
           "brand_audience": {}, "chosen": {}, "cta": "Book", "destination": None, "trip_name": None,
           "brand_rubric_text": "", "channel": "linkedin", "atom_id": "a1"}

    with patch.object(service, "write_content", side_effect=fake_write_content), \
         patch.object(service, "rewrite_with_feedback") as rw, \
         patch.object(service.hygiene, "autofix", side_effect=fake_autofix), \
         patch.object(service, "run_quality_gates", side_effect=fake_gates), \
         patch.object(service, "_finalize_piece", new=finalize):
        await t9._run_write_with_progress(uuid4(), uuid4(), ctx, pool, tracker)

    rw.assert_not_called()
    assert finalize.call_args.kwargs["repair_log"][0]["repair_mode"] == "sentences"  # a repair ran
    saved = finalize.call_args.kwargs["content_text"]
    final = json.loads(redis.store["wp:t1:piece:p1"])
    for snap in seen + redis.history + [final]:
        assert "replacements" not in json.dumps(snap)
        assert snap["revision"] == 1  # the repair is not a new draft
    after_splice = _all_text(seen[1])  # what the re-check saw in the live view
    assert after_splice.startswith("Fixed one.") and "Fixed two." in after_splice
    assert final["status"] == "done"
    assert _all_text(final) == saved  # the final snapshot is the saved post
    assert _all_text(seen[-1]) == saved  # ...already before the save step ran
    states = {s["key"]: s["state"] for s in final["steps"]}
    assert states["revise"] == "done" and states["save"] == "done"
