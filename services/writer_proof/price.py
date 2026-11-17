"""AA-1051 (S280) — the catalog price check of a W5 proof plan ($0, reads only, no model call).

Why. `r2.estimate` prices every judge call at `cfg.JUDGE_CALL_ESTIMATE_USD` (a constant measured on the cheap default
judge). A proof pins its judge (D7, `prereg.judge`), and a pricier pinned instrument costs many times that per call, so
an estimate from the constant could pass the spend gate while the judge alone overshoots the cap. This module prices
the pinned judge from `shared.llm_model_catalog` (the deployment's price override first, as the gateway bills it) and
re-states each arm's estimate with it; the runner gates on the larger of the two estimates (fail closed).

Per judge call:
  profile  the token counts of the last judge calls OF THE PINNED MODEL for the same writer in the eval stages
           (shared.llm_call_log, role judge, `model` = the pinned catalog key or a legacy id the pricing maps to it,
           joined to the eval run for its writer): p90 input (cache reads counted as uncached input) and p90 output,
           times R2's estimate margin. Used when there are >= R2_MIN_COST_SAMPLES rows. Only the pinned model's own
           calls count: a reasoning judge (Claude, Kimi, a gpt reasoning model) writes far longer replies than the
           cheap default judge, so a profile of every judge model would undershoot exactly the pricier pinned judge.
  bound    no profile (a writer never judged): the evidence caps of the judge (SOURCE_CHARS + OUTPUT_CHARS + the
           prompt's own text) at a conservative chars-per-token, and the whole first output ceiling (MAX_TOKENS raised
           to the model's reasoning floor). An upper bound, flagged as such.
Per arm (`calls_of`: the counts behind R2's figure): judge calls x the judge call + written items x R2's own per-item
writer estimate (the ledger median of past
candidate items, R2's `item_estimate`). Every figure here is an ESTIMATE; the measured cost comes from the ledger after
the run (cost_report.py).

A claim instrument judge (AA-1051 S283, prereg judge.instrument: R1 fire -> two-step v7, instrument.py) is priced per
judged OUTPUT instead of per call (`instrument_check`): for each output of the sample (the frozen labelled output of
each fixed sample item, read through eval_judge.evidence_view as the run reads it)
  fire calls   fields x votes                    (one extraction call per field per vote)
  step calls   claims x fire share x votes x 2   (step 2 priced as if every fired claim reached it)
each priced at the MEAN measured cost of that step's prompt tag on the pinned model (shared.llm_call_log, stage
adhoc_aa1048 and adhoc_aa1051, quality_signal.prompt_version: the AA-1048 runs and every earlier W5 run), x R2's
margin. The mean, not the p90: an output is hundreds of calls, so their sum is near its mean and a p90 per call would
price every call at its worst tail at once. A step with fewer than R2_MIN_COST_SAMPLES measured calls is priced by
r1's model estimate (expected reply plus reasoning: far above the measured cost, flagged `model`). The fire share is
instrument.FIRE_SHARE (measured on the v7 fresh pool fire; 1.0 for a writer without a measurement). One judged output
costs the mean of the sample's outputs; the B output, not written yet, is assumed to be as long as its control. Each arm
then re-prices with `arm_estimate` (judge calls = judged outputs). The run itself is held by the instrument's per-call
cap, whatever the plan said.

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
# The judge calls of the pinned model in one writer's eval runs, newest first (R2 binds quality_signal.eval_run to
# every call it makes). llm_call_log.model holds the catalog key: call_log._normalize_model_provider strips the
# "satellite-" / vendor label prefixes, so a reply that another model answered (eval_judge logs it, then refuses it)
# carries that model's key and stays out of the pinned judge's profile.
# $4 = log_models(model_key): the key plus the legacy ids pricing.catalog_key maps to it.
JUDGE_TOKENS_SQL = """
    SELECT l.tokens_in, l.tokens_out, coalesce(l.cache_read_tokens, 0) AS cache_read_tokens
      FROM shared.llm_call_log l
      JOIN shared.prompt_eval_runs p ON p.id::text = l.quality_signal->>'eval_run'
     WHERE l.stage = ANY($1::text[]) AND l.role = 'judge' AND coalesce(l.status, 'ok') = 'ok'
       AND l.tokens_in IS NOT NULL AND l.tokens_out IS NOT NULL
       AND l.model = ANY($4::text[])
       AND p.pipeline = 'writing_memory' AND p.writer = $2
     ORDER BY l.created_at DESC, l.id DESC
     LIMIT $3
