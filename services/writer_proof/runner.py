"""AA-1051 — the W5 proof runner: the eval null run and the T9 / S1 / T2 writer proofs under their pre-registration and
the D12 caps (root ADR 0020; S271 audit W5; S275 decision "code eval part first").

A proof is one or two R2 runs (services/writing_memory/r2.py) on the pre-registered fixed sample of the GATE side,
with the pinned judge, logged under stage adhoc_aa1051:
  B        the sample, candidate = the current writers (or the pre-registered release), control A = the stored output
           re-judged at $0 writer cost (release 0's frozen golden text);
  A_prime  only with noise_sample_ids: the same configuration again on the noise sample (run-to-run noise).
The null run is the T9 blog workflow with no candidate change: B == A by construction, i.e. the control's stored
outputs were written under today's writer configuration (r2.null_config_problems: the control run's fingerprint and
config_fp equal the current ones; a baseline import of frozen text is refused, a reverify on the current golden
version comes first). It must come out non_inferior with every item paired, one judge and no hard stop 1, or the eval
itself is not stable enough to judge a real candidate. Checked by `plan` and again by R2 on the worker.

Once only: a pre-registration runs one time. A live run is refused while a job of it is queued or running, and once
any of its runs has results (R2 also refuses an arm that already has a run with results); each arm's job carries an
idempotency key (pre-registration, sha, arm, attempt), so two operators starting the same run queue it once. `report`
decides on the first run with results of each arm and lists any other as a rerun.

AA-1051 S283: the judge may be a pinned claim instrument (prereg judge.instrument, instrument.py: R1 fire -> two-step
v7, ADVISORY). The R2 config then carries `judge_instrument`, the plan prices it per judged output (price.
instrument_check), the power check reads the fire report and the two-step gated report (--r1-report and
--two-step-report, power.chain_rates), and the report carries the advisory reason. A file may fix its sample as a rule
(prereg "sample"); `resolved` fills it from the registered golden version before every plan, run and report.

Commands (DRY RUN unless `run --live`; every command prints one JSON document, ids and counts only):

  python -m services.writer_proof.runner plan   --prereg w5-null-t9-blog [--r1-report <file> [--instrument <id>]]
                                                [--two-step-report <file>]
  python -m services.writer_proof.runner draft  --prereg w5-null-t9-blog [--n 12] [--noise 0]
  python -m services.writer_proof.runner run    --prereg w5-null-t9-blog --r1-report <file> --cap-usd 5 \\
                                                --live --confirm CONFIRM [--requested-by <who>]
  python -m services.writer_proof.runner report --prereg w5-null-t9-blog
  python -m services.writer_proof.runner campaign [--r1-report <file> [--instrument <id>]]

plan    reads the DB (READ ONLY): the pre-registration (valid, registered, golden version = the active one), the
        sample on the GATE side, each arm's R2 estimate (controls, calibration on CAL, judge family, caps), the W5
        spend so far, the lock (no earlier result under another sha), and the power check when an R1 report is given.
        S280: the pinned judge priced from shared.llm_model_catalog (price.py; each arm is gated on the larger of
        R2's estimate and the catalog one) and, for T9 / S1 / T2, a null run with the same judge and golden version
        that came out non_inferior (null_gate).
draft   fills a draft's sample from the GATE side (seeded hash, no data-driven choice) and prints it; a person saves it
        as prereg/<id>.json with status registered in a reviewed PR. Nothing is written.
run     everything `plan` checks, plus: a registered file, an adequate power check, the spend gate (--confirm CONFIRM
        and a cap within the pre-registered and D12 caps and the W5 totals) and an estimate within the cap. Then it
        queues one writing_memory_r2 job per arm (the paid work runs on the worker, never in an exec session). The
        lane that built this ran DRY RUN only.
report  the runs of the pre-registration: the lock check, the pre-registered verdict (decision.py), the targets, and
        cost / requests per object from the ledger (cost_report.py).
campaign the plan of every W5 proof in the D12 order (null run, T9, S1, T2): samples, stage, estimate vs cap, ready
        or not and why, and the estimates' sum against the D12 total. Nothing is written or queued.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Optional

from services.writer_proof import cost_report, decision, power, price, spend
from services.writer_proof import prereg as pr

GOALS = {"non_inferior": "non_inferior", "improve": "improve"}


class RunRefused(RuntimeError):
    """A live run may not start: nothing was queued."""


def arm_confs(pre: pr.Prereg, *, caps: Optional[dict[str, float]] = None, triggered_by: str = "w5:plan"):
    """{arm: R2Config} of a pre-registration (B always, A_prime with a noise sample). `caps`: each arm's run cap."""
    from services.writing_memory import config as cfg
    from services.writing_memory.r2 import R2Config
    doc = pre.doc
    arms = {"B": list(doc["sample_ids"])}
    if doc.get("noise_sample_ids"):
        arms["A_prime"] = sorted(doc["noise_sample_ids"])
    out = {}
    for arm, ids in arms.items():
        cap = min(float((caps or {}).get(arm, doc["cap_usd"])), cfg.R2_RUN_CAP_USD)
        out[arm] = R2Config(
            writer=pre.writer, release_id=doc.get("release_id"), cap_usd=cap, goal=GOALS[doc["decision"]["rule"]],
            triggered_by=f"{triggered_by}:{pre.id}:{arm}"[:128], log_stage=cfg.W5_STAGE, require_calibrated=True,
            judge_model=doc["judge"]["model_key"], judge_connection=doc["judge"].get("connection"),
            judge_instrument=doc["judge"].get("instrument"),
            eval_split=doc["eval_split"], sample_ids=ids, prereg_id=pre.id, prereg_sha256=pre.sha256,
            prereg_workflow=pre.workflow, prereg_arm=arm)
    return out


