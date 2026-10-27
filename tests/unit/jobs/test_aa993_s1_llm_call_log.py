"""AA-993 follow-up — every billed S1 call of an `s1_rewrite` job writes its shared.llm_call_log row, with the job id.

Live (Dev, 27/10): two s1_rewrite jobs enqueued through POST /admin/run-tour-async with `"tenant_id": "aa_internal"`
(the slug the endpoint has always accepted) ran writer v2 and wrote a new version, but no s1_generate / s1_judge /
s1_grounding / s1_flag_fix row reached llm_call_log. The worker bound the payload's tenant (`job_tenant_id`) as-is, so
every record_call*() below the handler sent `'aa_internal'` as `$1::uuid`; asyncpg refused the argument and
record_call swallowed the error (a log write never fails the call it records). The S1 page and the Regenerate modal
send the platform UUID, which is why their runs logged.

These tests drive the real path — Worker._execute → s1_rewrite_job.run → _execute_run_tour → _rewrite_tour → the
LangGraph graph — with LLM-free nodes that log exactly like the real ones (record_call_sync, tenant from the graph
state) and an llm_call_log connection that checks the `::uuid` arguments the way asyncpg does. No network, no LLM.
"""
import asyncio
import json
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from services.jobs import s1_rewrite_job  # noqa: F401 — registers the s1_rewrite kind
from shared.jobs import queue
from shared.jobs.worker import Worker, job_tenant_id
from shared.llm_client import call_log
from tests.unit.jobs.test_aa993_g2_s1_resume import BATCH_ID, TOUR_ID, FakeDB, FakeRewrite, _patches

LOG_URL = "postgresql://llm-call-log/test"
PLATFORM = call_log.PLATFORM_TENANT_ID

