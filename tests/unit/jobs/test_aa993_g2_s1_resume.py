"""AA-993 G2 (ADR 0016 D1) — an `s1_rewrite` job released mid-run (a deploy) and re-claimed resumes from the last step
it finished: no new writer call after the writer, one generated_content row, one quality_scores row, one review entry,
one pipeline_runs cost update.

Release + re-claim is simulated the way the worker does it: the handler's coroutine gets a CancelledError right
after (or right before) a checkpoint write, and a second run starts with the job row's stored progress. The DB is an
in-memory fake that keeps the rows the persist steps write; no LLM is called (the rewrite is a fake that counts
writer calls), no network.
"""
import asyncio
import json
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import admin_pipeline
from services.content_generation import s1_checkpoint
from services.content_generation.s1_checkpoint import S1Checkpoint, writer_state_delta
from services.jobs import s1_rewrite_job as sj
from shared.jobs import queue
from shared.jobs.registry import JobContext

TOUR_ID = "11111111-1111-1111-1111-111111111111"
BATCH_ID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")


# ── fakes ────────────────────────────────────────────────────────────────────────────────────


class FakeDB:
    """The rows `_execute_run_tour` writes, kept across the released run and the resumed one."""

    def __init__(self):
        self.versions: list[dict] = []
        self.scores: list[str] = []
        self.reviews: list[str] = []
        self.accounting = 0
        self.fix_pass_updates = 0
        self.audit_updates = 0

    def conn(self):
        return FakeConn(self)


class FakeConn:
    def __init__(self, db: FakeDB):
        self.db = db

    async def fetchrow(self, sql, *args):
        assert "silver_aa_internal.raw_tours" in sql
        return {
            "src_name": "Test Tour", "src_subtitle": "", "src_summary": "", "src_description": "",
            "src_highlights": [], "src_itineraries": "", "country": "Vietnam", "duration": "3 days",
            "price_raw": "100", "inclusions": "", "exclusions": "", "source_status": None, "activities": None,
            "group_size": None, "best_time_to_go": None,
        }

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        if "INSERT INTO silver_aa_internal.generated_content" in sql:
            vid = str(uuid.uuid4())
            self.db.versions.append({"id": vid, "metadata": json.loads(args[14])})
            return vid
        if "metadata @>" in sql:
            key, value = args[1], args[2]
            hits = [v["id"] for v in self.db.versions if v["metadata"].get(key) == value]
            return hits[-1] if hits else None
        raise AssertionError(f"unexpected fetchval: {sql[:80]}")

    async def execute(self, sql, *args):
        if "INSERT INTO silver_aa_internal.quality_scores" in sql:
            assert "WHERE NOT EXISTS" in sql   # the real guard; the fake applies the same rule
            if args[0] not in self.db.scores:
                self.db.scores.append(args[0])
        elif "INSERT INTO silver_aa_internal.review_queue" in sql:
            assert "WHERE NOT EXISTS" in sql
            if str(args[1]) not in self.db.reviews:
                self.db.reviews.append(str(args[1]))
        elif "UPDATE silver_aa_internal.review_queue" in sql:
            pass
        elif "UPDATE silver_aa_internal.quality_scores" in sql:
            self.db.audit_updates += 1
        elif "UPDATE silver_aa_internal.generated_content" in sql:
            self.db.fix_pass_updates += 1
        elif "UPDATE shared.pipeline_runs" in sql:
            self.db.accounting += 1
        else:
            raise AssertionError(f"unexpected execute: {sql[:80]}")

    async def close(self):
        pass


class FakeRewrite:
    """Stands in for `_rewrite_tour`: one "writer call" unless the run resumes after the writer, reports the writer
    state like the real graph loop does, and returns a held (review-queue) result with audit + fix-pass fields."""

    def __init__(self):
        self.calls = 0
        self.writer_calls = 0
        self.resume_states: list = []

    async def __call__(self, tour, **kw):
        self.calls += 1
        resume_state = kw.get("resume_state")
        self.resume_states.append(resume_state)
        if resume_state is None:
            self.writer_calls += 1
            draft = {"generated": {"name": "Rewritten", "summary": "s", "itineraries": "Day 1 — x"},
                     "model_used": "sonnet", "cost_usd": 0.05, "writer_version": "v2"}
            if kw.get("on_writer_state") is not None:   # the graph loop reports each writer node
                await kw["on_writer_state"](draft)
        else:
            draft = resume_state
        return {
            "status": "success", "generated": dict(draft["generated"]), "quality_score": 6.0,
            "model_used": draft["model_used"], "cost_usd": draft["cost_usd"] + 0.01,
            "input_tokens": 10, "output_tokens": 20, "sub_scores": {"brand": 6.0},
            "failure_codes": ["SUMMARY_OFF_BRAND"], "passed_count": 3, "failed_count": 1,
            "brand_audit_status": "flagged", "brand_audit_codes": ["X"],
            "fix_pass_applied": True, "fix_pass_fields": ["summary"], "writer_version": draft["writer_version"],
        }