def _read_json(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _power(pre: pr.Prereg, r1_report: Optional[str], instrument: Optional[str],
           two_step_report: Optional[str] = None) -> dict:
    if not r1_report:
        return {"checked": False, "why": "no R1 report given (--r1-report): the power check needs the R1 votes"}
    try:
        out = power.prereg_power(pre, _read_json(r1_report), instrument=instrument,
                                 two_step_report=_read_json(two_step_report))
        return {"checked": True, **out}
    except (OSError, ValueError, power.PowerRefused) as e:
        return {"checked": False, "why": str(e)}


async def resolved(pool, pre: pr.Prereg, *, set_id: Optional[str] = None) -> pr.Prereg:
    """S283: `pre` with its sample rule filled from the golden version's items (`set_id`, default the active set; a
    file with a sample list, or no golden set, is returned unchanged). READ ONLY."""
    if not isinstance(pre.doc.get("sample"), dict):
        return pre
    from services.writing_memory import r2
    store = r2.PgStore(pool)
    if set_id is None:
        gs = await store.golden_set()
        if gs is None:
            return pre
        set_id = gs["set_id"]
    return pr.resolve(pre, await store.items(set_id, pre.writer, None))


def _sample_short(pre: pr.Prereg) -> list[str]:
    """A blocking line when a sample rule drew fewer GATE items than it asks for (too few on the side). Pure."""
    res = pre.doc.get("sample_resolved") or {}
    if res and res.get("picked", 0) < res.get("asked", 0):
        return [f"the sample rule drew {res['picked']} of {res['asked']} GATE items (too few on the side)"]
    return []


def sample_outputs(pre: pr.Prereg, items: list[dict], shared_inputs: Optional[dict]) -> list[str]:
    """The labelled OUTPUT texts the judge reads of the fixed sample's frozen control outputs (eval_judge.evidence_view,
    as the run builds them; an item over the evidence cap is left out, the run refuses it at $0 too). Pure."""
    from services.writing_memory.eval_judge import evidence_view
    want = set(pre.doc.get("sample_ids") or [])
    out = []
    for it in items:
        if it.get("sample_id") not in want or it.get("control_output") is None:
            continue
        try:
            out.append(evidence_view(pre.writer, it.get("input") or {}, it["control_output"], shared_inputs).output)
        except ValueError:
            continue
    return out


async def _price(pool, pre: pr.Prereg, arms: dict) -> dict:
    """The price check of the plan: the catalog judge call (price.check), or a claim instrument per judged output."""
    judge = pre.doc["judge"]
    if not judge.get("instrument"):
        return await price.check(pool, model_key=judge["model_key"], connection=judge.get("connection"),
                                 writer=pre.writer, arms=arms)
    from services.writing_memory import r2
    store = r2.PgStore(pool)
    gs = await store.golden_set()
    items = await store.items(gs["set_id"], pre.writer, None) if gs else []
    return await price.instrument_check(pool, instrument=judge["instrument"], writer=pre.writer, arms=arms,
                                        outputs=sample_outputs(pre, items, (gs or {}).get("shared_inputs")))


def arm_caps(cap: Any, estimates: dict[str, float]) -> dict[str, float]:
    """Each arm's run cap: its share of the workflow cap (spend.split_cap), at most the R2 run cap. Pure."""
    from services.writing_memory import config as cfg
    c = spend._num(cap)
    if not estimates or c is None or c <= 0:
        return {}
    return {arm: min(share, cfg.R2_RUN_CAP_USD) for arm, share in spend.split_cap(c, estimates).items()}


async def _price_and_null_run(pool, pre: pr.Prereg, out: dict, estimates: dict[str, float]) -> list[str]:
    """S280, part of `plan`: the pinned judge priced from the catalog (each arm's estimate becomes the larger of R2's
    and the catalog one, in place) and the null-run-first gate. Fills out[r2_estimated_usd_max, price_check,
    estimated_usd_max, null_run]; returns the blocking lines."""
    out["r2_estimated_usd_max"] = round(sum(estimates.values()), 4) if estimates else None
    out["price_check"] = await _price(pool, pre, out["arms"])
    for arm, pc in out["price_check"]["arms"].items():
        estimates[arm] = max(estimates[arm], float(pc["usd_max"]))
    out["estimated_usd_max"] = round(sum(estimates.values()), 4) if estimates else None
    out["null_run"] = await null_gate(pool, pre)          # root ADR 0020 W5: the null run proves the eval stable first
    return list(out["price_check"]["blocking"]) + ([out["null_run"]["blocking"]] if out["null_run"].get("blocking")
                                                   else [])


async def plan(pool, pre: pr.Prereg, *, r1_report: Optional[str] = None, instrument: Optional[str] = None,
               cap_usd: Optional[float] = None, two_step_report: Optional[str] = None) -> dict:
    """The DRY RUN of a proof ($0, reads only). `blocking` lists what would refuse a live run (besides --confirm)."""
    from services.writing_memory import r2
    from shared import settings
    await settings.ensure_fresh(pool)           # S280: the D12 caps and minimum samples in force (CIS 419)
    # `pre` may have been loaded before this refresh (the CLI loads it first; an exec process has no refresher, so
    # load() checked it against the compiled defaults): check it again under the values an administrator set.
    blocking: list[str] = [f"pre-registration: {x}" for x in pr.problems(pre.doc)]
    pre = await resolved(pool, pre)              # S283: a sample rule drawn from the active golden version
    out: dict[str, Any] = {"prereg": pre.id, "sha256": pre.sha256, "workflow": pre.workflow, "writer": pre.writer,
                           "status": pre.doc.get("status"), "samples": len(pre.doc.get("sample_ids") or []),
                           "noise_samples": len(pre.doc.get("noise_sample_ids") or []),
                           "d12_cap_usd": pr.workflow_cap(pre.workflow), "prereg_cap_usd": pre.doc["cap_usd"],
                           "judge": pre.doc["judge"], "sample_resolved": pre.doc.get("sample_resolved"),
                           "sample_ids": list(pre.doc.get("sample_ids") or [])}
    blocking.extend(_sample_short(pre))
    if not pre.registered:
        blocking.append("status is draft (fill the sample with `draft`, register it in a reviewed PR)")
    store = r2.PgStore(pool, stages=(spend.W5_STAGE,))
    gs = await store.golden_set()
    want = pre.doc.get("golden_set") or {}
    out["golden_set"] = None if gs is None else f"{gs['name']}@{gs['version']}"
    if gs is None:
        blocking.append("no active golden set")
    elif want.get("version") and (want.get("name") != gs["name"] or want["version"] != gs["version"]):
        blocking.append(f"pre-registered on {want.get('name')}@{want['version']}, the active set is "
                        f"{gs['name']}@{gs['version']}: register a new file for the new version")
    async with pool.acquire() as conn:
        out["spend"] = await spend.read_spend(conn, pre.workflow)
        jobs = await spend.read_jobs(conn, pre.id)
        prior = [dict(r) for r in await conn.fetch(r2.PREREG_RUNS_SQL, pre.id)]
    out["spend"]["reserved"] = spend.reserved(jobs["active"], pre.workflow)
    out["jobs"] = {"active": jobs["active"], "attempts": jobs["attempts"]}
    mine = [j["job_id"] for j in jobs["active"] if j.get("prereg_id") == pre.id]
    if mine:
        blocking.append(f"a job of {pre.id} is already queued or running ({', '.join(mine)}): wait for its result; a "
                        "second run would spend the cap again")
    done = [r["run_id"] for r in prior if int(r.get("results") or 0) > 0]
    out["prior_runs"] = [{k: r.get(k) for k in ("run_id", "arm", "results", "verdict", "stop_reason")} for r in prior]
    if done:
        blocking.append(f"{pre.id} already has a run with results ({', '.join(done)}): a pre-registered proof runs "
                        "once (read it with `report`; register a new file for a new attempt)")
    estimates: dict[str, float] = {}
    out["arms"] = {}
    if pre.doc.get("sample_ids"):
        for arm, conf in arm_confs(pre).items():
            try:
                est = await r2.estimate(pool, conf)
            except r2.R2Refused as e:
                blocking.append(f"{arm}: {e}")
                continue
            estimates[arm] = float(est["estimated_usd_max"])
            # R2's own "estimated > cap" compares with a placeholder cap: the arm's real cap is checked below
            blocking.extend(f"{arm}: {w}" for w in est["would_refuse"] if not w.startswith("estimated "))
            out["arms"][arm] = est
    else:
        blocking.append("no sample ids")
    blocking.extend(await _price_and_null_run(pool, pre, out, estimates))
    total_est = out["estimated_usd_max"]
    gate_cap = cap_usd if cap_usd is not None else pre.doc["cap_usd"]
    out["spend_gate_preview"] = spend.refusals(
        workflow=pre.workflow, prereg_cap=float(pre.doc["cap_usd"]), cap_usd=gate_cap, confirm=spend.CONFIRM_TOKEN,
        spent_workflow=out["spend"]["workflow"], spent_total=out["spend"]["total"], estimate_usd=total_est,
        reserved_workflow=out["spend"]["reserved"]["workflow"], reserved_total=out["spend"]["reserved"]["total"])
    blocking.extend(out["spend_gate_preview"])
    out["arm_caps"] = arm_caps(gate_cap, estimates)
    for arm, est in estimates.items():
        # each arm's job stops at its own cap: an estimate above it would pay part of the run for an invalid verdict
        if est > out["arm_caps"][arm] + 1e-4:
            blocking.append(f"{arm}: estimated {est} USD > its run cap {out['arm_caps'][arm]} (its share of the "
                            f"{gate_cap} USD cap, at most the R2 run cap {r2.cfg.R2_RUN_CAP_USD}): plan fewer items")
    out["power"] = _power(pre, r1_report, instrument, two_step_report)
    if not out["power"].get("checked"):
        blocking.append(f"power not checked: {out['power'].get('why')}")
    else:
        if out["power"].get("writer_sd_below_floor"):
            blocking.append(f"power.writer_sd {out['power']['writer_sd']} is below the writer spread the claim rates "
                            f"imply ({out['power']['writer_sd_floor']}): state a writer_sd >= it, re-pick the margin")
        pw = out["power"]
        if pw["power"] < pw["min_power"]:
            blocking.append(f"underpowered ({pw['rule']} at a true effect of {pw['true_effect']}): "
                            f"power {pw['power']} < {pw['min_power']} "
                            f"(n {pw['n_items']}, needs {pw['n_needed'] or 'no n: effect <= margin'})")
    out["blocking"] = list(dict.fromkeys(blocking))
    out["ready_for_live"] = not out["blocking"]
    return out


def same_instrument(a: pr.Prereg, b: pr.Prereg) -> bool:
    """True when two pre-registrations pin the same judge (model, connection and, S283, claim instrument) on the same
    golden version. Pure."""
    ja, jb = a.doc.get("judge") or {}, b.doc.get("judge") or {}
    key = ("model_key", "connection", "instrument")
    return (tuple(ja.get(k) for k in key) == tuple(jb.get(k) for k in key)
            and (a.doc.get("golden_set") or {}) == (b.doc.get("golden_set") or {}))


async def null_gate(pool, pre: pr.Prereg, *, nulls: Optional[list[pr.Prereg]] = None) -> dict:
    """S280 (root ADR 0020 W5: "a null run comes first, to prove that the eval itself is stable"): a T9 / S1 / T2 proof
    is planned only after a registered null run with the SAME pinned judge and golden version came out non_inferior
    (its `report`). The null run itself passes. {"required", "passed_by", "checked", "blocking"} (READ ONLY)."""
    if pre.workflow == "null":
        return {"required": False}
    candidates = [n for n in (pr.registered_files("null") if nulls is None else nulls) if same_instrument(n, pre)]
    # a registered null run that no longer loads (lock mismatch, a raised minimum sample) is named with its reason
    ignored = pr.unloadable_registered("null") if nulls is None else []
    checked = []
    for n in candidates:
        rep = await report(pool, n)
        verdict = ((rep.get("decision") or {}).get("verdict")) if rep.get("ok") else None
        checked.append({"prereg": n.id, "verdict": verdict, "refused": rep.get("refused")})
        if verdict == "non_inferior":
            return {"required": True, "passed_by": n.id, "checked": checked, "ignored": ignored}
    judge = pre.doc.get("judge") or {}
    why = "".join(f"; registered null run {i['file']} ignored: {i['why']}" for i in ignored)
    return {"required": True, "passed_by": None, "checked": checked, "ignored": ignored,
            "blocking": (f"no null run with the same judge ({_judge_label(judge)}) and "
                         f"golden version came out non_inferior yet: run the null run first (root ADR 0020 W5){why}")}


def _judge_label(judge: dict) -> str:
    base = f"{judge.get('model_key')}@{judge.get('connection')}"
    return f"{judge['instrument']} ({base})" if judge.get("instrument") else base


CAMPAIGN_ORDER = ("w5-null-t9-blog-v7", "w5-t9", "w5-s1", "w5-t2")  # root ADR 0020 W5: null run, then T9, S1, T2


async def campaign(pool, *, ids: tuple[str, ...] = CAMPAIGN_ORDER, r1_report: Optional[str] = None,
                   instrument: Optional[str] = None, two_step_report: Optional[str] = None) -> dict:
    """The DRY RUN of the whole W5 campaign ($0, READ ONLY): each proof's plan in the D12 order (samples, stage,
    estimate vs its cap, ready or not and why) and the sum of the estimates, plus what the W5 ledger already shows
    spent and the queued / running W5 jobs reserve, against the D12 total. Queues nothing."""
    from shared import settings
    await settings.ensure_fresh(pool)           # before the first load: min samples / caps in force (CIS 419)
    rows = []
    total = 0.0
    committed: Optional[float] = None           # W5 spend + reservations (all workflows), the same in every plan
    for pid in ids:
        try:
            pre = pr.load(pid)
        except pr.PreregRefused as e:
            rows.append({"prereg": pid, "ok": False, "refused": str(e)})
            continue
        p = await plan(pool, pre, r1_report=r1_report, instrument=instrument, two_step_report=two_step_report)
        est = p.get("estimated_usd_max")
        total += float(est or 0.0)
        sp = p.get("spend") or {}
        committed = max(committed or 0.0, float(sp.get("total") or 0.0)
                        + float((sp.get("reserved") or {}).get("total") or 0.0))
        rows.append({"prereg": pid, "workflow": pre.workflow, "writer": pre.writer, "status": p["status"],
                     "stage": spend.W5_STAGE, "golden_set": p.get("golden_set"), "samples": p["samples"],
                     "noise_samples": p["noise_samples"],
                     "judge": _judge_label(pre.doc["judge"]),
                     "estimated_usd_max": est, "r2_estimated_usd_max": p.get("r2_estimated_usd_max"),
                     "judge_call_usd": ((p.get("price_check") or {}).get("judge") or {}).get("call"),
                     "prereg_cap_usd": p["prereg_cap_usd"], "d12_cap_usd": p["d12_cap_usd"],
                     "ready_for_live": p["ready_for_live"], "blocking": p["blocking"]})
    spent = round(committed or 0.0, 4)
    return {"mode": "dry_run", "order": list(ids), "proofs": rows, "estimated_usd_max_total": round(total, 4),
            "spent_or_reserved_usd": spent, "d12_total_cap_usd": pr.total_cap(),
            "fits_d12_total": spent + total <= pr.total_cap() + 1e-9,
            "note": ("estimates only; fits_d12_total = spent_or_reserved_usd + the estimates' sum (a proof that "
                     "already ran still counts by its plan's estimate); the measured cost comes from the ledger after "
                     "each run (runner report)")}


async def run_live(pool, pre: pr.Prereg, *, r1_report: str, instrument: Optional[str], cap_usd: float,
                   confirm: Optional[str], requested_by: str, enqueue=None,
                   two_step_report: Optional[str] = None) -> dict:
    """The paid run: refuses (RunRefused, nothing queued) unless every check passes; then queues one R2 job per arm.
    `enqueue(pool, kind, payload, created_by=)`: the job registry's, injectable for tests."""
    pr.require_registered(pre)
    p = await plan(pool, pre, r1_report=r1_report, instrument=instrument, cap_usd=cap_usd,
                   two_step_report=two_step_report)
    pre = await resolved(pool, pre)              # the sample the plan checked (S283: a sample rule)
    gate = spend.refusals(workflow=pre.workflow, prereg_cap=float(pre.doc["cap_usd"]), cap_usd=cap_usd,
                          confirm=confirm, spent_workflow=p["spend"]["workflow"], spent_total=p["spend"]["total"],
                          estimate_usd=p["estimated_usd_max"], reserved_workflow=p["spend"]["reserved"]["workflow"],
                          reserved_total=p["spend"]["reserved"]["total"])
    blocking = list(dict.fromkeys(p["blocking"] + gate))
    if blocking:
        raise RunRefused("; ".join(blocking))
    if enqueue is None:
        import services.jobs.writing_memory_job  # noqa: F401 — registers the kinds
        from shared.jobs.registry import enqueue
    from services.jobs.writing_memory_job import R2
    from services.writing_memory.r2 import payload_of
    queued = {}
    for arm, conf in arm_confs(pre, caps=p["arm_caps"], triggered_by=f"w5:{requested_by}").items():
        conf.validate()
        # one job per (pre-registration, sha, arm, attempt): a concurrent second start computes the same key and
        # gets the first one's job back instead of a second paid run
        key = idempotency_key(pre, arm, p["jobs"]["attempts"].get(arm, 0) + 1)
        job_id, created = await enqueue(pool, R2, payload_of(conf), created_by=conf.triggered_by,
                                        idempotency_key=key)
        queued[arm] = {"job_id": job_id, "created": created, "cap_usd": conf.cap_usd, "idempotency_key": key}
    return {"prereg": pre.id, "sha256": pre.sha256, "queued": queued, "estimated_usd_max": p["estimated_usd_max"],
            "cap_usd": cap_usd, "sample_ids": list(pre.doc.get("sample_ids") or [])}


def _advisory(pre: pr.Prereg) -> Optional[str]:
    """Why the verdict is advisory (a claim instrument that did not pass every AA-1048 bar), else None."""
    from services.writer_proof.instrument import ADVISORY
    iid = (pre.doc.get("judge") or {}).get("instrument")
    return ADVISORY.get(iid) if iid else None


def idempotency_key(pre: pr.Prereg, arm: str, attempt: int) -> str:
    """The job key of one arm's run. `attempt` = jobs ever queued for that arm + 1: a run refused at $0 (no result)
    may be started again, never two at once. Pure."""
    return f"w5:{pre.id}:{pre.sha256[:16]}:{arm}:{attempt}"


def counted_runs(prior: list[dict]) -> tuple[dict[str, str], list[dict]]:
    """({arm: run id}, reruns): the FIRST run with results of each arm decides (oldest first, PREREG_RUNS_SQL order);
    any later run of the same arm with results is a rerun, reported and never counted. Pure."""
    counted: dict[str, str] = {}
    reruns: list[dict] = []
    for r in prior:
        arm = r.get("arm")
        if not arm or int(r.get("results") or 0) <= 0:
            continue
        if arm in counted:
            reruns.append({"run_id": r["run_id"], "arm": arm, "results": int(r["results"])})
        else:
            counted[arm] = r["run_id"]
    return counted, reruns


async def report(pool, pre: pr.Prereg) -> dict:
    """The verdict of a proof from its runs (READ ONLY): refused when the file changed after its first result."""
    from services.writing_memory import r2
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            prior = [dict(r) for r in await conn.fetch(r2.PREREG_RUNS_SQL, pre.id)]
            runs, ledger, results = await cost_report.load(conn, prereg_id=pre.id, run_ids=[])
            details = {r["run_id"]: await conn.fetchval(
                "SELECT details FROM shared.prompt_eval_runs WHERE id = $1::uuid", r["run_id"]) for r in runs}
            first_b = counted_runs(prior)[0].get("B")
            set_id = await conn.fetchval("SELECT golden_set_id::text FROM shared.prompt_eval_runs WHERE id = $1::uuid",
                                         first_b) if first_b else None
    lock = pr.lock_problem(pre, prior)
    if lock:
        return {"prereg": pre.id, "ok": False, "refused": lock}
    latest, reruns = counted_runs(prior)                 # the first run with results of each arm
    if "B" not in latest:
        return {"prereg": pre.id, "ok": False, "refused": "no B run with results yet", "runs": len(runs)}
    pre = await resolved(pool, pre, set_id=set_id)       # S283: a sample rule, on the B run's golden version
    rows = {rid: [x for x in results if x["run_id"] == rid] for rid in latest.values()}
    det = details.get(latest["B"])
    det = json.loads(det) if isinstance(det, str) else (det or {})
    verdict = decision.decide(pre.doc, rows[latest["B"]], run_details=det,
                              a_prime_rows=rows.get(latest.get("A_prime"), None) if "A_prime" in latest else None)
    cost = cost_report.report(runs, ledger, results)
    computed = decision.computed_metrics(rows[latest["B"]], cost.pop("per_result"))
    return {"prereg": pre.id, "sha256": pre.sha256, "ok": True, "runs": latest, "reruns": reruns,
            "rerun_note": ("runs after the first one of an arm are reported, never counted: the verdict is the first "
                           "run's") if reruns else None,
            "decision": verdict,
            "computed": computed, "targets": decision.targets(pre.doc, computed), "cost": cost,
            "judge": pre.doc["judge"], "advisory": _advisory(pre),
            "d12": {"workflow_cap_usd": pr.workflow_cap(pre.workflow), "total_cap_usd": pr.total_cap()}}


async def draft(pool, pre: pr.Prereg, *, n: Optional[int], noise: int) -> dict:
    from services.writing_memory import r2
    store = r2.PgStore(pool)
    gs = await store.golden_set()
    if gs is None:
        raise RunRefused("no active golden set")
    items = await store.items(gs["set_id"], pre.writer, None)
    if all(it.get("eval_split") is None for it in items):
        raise RunRefused("the active golden set has no split rows: run `python -m services.writer_proof.splits` "
                         "(DRY RUN, then --apply) first")
    return pr.draft(pre.doc, items=items, golden_version=gs["version"], n=n, noise=noise)


def _parse(argv=None):
    ap = argparse.ArgumentParser(prog="python -m services.writer_proof.runner", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "draft", "run", "report", "campaign"):
        sp = sub.add_parser(name)
        if name != "campaign":
            sp.add_argument("--prereg", required=True, help="pre-registration id (prereg/<id>.json) or path")
        if name in ("plan", "run", "campaign"):
            sp.add_argument("--r1-report", help="the R1 report JSON with votes (power check)")
            sp.add_argument("--instrument", help="R1 instrument id (default: the report's recommended one)")
            sp.add_argument("--two-step-report", help="S283: the two-step gated report (a claim instrument judge)")
            if name != "campaign":
                sp.add_argument("--cap-usd", type=float)
        if name == "draft":
            sp.add_argument("--n", type=int)
            sp.add_argument("--noise", type=int, default=0)
        if name == "run":
            sp.add_argument("--live", action="store_true", help="queue the paid runs (without it: the plan only)")
            sp.add_argument("--confirm", help=f"type {spend.CONFIRM_TOKEN} after the tech lead's own yes")
            sp.add_argument("--requested-by", default="cli")
    return ap.parse_args(argv)


async def _main(argv=None) -> int:
    import asyncpg

    from shared import settings
    from shared.secrets import get_database_url
    a = _parse(argv)
    pre = None
    pool = await asyncpg.create_pool(get_database_url().strip(), ssl="require", min_size=1, max_size=2)
    try:
        # an exec process runs no settings refresher: read the values in force BEFORE load() checks the minimum
        # samples and the cap against them (else it would check the compiled defaults)
        await settings.ensure_fresh(pool)
        if a.cmd != "campaign":
            pre = pr.load(a.prereg)
        if a.cmd == "campaign":
            out: dict = await campaign(pool, r1_report=a.r1_report, instrument=a.instrument,
                                       two_step_report=a.two_step_report)
        elif a.cmd == "plan" or (a.cmd == "run" and not a.live):
            out = await plan(pool, pre, r1_report=a.r1_report, instrument=a.instrument, cap_usd=a.cap_usd,
                             two_step_report=a.two_step_report)
            out["mode"] = "dry_run"
        elif a.cmd == "run":
            out = {"mode": "live", **await run_live(pool, pre, r1_report=a.r1_report, instrument=a.instrument,
                                                    cap_usd=a.cap_usd, confirm=a.confirm,
                                                    requested_by=a.requested_by,
                                                    two_step_report=a.two_step_report)}
        elif a.cmd == "draft":
            out = await draft(pool, pre, n=a.n, noise=a.noise)
        else:
            out = await report(pool, pre)
    except (RunRefused, pr.PreregRefused) as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    finally:
        await pool.close()
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
