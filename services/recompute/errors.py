"""AA-1056 — the fixed error codes of the platform recompute (the `recompute` job kind).

Each failure the job can end with names one code, at the start of the exception message, so the job's `error` text
(the worker writes `<ExceptionName>: <message>`) always contains it and a Jobs-page search or a log query finds every
row of one cause:

- ``recompute_bad_payload``  — the payload cannot run (raised as the runner's NonRetryable: no retry).
- ``recompute_lock_timeout`` — another recompute held the platform recompute lock for longer than the kind's expected
  time (1,800 s): a stuck run. Retryable.
- ``recompute_stopped``      — the job was cancelled (deploy release, admin cancel, lost lease) and its thread stopped
  at the next stage boundary. The handler is released, not failed, so this code shows in the logs only.
- ``recompute_stage_failed`` — a stage raised; the message names the stage and the exception class. Retryable.

Not a failure (AA-1083): ``recompute_jev_cap`` starts the error of a run HELD because its Jev spend estimate is above
the per-run cap. The job ends ``stopped_budget`` (the runner's budget stop, cost_guard.BudgetExceeded), waiting for staff
to release it (services/recompute/jev_scope.py); it is not in ERROR_CODES.
"""
from __future__ import annotations

BAD_PAYLOAD = "recompute_bad_payload"
LOCK_TIMEOUT = "recompute_lock_timeout"
STOPPED = "recompute_stopped"
STAGE_FAILED = "recompute_stage_failed"
ERROR_CODES = (BAD_PAYLOAD, LOCK_TIMEOUT, STOPPED, STAGE_FAILED)
JEV_CAP_HELD = "recompute_jev_cap"   # AA-1083: a held run (stopped_budget), not a failure


class RecomputeError(RuntimeError):
    """A recompute failure with a fixed code (`code`) at the start of its message."""

    code = "recompute_failed"

    def __init__(self, detail: str = "") -> None:
        # Idempotent, so a copy / unpickle (which passes the full message back) keeps one prefix.
        if not detail:
            detail = self.code
        elif not str(detail).startswith(self.code):
            detail = f"{self.code}: {detail}"
        super().__init__(detail)


class RecomputeStopped(RecomputeError):
    code = STOPPED


class RecomputeLockTimeout(RecomputeError):
    code = LOCK_TIMEOUT


class RecomputeStageFailed(RecomputeError):
    code = STAGE_FAILED
    stage: str | None = None

    @classmethod
    def for_stage(cls, stage: str | None, detail: str = "") -> "RecomputeStageFailed":
        err = cls(f"stage {stage or 'none'}: {detail}" if detail else f"stage {stage or 'none'}")
        err.stage = stage
        return err
