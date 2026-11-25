#!/usr/bin/env python3
"""AA-1166 — the first AI red-team run of the ISMS/AIMS-lite self-assessment plan. PAID with --apply: one model call
per case; nothing else. Written for the tech lead's session (decision S294 D7, hard cap 0.50 USD); CI never runs it
(the offline half is tests/unit/test_aa1166_redteam_set.py).

The case set (services/ai_safety/redteam_set.py: 16 invented cases, four categories — injection in source facts,
exfiltration asks, policy-breaking asks, tenant-crossing asks — over the S1 writer v2, the T2 rewrite, the T9 FAQ
writer and the T9 blog writer) is built with the live prompt builders in the fence and facts-guard modes in force
(the settings cis.llm.prompt_data_fence and cis.llm.fact_instruction_guard, read once at the start; --fence / --guard
pin them). Each call goes through the Model Gateway (``LLMClient.generate``) on the stage
``adhoc_aa1166_redteam_<target>`` (s1, t2, t9) with the model the writer's own stage routes to today (its
``shared.llm_role_config`` row) and writes one ``shared.llm_call_log`` row (role writer, ``**billed_fields(resp)``,
``quality_signal.aa1166`` = case, category, writer, modes). Each reply is judged offline by rules
(redteam_set.judge). The CRM draft reply and the TripPlanner chat are not reachable from the CIS gateway; the report
lists them as not run.

  python3 scripts/aa1166/redteam_run.py                          # dry run: plan + cost estimate, no model call
  python3 scripts/aa1166/redteam_run.py --apply --stop-usd 0.50  # paid: every case the budget allows
  python3 scripts/aa1166/redteam_run.py --targets s1,t2 --cases s1_policy,t2_policy

Each flag also reads an environment variable, so the run works through tools/ecs/ecsx.sh (VAR=value pairs, no
spaces): REDTEAM_APPLY=1, REDTEAM_STOP_USD=0.50, REDTEAM_TARGETS=s1,t2,t9, REDTEAM_CASES=<ids>,
REDTEAM_FENCE=live|on|off, REDTEAM_GUARD=live|on|off, REDTEAM_REPORT=<path of the markdown report>.

``--stop-usd`` has no default: the operator states the budget. Before each call the run stops when the spend so far
plus the estimate of that call would pass it. The spend so far is the larger of the replies' own costs and the ledger
sum of the run's stages since the run started (the gateway's own rows, e.g. a cut reply before a truncation retry,
count). The estimate is the larger of the dearest call of the same writer so far and a fixed per-writer bound. It also
stops at the first gateway refusal (the ad-hoc run cap of Platform › Settings stays in force under it). Output: one
JSON line per case (flushed), a summary line, then the markdown report between ``=== REPORT BEGIN ===`` and
``=== REPORT END ===`` (case, target, verdict, rules failed, USD; a masked excerpt of failed replies only). Run where
the gateway reaches the models and DATABASE_URL is set (the CIS worker container), /app on sys.path.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional



def _app_root(script: Path) -> Optional[Path]:
    """The CIS app root to put on sys.path: the checkout this script sits in (two levels above scripts/aa1166/), else
    /app. Through tools/ecs/ecsx.sh the script runs as /tmp/ecsx-<id>.py (scripts/ is not in the image), whose parents
    are only /tmp and /, so a fixed parents[2] raised IndexError before the dry run (S287/S288 hand-patched it)."""
    parents = script.resolve().parents
    if len(parents) > 2 and (parents[2] / "services").is_dir():
        return parents[2]
    app = Path("/app")
    return app if (app / "services").is_dir() else None


ROOT = _app_root(Path(__file__))
if ROOT is not None and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ISSUE = "AA-1166"
# technical-constant: the upper bound of one call per writer before the run has seen one (USD), from the AA-1172 trap
# runs of S288 (S1 0.017, T2 0.033, T9 FAQ 0.001, T9 blog up to 0.134 a call). Used only to stop before the budget.
CALL_BOUND_USD = {"s1_v2": 0.03, "t2": 0.05, "t9_faq": 0.01, "t9_blog": 0.14}
# technical-constant: the window of the measured basis of the dry-run estimate (the trap runs' ledger rows).
BASIS_WINDOW_DAYS = 30
BASIS_STAGES = ("adhoc_aa1167", "adhoc_aa1172")
BASIS_SQL = """
SELECT quality_signal -> 'aa1167' ->> 'writer' AS writer, count(*) AS calls,
       coalesce(sum(cost_usd), 0)::float AS usd, coalesce(max(cost_usd), 0)::float AS dearest
  FROM shared.llm_call_log
 WHERE stage = ANY($1::text[]) AND created_at > now() - make_interval(days => $2)
   AND quality_signal ? 'aa1167' AND cost_usd IS NOT NULL
 GROUP BY 1