def _ctx(progress_store: dict, release_at=None, before_write=False):
    """A JobContext whose progress() writes into `progress_store` (the shared.job row). `release_at` = the
    persist_stage whose checkpoint write is followed (or, with before_write, preceded) by the release."""
    job = queue.Job(id="job-g2", kind="s1_rewrite",
                    payload={"tour_id": TOUR_ID, "batch_id": BATCH_ID, "tenant_id": "aa_internal"},
                    status="running", attempt=1, max_attempts=1, progress=dict(progress_store), cost_usd=0.0)
    ctx = JobContext(None, job, {"redis": None})

    async def progress(**fields):
        if release_at is not None and fields.get("persist_stage") == release_at and before_write:
            raise asyncio.CancelledError()
        progress_store.update(json.loads(json.dumps(fields)))
        if release_at is not None and fields.get("persist_stage") == release_at:
            raise asyncio.CancelledError()

    ctx.progress = progress
    return ctx


def _patches(db: FakeDB, rewrite: FakeRewrite) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch("api.routers.admin_pipeline.asyncpg.connect",
                              AsyncMock(side_effect=lambda *a, **k: db.conn())))
    stack.enter_context(patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(return_value=None)))
    stack.enter_context(patch("api.routers.admin_pipeline.load_s1_lessons_block", AsyncMock(return_value="")))
    stack.enter_context(patch("api.routers.admin_pipeline._rewrite_tour", rewrite))
    stack.enter_context(patch("api.routers.admin_pipeline._is_publishable", MagicMock(return_value=False)))
    stack.enter_context(patch("api.routers.admin_pipeline._build_s1_progress_sink", return_value=None))
    stack.enter_context(patch("services.writing_memory.insertion.resolve", AsyncMock(return_value=None)))
    stack.enter_context(patch("services.seo_intelligence.handler.process_seo",
                              AsyncMock(return_value={"data": {}, "status": "fetched"})))
    stack.enter_context(patch("services.seo_intelligence.seed_builder.build_seed",
                              MagicMock(return_value="Vietnam tours")))
    return stack


def _assert_one_of_each(db: FakeDB, out: dict):
    assert len(db.versions) == 1, "a resumed run wrote a second generated_content row"
    vid = db.versions[0]["id"]
    assert db.versions[0]["metadata"][s1_checkpoint.JOB_MARKER_KEY] == "job-g2"
    assert db.scores == [vid], "quality_scores rows"
    assert db.reviews == [vid], "review entries"
    assert db.accounting == 1, "pipeline_runs cost added once"
    assert out["version_id"] == vid


# ── release + re-claim at every stage ──────────────────────────────────────────────────────────

