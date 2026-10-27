"""AA-993 G2 (ADR 0016 D1) — resumable persist stages of one `s1_rewrite` job.

A deploy releases a running job on SIGTERM (`shared/jobs/worker.py`, G1) and a newer worker re-claims it. Before G2
the job only recorded `version_id` at the very end, so a release after the `generated_content` INSERT either wrote a
second version or (had it been checkpointed earlier) skipped the score / review / fix-pass steps. This object records
how far the run got in `shared.job.progress`, and `_execute_run_tour` resumes from there:

    stage              progress keys written            a resumed run ...
    writer_done        s1_writer_state                  re-enters the graph after the writer (no new writer call)
    rewritten          s1_rewrite_result,               skips the whole rewrite (writer, judge, fix pass, upgrade)
                       s1_auto_upgraded, s1_dataforseo_used
    content_inserted   version_id                       reuses the version (no second generated_content row)
    scored             -                                skips the quality_scores insert + brand audit update
    fix_pass_saved     -                                skips the fix-pass update
    routed             -                                skips the publish (process_export) / review queue entry
    accounted          -                                skips the pipeline_runs cost update

Every step after the INSERT is also idempotent on its own (score insert guarded by NOT EXISTS, `_enqueue_review`
guarded by NOT EXISTS, the UPDATEs write the same values), and a run resumed from `rewritten` finds its INSERT again
through the `metadata.s1_job_id` marker when the release fell between the INSERT and the `content_inserted` write.
The job ends with `phase = "done"` (the only early return, `services/jobs/s1_rewrite_job.py`) and clears the bulky
keys. The stage never moves back (a resumed run passing an earlier step again writes nothing).

Writes never raise: a failed checkpoint write is logged and the run goes on (the checkpoint saves money on a resume;
it must not fail a run that is otherwise fine). The one case that still makes a second version: the `rewritten` write
is lost (or the result is not plain JSON) and the job is then released after its INSERT; the resumed run has no result
to persist, so it rewrites and inserts again, as before G2 (logged `s1_checkpoint_result_missing` /
`s1_checkpoint_result_not_json`).
"""
from __future__ import annotations

import copy
import json
from typing import Any, Awaitable, Callable, Optional

import structlog

logger = structlog.get_logger()

STAGES = ("writer_done", "rewritten", "content_inserted", "scored", "fix_pass_saved", "routed", "accounted")

# progress keys holding a large payload; the job sets them to null when it ends (phase = "done")
BULKY_KEYS = ("s1_writer_state", "s1_rewrite_result")

# metadata key on silver_aa_internal.generated_content that ties a version to the s1_rewrite job that wrote it
JOB_MARKER_KEY = "s1_job_id"


