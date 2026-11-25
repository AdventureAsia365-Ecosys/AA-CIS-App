"""AA-1178 — the monitoring lane of the CIS job runner.

The short periodic watchers (``@job_kind(..., monitor=True)``) run in slots of their own on top of the worker's main
slots, with their own running cap (setting cis.ops.jobs_monitor_max_running); the long background kinds keep the AA-712
reserve and group cap (cis.ops.jobs_background_max_running); the waves and content kinds see the same main slots as
before. The ops metrics sampler (shared/jobs/backlog.py) applies the same rule (worker.claim_caps_for). No DB."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared import settings
from shared.jobs import backlog, lanes, queue, worker
from shared.jobs.registry import _KINDS, job_kind, kinds
from shared.jobs.worker import WORKER_MAX_PARALLEL_DEFAULT, load_kinds

MONITOR_KINDS = {"ops_status_sample", "llm_route_watch", "quality_funnel_watch", "compliance_db_checks",
                 "llm_model_lifecycle_sync", "ai_usage_rollup", "llm_connection_probe",
                 "ops_external_check"}   # AA-1142 follow-up (S293): the worker's external checks
LONG = "writing_memory_r2"          # the S292 case: expected 5,400 s, background
NOW = datetime(2026, 11, 25, 3, 13, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _caps_in_force(monkeypatch):
    """The seeded defaults (CIS 489), whatever the compiled defaults of this checkout hold."""
    monkeypatch.setattr(lanes, "background_max_running", lambda: 1)
    monkeypatch.setattr(lanes, "monitor_max_running", lambda: 2)
    load_kinds()


def _worker(running_kinds=()):
    w = worker.Worker(MagicMock(), max_parallel=WORKER_MAX_PARALLEL_DEFAULT)
    for i, kind in enumerate(running_kinds):
        w._running[f"j{i}"] = MagicMock()
        w._running_kinds[f"j{i}"] = kind
    return w


def _caps(w):
    return w.claim_caps(w.free_main_slots(), w.running_background(), w.running_monitor())


def _simulate(w, queue_kinds):
    """Fill the worker FIFO from `queue_kinds` the way _fill_slots does (each claim takes the first queued job whose
    kind is in the caps and under that kind's global cap)."""
    queued = list(queue_kinds)
    while True:
        caps = _caps(w)
        running = list(w._running_kinds.values())
        pick = next((i for i, k in enumerate(queued) if k in caps and running.count(k) < caps[k]), None)
        if pick is None:
            return queued
        jid = f"s{len(w._running_kinds)}-{pick}-{len(queued)}"
        w._running[jid] = MagicMock()
        w._running_kinds[jid] = queued.pop(pick)


# ── registry ────────────────────────────────────────────────────────────────────────────────────

def test_the_monitoring_kinds_are_tagged():
    assert worker.monitor_kinds() == MONITOR_KINDS
    for name in MONITOR_KINDS:
        k = kinds()[name]
        assert k.background and k.monitor and k.deploy_gate_exempt and not k.deploy_blocking, name
    # the long batches keep plain background behaviour
    for name in (LONG, "writing_memory_shadow", "place_geocode", "retention_job", "report_generate"):
        assert kinds()[name].background and not kinds()[name].monitor, name
    assert not kinds()["s1_rewrite"].monitor and not kinds()["a3_atomize"].monitor


def test_monitor_needs_background_and_no_deploy_block():
    async def handler(ctx):
        return None
    for kw in ({"monitor": True}, {"monitor": True, "background": True, "deploy_blocking": True}):
        with pytest.raises(ValueError, match="monitor=True"):
            job_kind("zz_invented_watch", **kw)(handler)
    assert "zz_invented_watch" not in _KINDS


# ── the Done-when cases ─────────────────────────────────────────────────────────────────────────

def test_a_monitoring_job_is_claimable_while_a_long_background_job_runs():
    w = _worker([LONG])
    caps = _caps(w)
    assert MONITOR_KINDS <= set(caps)
    assert w.running_background() == 1 and w.running_monitor() == 0


def test_a_second_long_background_job_is_still_held():
    w = _worker([LONG])
    caps = _caps(w)
    assert not (worker.background_kinds() - worker.monitor_kinds()) & set(caps)
    assert "place_geocode" not in caps and "writing_memory_shadow" not in caps


def test_the_s292_queue_drains_behind_a_long_run():
    """25/11/2026: 7 status samples, 2 route watches, 2 rollups and 1 compliance check queued behind R2."""
    w = _worker([LONG])
    queued = _simulate(w, ["ops_status_sample", "llm_route_watch", "ai_usage_rollup", "place_geocode",
                           "compliance_db_checks"])
    assert sorted(w._running_kinds.values()) == sorted([LONG, "ops_status_sample", "llm_route_watch"])
    assert queued == ["ai_usage_rollup", "place_geocode", "compliance_db_checks"]   # the lane cap (2) is full
    # a monitoring job ends: the next monitoring job, never the held background one
    jid = next(j for j, k in w._running_kinds.items() if k == "ops_status_sample")
    w._forget(jid)
    queued = _simulate(w, queued)
    assert "ai_usage_rollup" in w._running_kinds.values() and "place_geocode" in queued


def test_the_monitoring_lane_has_its_own_cap():
    w = _worker(["ops_status_sample", "llm_route_watch"])
    caps = _caps(w)
    assert not MONITOR_KINDS & set(caps)
    assert "place_geocode" in caps and "s1_rewrite" in caps       # the main slots are untouched by the lane
    assert w.free_main_slots() == WORKER_MAX_PARALLEL_DEFAULT


def test_wave_kinds_keep_their_slots_and_reserve():
    """The S217 shape (8 slots = s1_rewrite 6 + a3_atomize 1 + background 1) is unchanged by running monitors."""
    w = _worker([LONG, "ops_status_sample", "llm_route_watch"])
    queued = _simulate(w, ["s1_rewrite"] * 20 + ["a3_atomize"] * 5 + ["place_geocode"])
    running = list(w._running_kinds.values())
    assert running.count("s1_rewrite") == 6 and running.count("a3_atomize") == 1
    assert w.free_main_slots() == 0 and "place_geocode" in queued
    # with only the reserve free, no background kind of either lane takes a main slot
    w2 = _worker(["s1_rewrite"] * 6)
    caps = _caps(w2)
    assert "a3_atomize" in caps and not (worker.background_kinds() - worker.monitor_kinds()) & set(caps)
    # the monitoring lane keeps running during a full wave (its own slots)
    w3 = _worker(["s1_rewrite"] * 6 + ["a3_atomize", LONG])
    assert set(_caps(w3)) == MONITOR_KINDS


def test_without_monitor_kinds_the_rule_is_the_old_one():
    caps = {"s1_rewrite": 6, "a3_atomize": 1, "place_geocode": 1, "ops_status_sample": 1}
    bg = {"place_geocode", "ops_status_sample"}
    for free in range(1, 9):
        for running_bg in (0, 1):
            old = {n: c for n, c in caps.items()
                   if not (n in bg and (free <= 2 or running_bg >= 1))}
            assert worker.claim_caps_for(8, caps, free, running_bg, bg) == old, (free, running_bg)


def test_the_caps_are_the_settings(monkeypatch):
    w = _worker([LONG, "ops_status_sample"])
    monkeypatch.setattr(lanes, "monitor_max_running", lambda: 1)
    assert not MONITOR_KINDS & set(_caps(w))
    monkeypatch.setattr(lanes, "background_max_running", lambda: 2)
    assert "place_geocode" in _caps(w)
    # explicit values win over the settings (the sampler reads them once per sample)
    assert "ops_status_sample" in worker.claim_caps_for(8, {"ops_status_sample": 1}, 4, 0, {"ops_status_sample"},
                                                         running_monitor=1, monitor={"ops_status_sample"},
                                                         monitor_max=3)


def test_the_settings_reader_falls_back_to_the_seeded_defaults(monkeypatch):
    monkeypatch.undo()

    def boom(_key):
        raise KeyError("not compiled")
    monkeypatch.setattr(settings, "get", boom)
    monkeypatch.setattr(settings, "definition", boom)
    assert lanes.background_max_running() == 1 and lanes.monitor_max_running() == 2
    assert lanes.monitor_slots_bound() == 2
    monkeypatch.setattr(settings, "get", lambda key: {lanes.MONITOR_RUNNING_KEY: 3,
                                                     lanes.BACKGROUND_RUNNING_KEY: 0}[key])
    assert lanes.monitor_max_running() == 3 and lanes.background_max_running() == 1   # never below 1
    monkeypatch.setattr(settings, "definition", lambda key: SimpleNamespace(max_value=4))
    assert lanes.monitor_slots_bound() == 4


@pytest.mark.asyncio
async def test_fill_slots_claims_a_monitor_while_the_main_slots_are_full(monkeypatch):
    w = _worker(["s1_rewrite"] * 6 + ["a3_atomize", LONG])
    monkeypatch.setattr(queue, "newer_worker_alive", AsyncMock(return_value=False))
    monkeypatch.setattr(w, "_capacity_hold", AsyncMock(return_value=frozenset()))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(queue, "claim", claim)
    assert await w._fill_slots() == 0
    assert set(claim.call_args[0][2]) == MONITOR_KINDS


@pytest.mark.asyncio
async def test_fill_slots_stops_when_nothing_is_claimable(monkeypatch):
    w = _worker(["s1_rewrite"] * 6 + ["a3_atomize", LONG, "ops_status_sample", "llm_route_watch"])
    monkeypatch.setattr(queue, "newer_worker_alive", AsyncMock(return_value=False))
    monkeypatch.setattr(w, "_capacity_hold", AsyncMock(return_value=frozenset()))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(queue, "claim", claim)
    assert await w._fill_slots() == 0 and not claim.called


# ── the sampler applies the same rule ───────────────────────────────────────────────────────────

def _ago(s):
    return NOW - timedelta(seconds=s)


def _sample(queued, running, caps):
    worker_row = {"worker_id": "w1", "max_parallel": 8, "caps": caps, "alive": True, "fresh": True,
                  "started_at": _ago(7200), "last_seen_at": _ago(5), "task_revision": None}
    rows = [{"id": f"{k}-{i}", "kind": k, "locked_by": "w1", "started_at": _ago(2460), "attempt_started_at": None}
            for i, k in enumerate(running)]
    q = [{"kind": k, "n": 1, "oldest_run_after": _ago(age)} for k, age in queued]
    return backlog.backlog_sample(q, rows, [worker_row], kinds(), NOW, _ago(900))


def test_sampler_and_worker_agree():
    caps = {name: k.concurrency for name, k in kinds().items()}
    # a long run holds the background group only: the queued status sample is claimable, so it counts as starved
    out = _sample([("ops_status_sample", 2400), ("place_geocode", 2400)], [LONG], caps)
    assert set(out["starved"]) == {"ops_status_sample"}
    assert set(out["starved"]) == {k for k in ("ops_status_sample", "place_geocode") if k in _caps(_worker([LONG]))}
    # the lane at its cap: the third monitor waits by design
    out = _sample([("ai_usage_rollup", 2400)], [LONG, "ops_status_sample", "llm_route_watch"], caps)
    assert out["starved"] == {}
    # a full wave on the main slots does not hide a monitor the worker would claim
    full = ["s1_rewrite"] * 6 + ["a3_atomize", LONG]
    out = _sample([("llm_route_watch", 2400), ("s1_rewrite", 2400)], full, caps)
    assert set(out["starved"]) == {"llm_route_watch"}
