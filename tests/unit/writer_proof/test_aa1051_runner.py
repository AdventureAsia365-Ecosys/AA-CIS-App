"""AA-1051 — the W5 proof runner: plan (DRY RUN), the live gate (refuses without CONFIRM, a cap, a registered file and
an adequate power check; queues one R2 job per arm otherwise), draft and report. The DB and R2 estimate are faked; no
model is called and nothing is queued for real. Invented ids only."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from services.writer_proof import prereg as pr
from services.writer_proof import runner, spend
from services.writing_memory import config as cfg
from services.writing_memory import r2

GATE = sorted(f"zz-gate-{i:02d}" for i in range(12))


def _pre(registered=True, noise=(), **over) -> pr.Prereg:
    d = json.loads(pr.path_for("w5-null-t9-blog").read_text())
    if registered:
        d.update(status="registered", registered_by="tech_lead", registered_on="2026-11-20",
                 golden_set={"name": "aa729", "version": "1.4.0"}, sample_ids=GATE, noise_sample_ids=sorted(noise))
    d.update(over)
    return pr.from_doc(d)


def _r1_report(tmp_path, flaky_every=10 ** 9):
    votes = {f"{i:064x}": ([True, True, False] if i % flaky_every == 0 else [False, False, False])
             for i in range(720)}
    f = tmp_path / "r1.json"
    f.write_text(json.dumps({"recommended": "m@c:extraction", "votes": {"m@c:extraction": votes}}))
    return str(f)


class _Conn:
    def __init__(self, spend_row):
        self.spend_row = spend_row

    async def fetchrow(self, sql, *args):
        assert sql == spend.SPEND_SQL and args[0] == cfg.W5_STAGE
        return self.spend_row


class _Acq:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, spend_row=None):
        self.conn = _Conn(spend_row or {"total": 0.0, "workflow": 0.0, "rows": 0})

    def acquire(self):
        return _Acq(self.conn)


@pytest.fixture
def fakes(monkeypatch):
    seen = {"estimates": [], "version": "1.4.0", "est": 2.0, "would_refuse": []}

    class Store:
        def __init__(self, pool, stages=None):
            self.stages = stages

        async def golden_set(self):
            return {"set_id": "set", "name": "aa729", "version": seen["version"], "shared_inputs": {}}

        async def items(self, set_id, writer, tenant):
            return [{"sample_id": s, "writer": "t9_blog", "trap": False, "eval_split": "gate"} for s in GATE] + [
                {"sample_id": "zz-dev-1", "writer": "t9_blog", "trap": False, "eval_split": "dev"}]

    async def estimate(pool, conf):
        conf.validate()
        seen["estimates"].append(conf)
        return {"estimated_usd_max": seen["est"], "would_refuse": list(seen["would_refuse"])}

    monkeypatch.setattr(r2, "PgStore", Store)
    monkeypatch.setattr(r2, "estimate", estimate)
    return seen


def test_plan_of_a_draft_blocks_and_estimates_nothing(fakes):
    out = asyncio.run(runner.plan(_Pool(), pr.load("w5-null-t9-blog")))
    assert not out["ready_for_live"] and any("draft" in b for b in out["blocking"])
    assert any("no sample ids" in b for b in out["blocking"]) and not fakes["estimates"]


def test_plan_of_a_registered_file_is_ready_with_power(fakes, tmp_path):
    out = asyncio.run(runner.plan(_Pool(), _pre(), r1_report=_r1_report(tmp_path)))
    assert out["ready_for_live"], out["blocking"]
    conf = fakes["estimates"][0]
    assert conf.eval_split == "gate" and conf.sample_ids == GATE and conf.log_stage == cfg.W5_STAGE
    assert conf.prereg_arm == "B" and conf.goal == "non_inferior" and conf.judge_model == "gpt-6-luna-openai"
    assert out["arm_caps"] == {"B": 5.0} and out["power"]["adequate"]


def test_plan_blocks_without_power_on_another_golden_version_and_on_an_r2_refusal(fakes, tmp_path):
    fakes["version"] = "1.5.0"
    fakes["would_refuse"] = ["calibration cannot be measured", "estimated 9 > cap 5: size --cap-usd"]
    out = asyncio.run(runner.plan(_Pool(), _pre()))
    b = " | ".join(out["blocking"])
    assert "power not checked" in b and "active set is aa729@1.5.0" in b
    assert "calibration cannot be measured" in b and "estimated 9" not in b     # the arm cap decides that one


def test_plan_blocks_an_underpowered_file_and_spent_budget(fakes, tmp_path):
    metrics = [{"name": "fail_claims_per_item", "kind": "computed", "direction": "lower", "primary": True}]
    out = asyncio.run(runner.plan(_Pool({"total": 30.0, "workflow": 1.0, "rows": 9}),
                                  _pre(metrics=metrics,
                                       decision={"rule": "non_inferior", "metric": "fail_claims_per_item",
                                                 "margin_abs": 0.1, "alpha": 0.05, "noise_se_k": 2.0}),
                                  r1_report=_r1_report(tmp_path, flaky_every=3)))
    b = " | ".join(out["blocking"])
    assert "D12 cap 5.0" in b and "D12 total" in b and "underpowered" in b


def test_plan_with_a_noise_arm_shares_the_cap(fakes, tmp_path):
    out = asyncio.run(runner.plan(_Pool(), _pre(noise=GATE[:4]), r1_report=_r1_report(tmp_path)))
    assert set(out["arms"]) == {"B", "A_prime"} and sum(out["arm_caps"].values()) <= 5.0
    a_prime = [c for c in fakes["estimates"] if c.prereg_arm == "A_prime"][0]
    assert a_prime.sample_ids == GATE[:4]


@pytest.mark.parametrize("kw,needle", [
    ({"confirm": None}, "confirmation"), ({"confirm": "confirm"}, "confirmation"),
    ({"cap_usd": None}, "no cap"), ({"cap_usd": 7.0}, "above the pre-registered"),
])
def test_live_run_refuses_and_queues_nothing(fakes, tmp_path, kw, needle):
    queued = []

    async def enqueue(*a, **k):
        queued.append(a)
        return "job", True

    args = {"r1_report": _r1_report(tmp_path), "instrument": None, "cap_usd": 5.0, "confirm": "CONFIRM",
            "requested_by": "pytest", "enqueue": enqueue}
    args.update(kw)
    with pytest.raises(runner.RunRefused, match=needle):
        asyncio.run(runner.run_live(_Pool(), _pre(), **args))
    assert not queued


def test_live_run_refuses_a_draft_and_a_missing_power_check(fakes):
    with pytest.raises(pr.PreregRefused, match="draft"):
        asyncio.run(runner.run_live(_Pool(), pr.load("w5-null-t9-blog"), r1_report=None, instrument=None,
                                    cap_usd=5.0, confirm="CONFIRM", requested_by="pytest"))
    with pytest.raises(runner.RunRefused, match="power not checked"):
        asyncio.run(runner.run_live(_Pool(), _pre(), r1_report=None, instrument=None, cap_usd=5.0,
                                    confirm="CONFIRM", requested_by="pytest", enqueue=None))


def test_live_run_queues_one_r2_job_per_arm(fakes, tmp_path):
    queued = []

    async def enqueue(pool, kind, payload, created_by):
        queued.append((kind, payload, created_by))
        return f"job-{len(queued)}", True

    out = asyncio.run(runner.run_live(_Pool(), _pre(noise=GATE[:4]), r1_report=_r1_report(tmp_path),
                                      instrument=None, cap_usd=5.0, confirm="CONFIRM", requested_by="pytest",
                                      enqueue=enqueue))
    assert [q[0] for q in queued] == ["writing_memory_r2", "writing_memory_r2"]
    confs = [r2.config_from_payload(q[1]) for q in queued]
    assert {c.prereg_arm for c in confs} == {"B", "A_prime"}
    assert all(c.log_stage == cfg.W5_STAGE and c.prereg_sha256 == _pre(noise=GATE[:4]).sha256 for c in confs)
    assert sum(c.cap_usd for c in confs) <= 5.0 and set(out["queued"]) == {"B", "A_prime"}


def test_draft_fills_the_sample_from_gate(fakes):
    out = asyncio.run(runner.draft(_Pool(), pr.load("w5-null-t9-blog"), n=None, noise=0))
    assert out["sample_ids"] == GATE and out["status"] == "draft" and out["golden_set"]["version"] == "1.4.0"


# ── report ───────────────────────────────────────────────────────────────────────────────────────────────────────

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


class _ReportConn:
    def __init__(self, pre, *, sha=None):
        self.sha = sha or pre.sha256

    def transaction(self, readonly=False):
        class _T:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False
        return _T()

    async def fetch(self, sql, *args):
        if sql == r2.PREREG_RUNS_SQL:
            return [{"run_id": "run-b", "sha256": self.sha, "results": 24}]
        if "FROM shared.prompt_eval_runs p" in sql:
            return [{"run_id": "run-b", "writer": "t9_blog", "verdict": "pass", "stop_reason": "completed",
                     "cost_usd": 1.0, "prereg": {"id": "w5-null-t9-blog", "arm": "B"}, "created_at": T0}]
        if "FROM shared.llm_call_log" in sql:
            return [{"id": f"l{i}", "eval_run": "run-b", "created_at": T0 + timedelta(seconds=2 * i + 1),
                     "cost_usd": 0.05, "role": "writer" if i % 2 else "judge", "real_stage": "t9_write"}
                    for i in range(24)]
        if "FROM shared.eval_item_result" in sql:
            rows = []
            for i, sid in enumerate(GATE):
                rows.append({"result_id": f"a{i}", "run_id": "run-b", "sample_id": sid, "arm": "control",
                             "mode": "rejudge", "verdict": "pass", "created_at": T0 + timedelta(seconds=2 * i + 1.5),
                             "failure_codes": [], "hard_stop": False, "details": {}})
            for i, sid in enumerate(GATE):
                rows.append({"result_id": f"b{i}", "run_id": "run-b", "sample_id": sid, "arm": "candidate",
                             "mode": "regenerate", "verdict": "pass", "failure_codes": [], "hard_stop": False,
                             "created_at": T0 + timedelta(seconds=100 + i), "details": {}})
            return rows
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        return json.dumps({"judge_identities": [{"model": "m"}], "counts": {"hard_stops": {}}})


class _ReportPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acq(self.conn)


def test_report_gives_the_verdict_and_the_cost_per_object():
    pre = _pre()
    out = asyncio.run(runner.report(_ReportPool(_ReportConn(pre)), pre))
    assert out["ok"] and out["decision"]["verdict"] == "non_inferior" and out["decision"]["paired"] == 12
    assert out["cost"]["runs"][0]["ledger_usd"] == 1.2 and out["cost"]["runs"][0]["objects"] == 12
    assert out["computed"]["first_pass_yield"] == 1.0
    assert any(t["metric"] == "hard_stops" and t["status"] == "met" for t in out["targets"])


def test_report_refuses_when_the_file_was_edited_after_its_first_result():
    pre = _pre()
    out = asyncio.run(runner.report(_ReportPool(_ReportConn(pre, sha="e" * 64)), pre))
    assert not out["ok"] and "edited after its first result" in out["refused"]
