"""AA-737 — worker slot budget leaves room for a3_atomize while s1_rewrite runs at its cap."""
import pytest

from shared.jobs import worker
from shared.jobs.registry import kinds
from shared.jobs.worker import WORKER_MAX_PARALLEL_DEFAULT, load_kinds, worker_max_parallel


def test_default_slots(monkeypatch):
    monkeypatch.delenv("JOB_WORKER_MAX_PARALLEL", raising=False)
    assert worker_max_parallel() == WORKER_MAX_PARALLEL_DEFAULT == 8


@pytest.mark.parametrize("raw,expected", [("12", 12), ("0", 1), ("-3", 1), ("abc", 8)])
def test_env_override(monkeypatch, raw, expected):
    monkeypatch.setenv("JOB_WORKER_MAX_PARALLEL", raw)
    assert worker_max_parallel() == expected


def test_s1_rewrite_at_cap_still_leaves_a_slot_for_atomize():
    # S217: s1_rewrite filled every slot and starved a3_atomize for ~1h. With the default budget,
    # s1_rewrite at its cap plus a3_atomize at its cap must fit, with one slot to spare.
    load_kinds()
    caps = {name: k.concurrency for name, k in kinds().items()}
    assert caps["a3_atomize"] == 1  # deliberate cap (platform-wide recompute + Cohere pacing)
    assert caps["s1_rewrite"] + caps["a3_atomize"] < WORKER_MAX_PARALLEL_DEFAULT


def test_constructor_default_unchanged():
    # The in-API / test worker keeps its own default; only the standalone service entrypoint uses
    # worker_max_parallel().
    from unittest.mock import MagicMock
    assert worker.Worker(MagicMock()).max_parallel == 4


def test_a3_atomize_keeps_a_slot_with_every_other_kind_at_its_cap():
    # AA-712: claim() is FIFO across kinds. With every NON-background kind except a3_atomize running
    # up to its cap (bounded by the slots), the place kinds (background) must not take the slot left
    # for atomize: background kinds are only claimed with more than BACKGROUND_RESERVE free slots.
    from unittest.mock import MagicMock
    load_kinds()
    w = worker.Worker(MagicMock(), max_parallel=WORKER_MAX_PARALLEL_DEFAULT)
    # the S217 shape: s1_rewrite at its cap; the slots left are what atomize + others share
    free = WORKER_MAX_PARALLEL_DEFAULT - kinds()["s1_rewrite"].concurrency
    assert free <= w.background_reserve()                    # 8 - 6 = 2 free → only reserved slots
    caps = w.claim_caps(free)
    assert "a3_atomize" in caps
    assert not {n for n, k in kinds().items() if k.background} & set(caps)


@pytest.mark.parametrize("free", range(0, WORKER_MAX_PARALLEL_DEFAULT + 1))
def test_background_kind_never_claimed_with_two_or_fewer_free_slots(free):
    from unittest.mock import MagicMock
    load_kinds()
    w = worker.Worker(MagicMock(), max_parallel=WORKER_MAX_PARALLEL_DEFAULT)
    background = {n for n, k in kinds().items() if k.background}
    assert background == {"place_extract", "place_geocode", "tour_graph_build", "place_shape", "place_dedupe",
                          "place_relocate",   # AA-997
                          # AA-825 retention kinds (data standards C5.4d)
                          "retention_llm_call_log", "retention_tenant_api_usage", "retention_job",
                          "retention_decision_log", "retention_cost_explorer_snapshot",
                          # AA-816 C7 staff directory reconcile (report only, daily)
                          "staff_directory_reconcile",
                          # AA-839 data erasure (DRY RUN by default, manual only)
                          "data_erasure",
                          # AA-835 supply content LLM draft (one per Booking facts version, capped per day)
                          "supply_content_draft",
                          # AA-835 removal of the CIS copies of a withdrawn Booking supply photo
                          "supply_media_purge",
                          # AA-995 960 w rendition of the stored catalog photos ($0, manual, bounded)
                          "photo_medium_backfill",
                          # AA-671 S257 photo -> destination of its tour ($0, manual, DRY RUN by default)
                          "photo_destination_match",
                          # AA-917 writing memory: backfill, fact filter, mining (cap 1 each); AA-918 the R0 replay
                          "writing_signal_backfill", "writing_signal_fact_filter", "writing_memory_mine",
                          "writing_memory_r0",
                          # AA-919 WM-19 / WM-21: the R2 runner and the targeted shadow (paid, cap 1 each)
                          "writing_memory_r2", "writing_memory_shadow",
                          # AA-920 WM-26: the post-activation check ($0, hourly while a release with content is active)
                          "writing_memory_post_activation",
                          # AA-1003 report core: daily KPI snapshot and report rendering ($0, DB reads only)
                          "kpi_snapshot_daily", "report_generate"}
    assert bool(background & set(w.claim_caps(free))) == (free > worker.BACKGROUND_RESERVE)