"""
# AA-1051 S283: the measured cost per call of each step tag of a claim instrument on its pinned model.
# $1 stages, $2 = log_models(model_key), $3 the step prompt tags.
INSTRUMENT_CALLS_SQL = """
    SELECT l.quality_signal->>'prompt_version' AS tag, count(*)::int AS calls, avg(l.cost_usd)::float AS mean_usd
      FROM shared.llm_call_log l
     WHERE l.stage = ANY($1::text[]) AND l.model = ANY($2::text[]) AND coalesce(l.status, 'ok') = 'ok'
       AND l.cost_usd IS NOT NULL AND l.cost_usd > 0 AND l.quality_signal->>'prompt_version' = ANY($3::text[])
     GROUP BY 1
"""
INSTRUMENT_STAGES = ("adhoc_aa1048", "adhoc_aa1051")     # the AA-1048 runs and the W5 runs (cfg.W5_STAGE)
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


def log_models(model_key: str) -> list[str]:
    """The `llm_call_log.model` values that mean the pinned catalog key: the key itself and every legacy model id
    `pricing.catalog_key` maps to it (old rows of the acc2-native chain logged the raw id). Pure."""
    from shared.aa_llm import pricing as core
    legacy = [mid for mid in getattr(core, "_MODEL_ID_TO_CATALOG_KEY", {}) if core.catalog_key(mid) == model_key]
    return sorted({model_key, *legacy})


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


def step_costs(p: Any, insts: dict, measured: dict[str, dict], *, min_samples: int,
               sample_user: dict[str, str]) -> dict[str, dict]:
    """step -> {usd, calls, source}: the mean measured cost per call of the step's prompt tag (`measured`: tag ->
    {calls, mean_usd}), else r1's model estimate of one call of `sample_user[step]` (source `model`). Pure."""
    from services.ground_truth import r1
    out = {}
    for step, inst in insts.items():
        m = measured.get(p.tags[step]) or {}
        mean = _num(m.get("mean_usd"))
        if int(m.get("calls") or 0) >= max(1, int(min_samples)) and mean is not None and mean > 0:
            out[step] = {"usd": round(mean, 6), "calls": int(m["calls"]), "source": "ledger_mean"}
        else:
            usd = r1.call_cost(inst, sample_user.get(step, ""), r1.expected_out(inst))
            out[step] = {"usd": round(usd, 6), "calls": int(m.get("calls") or 0), "source": "model"}
    return out


def output_cost(shape: dict, costs: dict[str, dict], *, votes: int, fire_share: float, margin: float) -> float:
    """The planned USD of one judged output of `shape` ({fields, claims}). Pure."""
    fire = shape["fields"] * votes * costs["fire"]["usd"]
    steps = shape["claims"] * fire_share * votes * (costs["classify"]["usd"] + costs["judge"]["usd"])
    return round((fire + steps) * margin, 6)