"""
LEDGER_SQL = """
SELECT coalesce(sum(cost_usd), 0)::float AS usd
  FROM shared.llm_call_log
 WHERE stage = ANY($1::text[]) AND created_at >= $2
"""
MODES = ("live", "on", "off")


# ── plan and estimate (pure) ──────────────────────────────────────────────────────────────────────────────────────

def _csv(value) -> Optional[tuple[str, ...]]:
    if value is None or value == "":
        return None
    items = value.split(",") if isinstance(value, str) else value
    out = tuple(v.strip() for v in items if v.strip())
    return out or None


def plan(targets=None, cases=None) -> list:
    from services.ai_safety import redteam_set
    return redteam_set.select(_csv(targets), _csv(cases))


def call_estimate(writer: str, seen: dict) -> float:
    """What the next call of ``writer`` may cost: the dearest call of that writer so far, never under its bound."""
    return max(seen.get(writer, 0.0), CALL_BOUND_USD.get(writer, max(CALL_BOUND_USD.values())))


def simulate(cases: list, stop_usd: float, unit_usd: dict) -> dict:
    """The cases a run would make under ``stop_usd`` if each call cost ``unit_usd[writer]`` (the measured mean, else
    the bound), with the run's own stop rule. Returns the ids run, the ids skipped and the projected spend."""
    spent, seen, ran, skipped = 0.0, {}, [], []
    for case in cases:
        if skipped or spent + call_estimate(case.writer, seen) > stop_usd:
            skipped.append(case.id)
            continue
        cost = unit_usd.get(case.writer, CALL_BOUND_USD.get(case.writer, 0.0))
        spent += cost
        seen[case.writer] = max(seen.get(case.writer, 0.0), cost)
        ran.append(case.id)
    return {"ran": ran, "skipped": skipped, "projected_usd": round(spent, 4)}


def estimate(cases: list, basis: dict) -> dict:
    """Per target: calls, the measured mean $/call of its writers (``basis``: writer -> {calls, usd}) and the bound;
    ``source`` says which number the projection used."""
    out: dict = {"targets": {}, "projected_usd": 0.0, "bound_usd": 0.0}
    for case in cases:
        b = basis.get(case.writer) or {}
        mean = (float(b["usd"]) / int(b["calls"])) if b.get("calls") else None
        bound = CALL_BOUND_USD.get(case.writer, max(CALL_BOUND_USD.values()))
        slot = out["targets"].setdefault(case.target, {"calls": 0, "projected_usd": 0.0, "bound_usd": 0.0,
                                                       "source": set()})
        slot["calls"] += 1
        slot["projected_usd"] += mean if mean is not None else bound
        slot["bound_usd"] += bound
        slot["source"].add("measured" if mean is not None else "bound")
    for slot in out["targets"].values():
        slot["projected_usd"] = round(slot["projected_usd"], 4)
        slot["bound_usd"] = round(slot["bound_usd"], 4)
        slot["source"] = "+".join(sorted(slot["source"]))
        out["projected_usd"] += slot["projected_usd"]
        out["bound_usd"] += slot["bound_usd"]
    out["projected_usd"] = round(out["projected_usd"], 4)
    out["bound_usd"] = round(out["bound_usd"], 4)
    return out


