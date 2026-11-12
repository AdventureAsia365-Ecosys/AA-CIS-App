"""AA-1051 — the W5 proof runner: the eval null run and the T9 / S1 / T2 writer proofs under their pre-registration and
the D12 caps (root ADR 0020; S271 audit W5; S275 decision "code eval part first").

A proof is one or two R2 runs (services/writing_memory/r2.py) on the pre-registered fixed sample of the GATE side,
with the pinned judge, logged under stage adhoc_aa1051:
  B        the sample, candidate = the current writers (or the pre-registered release), control A = the stored output
           re-judged at $0 writer cost (release 0's frozen golden text);
  A_prime  only with noise_sample_ids: the same configuration again on the noise sample (run-to-run noise).
The null run is the T9 blog workflow with no candidate change (B == A by configuration): it must come out
non_inferior with every item paired, one judge and no hard stop 1, or the eval itself is not stable enough to judge a
real candidate.

Commands (DRY RUN unless `run --live`; every command prints one JSON document, ids and counts only):

  python -m services.writer_proof.runner plan   --prereg w5-null-t9-blog [--r1-report <file> [--instrument <id>]]
  python -m services.writer_proof.runner draft  --prereg w5-null-t9-blog [--n 12] [--noise 0]
  python -m services.writer_proof.runner run    --prereg w5-null-t9-blog --r1-report <file> --cap-usd 5 \\
                                                --live --confirm CONFIRM [--requested-by <who>]
  python -m services.writer_proof.runner report --prereg w5-null-t9-blog

plan    reads the DB (READ ONLY): the pre-registration (valid, registered, golden version = the active one), the
        sample on the GATE side, each arm's R2 estimate (controls, calibration on CAL, judge family, caps), the W5
        spend so far, the lock (no earlier result under another sha), and the power check when an R1 report is given.
draft   fills a draft's sample from the GATE side (seeded hash, no data-driven choice) and prints it; a person saves it
        as prereg/<id>.json with status registered in a reviewed PR. Nothing is written.
run     everything `plan` checks, plus: a registered file, an adequate power check, the spend gate (--confirm CONFIRM
        and a cap within the pre-registered and D12 caps and the W5 totals) and an estimate within the cap. Then it
        queues one writing_memory_r2 job per arm (the paid work runs on the worker, never in an exec session). The
        lane that built this ran DRY RUN only.
report  the runs of the pre-registration: the lock check, the pre-registered verdict (decision.py), the targets, and
        cost / requests per object from the ledger (cost_report.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Optional

from services.writer_proof import cost_report, decision, power, spend
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
            eval_split=doc["eval_split"], sample_ids=ids, prereg_id=pre.id, prereg_sha256=pre.sha256,
            prereg_workflow=pre.workflow, prereg_arm=arm)
    return out


def _power(pre: pr.Prereg, r1_report: Optional[str], instrument: Optional[str]) -> dict:
    if not r1_report:
        return {"checked": False, "why": "no R1 report given (--r1-report): the power check needs the R1 votes"}
    try:
        with open(r1_report, encoding="utf-8") as f:
            rep = json.load(f)
        return {"checked": True, **power.check_prereg(pre, power.votes_of(rep, instrument))}
    except (OSError, ValueError, power.PowerRefused) as e:
        return {"checked": False, "why": str(e)}


async def plan(pool, pre: pr.Prereg, *, r1_report: Optional[str] = None, instrument: Optional[str] = None,
               cap_usd: Optional[float] = None) -> dict:
    """The DRY RUN of a proof ($0, reads only). `blocking` lists what would refuse a live run (besides --confirm)."""
    from services.writing_memory import r2
    blocking: list[str] = []
    out: dict[str, Any] = {"prereg": pre.id, "sha256": pre.sha256, "workflow": pre.workflow, "writer": pre.writer,
                           "status": pre.doc.get("status"), "samples": len(pre.doc.get("sample_ids") or []),
                           "noise_samples": len(pre.doc.get("noise_sample_ids") or []),
                           "d12_cap_usd": pr.WORKFLOWS[pre.workflow]["cap_usd"], "prereg_cap_usd": pre.doc["cap_usd"]}
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
            blocking.extend(f"{arm}: {w}" for w in est["would_refuse"] if not w.startswith("estimated "))
            out["arms"][arm] = est
    else:
        blocking.append("no sample ids")
    total_est = round(sum(estimates.values()), 4) if estimates else None
    out["estimated_usd_max"] = total_est
    gate_cap = cap_usd if cap_usd is not None else pre.doc["cap_usd"]
    out["spend_gate_preview"] = spend.refusals(
        workflow=pre.workflow, prereg_cap=float(pre.doc["cap_usd"]), cap_usd=gate_cap, confirm=spend.CONFIRM_TOKEN,
        spent_workflow=out["spend"]["workflow"], spent_total=out["spend"]["total"], estimate_usd=total_est)
    blocking.extend(out["spend_gate_preview"])
    out["arm_caps"] = spend.split_cap(float(gate_cap), estimates) if estimates else {}
    out["power"] = _power(pre, r1_report, instrument)
    if not out["power"].get("checked"):
        blocking.append(f"power not checked: {out['power'].get('why')}")
    elif not out["power"]["adequate"]:
        blocking.append(f"underpowered: power {out['power']['power_at_zero']} < {out['power']['min_power']} "
                        f"(n {out['power']['n_items']}, needs {out['power']['n_needed']})")
    out["blocking"] = list(dict.fromkeys(blocking))
    out["ready_for_live"] = not out["blocking"]
    return out


async def run_live(pool, pre: pr.Prereg, *, r1_report: str, instrument: Optional[str], cap_usd: float,
                   confirm: Optional[str], requested_by: str, enqueue=None) -> dict:
    """The paid run: refuses (RunRefused, nothing queued) unless every check passes; then queues one R2 job per arm.
    `enqueue(pool, kind, payload, created_by=)`: the job registry's, injectable for tests."""
    pr.require_registered(pre)
    p = await plan(pool, pre, r1_report=r1_report, instrument=instrument, cap_usd=cap_usd)
    gate = spend.refusals(workflow=pre.workflow, prereg_cap=float(pre.doc["cap_usd"]), cap_usd=cap_usd,
                          confirm=confirm, spent_workflow=p["spend"]["workflow"], spent_total=p["spend"]["total"],
                          estimate_usd=p["estimated_usd_max"])
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
        job_id, created = await enqueue(pool, R2, payload_of(conf), created_by=conf.triggered_by)
        queued[arm] = {"job_id": job_id, "created": created, "cap_usd": conf.cap_usd}
    return {"prereg": pre.id, "sha256": pre.sha256, "queued": queued, "estimated_usd_max": p["estimated_usd_max"],
            "cap_usd": cap_usd}


