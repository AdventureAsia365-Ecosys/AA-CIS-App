"""S282 Sentry triage (AA-CIS-API-19) — a busy platform recompute lock reschedules the job instead of waiting.

The job used to wait in process up to 1,800 s for the lock, holding a worker slot and the one running recompute, and
then failed while the holder (an earlier attempt's thread, still working) was alive. Now it takes the lock at the first
try or ends the attempt with registry.Reschedule (the attempt not counted, due again after the release delay). The
conversion happens in run(), outside the thread: a Reschedule raised inside it would be wrapped as a stage failure.
Only an attempt that keeps finding the lock busy for LOCK_MAX_WAIT_SECONDS gives up (recompute_lock_timeout).
No DB: the in-memory lock table of the AA-1056 tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from services.jobs import recompute_job as rc
from services.recompute.errors import LOCK_BUSY, RecomputeLockTimeout, RecomputeStageFailed
from shared.jobs import registry
from shared.jobs.worker import failure_outcome
from tests.unit.jobs.test_aa1056_recompute_lock_and_sweep import _ctx, _job_pool, locks  # noqa: F401 (fixture)

NOW = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_a_busy_lock_reschedules_without_running_or_waiting(locks):  # noqa: F811
    plat = AsyncMock()
    async with rc.recompute_lock():
        with patch("services.export.handler.recompute_rankings_and_routes", plat), \
                patch("services.export.handler.open_job_pool", AsyncMock(side_effect=lambda: _job_pool())):
            with pytest.raises(registry.Reschedule) as ei:
                await rc.run(_ctx({"scope": "platform", "reason": "r"}))
    plat.assert_not_awaited()
    e = ei.value
    assert not isinstance(e, RecomputeStageFailed)
    assert e.reason == LOCK_BUSY and e.delay_seconds == rc.RELEASE_DELAY_SECONDS
    assert e.progress["phase"] == "lock_busy" and e.progress["lock_busy"]["attempt"] == 1
    assert not locks.held


def test_the_busy_window_is_kept_across_reschedules_of_one_attempt():
    ctx = _ctx({"scope": "platform"})
    first = rc.lock_busy_outcome(ctx, now=NOW)
    assert isinstance(first, registry.Reschedule)
    ctx.job.progress = dict(first.progress)
    later = rc.lock_busy_outcome(ctx, now=NOW + timedelta(seconds=600))
    assert isinstance(later, registry.Reschedule)
    assert later.progress["lock_busy"]["since"] == NOW.isoformat()
    stuck = rc.lock_busy_outcome(ctx, now=NOW + timedelta(seconds=rc.LOCK_MAX_WAIT_SECONDS))
    assert isinstance(stuck, RecomputeLockTimeout)
    error, retryable, _reason = failure_outcome(stuck)
    assert retryable and error.startswith("RecomputeLockTimeout: recompute_lock_timeout: ")


def test_a_new_attempt_starts_its_own_window():
    ctx = _ctx({"scope": "platform"})
    old = (NOW - timedelta(seconds=rc.LOCK_MAX_WAIT_SECONDS * 2)).isoformat()
    ctx.job.progress = {"lock_busy": {"attempt": 1, "since": old}}
    ctx.job.attempt = 2
    out = rc.lock_busy_outcome(ctx, now=NOW)
    assert isinstance(out, registry.Reschedule) and out.progress["lock_busy"] == {"attempt": 2,
                                                                                  "since": NOW.isoformat()}


@pytest.mark.parametrize("bad", ["not a time", None, 7])
def test_a_garbled_busy_mark_restarts_the_window(bad):
    ctx = _ctx({"scope": "platform"})
    ctx.job.progress = {"lock_busy": {"attempt": 1, "since": bad}}
    out = rc.lock_busy_outcome(ctx, now=NOW)
    assert isinstance(out, registry.Reschedule) and out.progress["lock_busy"]["since"] == NOW.isoformat()
