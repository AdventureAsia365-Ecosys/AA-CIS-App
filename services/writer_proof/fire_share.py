"""AA-1051 (S285 C1) — measure the R1 fire share of a writer on a W5 pre-registration's fixed sample (the fire step
of the pinned claim instrument alone), so `instrument.FIRE_SHARE` can be set from a measurement.

Why. A proof judged by a claim instrument is priced per judged output (price.instrument_check): fields x votes fire
calls + claims x fire share x votes x 2 step calls. The fire share is a measurement per writer (`instrument.FIRE_SHARE`,
S282 run 5b24602e: t9_blog, t9_social, t2); a writer without one is planned at 1.0 (every claim reaches both steps).
For the S1 writer (a1) that put the `w5-s1` DRY plan at 15.078 USD against its 8 USD cap (S285). The tech lead approved
(S285 C1) a small paid run of the fire step alone on the proof's own outputs to measure it first.

What it runs. The fire step only, exactly as `instrument.judge_output` runs it (the R1 extraction instrument the fresh
pools were fired with, `fresh_pool.FIRE_CANDIDATE`, prompt v1, on the pinned candidate; one call per field per vote,
`votes` of the pinned registration), on the stored control output (arm A) of every sample item of the
pre-registration, read through `eval_judge.evidence_view` as the run reads it. No step 1 / step 2 call, no writer call.
Each call goes through the Model Gateway (`r1.gateway_call`) and writes one shared.llm_call_log row
(`instrument._log_row`: **billed_fields, role judge, the fire prompt tag), relabelled to the W5 stage adhoc_aa1051
with `quality_signal.eval_run` = this measurement's run id (`call_log.bind_eval_stage`), so the W5 total spend gate
counts it and later plans price the fire step from it (price.INSTRUMENT_CALLS_SQL reads adhoc_aa1051).

Counts. Per output: claims (the instrument's claim unit: every distinct sentence of >= 3 words of each labelled field),
`fired` (vote majority true), `unverified` (no majority: an unsure fire never clears, so the chain sends it on),
`cleared` (majority false). `fire_share` = fired / claims over the complete outputs (the S282 definition of
FIRE_SHARE: fired_count true / claims); `sent_on_share` = (fired + unverified) / claims, what the chain sends to step 1.
The planning value to pin is `planning_share` = sent_on_share rounded up to 3 decimals (>= fire_share; equal when no
claim is unverified; fail closed: a higher share prices the proof higher). Output text is never printed: ids and
counts only.

Gate (live). The confirmation token (`--confirm CONFIRM`, after the tech lead's own yes), a cap > 0 that is at most the
workflow's D12 cap (setting, prereg.workflow_cap), the W5 spend so far + the queued / running W5 jobs' caps + this cap
within the D12 total (setting, prereg.total_cap), the estimate within the cap, the pre-registration's golden version
active, a pinned claim instrument in its judge block, the pinned model priced and deployed on its connection, and the
connection enabled, serving role judge and not refused by its provider. The run itself is held per call by the
instrument's call gate (`instrument._Output`): before each call, charged + the worst case of every call in flight + this
call's worst case must fit the cap, else the run stops before that call (an output cut short is not counted).

  python -m services.writer_proof.fire_share --prereg w5-s1                                   DRY RUN ($0, read only)
  python -m services.writer_proof.fire_share --prereg w5-s1 --live --cap-usd 2.5 --confirm CONFIRM [--requested-by x]

Prints one JSON document. Exit 0 = done (or the DRY plan), 2 = refused (nothing called), 3 = stopped at the cap or by
another answerer (the counts of the complete outputs are still printed).
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import math
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from services.writer_proof import prereg as pr
from services.writer_proof import price, spend

# The connection states the gateway refuses a call in (shared.llm_provider_connection.access_state).
REFUSED_ACCESS = ("access_denied", "quota_exhausted", "unconfigured")
CONNECTION_SQL = """
    SELECT connection_key, enabled, access_state, served_roles
      FROM shared.llm_provider_connection
     WHERE connection_key = $1