async def report(pool, pre: pr.Prereg) -> dict:
    """The verdict of a proof from its runs (READ ONLY): refused when the file changed after its first result."""
    from services.writing_memory import r2
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            prior = [dict(r) for r in await conn.fetch(r2.PREREG_RUNS_SQL, pre.id)]
            runs, ledger, results = await cost_report.load(conn, prereg_id=pre.id, run_ids=[])
            details = {r["run_id"]: await conn.fetchval(
                "SELECT details FROM shared.prompt_eval_runs WHERE id = $1::uuid", r["run_id"]) for r in runs}
    lock = pr.lock_problem(pre, prior)
    if lock:
        return {"prereg": pre.id, "ok": False, "refused": lock}
    latest = {}
    for r in runs:                                       # the newest run of each arm
        arm = (r.get("prereg") or {}).get("arm")
        if arm:
            latest[arm] = r["run_id"]
    if "B" not in latest:
        return {"prereg": pre.id, "ok": False, "refused": "no B run yet", "runs": len(runs)}
    rows = {rid: [x for x in results if x["run_id"] == rid] for rid in latest.values()}
    det = details.get(latest["B"])
    det = json.loads(det) if isinstance(det, str) else (det or {})
    verdict = decision.decide(pre.doc, rows[latest["B"]], run_details=det,
                              a_prime_rows=rows.get(latest.get("A_prime"), None) if "A_prime" in latest else None)
    cost = cost_report.report(runs, ledger, results)
    computed = decision.computed_metrics(rows[latest["B"]], cost.pop("per_result"))
    return {"prereg": pre.id, "sha256": pre.sha256, "ok": True, "runs": latest, "decision": verdict,
            "computed": computed, "targets": decision.targets(pre.doc, computed), "cost": cost,
            "d12": {"workflow_cap_usd": pr.WORKFLOWS[pre.workflow]["cap_usd"], "total_cap_usd": pr.TOTAL_CAP_USD}}


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
    for name in ("plan", "draft", "run", "report"):
        sp = sub.add_parser(name)
        sp.add_argument("--prereg", required=True, help="pre-registration id (prereg/<id>.json) or path")
        if name in ("plan", "run"):
            sp.add_argument("--r1-report", help="the R1 report JSON with votes (power check)")
            sp.add_argument("--instrument", help="R1 instrument id (default: the report's recommended one)")
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

    from shared.secrets import get_database_url
    a = _parse(argv)
    try:
        pre = pr.load(a.prereg)
    except pr.PreregRefused as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    pool = await asyncpg.create_pool(get_database_url().strip(), ssl="require", min_size=1, max_size=2)
    try:
        if a.cmd == "plan" or (a.cmd == "run" and not a.live):
            out: dict = await plan(pool, pre, r1_report=a.r1_report, instrument=a.instrument, cap_usd=a.cap_usd)
            out["mode"] = "dry_run"
        elif a.cmd == "run":
            out = {"mode": "live", **await run_live(pool, pre, r1_report=a.r1_report, instrument=a.instrument,
                                                    cap_usd=a.cap_usd, confirm=a.confirm,
                                                    requested_by=a.requested_by)}
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