STAGE_CASES = [
    ("writer_done", False),
    ("rewritten", False),
    ("content_inserted", True),    # released after the INSERT committed, before its checkpoint write
    ("content_inserted", False),
    ("scored", False),
    ("fix_pass_saved", False),
    ("routed", False),
    ("accounted", False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,before_write", STAGE_CASES,
                         ids=[f"{s}{'-before-write' if b else ''}" for s, b in STAGE_CASES])
async def test_release_and_reclaim_resumes_without_duplicates(stage, before_write):
    db, rewrite, row_progress = FakeDB(), FakeRewrite(), {}
    with _patches(db, rewrite):
        with pytest.raises(asyncio.CancelledError):
            await sj.run(_ctx(row_progress, release_at=stage, before_write=before_write))
        assert row_progress.get("phase") != "done"
        out = await sj.run(_ctx(row_progress))     # the re-claimed job, new worker

    assert rewrite.writer_calls == 1, "a resumed run paid the writer again"
    if stage == "writer_done":
        # re-entered the graph after the writer with the saved writer state (v2 stamp kept)
        assert rewrite.calls == 2 and rewrite.resume_states[1]["writer_version"] == "v2"
    else:
        assert rewrite.calls == 1, "a run resumed past the rewrite rewrote again"
    _assert_one_of_each(db, out)
    assert row_progress["phase"] == "done"
    assert row_progress["s1_writer_state"] is None and row_progress["s1_rewrite_result"] is None


@pytest.mark.asyncio
async def test_resumed_run_reports_the_stage_it_resumed_from():
    db, rewrite, row_progress = FakeDB(), FakeRewrite(), {}
    with _patches(db, rewrite):
        with pytest.raises(asyncio.CancelledError):
            await sj.run(_ctx(row_progress, release_at="scored"))
        await sj.run(_ctx(row_progress))
    assert row_progress["resumed_from"] == "scored"
    assert db.audit_updates == 1 and db.fix_pass_updates == 1   # neither re-applied after "scored"


@pytest.mark.asyncio
async def test_no_release_writes_each_row_once_and_clears_bulky_keys():
    db, rewrite, row_progress = FakeDB(), FakeRewrite(), {}
    with _patches(db, rewrite):
        out = await sj.run(_ctx(row_progress))
    _assert_one_of_each(db, out)
    assert row_progress["persist_stage"] == "accounted" and row_progress["phase"] == "done"
    assert row_progress["s1_rewrite_result"] is None


@pytest.mark.asyncio
async def test_version_id_alone_is_not_an_early_return():
    """Critic 9: progress.version_id is written at content_inserted, so it no longer means "finished"."""
    db, rewrite, row_progress = FakeDB(), FakeRewrite(), {}
    with _patches(db, rewrite):
        with pytest.raises(asyncio.CancelledError):
            await sj.run(_ctx(row_progress, release_at="content_inserted"))
        assert row_progress["version_id"] == db.versions[0]["id"]
        out = await sj.run(_ctx(row_progress))
    assert "resumed" not in out and out["version_id"] == db.versions[0]["id"]
    assert db.scores == [db.versions[0]["id"]]


@pytest.mark.asyncio
async def test_internal_retry_after_a_failed_persist_step_reuses_the_version():
    """_run_tour_safe's own retry (3 attempts) shares the checkpoint: a failure after the INSERT does not make a
    second version or a second rewrite."""
    db, rewrite = FakeDB(), FakeRewrite()
    real_execute = FakeConn.execute
    failed = {"n": 0}

    async def flaky_execute(self, sql, *args):
        if "INSERT INTO silver_aa_internal.quality_scores" in sql and not failed["n"]:
            failed["n"] += 1
            raise RuntimeError("connection reset")
        return await real_execute(self, sql, *args)

    row_progress: dict = {}
    with _patches(db, rewrite), patch.object(FakeConn, "execute", flaky_execute):
        out = await sj.run(_ctx(row_progress))
    assert failed["n"] == 1 and rewrite.calls == 1
    _assert_one_of_each(db, out)


@pytest.mark.asyncio
async def test_without_checkpoint_the_sync_path_is_unchanged():
    """/admin/run-tour (no job): no marker, no resume kwargs to _rewrite_tour."""
    db, rewrite = FakeDB(), FakeRewrite()
    with _patches(db, rewrite):
        out = await admin_pipeline._execute_run_tour(admin_pipeline.TourRunRequest(
            tour_id=TOUR_ID, batch_id=BATCH_ID, tenant_id="aa_internal"))
    assert rewrite.resume_states == [None]
    assert s1_checkpoint.JOB_MARKER_KEY not in db.versions[0]["metadata"]
    assert out["version_id"] == db.versions[0]["id"] and db.scores == [out["version_id"]]


# ── the checkpoint object ─────────────────────────────────────────────────────────────────────


def test_writer_state_delta_keeps_only_changed_plain_json_keys():
    initial = {"tour": {"name": "t"}, "generated": {}, "cost_usd": 0.0, "retry_count": 0}
    out = {**initial, "generated": {"name": "x"}, "cost_usd": 0.4, "writer_version": "v2"}
    assert writer_state_delta(initial, out) == {"generated": {"name": "x"}, "cost_usd": 0.4, "writer_version": "v2"}
    assert writer_state_delta(initial, {**initial}) is None                      # no draft
    assert writer_state_delta(initial, {**out, "x": {1, 2}}) is None             # a set is not plain JSON
    assert writer_state_delta(initial, {**out, "x": (1, 2)}) is None             # nor a tuple (comes back a list)


@pytest.mark.asyncio
async def test_checkpoint_write_failure_never_fails_the_run():
    save = AsyncMock(side_effect=RuntimeError("db down"))
    ck = S1Checkpoint("j", {}, save)
    await ck.mark("scored")
    assert ck.stage == "scored" and ck.reached("content_inserted") and not ck.reached("routed")


@pytest.mark.asyncio
async def test_rewrite_result_that_is_not_plain_json_is_not_checkpointed():
    save = AsyncMock()
    ck = S1Checkpoint("j", {}, save)
    await ck.save_rewrite({"generated": {"a": 1}, "codes": {"X"}}, auto_upgraded=False, dataforseo_used=True)
    save.assert_not_awaited()
    assert ck.stage is None
    await ck.mark("content_inserted", version_id="v")   # no stage past `rewritten` without its result
    save.assert_not_awaited()
    assert ck.stage is None


@pytest.mark.asyncio
async def test_stage_never_moves_back():
    save = AsyncMock()
    ck = S1Checkpoint("j", {}, save)
    await ck.mark("routed")
    await ck.mark("content_inserted", version_id="v")
    assert ck.stage == "routed" and save.await_count == 1


def test_stage_past_rewrite_without_its_result_starts_over():
    ck = S1Checkpoint("j", {"persist_stage": "scored", "version_id": "v"}, AsyncMock())
    assert ck.stage is None and ck.version_id is None


def test_unknown_stage_is_ignored():
    assert S1Checkpoint("j", {"persist_stage": "bogus"}, AsyncMock()).stage is None


# ── the graph: a resumed rewrite re-enters after the writer ─────────────────────────────────────


@pytest.fixture
def fake_nodes(monkeypatch):
    """Replace the graph's node functions (looked up when the graph is built) with LLM-free fakes. `generate` counts
    writer calls; `validate` can raise CancelledError once, i.e. the job is released right after the writer."""
    from services.content_generation import graph as g
    calls = {"generate": 0, "validate": 0, "release_in_validate": False}

    def generate(state):
        calls["generate"] += 1
        return {**state, "generated": {"name": "Draft", "summary": "s", "itineraries": "Day 1 — x"},
                "model_used": "sonnet", "cost_usd": state.get("cost_usd", 0.0) + 0.25, "writer_version": "v2"}

    def validate(state):
        calls["validate"] += 1
        if calls["release_in_validate"]:
            calls["release_in_validate"] = False
            raise asyncio.CancelledError()
        return {**state, "quality_score": 8.0, "failure_codes": []}

    def passthrough(state):
        return {**state}

    monkeypatch.setattr(g, "generate_node", generate)
    monkeypatch.setattr(g, "validate_node", validate)
    for name in ("judge_node", "brand_audit_node", "grounding_node", "flag_fix_node", "revalidate_node"):
        monkeypatch.setattr(g, name, passthrough)
    return calls


@pytest.mark.asyncio
async def test_rewrite_resumed_after_the_writer_makes_no_new_writer_call(fake_nodes):
    from api.routers.v1_pipeline import _rewrite_tour
    saved = []

    async def on_writer_state(delta):
        saved.append(delta)

    tour = {"name": "T", "country": "Vietnam", "duration": "1 day", "itineraries": "Day 1 — x"}
    fake_nodes["release_in_validate"] = True
    with pytest.raises(asyncio.CancelledError):   # released right after the writer node
        await _rewrite_tour(tour, idx=0, total=1, on_writer_state=on_writer_state)
    assert fake_nodes["generate"] == 1 and len(saved) == 1
    assert saved[0]["writer_version"] == "v2" and saved[0]["cost_usd"] == 0.25
    assert "tour" not in saved[0]   # only what the writer changed

    out = await _rewrite_tour(tour, idx=0, total=1, resume_state=saved[0], on_writer_state=on_writer_state)
    assert fake_nodes["generate"] == 1, "the resumed graph called the writer again"
    assert fake_nodes["validate"] == 2
    assert out["status"] == "success" and out["generated"]["name"] == "Draft"
    assert out["writer_version"] == "v2" and out["cost_usd"] == 0.25   # the draft keeps its stamp and cost
    assert len(saved) == 1   # no writer node ran, so nothing new to save


@pytest.mark.asyncio
async def test_resume_state_is_ignored_for_a_tenant_or_batch_rewrite(fake_nodes):
    from api.routers.v1_pipeline import _rewrite_tour
    tour = {"name": "T", "country": "Vietnam", "duration": "1 day", "itineraries": "Day 1 — x"}
    resume = {"generated": {"name": "Old"}, "writer_version": "v2"}
    await _rewrite_tour(tour, idx=0, total=1, is_tenant_rewrite=True, tenant_id=TOUR_ID, resume_state=resume)
    assert fake_nodes["generate"] == 1   # T2 is not resumable: the normal graph ran its writer


@pytest.mark.asyncio
async def test_a_value_the_writer_mutates_in_place_is_still_saved(fake_nodes, monkeypatch):
    """A node that appends to a list of the incoming state changes the initial state's list too; the delta is taken
    against a deep copy, so the appended entry is still saved and restored."""
    from api.routers.v1_pipeline import _rewrite_tour
    from services.content_generation import graph as g
    real_generate = g.generate_node

    def generate_mutating(state):
        state["degraded"].append({"path": "writer_fallback"})
        return real_generate(state)

    monkeypatch.setattr(g, "generate_node", generate_mutating)
    saved = []

    async def on_writer_state(delta):
        saved.append(delta)

    await _rewrite_tour({"name": "T", "itineraries": "Day 1 — x"}, idx=0, total=1, on_writer_state=on_writer_state)
    assert saved[0]["degraded"] == [{"path": "writer_fallback"}]