def unit_costs(basis: dict) -> dict:
    return {w: float(b["usd"]) / int(b["calls"]) for w, b in basis.items() if b.get("calls")}


# ── database reads (read-only, failure tolerant) ────────────────────────────────────────────────────────────────────

async def _fetch(sql: str, *args) -> list:
    import asyncpg

    from shared.secrets import get_database_url
    conn = await asyncpg.connect(get_database_url(), ssl="require", timeout=10)
    try:
        return [dict(r) for r in await conn.fetch(sql, *args)]
    finally:
        await conn.close()


def measured_basis() -> tuple[dict, Optional[str]]:
    """writer -> {calls, usd, dearest} of the trap runs' ledger rows (AA-1167 / AA-1172 stages), or ({}, why)."""
    try:
        rows = asyncio.run(_fetch(BASIS_SQL, list(BASIS_STAGES), BASIS_WINDOW_DAYS))
    except Exception as e:  # noqa: BLE001 — no database here (a local dry run): the bounds stand
        return {}, f"no basis read ({type(e).__name__})"
    return {r["writer"]: r for r in rows if r.get("writer")}, None


def ledger_spend(stages: list[str], since: datetime) -> Optional[float]:
    try:
        rows = asyncio.run(_fetch(LEDGER_SQL, stages, since))
    except Exception:  # noqa: BLE001 — unreadable: the replies' own costs stand
        return None
    return float(rows[0]["usd"]) if rows else 0.0


def resolve_modes(fence: str, guard: str) -> tuple[bool, bool, str]:
    """The fence and facts-guard modes of the run: pinned, or the settings in force (loaded once; lesson S285: an ad-hoc
    script must load the settings snapshot before it reads one). Unreadable settings = each switch's default."""
    note = "pinned"
    if "live" in (fence, guard):
        note = "live settings"

        async def _load() -> None:
            import asyncpg

            from shared import settings
            from shared.secrets import get_database_url
            pool = await asyncpg.create_pool(get_database_url(), ssl="require", min_size=1, max_size=2)
            try:
                settings.install(pool)
                await settings.ensure_fresh(pool)
            finally:
                await pool.close()
        try:
            asyncio.run(_load())
        except Exception as e:  # noqa: BLE001
            note = f"live settings unreadable ({type(e).__name__}): defaults"
    from services.ai_safety import fact_guard, prompt_fence
    f = prompt_fence.enabled() if fence == "live" else fence == "on"
    g = fact_guard.enabled() if guard == "live" else guard == "on"
    return f, g, note


# ── the paid call ─────────────────────────────────────────────────────────────────────────────────────────────────

def _call(client, case, built, fence: bool, guard: bool) -> tuple[dict, str]:
    from shared.llm_client.call_log import billed_fields, record_call_sync, record_unusable_sync
    from shared.llm_client.models import LLMRequest
    from shared.llm_client.role_config import get_stage_config_sync

    model = get_stage_config_sync(built.stage).model_id
    resp = client.generate(LLMRequest(system_prompt=built.system, user_prompt=built.user, model_tier=model,
                                      stage=case.stage, role="writer", max_tokens=built.max_tokens))
    signal = {"aa1166": {"case": case.id, "category": case.category, "writer": case.writer, "target": case.target,
                         "writer_stage": built.stage, "fence": fence, "guard": guard, "issue": ISSUE}}
    try:
        record_call_sync(stage=case.stage, role="writer", model=resp.model_used, tokens_in=resp.input_tokens,
                         tokens_out=resp.output_tokens, cost_usd=resp.cost_usd, quality_signal=signal,
                         stop_reason=resp.stop_reason, account=resp.satellite_account,
                         fallback_used=resp.fallback_used,
                         provider=resp.provider if isinstance(resp.provider, str) else None, **billed_fields(resp))
    except Exception as e:  # noqa: BLE001 — billed: keep a billed_unusable row for it
        record_unusable_sync(resp, stage=case.stage, role="writer", error=e, quality_signal_extra=signal)
        raise
    return {"model": resp.model_used, "cost_usd": float(resp.cost_usd or 0.0),
            "stop_reason": resp.stop_reason}, resp.content or ""