def json_roundtrip(value: Any) -> Optional[Any]:
    """`value` as it reads back from jsonb, or None when it is not plain JSON (no `default=str`: a set or an object
    turned into a string would change what a resumed run decides on, so such a value is not checkpointed)."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return None


def state_baseline(initial_state: dict) -> dict:
    """A copy of the graph's initial state to diff writer nodes against: deep, so a node that mutates a nested value in
    place (appends to a list of the state) still shows it as changed; shallow if something in it cannot be copied."""
    try:
        return copy.deepcopy(initial_state)
    except Exception:  # noqa: BLE001
        return dict(initial_state)


def writer_state_delta(initial_state: dict, node_output: dict) -> Optional[dict]:
    """The graph-state keys a writer node changed (the draft, its cost/model/stamp, retry_count, feedback, ...): what
    a resumed run merges back before `validate`. None when there is no draft or a changed value is not plain JSON."""
    delta = {k: v for k, v in node_output.items()
             if k not in initial_state or initial_state.get(k) != v}
    delta.pop("resume_state", None)
    if not delta.get("generated"):
        return None
    safe = json_roundtrip(delta)
    if safe != delta:
        return None
    return safe


class S1Checkpoint:
    """How far one s1_rewrite job got, read from and written to its `shared.job.progress`."""

    def __init__(self, job_id: str, progress: Optional[dict],
                 save: Callable[[dict], Awaitable[None]]):
        p = progress or {}
        self.job_id = str(job_id)
        stage = p.get("persist_stage")
        self.stage: Optional[str] = stage if stage in STAGES else None
        self.resumed_from: Optional[str] = self.stage
        self.writer_state: Optional[dict] = p.get("s1_writer_state") if isinstance(
            p.get("s1_writer_state"), dict) else None
        self.result: Optional[dict] = p.get("s1_rewrite_result") if isinstance(
            p.get("s1_rewrite_result"), dict) else None
        self.auto_upgraded: bool = bool(p.get("s1_auto_upgraded"))
        self.dataforseo_used: bool = bool(p.get("s1_dataforseo_used"))
        if self.reached("rewritten") and self.result is None:
            # a stage past the rewrite without its result (its write failed): nothing to run the persist steps on,
            # so the run starts over and writes a new version (logged; the pre-G2 behaviour for this rare case)
            logger.warning("s1_checkpoint_result_missing", job_id=self.job_id, stage=self.stage)
            self.stage = None
            self.resumed_from = None
        self.version_id: Optional[str] = p.get("version_id") if self.reached("content_inserted") else None
        self._save = save
        self._stopped = False   # the rewrite could not be checkpointed: later stages are not recorded either

    def reached(self, stage: str) -> bool:
        return self.stage is not None and STAGES.index(self.stage) >= STAGES.index(stage)

    @property
    def resume_writer_state(self) -> Optional[dict]:
        """The saved graph state to re-enter after the writer, only while the rewrite itself has not finished."""
        return self.writer_state if self.stage == "writer_done" else None

    async def mark(self, stage: str, **fields: Any) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown s1 persist stage {stage!r}")
        if self._stopped:
            return
        if self.stage is not None and STAGES.index(self.stage) > STAGES.index(stage):
            return   # a resumed run passing an earlier step again: never move the stage back
        self.stage = stage
        if "version_id" in fields:
            self.version_id = fields["version_id"]
        try:
            await self._save({"persist_stage": stage, **fields})
        except Exception as e:  # noqa: BLE001 — never fail a run over a checkpoint write
            logger.warning("s1_checkpoint_write_failed", job_id=self.job_id, stage=stage, error=str(e)[:200])

    async def save_writer_state(self, state: dict) -> None:
        """Called by `_rewrite_tour` after each writer node of the primary rewrite (`on_writer_state`)."""
        self.writer_state = state
        await self.mark("writer_done", s1_writer_state=state)

    async def save_rewrite(self, result: dict, *, auto_upgraded: bool, dataforseo_used: bool) -> None:
        safe = json_roundtrip(result)
        if safe is None or safe != result:
            # not plain JSON (a set, a tuple, an object): a resumed run would decide on different values, so the
            # rewrite is not checkpointed (a resume re-enters after the writer instead, if that state was saved)
            logger.warning("s1_checkpoint_result_not_json", job_id=self.job_id)
            self._stopped = True   # a stage past `rewritten` without its result could not be resumed from
            return
        self.result, self.auto_upgraded, self.dataforseo_used = safe, auto_upgraded, dataforseo_used
        self.writer_state = None
        await self.mark("rewritten", s1_rewrite_result=safe, s1_auto_upgraded=auto_upgraded,
                        s1_dataforseo_used=dataforseo_used, s1_writer_state=None)


async def mark(checkpoint: Optional[S1Checkpoint], stage: str, **fields: Any) -> None:
    """`checkpoint.mark` that does nothing without a checkpoint (the sync /admin/run-tour and batch paths)."""
    if checkpoint is not None:
        await checkpoint.mark(stage, **fields)


def done(checkpoint: Optional[S1Checkpoint], stage: str) -> bool:
    """True when a resumed run already finished `stage` (always False without a checkpoint)."""
    return checkpoint is not None and checkpoint.reached(stage)
