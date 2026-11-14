"""AA-1051 (S280) — the catalog price check of a W5 proof plan ($0, reads only, no model call).

Why. `r2.estimate` prices every judge call at `cfg.JUDGE_CALL_ESTIMATE_USD` (a constant measured on the cheap default
judge). A proof pins its judge (D7, `prereg.judge`), and a pricier pinned instrument costs many times that per call, so
an estimate from the constant could pass the spend gate while the judge alone overshoots the cap. This module prices
the pinned judge from `shared.llm_model_catalog` (the deployment's price override first, as the gateway bills it) and
re-states each arm's estimate with it; the runner gates on the larger of the two estimates (fail closed).

Per judge call:
  profile  the token counts of the last judge calls of the same writer in the eval stages (shared.llm_call_log, role
           judge, joined to the eval run for its writer): p90 input (cache reads counted as uncached input) and p90
           output, times R2's estimate margin. Used when there are >= R2_MIN_COST_SAMPLES rows.
  bound    no profile (a writer never judged): the evidence caps of the judge (SOURCE_CHARS + OUTPUT_CHARS + the
           prompt's own text) at a conservative chars-per-token, and the whole first output ceiling (MAX_TOKENS raised
           to the model's reasoning floor). An upper bound, flagged as such.
Per arm (`calls_of`: the counts behind R2's figure): judge calls x the judge call + written items x R2's own per-item
writer estimate (the ledger median of past
candidate items, R2's `item_estimate`). Every figure here is an ESTIMATE; the measured cost comes from the ledger after
the run (cost_report.py).

The check refuses (a `blocking` line, never an exception) when the pinned judge has no catalog row, no input or
output price, is disabled or blocked, or when the pinned connection has no enabled deployment of it (the gateway would
answer from another connection and eval_judge would refuse the paid reply).
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Optional

CATALOG_SQL = """
    SELECT model_key, enabled, blocked_reason,
           price_in_per_mtok::float AS price_in_per_mtok, price_out_per_mtok::float AS price_out_per_mtok,
           price_cache_read_per_mtok::float AS price_cache_read_per_mtok,
           price_cache_write_per_mtok::float AS price_cache_write_per_mtok,
           max_output_tokens, capabilities
      FROM shared.llm_model_catalog
     WHERE model_key = $1
"""
DEPLOYMENT_SQL = """
    SELECT connection_key, enabled,
           price_in_override::float AS price_in_override, price_out_override::float AS price_out_override,
           price_cache_read_override::float AS price_cache_read_override,
           price_cache_write_override::float AS price_cache_write_override,
           price_cache_write_1h_override::float AS price_cache_write_1h_override
      FROM shared.llm_model_deployment
     WHERE model_key = $1 AND connection_key = $2
"""
# The judge calls of one writer's eval runs, newest first (R2 binds quality_signal.eval_run to every call it makes).
JUDGE_TOKENS_SQL = """
    SELECT l.tokens_in, l.tokens_out, coalesce(l.cache_read_tokens, 0) AS cache_read_tokens
      FROM shared.llm_call_log l
      JOIN shared.prompt_eval_runs p ON p.id::text = l.quality_signal->>'eval_run'
     WHERE l.stage = ANY($1::text[]) AND l.role = 'judge' AND coalesce(l.status, 'ok') = 'ok'
       AND l.tokens_in IS NOT NULL AND l.tokens_out IS NOT NULL
       AND p.pipeline = 'writing_memory' AND p.writer = $2
     ORDER BY l.created_at DESC, l.id DESC
     LIMIT $3