def test_place_kinds_are_cap_one():
    load_kinds()
    for kind in ("place_extract", "place_geocode", "tour_graph_build", "place_shape", "place_dedupe", "place_relocate"):
        assert kinds()[kind].concurrency == 1


@pytest.mark.asyncio
async def test_fill_slots_passes_the_reduced_caps_to_claim(monkeypatch):
    # the loop itself asks claim() without background kinds once only 2 slots are free
    from unittest.mock import AsyncMock, MagicMock
    from shared.jobs import queue
    load_kinds()
    w = worker.Worker(MagicMock(), max_parallel=4)
    w._running = {"a": MagicMock(), "b": MagicMock()}           # 2 of 4 busy → 2 free
    monkeypatch.setattr(queue, "newer_worker_alive", AsyncMock(return_value=False))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(queue, "claim", claim)
    await w._fill_slots()
    caps = claim.call_args[0][2]
    assert "a3_atomize" in caps and "place_extract" not in caps


def _simulate(w, queue_kinds, running):
    """Fill the worker FIFO from `queue_kinds` (claim order) the way _fill_slots does: each claim
    takes the first queued job whose kind is in claim_caps() and under that kind's cap."""
    queued = list(queue_kinds)
    while len(running) < w.max_parallel:
        background = {n for n, k in kinds().items() if k.background}
        caps = w.claim_caps(w.max_parallel - len(running), sum(1 for k in running if k in background))
        pick = next((i for i, k in enumerate(queued) if k in caps and running.count(k) < caps[k]), None)
        if pick is None:
            break
        running.append(queued.pop(pick))
    return running, queued


def test_background_jobs_running_before_a_wave_cannot_starve_atomize():
    # review finding 2 (AA-712): with 8 free slots before a wave, the place kinds must not each take
    # a slot; when the wave arrives and a background job ends, a3_atomize still finds a slot.
    from unittest.mock import MagicMock
    load_kinds()
    w = worker.Worker(MagicMock(), max_parallel=WORKER_MAX_PARALLEL_DEFAULT)
    running, _ = _simulate(w, ["place_extract", "place_geocode", "tour_graph_build"], [])
    assert running == ["place_extract"]                        # one background job at a time
    w._running_kinds = {"j1": "place_extract"}
    assert not {n for n, k in kinds().items() if k.background} & set(w.claim_caps(7, w.running_background()))
    # the wave arrives: s1_rewrite floods the queue ahead of atomize
    running, queued = _simulate(w, ["s1_rewrite"] * 20 + ["a3_atomize"] * 5 + ["place_geocode"], running)
    assert running.count("s1_rewrite") == 6 and running.count("a3_atomize") == 1
    assert len(running) == WORKER_MAX_PARALLEL_DEFAULT
    # the background job ends: its slot never lets a background job back in ahead of the wave
    running.remove("place_extract")
    running, queued = _simulate(w, queued, running)
    assert running.count("a3_atomize") == 1 and "place_geocode" not in running


@pytest.mark.asyncio
async def test_fill_slots_counts_running_background_jobs(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from shared.jobs import queue
    load_kinds()
    w = worker.Worker(MagicMock(), max_parallel=8)
    w._running = {"a": MagicMock()}
    w._running_kinds = {"a": "place_geocode"}                  # 7 free, but one background running
    monkeypatch.setattr(queue, "newer_worker_alive", AsyncMock(return_value=False))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(queue, "claim", claim)
    await w._fill_slots()
    caps = claim.call_args[0][2]
    assert "s1_rewrite" in caps and "place_extract" not in caps and "tour_graph_build" not in caps
