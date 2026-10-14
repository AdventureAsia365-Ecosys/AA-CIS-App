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
    assert background == {"place_extract", "place_geocode", "tour_graph_build"}
    assert bool(background & set(w.claim_caps(free))) == (free > worker.BACKGROUND_RESERVE)


def test_place_kinds_are_cap_one():
    load_kinds()
    for kind in ("place_extract", "place_geocode", "tour_graph_build"):
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
