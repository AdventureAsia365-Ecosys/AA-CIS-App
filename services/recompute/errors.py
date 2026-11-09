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
"""
from __future__ import annotations

BAD_PAYLOAD = "recompute_bad_payload"
LOCK_TIMEOUT = "recompute_lock_timeout"
STOPPED = "recompute_stopped"
STAGE_FAILED = "recompute_stage_failed"
ERROR_CODES = (BAD_PAYLOAD, LOCK_TIMEOUT, STOPPED, STAGE_FAILED)


class RecomputeError(RuntimeError):
    """A recompute failure with a fixed code (`code`) at the start of its message."""

    code = "recompute_failed"

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class RecomputeStopped(RecomputeError):
    code = STOPPED


class RecomputeLockTimeout(RecomputeError):
    code = LOCK_TIMEOUT


class RecomputeStageFailed(RecomputeError):
    code = STAGE_FAILED

    def __init__(self, stage: str | None, detail: str = "") -> None:
        self.stage = stage
        super().__init__(f"stage {stage or 'none'}: {detail}" if detail else f"stage {stage or 'none'}")