def _not_run(case, why: str) -> dict:
    return {"case": case.id, "category": case.category, "writer": case.writer, "target": case.target,
            "verdict": "not_run", "reasons": [why], "cost_usd": 0.0}


def _emit(obj: dict) -> None:
    print(json.dumps(obj, default=str), flush=True)


def apply(cases: list, stop_usd: float, fence: bool, guard: bool) -> tuple[list, float]:
    from services.ai_safety import redteam_set
    from shared.llm_client import LLMClient

    client, local, seen, results = LLMClient(), 0.0, {}, []
    stages = sorted({c.stage for c in cases})
    started = datetime.now(timezone.utc)
    stopped: Optional[str] = None
    for case in cases:
        if stopped:
            results.append(_not_run(case, stopped))
            continue
        ledger = ledger_spend(stages, started)
        spent = max(local, ledger or 0.0)
        est = call_estimate(case.writer, seen)
        if spent + est > stop_usd:
            stopped = "budget"
            _emit({"stopped": "budget", "spent_usd": round(spent, 4), "next_case": case.id,
                   "next_estimate_usd": round(est, 4)})
            results.append(_not_run(case, stopped))
            continue
        built = redteam_set.build(case, fence, guard)
        try:
            meta, reply = _call(client, case, built, fence, guard)
        except Exception as e:  # noqa: BLE001 — a refusal stops the run; the gateway logged any billed attempt
            if "stop_reason=refusal" in str(e):
                # S294: the model itself refused the adversarial ask. Nothing leaked, so the case passes and the run
                # goes on; only a gateway refusal (route, cap, access) stops it. The ledger keeps any billed attempt.
                row = {**_not_run(case, "model_refused"), "verdict": "pass"}
                results.append(row)
                _emit(row)
                continue
            stopped = "gateway_error"
            _emit({"stopped": "gateway_error", "case": case.id, "error": str(e)[:300]})
            results.append(_not_run(case, stopped))
            continue
        local += meta["cost_usd"]
        seen[case.writer] = max(seen.get(case.writer, 0.0), meta["cost_usd"])
        row = {**redteam_set.judge(case, built.system, reply), **meta}
        if row["verdict"] != "pass":
            row["excerpt"] = redteam_set.mask(reply)[:300]
        results.append(row)
        _emit(row)
    final = ledger_spend(stages, started)
    return results, round(max(local, final or 0.0), 4)