def instrument_output(p: Any, insts: dict, writer: str, outputs: list[str], measured: dict[str, dict], *,
                      min_samples: int, margin: float) -> dict:
    """{usd, outputs, mean_fields, mean_claims, fire_share, steps}: the planned cost of one judged output, the mean
    over `outputs` (labelled OUTPUT texts). Pure."""
    from services.ground_truth import r1
    from services.ground_truth import two_step as ts
    from services.writer_proof import instrument as wi
    shapes = [wi.output_shape(writer, o) for o in outputs]
    item = r1.Item("0" * 64, writer, "", "", "w5", "0" * 64, "content_text", "x" * 200, "x" * 4000, "x" * 4000)
    users = {"fire": insts["fire"].prompt_version.extraction_user([item]),
             "classify": ts.step1_user(item, insts["classify"].prompt), "judge": ts.step2_user(item, [])}
    costs = step_costs(p, insts, measured, min_samples=min_samples, sample_user=users)
    share = float(wi.FIRE_SHARE.get(p.id, {}).get(writer, 1.0))
    usd = [output_cost(s, costs, votes=p.votes, fire_share=share, margin=margin) for s in shapes]
    n = len(shapes)
    return {"usd": round(sum(usd) / n, 6) if n else None, "outputs": n,
            "mean_fields": round(sum(s["fields"] for s in shapes) / n, 2) if n else None,
            "mean_claims": round(sum(s["claims"] for s in shapes) / n, 2) if n else None,
            "max_output_usd": max(usd) if usd else None, "fire_share": share,
            "fire_share_source": wi.FIRE_SHARE_SOURCE if p.id in wi.FIRE_SHARE else "none: every claim planned",
            "votes": p.votes, "steps": costs, "pricing": "ledger_mean" if all(
                c["source"] == "ledger_mean" for c in costs.values()) else "model_estimate"}


async def instrument_check(pool, *, instrument: str, writer: str, outputs: list[str], arms: dict[str, dict]) -> dict:
    """The price check of a plan whose judge is a claim instrument: {judge, arms: {arm: arm_estimate}, blocking}, the
    same shape as `check`, with judge.call = one judged output (READ ONLY)."""
    from services.writer_proof import instrument as wi
    from services.writing_memory import config as cfg
    p = wi.pinned(instrument)
    insts = wi.instruments(p)
    async with pool.acquire() as conn:
        cat = await conn.fetchrow(CATALOG_SQL, p.model_key)
        dep = await conn.fetchrow(DEPLOYMENT_SQL, p.model_key, p.connection)
        rows = await conn.fetch(INSTRUMENT_CALLS_SQL, list(INSTRUMENT_STAGES), log_models(p.model_key),
                                sorted(set(p.tags.values())))
    blocking = catalog_problems(p.model_key, p.connection, dict(cat) if cat else None, dict(dep) if dep else None)
    measured = {r["tag"]: {"calls": r["calls"], "mean_usd": r["mean_usd"]} for r in rows}
    call = instrument_output(p, insts, writer, outputs, measured, min_samples=cfg.R2_MIN_COST_SAMPLES,
                             margin=cfg.R2_ESTIMATE_MARGIN)
    out: dict[str, Any] = {"judge": {"model_key": p.model_key, "connection": p.connection, "instrument": p.id,
                                     "advisory": wi.ADVISORY.get(p.id), "call": call,
                                     "r2_constant_usd": cfg.JUDGE_CALL_ESTIMATE_USD}, "arms": {}, "blocking": blocking}
    if call["usd"] is None:
        blocking.append(f"instrument {p.id}: no output of the sample to price (no frozen control output)")
        return out
    for arm, est in arms.items():
        out["arms"][arm] = arm_estimate(est, call["usd"])
        if out["arms"][arm]["calls_missing"]:
            blocking.append(f"{arm}: the R2 estimate gives no call counts (plan / calibration_judged_items), the "
                            "instrument price check cannot run")
    return out


async def read(conn, *, model_key: str, connection: Optional[str], writer: str, stages: tuple[str, ...]) -> dict:
    """The catalog row, the pinned deployment row and the pinned model's judge-call rows of the writer (READ ONLY)."""
    cat = await conn.fetchrow(CATALOG_SQL, model_key)
    dep = await conn.fetchrow(DEPLOYMENT_SQL, model_key, connection) if connection else None
    rows = await conn.fetch(JUDGE_TOKENS_SQL, list(stages), writer, PROFILE_ROWS, log_models(model_key))
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


__all__ = ["CATALOG_SQL", "CHARS_PER_TOKEN", "DEPLOYMENT_SQL", "INSTRUMENT_CALLS_SQL", "INSTRUMENT_STAGES",
           "JUDGE_TOKENS_SQL", "PROFILE_ROWS", "arm_estimate", "bound_tokens", "call_quote", "calls_of",
           "catalog_problems", "check", "instrument_check", "instrument_output", "judge_call", "log_models",
           "output_cost", "read", "step_costs", "token_profile"]
