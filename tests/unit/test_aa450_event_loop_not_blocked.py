"""
tests/unit/test_aa450_event_loop_not_blocked.py — AA-450: the T9+T10-inline write/check loop must
NOT block the shared ECS-task event loop, built in from the start (not patched in after an
incident the way N7's AA-416 fix was — see docs/claude_audit/
AA-450-01-t9-t10-retry-loop-investigation.md).

Every blocking LLM call inside services/acp_content_writing/service.py::run_write_background()
(the write call, and quality_gates.py's 2 judge calls) is wrapped in `asyncio.to_thread()` from
this module's first version. AA-466 split the old single write_and_check() into a fast
pre-flight (start_write()) and this background loop (run_write_background()) — this test now
targets the background loop directly (that's the only part with any real blocking-risk LLM
call; start_write() is DB-only, never at risk of blocking the loop for any meaningful time).
Reproduces AA-416's own test shape exactly (a slow synchronous call standing in for a real
invoke_claude()/invoke_judge() call, run concurrently with a cheap health-check coroutine on the
same event loop) against the REAL run_write_background() call path, not just a synthetic
stand-in for run_gates() the way AA-416's test used.
"""
import asyncio
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import service

REQUEST_ID = uuid.uuid4()
PIECE_ID = uuid.uuid4()

GOAL = {"key": "promotion", "name": "Promotion", "description": "d", "logic": "AIDA", "marketing_term": "AIDA"}
ANGLE = {"idx": 0, "name": "A", "why_it_works": "wa", "formula_fit": "AIDA",
         "best_final_style": "warm", "recommended": True, "chosen": True}

# Same order of magnitude as AA-416's own test (standing in for the real ~13.8s measured
# invoke_claude() latency, shortened for test speed).
_SIMULATED_BEDROCK_LATENCY_SECONDS = 0.5
_HEALTH_CHECK_MAX_LATENCY_SECONDS = 0.05


def _context():
    return {
        "atom_text": "atom text", "goal": GOAL, "channel_style": {"key": "facebook"},
        "brand_audience": {}, "chosen": ANGLE, "cta": "Book a consultation",
        "destination": None, "trip_name": None, "brand_rubric_text": "rubric",
        "channel": "facebook", "atom_id": "atom_abc123",
    }


def _slow_sync_write(*args, **kwargs) -> tuple[str, float, dict, None]:
    """Stands in for the real generate.py::write_content() -> LLMClient.generate() ->
    boto3 invoke_model_with_response_stream(), a synchronous, blocking HTTP call."""
    time.sleep(_SIMULATED_BEDROCK_LATENCY_SECONDS)
    return "Final piece text.", 0.02, {}, None  # S234 round 4: a lower-case fragment would be held


def _fast_passing_gates(*args, **kwargs):
    return {"passed": True, "gate_ledger": [], "first_failure": None, "flags": []}


async def _fake_health_check() -> dict:
    return {"status": "ok"}


@pytest.mark.asyncio
async def test_run_write_background_does_not_block_concurrent_health_check():
    health_latencies: list[float] = []
    stop = asyncio.Event()

    async def _poll_health():
        while not stop.is_set():
            t0 = time.monotonic()
            await _fake_health_check()
            health_latencies.append(time.monotonic() - t0)
            await asyncio.sleep(0.01)

    poller = asyncio.create_task(_poll_health())

    finalized: dict = {}

    async def _capture_finalize(pool, **kwargs):
        finalized.update(kwargs)
        return None

    with patch.object(service, "write_content", side_effect=_slow_sync_write), \
         patch.object(service, "run_quality_gates", side_effect=_fast_passing_gates), \
         patch.object(service, "_finalize_piece", new=AsyncMock(side_effect=_capture_finalize)):
        t_start = time.monotonic()
        await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())
        elapsed = time.monotonic() - t_start

    stop.set()
    await poller

    assert finalized["status"] == "approved"
    assert elapsed >= _SIMULATED_BEDROCK_LATENCY_SECONDS  # the slow write really did run
    assert health_latencies, "health poller never got a chance to run concurrently"
    assert max(health_latencies) < _HEALTH_CHECK_MAX_LATENCY_SECONDS, (
        f"health check latency spiked to {max(health_latencies):.3f}s while run_write_background() "
        f"was writing — the event loop was blocked, the built-in-from-the-start asyncio.to_thread "
        f"wrapping is missing or broken"
    )


@pytest.mark.asyncio
async def test_bare_sync_write_call_WOULD_block_health_check_negative_control():
    """Negative control, same shape as AA-416's own — proves the positive test above is
    exercising a real mechanism, not a no-op that would pass regardless."""
    t_test_start = time.monotonic()
    time_health_check_ran: list[float] = []

    async def _poll_health_once_after_yield():
        await asyncio.sleep(0)
        await _fake_health_check()
        time_health_check_ran.append(time.monotonic() - t_test_start)

    poller = asyncio.ensure_future(_poll_health_once_after_yield())
    # Bare call, no await/to_thread — the pre-fix shape this test proves would have blocked.
    _slow_sync_write()
    await poller

    assert time_health_check_ran[0] >= _SIMULATED_BEDROCK_LATENCY_SECONDS * 0.9, (
        "expected the bare (unfixed) call shape to starve the concurrent health-check coroutine "
        "of any chance to run until the sleep had passed — if this fails, the negative control "
        "itself is broken and the positive test above proves nothing"
    )
