#!/usr/bin/env python3
"""AA-919 — judge-model comparison: the same calibration pairs through 2-3 judge models, kappa per model.

The R2 gate (WM-19 scope 4) needs the judge to agree with the golden labels: kappa >= 0.6 on >= 10 pairs with >= 3 of
each label (`eval_checks.judge_counts`; the threshold is not changed here). Runs so far used the `writing_memory_eval`
route (gpt-5.6-luna first). This harness answers "is it the judge model?" before anyone touches the rubric or labels
again: it re-judges the FROZEN labelled outputs of the active golden set (`r2.calibration_items`: labelled, not a trap,
not post_edit) with the R2 judge's own prompt, source and schema (`eval_judge.judge_output`, `judge_source_text`),
once per model, and prints per model: kappa, the gate decision, confusion, recall per label, kappa_max, score AUC,
per-stratum kappa (channel, tenant, tier, split), cost; then how often the models agree with each other.

Models: from shared.llm_model_catalog, acc3 only — enabled, Bedrock Converse, served on acc3 (`account_for("acc3")`),
priced. An OpenAI platform key (`*-openai`, api_style openai_chat) leaves AWS and is refused; an Anthropic model is
refused unless --allow-anthropic (the writers it scores are Anthropic: app ADR 0010 decision 5). Default models: the
`writing_memory_eval` route's eligible models; `--models a,b,c` names them (at most 3).

Money: DRY RUN by default (plan + estimate, $0: reads only). `--apply` makes the calls and needs `--max-usd`; the
estimate (worst case: every reply at the judge's MAX_TOKENS) is printed before the first call and the run is refused
when it is over --max-usd (hard ceiling HARD_MAX_USD). During the run a call starts only if the logged-so-far cost +
the worst case of the calls in flight + its own worst case fit --max-usd. Every call goes through
LLMClient.generate() (the gateway's per-request model override, no route, no fallback) and writes one
shared.llm_call_log row under stage `adhoc_aa919`, role judge (a billed but unusable reply: a billed_unusable row),
quality_signal {issue AA-919, run_id = this comparison's id, model_key, sample_id, item_id}. At the end it counts those
rows (lesson S252: 0 rows after a paid run = lost logs, not $0). No other row is written.

Runs inside aa-cis-dev-worker as cis_app (container DATABASE_URL). scripts/ is not in the image: the file is exec()'d
with no argv (root `.tmp-session/<S>/exec_svc.sh`); pass arguments by prepending one line to a copy:
  import sys; sys.argv = ["judge_compare", "--writer", "t9_social", "--models", "gpt-5.6-luna,gpt-6-luna,gpt-6-astra"]
  ... then the same with "--apply", "--max-usd", "0.5"
Keep it under 5 minutes (concurrency 3; t9_social: 13 pairs x 3 models ~ 1-2 min): a long exec session can drop.
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time
import uuid
from typing import Any, Optional

for _p in (os.getcwd(), "/app"):
    if os.path.isdir(os.path.join(_p, "services")) and _p not in sys.path:
        sys.path.insert(0, _p)

from services.writing_memory import calibration as cal  # noqa: E402
from services.writing_memory import config as cfg  # noqa: E402
from services.writing_memory import eval_checks as ec  # noqa: E402

STAGE = "adhoc_aa919"
ACCOUNT = "acc3"
MAX_MODELS = 3
MAX_CONCURRENCY = 4
HARD_MAX_USD = 5.0               # one comparison never spends more, whatever --max-usd says
CHARS_PER_TOKEN = 3.0            # conservative (English prose ~4 chars a token): over-estimates the input
EXPECTED_OUT_TOKENS = 700        # verdict + score + classes + up to 8 quoted claims
WRITERS = ("a1", "t2", "t9_blog", "t9_social")


# ── pure parts (unit-tested) ───────────────────────────────────────────────────────────────────────────────────────

def refusal(m: Any, *, allow_anthropic: bool = False) -> Optional[str]:
    """Why catalog model `m` (a shared.llm_client.catalog.CatalogModel, or None) cannot be a compared judge; None
    when it can."""
    if m is None:
        return "not in shared.llm_model_catalog"
    if not m.enabled:
        return f"disabled in the catalog ({m.blocked_reason or 'no reason given'})"
    if m.api_style != "converse" or m.provider != "bedrock":
        return f"not a Bedrock Converse model (api_style {m.api_style}, provider {m.provider}): it would leave AWS"
    if m.account_for(ACCOUNT) != ACCOUNT:
        return "not served on acc3"
    if m.price_in_per_mtok is None or m.price_out_per_mtok is None:
        return "no price in the catalog"
    if m.vendor == "anthropic" and not allow_anthropic:
        return "same vendor as the writers it scores (app ADR 0010 decision 5); --allow-anthropic to include"
    return None


def choose_models(catalog: dict[str, Any], requested: list[str], *, allow_anthropic: bool = False
                  ) -> tuple[list[str], dict[str, str]]:
    """(chosen model keys in the requested order, {refused key: reason}); at most MAX_MODELS chosen."""
    chosen, refused = [], {}
    for key in dict.fromkeys(k.strip() for k in requested if k and k.strip()):
        why = refusal(catalog.get(key), allow_anthropic=allow_anthropic)
        if why:
            refused[key] = why
        elif len(chosen) >= MAX_MODELS:
            refused[key] = f"over the {MAX_MODELS}-model limit"
        else:
            chosen.append(key)
    return chosen, refused


def call_usd(m: Any, prompt_chars: int, out_tokens: int) -> float:
    """List-price cost of one call: input from the prompt length (CHARS_PER_TOKEN), output `out_tokens`."""
    tin = math.ceil(prompt_chars / CHARS_PER_TOKEN)
    return (tin * float(m.price_in_per_mtok) + out_tokens * float(m.price_out_per_mtok)) / 1_000_000


def estimate(catalog: dict[str, Any], models: list[str], prompt_chars: list[int], max_out_tokens: int) -> dict:
    """{model: {calls, expected_usd, max_usd}, total_expected_usd, total_max_usd}; max = every reply at MAX_TOKENS."""
    per = {}
    for k in models:
        m = catalog[k]
        per[k] = {"calls": len(prompt_chars),
                  "expected_usd": round(sum(call_usd(m, c, EXPECTED_OUT_TOKENS) for c in prompt_chars), 4),
                  "max_usd": round(sum(call_usd(m, c, max_out_tokens) for c in prompt_chars), 4)}
    return {"per_model": per, "total_expected_usd": round(sum(v["expected_usd"] for v in per.values()), 4),
            "total_max_usd": round(sum(v["max_usd"] for v in per.values()), 4)}


class Budget:
    """--max-usd during the run: a call starts only when spent + in-flight reservations + its own worst case fit."""

    def __init__(self, max_usd: float):
        self.max_usd = float(max_usd)
        self.spent = 0.0
        self.reserved = 0.0
        self.skipped = 0

    def try_reserve(self, worst: float) -> bool:
        if self.spent + self.reserved + worst > self.max_usd:
            self.skipped += 1
            return False
        self.reserved += worst
        return True

    def settle(self, worst: float, actual: Optional[float]) -> None:
        """Release the reservation and book the actual cost (unknown = the worst case, conservative)."""
        self.reserved = max(0.0, self.reserved - worst)
        self.spent += worst if actual is None else max(0.0, float(actual))


def check_max_usd(max_usd: Optional[float], total_max: float) -> Optional[str]:
    """Why an --apply run is refused before the first call; None when it may start."""
    if max_usd is None:
        return "--apply needs --max-usd"
    if max_usd <= 0 or max_usd > HARD_MAX_USD:
        return f"--max-usd must be in (0, {HARD_MAX_USD}]"
    if total_max > max_usd:
        return f"the worst-case estimate ${total_max:.4f} is over --max-usd ${max_usd:.4f}"
    return None


def agreement(per_model: dict[str, dict[str, str]]) -> dict[str, dict]:
    """Pairwise: {"a|b": {n, agree, kappa}} on the samples both models judged (judge vs judge, no labels)."""
    keys = sorted(per_model)
    out = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            common = sorted(set(per_model[a]) & set(per_model[b]))
            pairs = [(per_model[a][s], per_model[b][s]) for s in common]
            out[f"{a}|{b}"] = {"n": len(pairs), "agree": sum(1 for x, y in pairs if x == y),
                               "kappa": ec.cohen_kappa(pairs)}
    return out


# ── the run ────────────────────────────────────────────────────────────────────────────────────────────────────────

def parse(argv):
    ap = argparse.ArgumentParser(description="AA-919 judge-model comparison (DRY RUN by default, $0)")
    ap.add_argument("--writer", default="t9_social", choices=WRITERS)
    ap.add_argument("--models", help=f"comma-separated catalog Model Keys (<= {MAX_MODELS}); default: the "
                                     f"{cfg.EVAL_STAGE} route's acc3 models")
    ap.add_argument("--limit", type=int, help="first N calibration pairs only (by sample id)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--allow-anthropic", action="store_true")
    ap.add_argument("--apply", action="store_true", help="make the paid calls")
    ap.add_argument("--max-usd", type=float)
    ap.add_argument("--json", action="store_true", help="also print the full result as one JSON line")
    return ap.parse_args(argv)


async def _load(pool, writer: str, limit: Optional[int]):
    from services.writing_memory import r2
    store = r2.PgStore(pool)
    gs = await store.golden_set()
    if gs is None:
        raise SystemExit("no active golden set")
    items = r2.calibration_items(await store.items(gs["set_id"], writer, None))
    items.sort(key=lambda it: it["sample_id"])
    return gs, items[:limit] if limit else items


async def main(argv=None) -> int:
    import asyncpg

    from services.writing_memory import eval_writers as ew
    from services.writing_memory.eval_judge import MAX_TOKENS, build_user_prompt, judge_output, judge_source_text
    from services.writing_memory.eval_judge import judge_version, system_prompt
    from shared.llm_client import catalog as catalog_mod
    from shared.llm_client.role_config import get_stage_config

    a = parse(sys.argv[1:] if argv is None else argv)
    conc = max(1, min(MAX_CONCURRENCY, a.concurrency))
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"].strip(), ssl="require", min_size=1, max_size=conc + 1)
    try:
        gs, items = await _load(pool, a.writer, a.limit)
        catalog = {m.model_key: m for m in await catalog_mod.list_models()}
        if a.models:
            requested = a.models.split(",")
        else:
            route = await get_stage_config(cfg.EVAL_STAGE)
            requested = [route.model_id, *route.fallback_model_ids]
        models, refused = choose_models(catalog, requested, allow_anthropic=a.allow_anthropic)
        shared = gs.get("shared_inputs") or {}
        prompts = []
        for it in items:
            src = judge_source_text(a.writer, it["input"], shared)
            out = ew.output_text(a.writer, it["control_output"])
            prompts.append((it, src, out, len(system_prompt()) + len(build_user_prompt(
                writer=a.writer, source_text=src, output_text=out))))
        est = estimate(catalog, models, [p[3] for p in prompts], MAX_TOKENS)
        labels = ec.label_counts((it["label_verdict"], it["label_verdict"]) for it in items)
        plan = {"writer": a.writer, "golden_set": f"{gs['name']}@{gs['version']}", "pairs": len(items),
                "labels": labels, "calibrated_possible": len(items) >= cfg.MIN_KAPPA_PAIRS
                and min(labels.values()) >= cfg.MIN_KAPPA_PER_LABEL, "requested": requested, "models": models,
                "refused": refused, "judge_version": judge_version(), "stage": STAGE, "estimate": est,
                "strata": ec.strata_label_counts(items)}
        print(json.dumps(plan, indent=1, default=str), flush=True)
        if len(models) < 2:
            print("REFUSED: fewer than 2 eligible models (a comparison needs 2-3)")
            return 2
        if not a.apply:
            print("DRY RUN: no model call. Add --apply --max-usd <usd> to run.")
            return 0
        why = check_max_usd(a.max_usd, est["total_max_usd"])
        if why:
            print(f"REFUSED: {why}")
            return 2

        compare_id = str(uuid.uuid4())
        print(f"comparison {compare_id}: {len(models)} models x {len(items)} pairs, max ${a.max_usd}", flush=True)
        budget = Budget(a.max_usd)
        sem = asyncio.Semaphore(conc)
        results: dict[str, list[dict]] = {k: [] for k in models}
        errors: dict[str, int] = {k: 0 for k in models}
        t0 = time.monotonic()

        async def one(key: str, it: dict, src: str, out: str, chars: int):
            worst = call_usd(catalog[key], chars, MAX_TOKENS)
            async with sem:
                if not budget.try_reserve(worst):
                    return
                actual = None
                try:
                    v = await judge_output(pool, writer=a.writer, source_text=src, output_text=out, run_id=compare_id,
                                           item_id=it["item_id"], arm="calibration", model_key=key, stage=STAGE,
                                           signal_extra={"model_key": key, "sample_id": it["sample_id"],
                                                         "harness": "judge_compare"})
                    actual = v.cost_usd
                    results[key].append({"sample": it["sample_id"], "label": it["label_verdict"], "judge": v.verdict,
                                         "score": v.score, "codes": list(v.failure_codes),
                                         "strata": ec.item_strata(it), "model_used": v.model})
                except Exception as e:  # noqa: BLE001 — a failed call is a counted error, never a verdict
                    errors[key] += 1
                    print(f"  error {key} {it['sample_id']}: {type(e).__name__}: {str(e)[:160]}", flush=True)
                finally:
                    budget.settle(worst, actual)

        await asyncio.gather(*(one(k, *p) for k in models for p in prompts))
        from shared.llm_client.call_log import wait_for_pending_writes
        await asyncio.to_thread(wait_for_pending_writes, 15.0)
        logged = await pool.fetchrow(
            "SELECT count(*) AS n, coalesce(sum(cost_usd), 0)::float8 AS usd FROM shared.llm_call_log "
            "WHERE stage = $1 AND quality_signal->>'run_id' = $2 AND created_at > now() - interval '1 day'",
            STAGE, compare_id)
        calls = sum(len(v) for v in results.values()) + sum(errors.values())
        summary = {}
        for k in models:
            rep = cal.report(results[k])
            summary[k] = {"kappa": rep["kappa"], "counts": rep["counts"], "pairs": rep["pairs"], "errors": errors[k],
                          "confusion": rep["confusion"], "pass_recall": rep["pass_recall"],
                          "fail_recall": rep["fail_recall"], "kappa_max": rep["kappa_max"],
                          "score_auc": rep["score_auc"], "loo_kappa": rep["loo_kappa"],
                          "disagreements": rep["disagreements"], "strata_below": rep["strata_below"],
                          "models_used": sorted({r["model_used"] for r in results[k]})}
            print(f"\n=== {k}: kappa {rep['kappa']} on {rep['pairs']} pairs  counts={rep['counts']}  errors "
                  f"{errors[k]}  confusion {rep['confusion']}  recall pass {rep['pass_recall']} fail "
                  f"{rep['fail_recall']}  kappa_max {rep['kappa_max']}  AUC {rep['score_auc']}")
            print(f"    disagreements: {rep['disagreements']}")
            for line in cal.strata_lines(rep["strata"]):
                print("  " + line)
            summary[k]["strata"] = rep["strata"]
        verdicts = {k: {r["sample"]: r["judge"] for r in results[k]} for k in models}
        out = {"comparison_id": compare_id, "writer": a.writer, "golden_set": plan["golden_set"],
               "judge_version": plan["judge_version"], "seconds": round(time.monotonic() - t0, 1),
               "spent_usd_by_reply": round(budget.spent, 6), "skipped_by_budget": budget.skipped,
               "llm_call_log": {"rows": logged["n"], "usd": round(logged["usd"], 6), "calls_made": calls},
               "models": summary, "agreement": agreement(verdicts),
               "verdicts": verdicts}
        print(f"\nmodel-vs-model agreement: {json.dumps(out['agreement'])}")
        print(f"llm_call_log rows {logged['n']} for {calls} calls (a call that failed before billing writes none), "
              f"logged ${logged['usd']:.4f}; skipped by the budget {budget.skipped}")
        if a.json:
            print("JSON " + json.dumps(out, default=str))
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
