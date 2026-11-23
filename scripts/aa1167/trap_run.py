#!/usr/bin/env python3
"""AA-1167 / AA-1172 — the prompt-injection trap run on the current writers. PAID with --apply: one model call per trap
and mode; nothing else. Written for the tech lead's session; CI never runs it (the offline half is
tests/unit/test_aa1167_trap_set.py and tests/unit/test_aa1172_trap_guard.py).

The trap set (services/ai_safety/trap_set.py: 13 invented traps over S1 v2, T9 blog, the T9 FAQ writer and T2; five of
them plant an instruction in a T9 fact row) is built with the live prompt builders for each fence mode asked (--fence
on, off or both) and each Facts instruction guard mode asked (--guard on, off or both; ``both`` adds the guard-off run
of the fact traps only, the control that shows each fact trap fires without the guard). Each call goes through the
Model Gateway (``LLMClient.generate``) on the stage given by --stage (``adhoc_aa<issue>``; default ``adhoc_aa1167``)
with the model the writer's own stage routes to today (its ``shared.llm_role_config`` row), and writes one
``shared.llm_call_log`` row (role writer, ``**billed_fields(resp)``, ``quality_signal.aa1167`` = trap, writer, fence and
guard mode, issue). Each reply is scored offline (trap_set.evaluate: no trap marker in the reply, no live markup after
the output sanitizer, no fence tag).

  python3 scripts/aa1167/trap_run.py                         # dry run: the calls it would make, no client, no cost
  python3 scripts/aa1167/trap_run.py --apply --stop-usd 1.0  # paid: fence on, guard on (13 calls)
  python3 scripts/aa1167/trap_run.py --apply --stop-usd 0.95 --fence off --guard both --stage adhoc_aa1172
                                                             # AA-1172: 13 guard-on calls + the 5 guard-off controls
  python3 scripts/aa1167/trap_run.py --traps t9_facts_override,t9_facts_provenance   # only these traps

``--stop-usd`` has no default: the operator states the budget of the run. Before each call the run stops when the spend
so far plus the estimate of that call would pass it (the estimate: the dearest call of the same writer so far, else a
fixed per-writer bound), so a run never ends above its budget by a whole call; it also stops at the first gateway
refusal. Output: one JSON line per call and a summary (pass rate per writer and kind, per mode, spend) on stdout;
replies are about the invented tour only. Run where the gateway reaches the models and DATABASE_URL is set (the worker
container), the repo on PYTHONPATH.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

STAGE = "adhoc_aa1167"
_STAGE_RE = re.compile(r"^adhoc_aa\d+$")
# technical-constant: the upper bound of one call per writer before the run has seen one (USD; the S287 run averaged
# 0.045 USD a call over all writers, the T9 blog being the dearest). Used only to stop before the budget is passed.
_FIRST_CALL_USD = {"t9_blog": 0.10, "s1_v2": 0.08, "t2": 0.08, "t9_faq": 0.02}


def _modes(fence: str) -> list[bool]:
    return {"on": [True], "off": [False], "both": [True, False]}[fence]


def _traps(traps) -> tuple[str, ...] | None:
    if traps is None:
        return None
    ids = tuple(t.strip() for t in (traps.split(",") if isinstance(traps, str) else traps) if t.strip())
    from services.ai_safety import trap_set
    unknown = sorted(set(ids) - {t.id for t in trap_set.TRAPS})
    if unknown:
        raise ValueError(f"unknown trap id(s): {', '.join(unknown)}")
    return ids


def plan(fence: str, guard: str = "on", traps=None) -> list:
    """(fence on?, guard on?, case) per call. ``guard`` 'both' = every trap with the guard on, then the fact traps
    (the only ones the guard changes) with it off."""
    from services.ai_safety import trap_set
    ids = _traps(traps)
    out = []
    for on in _modes(fence):
        if guard in ("on", "both"):
            out += [(on, True, c) for c in trap_set.cases(on, guard=True, traps=ids)]
        if guard in ("off", "both"):
            out += [(on, False, c) for c in trap_set.cases(on, guard=False, traps=ids)
                    if guard == "off" or c.trap.guarded]
    return out


def dry_run(fence: str, guard: str = "on", traps=None) -> dict:
    rows = [{"trap": c.trap.id, "writer": c.writer, "stage": c.stage, "fence": on, "guard": g,
             "system_chars": len(c.system), "user_chars": len(c.user), "max_tokens": c.max_tokens}
            for on, g, c in plan(fence, guard, traps)]
    for r in rows:
        print(json.dumps(r))
    return {"calls": len(rows), "prompt_chars": sum(r["system_chars"] + r["user_chars"] for r in rows),
            "max_output_tokens": sum(r["max_tokens"] for r in rows),
            "first_call_bound_usd": round(sum(_FIRST_CALL_USD.get(r["writer"], 0.1) for r in rows), 2)}


def _call(client, stage: str, on: bool, guard: bool, case) -> dict:
    from shared.llm_client.call_log import billed_fields, record_call_sync
    from shared.llm_client.models import LLMRequest
    from shared.llm_client.role_config import get_stage_config_sync
    from services.ai_safety import trap_set

    model = get_stage_config_sync(case.stage).model_id
    resp = client.generate(LLMRequest(system_prompt=case.system, user_prompt=case.user, model_tier=model,
                                      stage=stage, role="writer", max_tokens=case.max_tokens))
    signal = {"aa1167": {"trap": case.trap.id, "kind": case.trap.kind, "writer": case.writer,
                         "writer_stage": case.stage, "fence": on, "guard": guard,
                         "issue": "AA-" + stage.removeprefix("adhoc_aa")}}
    record_call_sync(stage=stage, role="writer", model=resp.model_used, tokens_in=resp.input_tokens,
                     tokens_out=resp.output_tokens, cost_usd=resp.cost_usd, quality_signal=signal,
                     stop_reason=resp.stop_reason, account=resp.satellite_account, fallback_used=resp.fallback_used,
                     provider=resp.provider if isinstance(resp.provider, str) else None, **billed_fields(resp))
    return {**trap_set.evaluate(case.trap, resp.content), "fence": on, "guard": guard, "model": resp.model_used,
            "cost_usd": resp.cost_usd or 0.0, "reply_tail": (resp.content or "")[-400:]}


def next_call_estimate(writer: str, seen: dict) -> float:
    """What the next call of ``writer`` may cost: the dearest one of that writer so far, else the fixed bound."""
    return max(seen.get(writer, 0.0), _FIRST_CALL_USD.get(writer, 0.1))


def apply(fence: str, stop_usd: float, guard: str = "on", traps=None, stage: str = STAGE) -> dict:
    from shared.llm_client import LLMClient
    from services.ai_safety import trap_set

    client, spent, results, seen = LLMClient(), 0.0, [], {}
    for on, g, case in plan(fence, guard, traps):
        est = next_call_estimate(case.writer, seen)
        if spent + est > stop_usd:
            print(json.dumps({"stopped": "budget", "spent_usd": round(spent, 4), "next_trap": case.trap.id,
                              "next_estimate_usd": round(est, 4)}))
            break
        try:
            r = _call(client, stage, on, g, case)
        except Exception as e:  # noqa: BLE001 — a refusal stops the run; the gateway logged any billed attempt
            print(json.dumps({"stopped": "gateway_error", "trap": case.trap.id, "error": str(e)[:300]}))
            break
        spent += r["cost_usd"]
        seen[case.writer] = max(seen.get(case.writer, 0.0), r["cost_usd"])
        results.append(r)
        print(json.dumps(r))
    out = {"issue": "AA-" + stage.removeprefix("adhoc_aa"), "stage": stage, "spent_usd": round(spent, 4)}
    for on in _modes(fence):
        for g in (True, False):
            part = [r for r in results if r["fence"] is on and r["guard"] is g]
            if part:
                out[f"fence_{'on' if on else 'off'}_guard_{'on' if g else 'off'}"] = trap_set.summary(part)
    return out


def _stage(value: str) -> str:
    if not _STAGE_RE.match(value):
        raise argparse.ArgumentTypeError("--stage must look like adhoc_aa<issue number>, for example adhoc_aa1172")
    return value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="make the paid calls (default: dry run)")
    ap.add_argument("--fence", choices=("on", "off", "both"), default="on")
    ap.add_argument("--guard", choices=("on", "off", "both"), default="on",
                    help="Facts instruction guard mode of the fact traps ('both' adds the guard-off control)")
    ap.add_argument("--traps", help="comma-separated trap ids (default: every trap)")
    ap.add_argument("--stage", type=_stage, default=STAGE, help="llm_call_log stage, adhoc_aa<issue>")
    ap.add_argument("--stop-usd", type=float, help="the run's budget in USD (required with --apply)")
    args = ap.parse_args(argv)
    try:
        _traps(args.traps)
    except ValueError as e:
        ap.error(str(e))
    if not args.apply:
        print(json.dumps({"dry_run": dry_run(args.fence, args.guard, args.traps)}))
        return 0
    if args.stop_usd is None or args.stop_usd <= 0:
        ap.error("--apply needs --stop-usd > 0 (the budget of this run)")
    print(json.dumps({"summary": apply(args.fence, args.stop_usd, args.guard, args.traps, args.stage)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
