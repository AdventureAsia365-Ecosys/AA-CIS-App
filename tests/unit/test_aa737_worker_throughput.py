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