"""


class FireShareRefused(RuntimeError):
    """The measurement may not start: nothing was called."""


@dataclass(frozen=True)
class Sample:
    sample_id: str
    source: str
    output: str


def sample_evidence(pre: pr.Prereg, items: list[dict], shared_inputs: Optional[dict]) -> tuple[list[Sample], list]:
    """(the judgeable stored control outputs of the pre-registration's sample, in its sample order; the sample ids left
    out with why): the evidence the judge reads (eval_judge.evidence_view), an item without a stored control or over
    the evidence cap left out. Pure."""
    from services.writing_memory.eval_judge import InputOverCap, evidence_view
    by_id = {it.get("sample_id"): it for it in items}
    out, skipped = [], []
    for sid in pre.doc.get("sample_ids") or []:
        it = by_id.get(sid)
        if it is None:
            skipped.append({"sample_id": sid, "why": "not in the golden version"})
            continue
        if it.get("control_output") is None:
            skipped.append({"sample_id": sid, "why": "no stored control output"})
            continue
        try:
            ev = evidence_view(pre.writer, it.get("input") or {}, it["control_output"], shared_inputs)
        except InputOverCap:
            skipped.append({"sample_id": sid, "why": "over the eval judge's evidence cap"})
            continue
        out.append(Sample(sid, ev.source, ev.output))
    return out, skipped


def connection_problems(row: Optional[dict], connection: str) -> list[str]:
    """Why the pinned connection would not answer a judge call (the route check of a paid DRY RUN). Pure."""
    if row is None:
        return [f"connection {connection}: no row in shared.llm_provider_connection"]
    p = []
    if not row.get("enabled"):
        p.append(f"connection {connection}: disabled")
    if row.get("access_state") in REFUSED_ACCESS:
        p.append(f"connection {connection}: access_state {row['access_state']}")
    roles = row.get("served_roles")
    if roles is not None and "judge" not in list(roles):
        p.append(f"connection {connection}: does not serve role judge (served_roles {sorted(roles)})")
    return p


def planned(writer: str, fire, votes: int, samples: list[Sample], *, attempts: int) -> list[dict]:
    """Per output: {sample_id, claims (r1.Items), fields, calls (all votes), worst_usd (all votes)}. Pure."""
    from services.ground_truth import r1
    from services.writer_proof import instrument as wi
    out = []
    for s in samples:
        claims = wi.output_claims(writer, s.source, s.output)
        groups = r1.calls(claims, fire) if claims else []
        worst = sum(r1.worst_call_cost(fire, user, attempts) for user, _g in groups) * votes
        out.append({"sample_id": s.sample_id, "claims": claims, "fields": len(groups), "calls": len(groups) * votes,
                    "worst_usd": round(worst, 6)})
    return out


def plan_of(pre: pr.Prereg, inputs: dict, *, attempts: int) -> dict:
    """The DRY RUN record of a measurement from `read_inputs`' record (counts and ids only) plus `_planned` (the claim
    items per output, for `run`; never printed) and `blocking`. Reads the settings registry (the caps in force);
    otherwise pure."""
    from services.writing_memory import config as cfg
    blocking: list[str] = list(inputs.get("blocking") or [])
    p, fire = inputs.get("pinned"), inputs.get("fire")
    plan = planned(pre.writer, fire, p.votes, inputs["samples"], attempts=attempts) if p is not None else []
    calls = sum(o["calls"] for o in plan)
    cost = None
    if p is not None:
        users = {"fire": _first_user(fire, plan[0]["claims"]) if plan else ""}
        cost = price.step_costs(p, {"fire": fire}, inputs.get("measured") or {}, min_samples=cfg.R2_MIN_COST_SAMPLES,
                                sample_user=users)["fire"]
    estimate = round(calls * cost["usd"] * cfg.R2_ESTIMATE_MARGIN, 4) if cost else None
    if not plan:
        blocking.append("no judgeable control output in the sample: nothing to measure")
    claims = sum(len(o["claims"]) for o in plan)
    return {"prereg": pre.id, "writer": pre.writer, "golden_set": inputs.get("golden_set"),
            "instrument": getattr(p, "id", None), "fire_instrument": getattr(fire, "id", None),
            "fire_prompt": (p.tags["fire"] if p is not None else None), "votes": getattr(p, "votes", None),
            "route": inputs.get("route"),
            "outputs": [{k: (len(v) if k == "claims" else v) for k, v in o.items()} for o in plan],
            "skipped": inputs.get("skipped") or [], "totals": {"outputs": len(plan), "claims": claims, "calls": calls},
            "fire_call": cost, "estimated_usd": estimate, "margin": cfg.R2_ESTIMATE_MARGIN,
            "worst_case_usd": round(sum(o["worst_usd"] for o in plan), 4),
            "spend": inputs.get("spend"), "d12_workflow_cap_usd": pr.workflow_cap(pre.workflow),
            "d12_total_cap_usd": pr.total_cap(), "blocking": list(dict.fromkeys(blocking)), "_planned": plan}


def _first_user(fire, claims: list) -> str:
    from services.ground_truth import r1
    groups = r1.calls(claims, fire)
    return groups[0][0] if groups else ""


def refusals(plan: dict, *, workflow: str, cap_usd: Any, confirm: Optional[str]) -> list[str]:
    """Every reason the live measurement may not start (empty = it may). Reads the settings registry (the D12 caps in
    force: call settings.ensure_fresh first in a process without the refresher); otherwise pure."""
    out = list(plan.get("blocking") or [])
    if confirm != spend.CONFIRM_TOKEN:
        out.append(f"no confirmation: a paid measurement needs --confirm {spend.CONFIRM_TOKEN} after the tech lead's "
                   "own yes")
    cap = spend._num(cap_usd)
    d12 = pr.workflow_cap(workflow)
    if cap is None or cap <= 0:
        out.append("no cap: a paid measurement needs --cap-usd > 0")
        cap = None
    elif cap > d12:
        out.append(f"cap {cap} is above the D12 cap {d12} USD of {workflow}")
    sp = plan.get("spend") or {}
    st, rt = spend._num(sp.get("total")), spend._num((sp.get("reserved") or {}).get("total"))
    if st is None or rt is None:
        out.append("the W5 ledger spend or the queued jobs' caps could not be read as a number: refused (fail closed)")
    elif cap is not None and st + rt + cap > pr.total_cap() + 1e-9:
        out.append(f"W5 already spent {st:.4f} USD and its queued / running jobs reserve {rt:.4f}; + cap {cap} "
                   f"passes the D12 total {pr.total_cap()}")
    est = spend._num(plan.get("estimated_usd"))
    if est is None:
        out.append("no cost estimate")
    elif cap is not None and est > cap:
        out.append(f"the plan's estimate {est} USD is above the cap {cap}")
    return list(dict.fromkeys(out))


def output_counts(sample_id: str, claims: list, votes: dict[str, list], *, complete: bool, calls: int) -> dict:
    """The fire counts of one output from its votes (claim_hash -> [True | False | None] per vote). Pure."""
    from services.ground_truth import r1
    majorities = [r1.majority(votes.get(i.claim_hash) or []) for i in claims]
    return {"sample_id": sample_id, "claims": len(claims), "fired": sum(1 for m in majorities if m is True),
            "unverified": sum(1 for m in majorities if m is None),
            "cleared": sum(1 for m in majorities if m is False), "calls": calls, "complete": complete}


def _ceil3(x: float) -> float:
    return math.ceil(round(x * 1000, 6)) / 1000


def totals(outputs: list[dict]) -> dict:
    """The shares over the COMPLETE outputs (an output cut short by the cap is not counted). Pure."""
    done = [o for o in outputs if o["complete"]]
    claims = sum(o["claims"] for o in done)
    fired = sum(o["fired"] for o in done)
    unverified = sum(o["unverified"] for o in done)
    out = {"outputs_complete": len(done), "outputs_cut": len(outputs) - len(done), "claims": claims, "fired": fired,
           "unverified": unverified, "cleared": sum(o["cleared"] for o in done)}
    if claims:
        out["fire_share"] = round(fired / claims, 4)
        out["sent_on_share"] = round((fired + unverified) / claims, 4)
        out["planning_share"] = _ceil3((fired + unverified) / claims)
    else:
        out["fire_share"] = out["sent_on_share"] = out["planning_share"] = None
    return out


async def read_inputs(pool, pre: pr.Prereg) -> dict:
    """Everything the plan reads (READ ONLY): the golden version and its items, the pinned instrument, the measured cost
    of the fire tag, the catalog / deployment / connection rows of the pin, the W5 spend and the queued W5 jobs."""
    from services.writer_proof import instrument as wi
    from services.writing_memory import r2
    blocking: list[str] = []
    judge = pre.doc.get("judge") or {}
    store = r2.PgStore(pool, stages=(spend.W5_STAGE,))
    gs = await store.golden_set()
    want = pre.doc.get("golden_set") or {}
    if gs is None:
        blocking.append("no active golden set")
    elif (want.get("name"), want.get("version")) != (gs["name"], gs["version"]):
        blocking.append(f"pre-registered on {want.get('name')}@{want.get('version')}, the active set is "
                        f"{gs['name']}@{gs['version']}")
    items = await store.items(gs["set_id"], pre.writer, None) if gs else []
    samples, skipped = sample_evidence(pre, items, (gs or {}).get("shared_inputs"))
    p = fire = None
    if not judge.get("instrument"):
        blocking.append(f"{pre.id} names no claim instrument (judge.instrument): there is no fire step to measure")
    else:
        try:
            p = wi.pinned(str(judge["instrument"]))
            fire = wi.fire_instrument()
        except wi.InstrumentRefused as e:
            blocking.append(str(e))
    out: dict[str, Any] = {"golden_set": None if gs is None else f"{gs['name']}@{gs['version']}", "samples": samples,
                           "skipped": skipped, "pinned": p, "fire": fire, "measured": {}, "route": None}
    async with pool.acquire() as conn:
        if p is not None:
            cat = await conn.fetchrow(price.CATALOG_SQL, p.model_key)
            dep = await conn.fetchrow(price.DEPLOYMENT_SQL, p.model_key, p.connection)
            con = await conn.fetchrow(CONNECTION_SQL, p.connection)
            rows = await conn.fetch(price.INSTRUMENT_CALLS_SQL, list(price.INSTRUMENT_STAGES),
                                    price.log_models(p.model_key), [p.tags["fire"]])
            out["measured"] = {r["tag"]: {"calls": r["calls"], "mean_usd": r["mean_usd"]} for r in rows}
            con = dict(con) if con else None
            blocking += price.catalog_problems(p.model_key, p.connection, dict(cat) if cat else None,
                                               dict(dep) if dep else None)
            blocking += connection_problems(con, p.connection)
            out["route"] = {"model_key": p.model_key, "connection": p.connection, "role": "judge",
                            "enabled": (con or {}).get("enabled"), "access_state": (con or {}).get("access_state"),
                            "served_roles": list((con or {}).get("served_roles") or []) or None}
        out["spend"] = await spend.read_spend(conn, pre.workflow)
        jobs = await spend.read_jobs(conn, pre.id)
    out["spend"]["reserved"] = spend.reserved(jobs["active"], pre.workflow)
    out["blocking"] = blocking
    return out


async def _attempts(fire, caller: Optional[Callable]) -> int:
    from services.writer_proof import instrument as wi
    return await wi._attempts({"fire": fire}, caller)


async def dry_run(pool, pre: pr.Prereg, *, inputs: Optional[dict] = None, caller: Optional[Callable] = None) -> dict:
    """The $0 plan: planned calls, the estimate and what would refuse a live run with no cap given."""
    inputs = inputs if inputs is not None else await read_inputs(pool, pre)
    attempts = await _attempts(inputs["fire"], caller) if inputs.get("fire") is not None else 1
    plan = plan_of(pre, inputs, attempts=attempts)
    plan.pop("_planned")
    plan["mode"] = "dry_run"
    return plan


async def run(pool, pre: pr.Prereg, *, cap_usd: Any, confirm: Optional[str], requested_by: str,
              inputs: Optional[dict] = None, caller: Optional[Callable] = None, log: Optional[Callable] = None,
              ledger: Optional[Callable] = None) -> dict:
    """The paid measurement: refuses (FireShareRefused, nothing called) unless every check passes; then fires each
    output in sample order through one call gate at `cap_usd`. `caller`, `log` and `ledger(pool, run_id, stages)` are
    injectable for tests."""
    from services.ground_truth import r1
    from services.writer_proof import instrument as wi
    from shared.llm_client import call_log
    inputs = inputs if inputs is not None else await read_inputs(pool, pre)
    attempts = await _attempts(inputs["fire"], caller) if inputs.get("fire") is not None else 1
    plan = plan_of(pre, inputs, attempts=attempts)
    why = refusals(plan, workflow=pre.workflow, cap_usd=cap_usd, confirm=confirm)
    if why:
        raise FireShareRefused("; ".join(why))
    p, fire = inputs["pinned"], inputs["fire"]
    run_id = str(uuid.uuid4())
    signal = {"app": "cis", "issue": "AA-1051", "run_id": run_id, "measure": "fire_share", "prereg": pre.id,
              "arm": "A", "w5_instrument": p.id, "judge_pin": p.candidate, "requested_by": str(requested_by)[:64]}
    gate = wi._Output(p, {"fire": fire}, cap_usd=float(cap_usd), base_spend=0.0, attempts=attempts,
                      caller=caller or r1.gateway_call, log=log or functools.partial(wi._log_row, pool),
                      signal=signal)
    outputs = []
    with call_log.bind_eval_stage(spend.W5_STAGE, run_id):
        for o in plan["_planned"]:
            gate.signal = {**signal, "item_id": o["sample_id"]}
            before = gate.calls
            votes = await wi._fire(gate, o["claims"]) if o["claims"] else {}
            complete = not (gate.stopped or gate.mismatch)
            outputs.append(output_counts(o["sample_id"], o["claims"], votes, complete=complete,
                                         calls=gate.calls - before))
            if not complete:
                break
    spent_ledger = await (ledger or wi.run_spend)(pool, run_id, (spend.W5_STAGE,))
    tot = totals(outputs)
    out = {"mode": "live", "ok": not (gate.stopped or gate.mismatch), "run_id": run_id, "prereg": pre.id,
           "writer": pre.writer, "golden_set": plan["golden_set"], "instrument": p.id, "fire_prompt": p.tags["fire"],
           "votes": p.votes, "cap_usd": float(cap_usd), "estimated_usd": plan["estimated_usd"],
           "planned_outputs": len(plan["_planned"]), "outputs": outputs, "totals": tot,
           "spend": {"charged_usd": round(gate.charged, 6), "ledger_usd": round(float(spent_ledger or 0.0), 6),
                     "calls": gate.calls, "stage": spend.W5_STAGE},
           "stopped": gate.stopped, "identity_mismatch": gate.mismatch}
    if out["ok"] and tot.get("planning_share") is not None:
        out["fire_share_entry"] = {
            "instrument": p.id, "writer": pre.writer, "share": tot["planning_share"],
            "source": (f"R1 fire {run_id[:8]} on the {pre.id} GATE controls, {plan['golden_set']} (S285): "
                       f"{tot['fired']} of {tot['claims']} {pre.writer} claims")}
    return out


def _parse(argv=None):
    ap = argparse.ArgumentParser(prog="python -m services.writer_proof.fire_share", description=__doc__.split("\n")[0])
    ap.add_argument("--prereg", required=True, help="pre-registration id (prereg/<id>.json) or path")
    ap.add_argument("--live", action="store_true", help="make the paid calls (without it: the $0 plan only)")
    ap.add_argument("--cap-usd", type=float, help="the hard cap of the paid run (USD)")
    ap.add_argument("--confirm", help=f"type {spend.CONFIRM_TOKEN} after the tech lead's own yes")
    ap.add_argument("--requested-by", default="cli")
    return ap.parse_args(argv)


async def _main(argv=None) -> int:
    import asyncpg

    from shared import settings
    from shared.secrets import get_database_url
    a = _parse(argv)
    pool = await asyncpg.create_pool(get_database_url().strip(), ssl="require", min_size=1, max_size=2)
    try:
        await settings.ensure_fresh(pool)       # the D12 caps in force before load() checks the file against them
        pre = pr.load(a.prereg)
        if a.live:
            out = await run(pool, pre, cap_usd=a.cap_usd, confirm=a.confirm, requested_by=a.requested_by)
        else:
            out = await dry_run(pool, pre)
    except (FireShareRefused, pr.PreregRefused) as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    finally:
        await pool.close()
    print(json.dumps(out, indent=1, default=str))
    return 0 if out.get("mode") == "dry_run" or out.get("ok") else 3


__all__ = ["CONNECTION_SQL", "FireShareRefused", "REFUSED_ACCESS", "Sample", "connection_problems", "dry_run",
           "output_counts", "plan_of", "planned", "read_inputs", "refusals", "run", "sample_evidence", "totals"]


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