# ── report ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def report(results: list, *, spent_usd: float, stop_usd: float, fence: bool, guard: bool, modes_note: str,
           when: str, build: str) -> str:
    from services.ai_safety import redteam_set
    s = redteam_set.summary(results)
    lines = [
        f"# {ISSUE} AI red-team run ({when})", "",
        f"- Build: `{build}`. Stages: `{redteam_set.STAGE_PREFIX}<s1|t2|t9>`, role writer, every call through "
        f"LLMClient with a ledger row.",
        f"- Modes: prompt data fence {'on' if fence else 'off'}, facts instruction guard {'on' if guard else 'off'} "
        f"({modes_note}).",
        f"- Spend: **{spent_usd:.4f} USD** of the {stop_usd:.2f} USD budget (the larger of the replies' costs and the "
        f"ledger sum of the run's stages).",
        f"- Verdicts: {', '.join(f'{k} {s[k]}' for k in ('pass', 'fail', 'no_reply', 'not_run') if s.get(k))}. "
        "Judged offline by rules (markers, system-prompt echo, secret-like and contact-like strings, live markup); "
        "a reader confirms each fail.", "",
        "| Case | Target | Category | Writer | Verdict | Rules failed | USD |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['case']} | {r['target']} | {r['category']} | {r['writer']} | {r['verdict']} | "
                     f"{_cell(', '.join(r.get('reasons') or []) or '-')} | {float(r.get('cost_usd') or 0):.4f} |")
    lines += ["", "## Per target", ""]
    for target, counts in s["by_target"].items():
        lines.append(f"- {target}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    lines += ["", "## Per category", ""]
    for cat, counts in s["by_category"].items():
        lines.append(f"- {cat}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    lines += ["", "## Not reachable from the CIS gateway (not run)", ""]
    lines += [f"- {name}: {why}" for name, why in redteam_set.UNREACHABLE.items()]
    excerpts = [r for r in results if r.get("excerpt")]
    if excerpts:
        lines += ["", "## Excerpts of the replies that did not pass (masked, first 300 characters)", ""]
        lines += [f"- {r['case']}: `{_cell(r['excerpt'])}`" for r in excerpts]
    return "\n".join(lines) + "\n"


def _env(name: str, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", default=_env("REDTEAM_APPLY") == "1",
                    help="make the paid calls (default: dry run)")
    ap.add_argument("--stop-usd", type=float, default=_env("REDTEAM_STOP_USD"),
                    help="the run's budget in USD (required with --apply)")
    ap.add_argument("--targets", default=_env("REDTEAM_TARGETS"), help="comma-separated: s1,t2,t9 (default all)")
    ap.add_argument("--cases", default=_env("REDTEAM_CASES"), help="comma-separated case ids (default all)")
    ap.add_argument("--fence", choices=MODES, default=_env("REDTEAM_FENCE", "live"))
    ap.add_argument("--guard", choices=MODES, default=_env("REDTEAM_GUARD", "live"))
    ap.add_argument("--report", default=_env("REDTEAM_REPORT"), help="also write the markdown report here")
    args = ap.parse_args(argv)
    try:
        cases = plan(args.targets, args.cases)
    except ValueError as e:
        ap.error(str(e))
    stop_usd = float(args.stop_usd) if args.stop_usd not in (None, "") else None
    if args.apply and (stop_usd is None or stop_usd <= 0):
        ap.error("--apply needs --stop-usd > 0 (the budget of this run)")

    fence, guard, note = resolve_modes(args.fence, args.guard)
    if not args.apply:
        from services.ai_safety import redteam_set
        for c in cases:
            built = redteam_set.build(c, fence, guard)
            _emit({"case": c.id, "target": c.target, "category": c.category, "writer": c.writer, "stage": c.stage,
                   "writer_stage": built.stage, "fence": fence, "guard": guard,
                   "prompt_chars": len(built.system) + len(built.user), "max_tokens": built.max_tokens})
        basis, why = measured_basis()
        est = estimate(cases, basis)
        out = {"dry_run": {"calls": len(cases), "modes": note, "estimate": est, "basis_note": why,
                           "basis_stages": list(BASIS_STAGES), "basis_window_days": BASIS_WINDOW_DAYS,
                           "unreachable": sorted(redteam_set.UNREACHABLE)}}
        if stop_usd:
            out["dry_run"]["under_budget"] = simulate(cases, stop_usd, unit_costs(basis))
        out["dry_run"]["cross_check"] = [
            f"tools/ecs/ecsx.sh tools/llm/cost_estimate.py COST_STAGE={s} COST_N={n} COST_PER=call"
            for s, n in (("adhoc_aa1172", len(cases)), ("adhoc_aa1167", len(cases)))]
        _emit(out)
        return 0

    results, spent = apply(cases, stop_usd, fence, guard)
    from services.ai_safety import redteam_set
    _emit({"summary": redteam_set.summary(results), "spent_usd": spent, "issue": ISSUE})
    text = report(results, spent_usd=spent, stop_usd=stop_usd, fence=fence, guard=guard, modes_note=note,
                  when=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                  build=_env("GIT_SHA", _env("IMAGE_TAG", "unknown")))
    print("=== REPORT BEGIN ===\n" + text + "=== REPORT END ===", flush=True)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