# _INSERT_SQL positions (0-based) of the `::uuid` arguments: tenant_id, content_piece_id, angle_gate_request_id, job_id.
_UUID_ARGS = {0: "tenant_id", 8: "content_piece_id", 9: "angle_gate_request_id", 14: "job_id"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")


class LogConn:
    """The llm_call_log connection. asyncpg encodes a `$n::uuid` argument client-side and raises DataError on a
    string that is not a UUID; this fake applies the same rule, so a bad tenant loses the row as it did live."""

    def __init__(self, rows: list):
        self.rows = rows

    async def execute(self, sql, *args):
        assert "INSERT INTO shared.llm_call_log" in sql
        for pos, name in _UUID_ARGS.items():
            if args[pos] is not None:
                try:
                    uuid.UUID(str(args[pos]))
                except ValueError:
                    raise asyncpg.exceptions.DataError(
                        f"invalid input for query argument ${pos + 1}: {args[pos]!r} (invalid UUID) [{name}]")
        self.rows.append({"tenant_id": args[0], "stage": args[1], "role": args[2], "cost_usd": args[6],
                          "job_id": args[14]})

    async def close(self):
        pass


@pytest.fixture
def llm_rows():
    rows: list = []
    yield rows


@pytest.fixture
def logging_nodes(monkeypatch):
    """LLM-free graph nodes. The writer and the judge make a "billed" fake call and log it the way the real
    generate_node / judge_node do (record_call_sync, tenant_id from the graph state: None for an A1 rewrite).
    `release_in_validate` = raise CancelledError once in validate (a deploy releases the job after the writer)."""
    from services.content_generation import graph as g
    from shared.llm_client.call_log import record_call_sync
    calls = {"generate": 0, "judge": 0, "release_in_validate": False}

    def generate(state):
        calls["generate"] += 1
        record_call_sync(stage=state.get("generate_stage") or "s1_generate", role="writer", model="sonnet-5-5",
                         tokens_in=100, tokens_out=50, cost_usd=0.25, tenant_id=state.get("tenant_id"),
                         quality_signal={"json_parsed": True})
        return {**state, "generated": {"name": "Draft", "summary": "s", "itineraries": "Day 1 — x"},
                "model_used": "sonnet", "cost_usd": state.get("cost_usd", 0.0) + 0.25, "writer_version": "v2"}

    def validate(state):
        if calls["release_in_validate"]:
            calls["release_in_validate"] = False
            raise asyncio.CancelledError()
        return {**state, "quality_score": 8.0, "failure_codes": []}

    def judge(state):
        calls["judge"] += 1
        record_call_sync(stage="s1_judge", role="judge", model="haiku-4-5", tokens_in=80, tokens_out=20,
                         cost_usd=0.01, tenant_id=state.get("tenant_id"), quality_signal={"score": 8.0})
        return {**state}

    def passthrough(state):
        return {**state}

    monkeypatch.setattr(g, "generate_node", generate)
    monkeypatch.setattr(g, "validate_node", validate)
    monkeypatch.setattr(g, "judge_node", judge)
    for name in ("brand_audit_node", "grounding_node", "flag_fix_node", "revalidate_node"):
        monkeypatch.setattr(g, name, passthrough)
    return calls


def _job(job_id: str, tenant_id, progress: dict) -> queue.Job:
    return queue.Job(id=job_id, kind="s1_rewrite",
                     payload={"tour_id": TOUR_ID, "batch_id": BATCH_ID, "tenant_id": tenant_id},
                     status="running", attempt=1, max_attempts=1, progress=dict(progress), cost_usd=0.0)


class _Pool:
    async def fetchval(self, *a, **k):   # Worker._llm_cost
        return 0.0


def _worker_patches(db: FakeDB, rows: list, progress_store: dict) -> ExitStack:
    """The G2 harness (fake generated_content / quality_scores / review rows, real `_rewrite_tour` + graph) plus the
    worker's queue writes and the llm_call_log connection."""
    from api.routers.v1_pipeline import _rewrite_tour
    stack = _patches(db, _rewrite_tour)

    def connect(url=None, *a, **k):   # one asyncpg module: dispatch the llm_call_log writes by URL
        return LogConn(rows) if url == LOG_URL else db.conn()

    stack.enter_context(patch("asyncpg.connect", AsyncMock(side_effect=connect)))
    stack.enter_context(patch("shared.llm_client.call_log.get_database_url", return_value=LOG_URL))

    async def set_progress(pool, job_id, fields, cost_usd=None):
        progress_store.update(json.loads(json.dumps(fields)))

    stack.enter_context(patch.object(queue, "set_progress", set_progress))
    stack.enter_context(patch.object(queue, "complete", AsyncMock()))
    stack.enter_context(patch.object(queue, "fail", AsyncMock(return_value="failed")))
    stack.enter_context(patch.object(queue, "release", AsyncMock(return_value="queued")))
    stack.enter_context(patch.object(queue, "heartbeat", AsyncMock(return_value=(True, False))))
    stack.enter_context(patch.object(queue, "newer_worker_alive", AsyncMock(return_value=True)))
    stack.enter_context(patch("shared.jobs.worker.run_terminal_hook", AsyncMock()))
    return stack


async def _run(worker: Worker, job: queue.Job) -> None:
    await worker._execute(job)
    call_log.wait_for_pending_writes()


# ── the live case: payload tenant "aa_internal" ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_id", ["aa_internal", PLATFORM], ids=["slug", "platform-uuid"])
async def test_s1_rewrite_job_logs_writer_and_judge_rows_with_the_job_id(logging_nodes, llm_rows, tenant_id):
    db, progress_store, job_id = FakeDB(), {}, str(uuid.uuid4())
    with _worker_patches(db, llm_rows, progress_store):
        await _run(Worker(_Pool(), worker_id="w-test"), _job(job_id, tenant_id, progress_store))

    assert progress_store["phase"] == "done" and len(db.versions) == 1
    stages = sorted(r["stage"] for r in llm_rows)
    assert stages == ["s1_generate", "s1_judge"], f"llm_call_log rows: {llm_rows}"
    assert all(r["job_id"] == job_id for r in llm_rows)
    assert all(r["tenant_id"] is None for r in llm_rows), "platform rows keep tenant_id NULL"


@pytest.mark.asyncio
async def test_resumed_graph_after_the_writer_still_logs_with_the_job_id(logging_nodes, llm_rows):
    """A release right after the writer (G2 writer_done), then the re-claimed job: build_graph_resumed() makes no
    second writer call, and the judge it runs still writes its row with the job id."""
    db, progress_store, job_id = FakeDB(), {}, str(uuid.uuid4())
    logging_nodes["release_in_validate"] = True
    with _worker_patches(db, llm_rows, progress_store):
        with pytest.raises(asyncio.CancelledError):
            await _run(Worker(_Pool(), worker_id="w-old"), _job(job_id, "aa_internal", progress_store))
        assert progress_store["persist_stage"] == "writer_done"
        assert [r["stage"] for r in llm_rows] == ["s1_generate"]

        await _run(Worker(_Pool(), worker_id="w-new"), _job(job_id, "aa_internal", progress_store))

    assert logging_nodes["generate"] == 1, "the resumed job paid the writer again"
    assert logging_nodes["judge"] == 1
    assert sorted(r["stage"] for r in llm_rows) == ["s1_generate", "s1_judge"]
    assert all(r["job_id"] == job_id for r in llm_rows)
    assert progress_store["phase"] == "done" and len(db.versions) == 1


@pytest.mark.asyncio
async def test_harness_runs_without_llm_rows_when_no_node_is_billed(llm_rows):
    """Guard for the harness itself: with the G2 FakeRewrite (no graph, no billed call) the job completes through the
    worker and no llm_call_log row appears, so the rows above come from the graph nodes alone."""
    db, progress_store, job_id = FakeDB(), {}, str(uuid.uuid4())
    with _worker_patches(db, llm_rows, progress_store), \
            patch("api.routers.admin_pipeline._rewrite_tour", FakeRewrite()):
        await _run(Worker(_Pool(), worker_id="w-test"), _job(job_id, PLATFORM, progress_store))
    assert progress_store["phase"] == "done" and llm_rows == []


# ── the two seams the fix touches ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tid", ["aa_internal", "AA_INTERNAL", " aa_internal "])
def test_job_tenant_id_treats_the_platform_slug_as_platform(tid):
    assert job_tenant_id({"tenant_id": tid, "tour_id": "t"}) is None


def test_job_tenant_id_drops_a_value_that_is_not_a_tenant_uuid():
    assert job_tenant_id({"tenant_id": "not-a-uuid"}) is None
    real = "11111111-2222-4333-8444-555555555555"
    assert job_tenant_id({"tenant_id": real.upper()}) == real


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["aa_internal", "tenant-abc"])
async def test_record_call_keeps_the_row_when_the_tenant_is_not_a_uuid(llm_rows, bad):
    """Defence in depth: a call that is already billed writes its row with tenant_id NULL instead of losing it."""
    with patch("shared.llm_client.call_log.asyncpg.connect", AsyncMock(return_value=LogConn(llm_rows))), \
            patch("shared.llm_client.call_log.get_database_url", return_value=LOG_URL):
        with call_log.bind_tenant(bad):
            await call_log.record_call(stage="s1_judge", role="judge", model="haiku-4-5", tokens_in=1, tokens_out=1,
                                       cost_usd=0.01, quality_signal={})
        await call_log.record_call(stage="s1_judge", role="judge", model="haiku-4-5", tokens_in=1, tokens_out=1,
                                   cost_usd=0.01, quality_signal={}, tenant_id=bad)
    assert [r["tenant_id"] for r in llm_rows] == [None, None]


@pytest.mark.asyncio
async def test_record_calls_and_pool_path_keep_the_row_when_the_tenant_is_not_a_uuid(llm_rows):
    conn = LogConn(llm_rows)

    class _Acq:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    class _P:
        def acquire(self):
            return _Acq()

    with patch("shared.llm_client.call_log.asyncpg.connect", AsyncMock(return_value=conn)), \
            patch("shared.llm_client.call_log.get_database_url", return_value=LOG_URL):
        with call_log.bind_tenant("aa_internal"):
            await call_log.record_call_with_pool(_P(), stage="t5", role="judge", model="m", tokens_in=1,
                                                 tokens_out=1, cost_usd=0.0, quality_signal={})
    assert [r["tenant_id"] for r in llm_rows] == [None]
    args = call_log._insert_args({"stage": "s", "role": "judge", "tenant_id": "aa_internal"})
    assert args[0] is None
