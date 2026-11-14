"""AA-1051 — the spend gate of a W5 proof (root ADR 0020 D12: eval null run <= $5, T9 <= $15, S1 <= $8, T2 <= $5,
<= $33 in total, each run after the tech lead's own yes).

A live proof starts only when ALL hold (`refusals` returns every reason it may not):
  - the operator typed the confirmation token (`--confirm CONFIRM`): the tech lead's yes is recorded by the person
    who types it, never by a default;
  - a cap is given (`--cap-usd`), > 0, <= the pre-registration's cap_usd and <= the workflow's D12 cap;
  - what the workflow already spent (ledger, stage adhoc_aa1051, runs of that workflow) + what its queued or running
    W5 jobs may still spend (their R2 caps, `read_jobs`) + the cap <= the D12 cap;
  - what W5 already spent in total (every adhoc_aa1051 row) + every queued or running W5 job's cap + the cap <= $33;
  - the plan's estimate <= the cap (an estimate over the cap would stop at budget_cap after paying part of it).
The spend is read from shared.llm_call_log (the ledger every gateway call writes), not from the runs' own totals: a
run that died mid-way still counts what it billed. A queued job has billed nothing yet, so its cap is counted as
reserved; a running job counts both its ledger rows so far and its whole cap (an over-count: fail closed). The runner
also refuses a second run of a pre-registration while one of its jobs is queued or running (the R2 kind runs one job
at a time, so a second job would wait in the queue with $0 in the ledger and pass a ledger-only gate).
"""
from __future__ import annotations

import math
from typing import Any, Optional

from services.writer_proof.prereg import total_cap, workflow_cap

W5_STAGE = "adhoc_aa1051"            # = services.writing_memory.config.W5_STAGE (a unit test keeps them equal)
CONFIRM_TOKEN = "CONFIRM"

# The W5 ledger spend: all of it, and the part whose eval run belongs to one workflow (R2 details.prereg.workflow).
SPEND_SQL = """
    SELECT coalesce(sum(l.cost_usd), 0)::float AS total,
           coalesce(sum(l.cost_usd) FILTER (WHERE p.details->'prereg'->>'workflow' = $2), 0)::float AS workflow,
           count(*)::int AS rows
      FROM shared.llm_call_log l
      LEFT JOIN shared.prompt_eval_runs p ON p.id::text = l.quality_signal->>'eval_run'
     WHERE l.stage = $1
"""

