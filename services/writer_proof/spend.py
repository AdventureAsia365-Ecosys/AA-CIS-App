"""AA-1051 — the spend gate of a W5 proof (root ADR 0020 D12: eval null run <= $5, T9 <= $15, S1 <= $8, T2 <= $5,
<= $33 in total, each run after the tech lead's own yes).

A live proof starts only when ALL hold (`refusals` returns every reason it may not):
  - the operator typed the confirmation token (`--confirm CONFIRM`): the tech lead's yes is recorded by the person
    who types it, never by a default;
  - a cap is given (`--cap-usd`), > 0, <= the pre-registration's cap_usd and <= the workflow's D12 cap;
  - what the workflow already spent (ledger, stage adhoc_aa1051, runs of that workflow) + the cap <= the D12 cap;
  - what W5 already spent in total (every adhoc_aa1051 row) + the cap <= $33;
  - the plan's estimate <= the cap (an estimate over the cap would stop at budget_cap after paying part of it).
The spend is read from shared.llm_call_log (the ledger every gateway call writes), not from the runs' own totals: a
run that died mid-way still counts what it billed.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from services.writer_proof.prereg import TOTAL_CAP_USD, WORKFLOWS

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


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def refusals(*, workflow: str, prereg_cap: float, cap_usd: Any, confirm: Optional[str], spent_workflow: Any,
             spent_total: Any, estimate_usd: Any) -> list[str]:
    """Every reason a live run of `workflow` may not start (empty = it may). Pure."""
    out: list[str] = []
    d12 = WORKFLOWS[workflow]["cap_usd"]
    if confirm != CONFIRM_TOKEN:
        out.append(f"no confirmation: a paid W5 run needs --confirm {CONFIRM_TOKEN} after the tech lead's own yes")
    cap = _num(cap_usd)
    if cap is None or cap <= 0:
        out.append("no cap: a paid W5 run needs --cap-usd > 0")
        cap = None
    elif cap > min(prereg_cap, d12):
        out.append(f"cap {cap} is above the pre-registered {prereg_cap} / D12 {d12} USD for {workflow}")
    sw, st = _num(spent_workflow), _num(spent_total)
    if sw is None or st is None:
        out.append("the ledger spend could not be read as a number: refused (fail closed)")
    elif cap is not None:
        if sw + cap > d12 + 1e-9:
            out.append(f"{workflow} already spent {sw:.4f} USD; + cap {cap} passes its D12 cap {d12}")
        if st + cap > TOTAL_CAP_USD + 1e-9:
            out.append(f"W5 already spent {st:.4f} USD; + cap {cap} passes the D12 total {TOTAL_CAP_USD}")
    est = _num(estimate_usd)
    if est is None:
        out.append("no cost estimate: run the DRY RUN plan first")
    elif cap is not None and est > cap:
        out.append(f"the plan's estimate {est} USD is above the cap {cap}: plan fewer items or a bigger cap")
    return out


async def read_spend(conn, workflow: str) -> dict:
    r = await conn.fetchrow(SPEND_SQL, W5_STAGE, workflow)
    return {"total": r["total"], "workflow": r["workflow"], "rows": r["rows"]}


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


__all__ = ["CONFIRM_TOKEN", "SPEND_SQL", "W5_STAGE", "read_spend", "refusals", "split_cap"]
