"""AA-1051 — the W5 proof runner: plan (DRY RUN), the live gate (refuses without CONFIRM, a cap, a registered file and
an adequate power check; queues one R2 job per arm otherwise), draft and report. The DB and R2 estimate are faked; no
model is called and nothing is queued for real. Invented ids only."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from services.writer_proof import prereg as pr
from services.writer_proof import price, runner, spend
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


def _t9(**over) -> pr.Prereg:
    """A registered T9 proof powered for a large improvement (adequate on the unanimous R1 report)."""
    d = json.loads(pr.path_for("w5-t9").read_text())
    d.update(status="registered", registered_by="tech_lead", registered_on="2026-11-20",
             golden_set={"name": "aa729", "version": "1.4.0"}, sample_ids=GATE, noise_sample_ids=[])
    d["power"] = {**d["power"], "effect_abs": 0.9}
    d.update(over)
    return pr.from_doc(d)


def _r1_report(tmp_path, flaky_every=10 ** 9):
    votes = {f"{i:064x}": ([True, True, False] if i % flaky_every == 0 else [False, False, False])
             for i in range(720)}
    f = tmp_path / "r1.json"
    f.write_text(json.dumps({"recommended": "m@c:extraction", "votes": {"m@c:extraction": votes}}))
    return str(f)


# S280: the catalog row of the drafts' judge (invented prices) and judge-call token rows of the writer's eval runs
CATALOG = {"model_key": "gpt-6-luna-openai", "enabled": True, "blocked_reason": None, "price_in_per_mtok": 0.10,
           "price_out_per_mtok": 0.50, "price_cache_read_per_mtok": None, "price_cache_write_per_mtok": None,
           "max_output_tokens": None, "capabilities": {}}
DEPLOYMENT = {"connection_key": "openai-direct", "enabled": True, "price_in_override": None,
              "price_out_override": None, "price_cache_read_override": None, "price_cache_write_override": None,
              "price_cache_write_1h_override": None}
TOKEN_ROWS = [{"tokens_in": 9000, "tokens_out": 900, "cache_read_tokens": 0} for _ in range(20)]


class _Conn:
    """The runner's reads: the ledger spend, the W5 jobs (a stub shared.job), the earlier runs of the prereg and
    (S280) the judge's catalog row, its pinned deployment and the writer's judge-call tokens."""

    def __init__(self, spend_row, jobs, prior, catalog=None, deployment=None, token_rows=None):
        self.spend_row, self.jobs, self.prior = spend_row, jobs, prior
        self.catalog = {"gpt-6-luna-openai": CATALOG} if catalog is None else catalog
        self.deployment = {("gpt-6-luna-openai", "openai-direct"): DEPLOYMENT} if deployment is None else deployment
        self.token_rows = TOKEN_ROWS if token_rows is None else token_rows

    async def fetchrow(self, sql, *args):
        if sql == price.CATALOG_SQL:
            return self.catalog.get(args[0])
        if sql == price.DEPLOYMENT_SQL:
            return self.deployment.get((args[0], args[1]))
        assert sql == spend.SPEND_SQL and args[0] == cfg.W5_STAGE
        return self.spend_row

    async def fetch(self, sql, *args):
        if sql == price.JUDGE_TOKENS_SQL:
            assert tuple(args[0]) == tuple(cfg.EVAL_LOG_STAGES) and args[2] == price.PROFILE_ROWS
            return list(self.token_rows)
        if sql == spend.ACTIVE_JOBS_SQL:
            assert args == (spend.R2_JOB_KIND,)
            return [{k: j[k] for k in ("job_id", "status", "prereg_id", "workflow", "arm", "cap_usd")}
                    for j in self.jobs if j["status"] in ("queued", "running")]
        if sql == spend.ATTEMPTS_SQL:
            counts: dict = {}
            for j in self.jobs:
                if j["prereg_id"] == args[1]:
                    counts[j["arm"]] = counts.get(j["arm"], 0) + 1
            return [{"arm": a, "jobs": n} for a, n in counts.items()]
        if sql == r2.PREREG_RUNS_SQL:
            return list(self.prior)
        raise AssertionError(sql)


class _Acq:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, spend_row=None, jobs=None, prior=None, **price_rows):
        self.jobs = [] if jobs is None else jobs
        self.conn = _Conn(spend_row or {"total": 0.0, "workflow": 0.0, "rows": 0}, self.jobs, prior or [],
                          **price_rows)

    def acquire(self):
        return _Acq(self.conn)


async def _stub_enqueue(pool, kind, payload, created_by, idempotency_key=None):
    """registry.enqueue on the stub shared.job: a used idempotency key returns the existing job."""
    for j in pool.jobs:
        if idempotency_key is not None and j["key"] == idempotency_key:
            return j["job_id"], False
    job = {"job_id": f"job-{len(pool.jobs) + 1}", "status": "queued", "prereg_id": payload.get("prereg_id"),
           "workflow": payload.get("prereg_workflow"), "arm": payload.get("prereg_arm"),
           "cap_usd": payload.get("cap_usd"), "key": idempotency_key, "kind": kind, "payload": payload}
    pool.jobs.append(job)
    return job["job_id"], True


@pytest.fixture
def fakes(monkeypatch):
    seen = {"estimates": [], "version": "1.4.0", "est": 2.0, "would_refuse": [], "calls": {"judge": 30,
                                                                                         "writer_items": 12},
            "item_usd": 0.1, "null_gate": {"required": True, "passed_by": "w5-null-zz", "checked": []}}

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
        out = {"estimated_usd_max": seen["est"], "would_refuse": list(seen["would_refuse"]),
               "item_estimate": {"item_usd": seen["item_usd"]}}
        if seen["calls"] is not None:
            out["calls"] = dict(seen["calls"])
        return out

    real_null_gate = runner.null_gate

    async def null_gate(pool, pre, **kw):
        if pre.workflow == "null" or seen["null_gate"] == "real":
            return await real_null_gate(pool, pre, **kw)
        return dict(seen["null_gate"])

    monkeypatch.setattr(r2, "PgStore", Store)
    monkeypatch.setattr(r2, "estimate", estimate)
    monkeypatch.setattr(runner, "null_gate", null_gate)
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


def _live(pool, pre, tmp_path, cap=5.0):
    return asyncio.run(runner.run_live(pool, pre, r1_report=_r1_report(tmp_path), instrument=None, cap_usd=cap,
                                       confirm="CONFIRM", requested_by="pytest", enqueue=_stub_enqueue))


def test_live_run_queues_one_r2_job_per_arm(fakes, tmp_path):
    pool = _Pool()
    pre = _pre(noise=GATE[:4])
    out = _live(pool, pre, tmp_path)
    assert [j["kind"] for j in pool.jobs] == ["writing_memory_r2", "writing_memory_r2"]
    confs = [r2.config_from_payload(j["payload"]) for j in pool.jobs]
    assert {c.prereg_arm for c in confs} == {"B", "A_prime"}
    assert all(c.log_stage == cfg.W5_STAGE and c.prereg_sha256 == pre.sha256 for c in confs)
    assert sum(c.cap_usd for c in confs) <= 5.0 and set(out["queued"]) == {"B", "A_prime"}
    assert {j["key"] for j in pool.jobs} == {f"w5:{pre.id}:{pre.sha256[:16]}:B:1",
                                             f"w5:{pre.id}:{pre.sha256[:16]}:A_prime:1"}


def test_a_second_live_run_is_refused_while_the_first_job_is_queued(fakes, tmp_path):
    pool = _Pool()
    _live(pool, _pre(), tmp_path)
    assert len(pool.jobs) == 1 and pool.jobs[0]["status"] == "queued"
    # the ledger is still 0 (nothing billed yet): the queued job's cap and the job itself refuse the second start
    with pytest.raises(runner.RunRefused, match="already queued or running") as e:
        _live(pool, _pre(), tmp_path)
    assert "reserve 5.0000" in str(e.value)
    pool.jobs[0]["status"] = "running"
    with pytest.raises(runner.RunRefused, match="already queued or running"):
        _live(pool, _pre(), tmp_path)
    assert len(pool.jobs) == 1


def test_a_t9_proof_cannot_queue_twice_its_d12_cap(fakes, tmp_path):
    """Back to back --cap-usd 15 starts of T9: the second is refused before any ledger row (the reviewer's case 1)."""
    fakes["est"] = 6.0
    t9 = _t9()
    pool = _Pool()
    _live(pool, t9, tmp_path, cap=15.0)
    with pytest.raises(runner.RunRefused, match="D12 cap 15.0"):
        _live(pool, t9, tmp_path, cap=15.0)
    assert sum(j["cap_usd"] for j in pool.jobs) <= 15.0


def test_two_concurrent_starts_share_one_job_per_arm(fakes, tmp_path, monkeypatch):
    """Both starts read the queue before either enqueued (a stale read): the same idempotency key returns the first
    start's job, so the second start queues nothing."""
    async def stale(conn, prereg_id):
        return {"active": [], "attempts": {}}

    monkeypatch.setattr(spend, "read_jobs", stale)
    pool = _Pool()
    pre = _pre()
    a = _live(pool, pre, tmp_path)
    b = _live(pool, pre, tmp_path)
    assert a["queued"]["B"]["created"] and not b["queued"]["B"]["created"]
    assert b["queued"]["B"]["job_id"] == a["queued"]["B"]["job_id"] and len(pool.jobs) == 1


def test_a_rerun_after_a_result_is_refused_and_an_empty_run_may_be_retried(fakes, tmp_path):
    pre = _pre()
    done = [{"run_id": "run-1", "sha256": pre.sha256, "arm": "B", "results": 24, "verdict": "pass",
             "stop_reason": "completed"}]
    with pytest.raises(runner.RunRefused, match="already has a run with results"):
        _live(_Pool(prior=done), pre, tmp_path)
    # a run refused at $0 left a failed job and no result: a new start is attempt 2
    jobs = [{"job_id": "job-old", "status": "failed", "prereg_id": pre.id, "workflow": "null", "arm": "B",
             "cap_usd": 5.0, "key": runner.idempotency_key(pre, "B", 1)}]
    empty = [{**done[0], "results": 0, "verdict": None}]
    pool = _Pool(jobs=jobs, prior=empty)
    out = _live(pool, pre, tmp_path)
    assert out["queued"]["B"]["created"] and out["queued"]["B"]["idempotency_key"].endswith(":B:2")


def test_an_arm_estimate_above_its_own_run_cap_is_refused(fakes, tmp_path):
    """A single-arm T9 proof estimated at 12 USD with --cap-usd 15 passes the workflow cap but its R2 job would stop
    at the 10 USD run cap after paying for part of the run: refused at $0."""
    fakes["est"] = 12.0
    out = asyncio.run(runner.plan(_Pool(), _t9(), r1_report=_r1_report(tmp_path), cap_usd=15.0))
    assert out["arm_caps"] == {"B": cfg.R2_RUN_CAP_USD}
    assert any("B: estimated 12.0 USD > its run cap 10.0" in b for b in out["blocking"])
    fakes["est"] = 9.0
    out = asyncio.run(runner.plan(_Pool(), _t9(), r1_report=_r1_report(tmp_path), cap_usd=15.0))
    assert not any("its run cap" in b for b in out["blocking"])


def test_a_split_arm_estimate_above_its_share_is_refused(fakes, tmp_path):
    fakes["est"] = 2.0
    out = asyncio.run(runner.plan(_Pool(), _pre(noise=GATE[:4]), r1_report=_r1_report(tmp_path), cap_usd=3.0))
    assert out["arm_caps"] == {"B": 1.5, "A_prime": 1.5}
    assert any("B: estimated 2.0 USD > its run cap 1.5" in b for b in out["blocking"])
    assert any("A_prime: estimated 2.0 USD > its run cap 1.5" in b for b in out["blocking"])


def test_plan_reports_rs_null_run_refusal(fakes, tmp_path):
    fakes["would_refuse"] = ["null run: the control run records no writer configuration (config_fp)"]
    out = asyncio.run(runner.plan(_Pool(), _pre(), r1_report=_r1_report(tmp_path)))
    assert not out["ready_for_live"] and any(b.startswith("B: null run:") for b in out["blocking"])


def test_draft_fills_the_sample_from_gate(fakes):
    out = asyncio.run(runner.draft(_Pool(), pr.load("w5-null-t9-blog"), n=None, noise=0))
    assert out["sample_ids"] == GATE and out["status"] == "draft" and out["golden_set"]["version"] == "1.4.0"


# ── report ───────────────────────────────────────────────────────────────────────────────────────────────────────

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


class _ReportConn:
    def __init__(self, pre, *, sha=None, rerun=False):
        self.sha = sha or pre.sha256
        self.rerun = rerun

    def transaction(self, readonly=False):
        class _T:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False
        return _T()

    async def fetch(self, sql, *args):
        if sql == r2.PREREG_RUNS_SQL:
            rows = [{"run_id": "run-b", "sha256": self.sha, "arm": "B", "results": 24}]
            if self.rerun:      # a later B run that passed by chance: reported, never counted
                rows.append({"run_id": "run-b2", "sha256": self.sha, "arm": "B", "results": 24})
            return rows
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


def test_report_counts_the_first_run_of_an_arm_and_flags_reruns():
    pre = _pre()
    out = asyncio.run(runner.report(_ReportPool(_ReportConn(pre, rerun=True)), pre))
    assert out["ok"] and out["runs"] == {"B": "run-b"} and out["rerun_note"]
    assert out["reruns"] == [{"run_id": "run-b2", "arm": "B", "results": 24}]
    plain = asyncio.run(runner.report(_ReportPool(_ReportConn(pre)), pre))
    assert plain["reruns"] == [] and plain["rerun_note"] is None


def test_counted_runs_skips_empty_runs():
    prior = [{"run_id": "r0", "arm": "B", "results": 0}, {"run_id": "r1", "arm": "B", "results": 5},
             {"run_id": "r2", "arm": "A_prime", "results": 3}, {"run_id": "r3", "arm": "B", "results": 7}]
    assert runner.counted_runs(prior) == ({"B": "r1", "A_prime": "r2"}, [{"run_id": "r3", "arm": "B", "results": 7}])