# The W5 jobs that may still spend (queued or running writing_memory_r2 jobs of a pre-registration), with their caps.
ACTIVE_JOBS_SQL = """
    SELECT id::text AS job_id, status, payload->>'prereg_id' AS prereg_id, payload->>'prereg_workflow' AS workflow,
           payload->>'prereg_arm' AS arm, (payload->>'cap_usd')::float AS cap_usd
      FROM shared.job
     WHERE kind = $1 AND status IN ('queued', 'running') AND payload ? 'prereg_id'
     ORDER BY created_at, id
"""
# Every job ever queued for one pre-registration, per arm (the attempt number of the next job's idempotency key).
ATTEMPTS_SQL = """
    SELECT payload->>'prereg_arm' AS arm, count(*)::int AS jobs
      FROM shared.job
     WHERE kind = $1 AND payload->>'prereg_id' = $2
     GROUP BY 1
"""
R2_JOB_KIND = "writing_memory_r2"     # = services.jobs.writing_memory_job.R2 (a unit test keeps them equal)
R2_RUN_CAP_USD = 10.0                 # = services.writing_memory.config.R2_RUN_CAP_USD (same test): a job with no cap


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def refusals(*, workflow: str, prereg_cap: float, cap_usd: Any, confirm: Optional[str], spent_workflow: Any,
             spent_total: Any, estimate_usd: Any, reserved_workflow: Any = 0.0, reserved_total: Any = 0.0) -> list[str]:
    """Every reason a live run of `workflow` may not start (empty = it may). `reserved_*`: the caps of the queued or
    running W5 jobs (of the workflow / of all W5). Reads the settings registry (the D12 caps in force, CIS 419: call
    settings.ensure_fresh first in a process without the refresher); otherwise pure."""
    out: list[str] = []
    d12 = workflow_cap(workflow)
    d12_total = total_cap()
    if confirm != CONFIRM_TOKEN:
        out.append(f"no confirmation: a paid W5 run needs --confirm {CONFIRM_TOKEN} after the tech lead's own yes")
    cap = _num(cap_usd)
    if cap is None or cap <= 0:
        out.append("no cap: a paid W5 run needs --cap-usd > 0")
        cap = None
    elif cap > min(prereg_cap, d12):
        out.append(f"cap {cap} is above the pre-registered {prereg_cap} / D12 {d12} USD for {workflow}")
    sw, st = _num(spent_workflow), _num(spent_total)
    rw, rt = _num(reserved_workflow), _num(reserved_total)
    if sw is None or st is None or rw is None or rt is None:
        out.append("the ledger spend or the queued jobs' caps could not be read as a number: refused (fail closed)")
    elif cap is not None:
        if sw + rw + cap > d12 + 1e-9:
            out.append(f"{workflow} already spent {sw:.4f} USD and its queued / running jobs reserve {rw:.4f}; "
                       f"+ cap {cap} passes its D12 cap {d12}")
        if st + rt + cap > d12_total + 1e-9:
            out.append(f"W5 already spent {st:.4f} USD and its queued / running jobs reserve {rt:.4f}; + cap {cap} "
                       f"passes the D12 total {d12_total}")
    est = _num(estimate_usd)
    if est is None:
        out.append("no cost estimate: run the DRY RUN plan first")
    elif cap is not None and est > cap:
        out.append(f"the plan's estimate {est} USD is above the cap {cap}: plan fewer items or a bigger cap")
    return out


async def read_spend(conn, workflow: str) -> dict:
    r = await conn.fetchrow(SPEND_SQL, W5_STAGE, workflow)
    return {"total": r["total"], "workflow": r["workflow"], "rows": r["rows"]}


async def read_jobs(conn, prereg_id: str) -> dict:
    """The queued / running W5 jobs (all pre-registrations) with what they reserve, and the jobs ever queued for
    `prereg_id` per arm."""
    active = []
    for r in await conn.fetch(ACTIVE_JOBS_SQL, R2_JOB_KIND):
        d = dict(r)
        cap = _num(d.get("cap_usd"))
        d["cap_usd"] = R2_RUN_CAP_USD if cap is None else cap      # no readable cap: the R2 hard cap (fail closed)
        active.append(d)
    attempts = {r["arm"]: int(r["jobs"]) for r in await conn.fetch(ATTEMPTS_SQL, R2_JOB_KIND, prereg_id)}
    return {"active": active, "attempts": attempts}


def reserved(active: list[dict], workflow: str) -> dict:
    """{workflow, total}: the caps the queued / running W5 jobs may still spend. Pure."""
    return {"workflow": round(sum(j["cap_usd"] for j in active if j.get("workflow") == workflow), 4),
            "total": round(sum(j["cap_usd"] for j in active), 4)}


def split_cap(cap: float, estimates: dict[str, float]) -> dict[str, float]:
    """The cap of each arm's run: the workflow cap shared in proportion to the arms' estimates (each at least its
    own estimate when the total fits), rounded down to 4 decimals so the sum never passes the cap. Pure."""
    total = sum(max(0.0, e) for e in estimates.values())
    if not estimates:
        return {}
    if total <= 0:
        share = cap / len(estimates)
        return {k: math.floor(share * 10_000) / 10_000 for k in estimates}
    return {k: math.floor(cap * max(0.0, e) / total * 10_000) / 10_000 for k, e in estimates.items()}


__all__ = ["ACTIVE_JOBS_SQL", "ATTEMPTS_SQL", "CONFIRM_TOKEN", "R2_JOB_KIND", "R2_RUN_CAP_USD", "SPEND_SQL", "W5_STAGE",
           "read_jobs", "read_spend", "refusals", "reserved", "split_cap"]
