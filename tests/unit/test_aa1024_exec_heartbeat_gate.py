"""AA-1024 G3 (c) — the deploy gate refuses while an ECS exec script runs (a fresh ecsx heartbeat on the bronze
bucket), so a deploy no longer kills a long script (root lessons log, ECS exec family, S234). Fake S3 client only."""
from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.jobs import exec_heartbeat as hb
from shared.jobs import queue
from shared.jobs import worker as worker_mod

NOW = dt.datetime(2026, 11, 7, 8, 0, tzinfo=dt.timezone.utc)


def _obj(key, age_s):
    return {"Key": key, "LastModified": NOW - dt.timedelta(seconds=age_s)}


def test_only_fresh_heartbeats_without_a_done_marker_count():
    listing = [
        _obj("scripts/out/aaa111.txt.running", 20),                  # running now
        _obj("scripts/out/bbb222.txt.running", 15),                  # finished: .done is there
        _obj("scripts/out/bbb222.txt.done", 5), _obj("scripts/out/bbb222.txt", 5),
        _obj("scripts/out/ccc333.txt.running", 900),                 # stale: the run died
        _obj("scripts/out/aa645/report.json", 1),                    # other script output
        _obj("scripts/out/ddd444.txt.running", 119),
    ]
    assert hb.running_from_listing(listing, NOW) == [{"run_id": "ddd444", "age_s": 119},
                                                     {"run_id": "aaa111", "age_s": 20}]


class _Client:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class _P:
            def paginate(self, **kw):
                client.calls.append(kw)
                return iter(client.pages)
        return _P()


def test_lists_the_out_prefix_of_the_bronze_bucket(monkeypatch):
    monkeypatch.setenv("DEPLOY_GATE_ECSX", "on")
    monkeypatch.setenv("BRONZE_BUCKET", "aa-cis-bronze-test")
    c = _Client([{"Contents": [_obj("scripts/out/eee555.txt.running", 3)]}, {}])
    assert hb.running_exec_scripts(client=c, now=NOW) == [{"run_id": "eee555", "age_s": 3}]
    assert c.calls == [{"Bucket": "aa-cis-bronze-test", "Prefix": "scripts/out/"}]


def test_off_switch_and_a_failed_list_raises(monkeypatch):
    monkeypatch.setenv("DEPLOY_GATE_ECSX", "off")
    assert hb.running_exec_scripts(client=MagicMock(side_effect=AssertionError)) == []
    monkeypatch.setenv("DEPLOY_GATE_ECSX", "on")
    broken = MagicMock()
    broken.get_paginator.side_effect = RuntimeError("AccessDenied")
    with pytest.raises(RuntimeError):     # the gate fails closed (the endpoint answers 5xx, CI blocks)
        hb.running_exec_scripts(client=broken, now=NOW)


@pytest.mark.asyncio
async def test_a_running_script_makes_the_deploy_unsafe():
    with patch.object(worker_mod, "load_kinds", lambda: None), \
            patch.object(queue, "active_count", AsyncMock(return_value={"active": 0, "by_status": {},
                                                                         "scheduled_later": 0})), \
            patch.object(queue, "deploy_blockers", AsyncMock(return_value={"open_waves": 0, "blocking_running": {},
                                                                            "blocking_due": {}})), \
            patch.object(hb, "running_exec_scripts", return_value=[{"run_id": "fff666", "age_s": 12}]):
        out = await worker_mod.deploy_gate_status(MagicMock())
    assert out["safe_to_deploy"] is False and out["exec_scripts"] == [{"run_id": "fff666", "age_s": 12}]
    assert out["blocked_by"] == ["ECS exec script fff666 running (heartbeat 12 s ago)"]


def test_the_fresh_boundary_and_a_done_without_a_heartbeat():
    listing = [_obj("scripts/out/ggg777.txt.running", hb.FRESH_S),       # exactly FRESH_S old: still running
               _obj("scripts/out/hhh888.txt.running", hb.FRESH_S + 1),   # one second past: a dead run
               _obj("scripts/out/iii999.txt.done", 2), _obj("scripts/out/iii999.txt", 2)]  # no .running at all
    assert hb.running_from_listing(listing, NOW) == [{"run_id": "ggg777", "age_s": hb.FRESH_S}]


def _gate_patches(exec_side):
    return (patch.object(worker_mod, "load_kinds", lambda: None),
            patch.object(queue, "active_count", AsyncMock(return_value={"active": 0, "by_status": {},
                                                                        "scheduled_later": 0})),
            patch.object(queue, "deploy_blockers", AsyncMock(return_value={"open_waves": 1, "blocking_running": {},
                                                                           "blocking_due": {}})),
            patch.object(hb, "running_exec_scripts", side_effect=exec_side))


@pytest.mark.asyncio
async def test_an_unreadable_heartbeat_blocks_but_keeps_the_rest_of_the_answer():
    # S272 review: the S3 error used to raise, so the CI endpoint answered 5xx (fine) but the platform card lost its
    # wave and job counts too. Now it is one blocked_by line: the CI guard still blocks on safe_to_deploy.
    a, b, c, d = _gate_patches(RuntimeError("AccessDenied"))
    with a, b, c, d:
        out = await worker_mod.deploy_gate_status(MagicMock())
    assert out["safe_to_deploy"] is False and out["exec_scripts"] is None
    assert out["open_waves"] == 1 and "1 rerun wave(s) open" in out["blocked_by"]
    assert out["exec_scripts_error"] in out["blocked_by"] and "unreadable" in out["exec_scripts_error"]


@pytest.mark.asyncio
async def test_the_ci_endpoint_reports_the_unreadable_heartbeat_as_unsafe():
    from api.routers import admin_job_runner

    a, b, c, d = _gate_patches(RuntimeError("AccessDenied"))
    with a, b, c, d, patch.object(admin_job_runner, "verify_admin_secret"):
        out = await admin_job_runner.deploy_gate(MagicMock(), x_admin_secret="s")
    assert out["safe_to_deploy"] is False
    assert any("unreadable" in x for x in out["blocked_by"])