"""
PROFILE_ROWS = 200           # technical-constant: how many recent judge calls the token profile reads
CHARS_PER_TOKEN = 3.0        # technical-constant: = ground_truth.r1 (conservative; English is about 4)
PRICED_BASES = ("catalog", "override")


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _p90(vals: list[float]) -> float:
    if len(vals) == 1:
        return float(vals[0])
    return float(statistics.quantiles(vals, n=10, method="inclusive")[-1])


def token_profile(rows: list[dict], *, min_samples: int) -> Optional[dict]:
    """{samples, in_p50, in_p90, out_p50, out_p90} of judge calls (input = tokens_in + cache reads), or None with
    fewer than `min_samples` usable rows. Pure."""
    ins, outs = [], []
    for r in rows or []:
        ti, to = _num(r.get("tokens_in")), _num(r.get("tokens_out"))
        if ti is None or to is None or ti < 0 or to < 0:
            continue
        ins.append(ti + max(0.0, _num(r.get("cache_read_tokens")) or 0.0))
        outs.append(to)
    if len(ins) < max(1, int(min_samples)):
        return None
    return {"samples": len(ins), "in_p50": statistics.median(ins), "in_p90": _p90(ins),
            "out_p50": statistics.median(outs), "out_p90": _p90(outs)}


def bound_tokens(model_key: str, catalog_row: Optional[dict]) -> dict:
    """The per-call token upper bound of the eval judge when no profile exists: the evidence caps plus the prompt's
    own text, and the whole first output ceiling. Pure (reads eval_judge's constants)."""
    from services.writing_memory import eval_judge as ej
    from shared.aa_llm import tokens as llm_tokens
    row = catalog_row or {}
    caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else None
    frame = len(ej.system_prompt()) + len(ej.build_user_prompt(writer="t9_social", source_text="", output_text=""))
    tokens_in = math.ceil((ej.SOURCE_CHARS + ej.OUTPUT_CHARS + frame) / CHARS_PER_TOKEN)
    tokens_out = llm_tokens.floored(ej.MAX_TOKENS, model_key, caps, row.get("max_output_tokens"))
    return {"tokens_in": int(tokens_in), "tokens_out": int(tokens_out)}


def catalog_problems(model_key: str, connection: Optional[str], catalog_row: Optional[dict],
                     deployment_row: Optional[dict]) -> list[str]:
    """Why the pinned judge cannot be priced (or would not answer on its pinned connection). Pure."""
    if catalog_row is None:
        return [f"judge {model_key}: no row in shared.llm_model_catalog"]
    p = []
    if _num(catalog_row.get("price_in_per_mtok")) is None or _num(catalog_row.get("price_out_per_mtok")) is None:
        p.append(f"judge {model_key}: the catalog row has no input / output price")
    if not catalog_row.get("enabled"):
        p.append(f"judge {model_key}: disabled in the catalog")
    if catalog_row.get("blocked_reason"):
        p.append(f"judge {model_key}: blocked in the catalog ({str(catalog_row['blocked_reason'])[:80]})")
    if connection:
        if deployment_row is None:
            p.append(f"judge {model_key}@{connection}: no deployment of the model on its pinned connection (the "
                     "gateway would answer from another one and the judge refuses that paid reply)")
        elif not deployment_row.get("enabled"):
            p.append(f"judge {model_key}@{connection}: the pinned deployment is disabled")
    return p


def call_quote(model_key: str, catalog_row: dict, deployment_row: Optional[dict], tokens_in: float,
               tokens_out: float) -> tuple[float, str]:
    """(USD, pricing basis) of one judge call with these tokens, priced like the gateway bills it. Pure."""
    from shared.aa_llm import pricing as core
    rates = core.catalog_rates(catalog_row.get("price_in_per_mtok"), catalog_row.get("price_out_per_mtok"),
                               catalog_row.get("price_cache_read_per_mtok"),
                               catalog_row.get("price_cache_write_per_mtok"))
    usage = core.Usage(input_tokens=int(math.ceil(tokens_in)), output_tokens=int(math.ceil(tokens_out)))
    q = core.quote(model_key, usage, deployment_row, catalog=rates)
    return q.cost_usd, q.pricing_mode


def judge_call(model_key: str, catalog_row: dict, deployment_row: Optional[dict], profile: Optional[dict], *,
               margin: float) -> dict:
    """The reserved USD of one judge call: the profile's p90 tokens x `margin`, else the bound. Pure."""
    if profile is not None:
        t_in, t_out, source = profile["in_p90"] * margin, profile["out_p90"] * margin, "ledger_p90"
    else:
        b = bound_tokens(model_key, catalog_row)
        t_in, t_out, source = b["tokens_in"], b["tokens_out"], "bound"
    usd, basis = call_quote(model_key, catalog_row, deployment_row, t_in, t_out)
    return {"usd": round(usd, 6), "tokens_in": int(math.ceil(t_in)), "tokens_out": int(math.ceil(t_out)),
            "source": source, "pricing": basis, "samples": (profile or {}).get("samples", 0)}


def calls_of(r2_estimate: dict) -> Optional[dict]:
    """{judge, writer_items}: the calls behind R2's estimated_usd_max, from the fields r2.estimate returns (the same
    formula: every calibration item judged once, and each written item judged twice, the control and the candidate;
    a calibrate-only run writes nothing). None when a field is missing. A proof never seeds a fault. Pure.
    tests/unit/writer_proof/test_aa1051_s280_price_null_lock.py rebuilds R2's figure from these counts, so a change
    of R2's formula fails there."""
    plan, judged = r2_estimate.get("plan"), r2_estimate.get("calibration_judged_items")
    if not isinstance(plan, dict) or not isinstance(judged, int) or isinstance(judged, bool):
        return None
    written = 0 if r2_estimate.get("calibrate_only") else sum(int(v or 0) for v in plan.values())
    return {"judge": judged + 2 * written, "writer_items": written}


def arm_estimate(r2_estimate: dict, judge_call_usd: float) -> dict:
    """{price_usd_max, judge_calls, writer_items, r2_usd_max, usd_max}: one arm re-priced with the catalog judge call
    and R2's own per-item writer estimate; `usd_max` = the larger of R2's and this one. Pure.

    Without the call counts (`calls_of` is None) the arm is re-priced from nothing and only R2's figure stands, flagged
    `calls_missing` (the runner blocks on it)."""
    calls = calls_of(r2_estimate)
    r2_usd = _num(r2_estimate.get("estimated_usd_max")) or 0.0
    if calls is None:
        return {"price_usd_max": None, "judge_calls": None, "writer_items": None, "r2_usd_max": r2_usd,
                "usd_max": r2_usd, "calls_missing": True}
    judge_calls = int(calls.get("judge") or 0)
    writer_items = int(calls.get("writer_items") or 0)
    item_usd = _num((r2_estimate.get("item_estimate") or {}).get("item_usd")) or 0.0
    price = round(judge_calls * judge_call_usd + writer_items * item_usd, 4)
    return {"price_usd_max": price, "judge_calls": judge_calls, "writer_items": writer_items,
            "writer_item_usd": item_usd, "r2_usd_max": r2_usd, "usd_max": round(max(r2_usd, price), 4),
            "calls_missing": False}


async def read(conn, *, model_key: str, connection: Optional[str], writer: str, stages: tuple[str, ...]) -> dict:
    """The catalog row, the pinned deployment row and the judge-call rows of the writer (READ ONLY)."""
    cat = await conn.fetchrow(CATALOG_SQL, model_key)
    dep = await conn.fetchrow(DEPLOYMENT_SQL, model_key, connection) if connection else None
    rows = await conn.fetch(JUDGE_TOKENS_SQL, list(stages), writer, PROFILE_ROWS)
    return {"catalog": dict(cat) if cat else None, "deployment": dict(dep) if dep else None,
            "token_rows": [dict(r) for r in rows]}


async def check(pool, *, model_key: str, connection: Optional[str], writer: str, arms: dict[str, dict]) -> dict:
    """The price check of a plan: {judge, arms: {arm: arm_estimate}, blocking}. `arms`: {arm: r2.estimate output}."""
    from services.writing_memory import config as cfg
    async with pool.acquire() as conn:
        got = await read(conn, model_key=model_key, connection=connection, writer=writer,
                         stages=tuple(cfg.EVAL_LOG_STAGES))
    blocking = catalog_problems(model_key, connection, got["catalog"], got["deployment"])
    out: dict[str, Any] = {"judge": {"model_key": model_key, "connection": connection}, "arms": {},
                           "blocking": blocking}
    if got["catalog"] is None or any("no input / output price" in b for b in blocking):
        return out
    profile = token_profile(got["token_rows"], min_samples=cfg.R2_MIN_COST_SAMPLES)
    jc = judge_call(model_key, got["catalog"], got["deployment"], profile, margin=cfg.R2_ESTIMATE_MARGIN)
    out["judge"].update(call=jc, profile=profile, r2_constant_usd=cfg.JUDGE_CALL_ESTIMATE_USD)
    if jc["pricing"] not in PRICED_BASES:
        blocking.append(f"judge {model_key}: priced by {jc['pricing']}, not the catalog")
    for arm, est in arms.items():
        out["arms"][arm] = arm_estimate(est, jc["usd"])
        if out["arms"][arm]["calls_missing"]:
            blocking.append(f"{arm}: the R2 estimate gives no call counts (plan / calibration_judged_items), the "
                            "catalog price check cannot run")
    return out


__all__ = ["CATALOG_SQL", "CHARS_PER_TOKEN", "DEPLOYMENT_SQL", "JUDGE_TOKENS_SQL", "PROFILE_ROWS", "arm_estimate",
           "bound_tokens", "call_quote", "calls_of", "catalog_problems", "check", "judge_call", "read", "token_profile"]
