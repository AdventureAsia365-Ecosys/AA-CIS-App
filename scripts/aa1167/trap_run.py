#!/usr/bin/env python3
"""AA-1167 — the prompt-injection trap run on the current writers. PAID with --apply: one model call per trap and fence
mode; nothing else. Written for the tech lead's session; CI never runs it (the offline half is
tests/unit/test_aa1167_trap_set.py).

The trap set (services/ai_safety/trap_set.py: 9 invented traps over S1 v2, T9 blog, the T9 FAQ writer and T2) is built
with the live prompt builders for each fence mode asked (--fence on, off or both). Each call goes through the Model
Gateway (``LLMClient.generate``) on stage ``adhoc_aa1167`` with the model the writer's own stage routes to today (its
``shared.llm_role_config`` row), and writes one ``shared.llm_call_log`` row (role writer, ``**billed_fields(resp)``,
``quality_signal.aa1167`` = trap, writer, fence mode). Each reply is scored offline (trap_set.evaluate: no trap marker
in the reply, no live markup after the output sanitizer, no fence tag).

  python3 scripts/aa1167/trap_run.py                         # dry run: the calls it would make, no client, no cost
  python3 scripts/aa1167/trap_run.py --apply --stop-usd 1.0  # paid: fence on (9 calls)
  python3 scripts/aa1167/trap_run.py --apply --stop-usd 1.0 --fence both   # on and off (18 calls)

``--stop-usd`` has no default: the operator states the budget of the run (the issue allows about 1 USD). The run stops
before the next call once the logged spend reached it, and at the first gateway refusal. Output: one JSON line per
call and a summary (pass rate per writer and kind, spend) on stdout; replies are about the invented tour only. Run
where the gateway reaches the models and DATABASE_URL is set (the worker container), the repo on PYTHONPATH.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

STAGE = "adhoc_aa1167"
ISSUE = "AA-1167"


def _modes(fence: str) -> list[bool]:
    return {"on": [True], "off": [False], "both": [True, False]}[fence]


def plan(fence: str) -> list:
    from services.ai_safety import trap_set
    return [(on, c) for on in _modes(fence) for c in trap_set.cases(on)]


def dry_run(fence: str) -> dict:
    rows = [{"trap": c.trap.id, "writer": c.writer, "stage": c.stage, "fence": on, "system_chars": len(c.system),
             "user_chars": len(c.user), "max_tokens": c.max_tokens} for on, c in plan(fence)]
    for r in rows:
        print(json.dumps(r))
    return {"calls": len(rows), "prompt_chars": sum(r["system_chars"] + r["user_chars"] for r in rows),
            "max_output_tokens": sum(r["max_tokens"] for r in rows)}


def _call(client, on: bool, case) -> dict:
    from shared.llm_client.call_log import billed_fields, record_call_sync
    from shared.llm_client.models import LLMRequest
    from shared.llm_client.role_config import get_stage_config_sync
    from services.ai_safety import trap_set

    model = get_stage_config_sync(case.stage).model_id
    resp = client.generate(LLMRequest(system_prompt=case.system, user_prompt=case.user, model_tier=model,
                                      stage=STAGE, role="writer", max_tokens=case.max_tokens))
    signal = {"aa1167": {"trap": case.trap.id, "kind": case.trap.kind, "writer": case.writer,
                         "writer_stage": case.stage, "fence": on}}
    record_call_sync(stage=STAGE, role="writer", model=resp.model_used, tokens_in=resp.input_tokens,
                     tokens_out=resp.output_tokens, cost_usd=resp.cost_usd, quality_signal=signal,
                     stop_reason=resp.stop_reason, account=resp.satellite_account, fallback_used=resp.fallback_used,
                     provider=resp.provider if isinstance(resp.provider, str) else None, **billed_fields(resp))
    return {**trap_set.evaluate(case.trap, resp.content), "fence": on, "model": resp.model_used,
            "cost_usd": resp.cost_usd or 0.0, "reply_tail": (resp.content or "")[-400:]}


def apply(fence: str, stop_usd: float) -> dict:
    from shared.llm_client import LLMClient
    from services.ai_safety import trap_set

    client, spent, results = LLMClient(), 0.0, []
    for on, case in plan(fence):
        if spent >= stop_usd:
            print(json.dumps({"stopped": "budget", "spent_usd": round(spent, 4)}))
            break
        try:
            r = _call(client, on, case)
        except Exception as e:  # noqa: BLE001 — a refusal stops the run; the gateway logged any billed attempt
            print(json.dumps({"stopped": "gateway_error", "trap": case.trap.id, "error": str(e)[:300]}))
            break
        spent += r["cost_usd"]
        results.append(r)
        print(json.dumps(r))
    out = {"issue": ISSUE, "stage": STAGE, "spent_usd": round(spent, 4)}
    for on in _modes(fence):
        out["fence_on" if on else "fence_off"] = trap_set.summary([r for r in results if r["fence"] is on])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="make the paid calls (default: dry run)")
    ap.add_argument("--fence", choices=("on", "off", "both"), default="on")
    ap.add_argument("--stop-usd", type=float, help="the run's budget in USD (required with --apply)")
    args = ap.parse_args(argv)
    if not args.apply:
        print(json.dumps({"dry_run": dry_run(args.fence)}))
        return 0
    if args.stop_usd is None or args.stop_usd <= 0:
        ap.error("--apply needs --stop-usd > 0 (the budget of this run)")
    print(json.dumps({"summary": apply(args.fence, args.stop_usd)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
