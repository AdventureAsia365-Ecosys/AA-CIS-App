#!/usr/bin/env python3
"""AA-1000 — offline calibration of the T10 tier-2 adjudicator (services/acp_content_writing/seed_align/adjudicate.py)
on the AA-789 eval set. PAID: `run` makes real model calls. Written for a later session; this PR does not run it.

Every call goes through the Model Gateway (`LLMClient.generate`) on stage `adhoc_aa1000`, role `judge`, model
`--model` (no route row for an ad-hoc stage, so the key is named), and writes one `shared.llm_call_log` row per reply
(the adjudicator's own logging, AA-927 shape). The run stops BEFORE a call whose worst-case cost (prompt tokens +
max_tokens at the catalog price) would take the spend past the cap; the cap defaults to and may never exceed
HARD_CAP_USD = $5 (plan §5: "cap Tier 2 one-off spend at $5").

Per eval item: tier 1 (seed alignment, needs spaCy + en_core_web_sm) runs first and gives the record tier 2 reads;
tier 2's triggers then pick the sentences tier 1 left alone; one call adjudicates them (≤ adjudicate.MAX_ITEMS).

  dry-run EVAL.jsonl
      No call, no client: counts of items with a trigger, triggers per class and the worst-case cost of a full run.
  run EVAL.jsonl OUT.jsonl [--cap USD] [--model KEY] [--repeats N] [--limit N]
      One prediction file per repeat (OUT.r1.jsonl, …): {"id", "flagged" (tier 2 alone), "class", "tier1_flagged",
      "combined_flagged", "triggers", "status"} per item, for the eval set's score.py (`--strict-class`). The summary
      (calls, spend vs cap, statuses, test-retest agreement across repeats) goes to stderr. Exit 3 when the cap
      stopped the run.

The eval set and the prediction files hold real tenant copy: keep both in the gitignored scratch folder, never in the
repo. Run where the gateway can reach the models and DATABASE_URL is set (the worker container, via ECS exec), with
the repo on PYTHONPATH.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Callable, Optional

HARD_CAP_USD = 5.0
STAGE = "adhoc_aa1000"
DEFAULT_MODEL = "gpt-6-luna"
# USD per million tokens (in, out), the catalog prices of the judge-route models (root docs/generated/llm-routes.md,
# 29/10/2026). A model not listed is refused: without a price the cap cannot be kept.
PRICES = {"gpt-6-luna": (0.10, 0.50), "gpt-5.6-luna": (0.20, 1.20), "gpt-6-luna-openai": (0.10, 0.50)}
CHARS_PER_TOKEN = 3.0  # conservative (English is ~4): over-estimates the prompt side


class Budget:
    """The run's spend against its cap. `allows` is asked before each call with that call's worst-case cost."""

    def __init__(self, cap_usd: float):
        if not 0 < cap_usd <= HARD_CAP_USD:
            raise ValueError(f"cap must be in (0, {HARD_CAP_USD}] USD, got {cap_usd}")
        self.cap = cap_usd
        self.spent = 0.0
        self.calls = 0

    def allows(self, estimate: float) -> bool:
        return self.spent + estimate <= self.cap

    def charge(self, cost: Optional[float], estimate: float) -> None:
        """The larger of the reply's own cost and the worst-case estimate: the count never runs below the real spend,
        so the cap holds even when a reply costs more than its estimate or carries no cost."""
        self.spent += max(cost or 0.0, estimate)
        self.calls += 1


def worst_case_cost(items, model: str) -> float:
    from services.acp_content_writing.seed_align import adjudicate as adj

    price_in, price_out = PRICES[model]
    tokens_in = (len(adj.SYSTEM_PROMPT) + len(adj.build_user_prompt(items))) / CHARS_PER_TOKEN
    return (tokens_in * price_in + adj.MAX_TOKENS * price_out) / 1e6


def tier1_record(text: str, seed: str) -> dict:
    """Tier 1 over one item, as the record tier 2 reads (every built class run in shadow)."""
    from services.acp_content_writing.seed_align import checks, seed_graph

    g = seed_graph.build(seed)
    found = checks.find_candidates(text, g, names=set(g.place_names))
    if found is None:
        sys.exit("spaCy or en_core_web_sm is not installed: tier 1 cannot run, so tier 2 has nothing to read")
    return {"status": "ok", "modes": {c: "shadow" for c in checks.BUILT_CLASSES},
            "candidates": [{"cls": c.cls, "sentence": c.sentence[:300]} for c in found]}


def _items(path: str, limit: Optional[int]) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
            if limit and len(out) >= limit:
                break
    return out


def _triggers(item: dict, t1: Callable[[str, str], dict]) -> tuple[dict, list]:
    from services.acp_content_writing.seed_align import adjudicate as adj

    rec = t1(item["text"], item["seed_excerpt"])
    return rec, adj.find_triggers(item["text"], item["seed_excerpt"], rec)


def dry_run(items: list[dict], *, model: str = DEFAULT_MODEL, t1: Callable[[str, str], dict] = tier1_record) -> dict:
    from services.acp_content_writing.seed_align import adjudicate as adj

    by_class: Counter = Counter()
    with_trigger, worst = 0, 0.0
    for item in items:
        _rec, trig = _triggers(item, t1)
        by_class.update(t.cls for t in trig[:adj.MAX_ITEMS])
        if trig:
            with_trigger += 1
            worst += worst_case_cost(trig[:adj.MAX_ITEMS], model)
    return {"items": len(items), "items_with_a_trigger": with_trigger, "by_class": dict(sorted(by_class.items())),
            "worst_case_usd_per_repeat": round(worst, 4), "hard_cap_usd": HARD_CAP_USD}


def calibrate(items: list[dict], out_paths: list[str], *, client, cap_usd: float = HARD_CAP_USD,
              model: str = DEFAULT_MODEL, t1: Callable[[str, str], dict] = tier1_record) -> dict:
    """Tier 1 + tier 2 over `items`, once per path in `out_paths` (the repeats). Stops before the call that could pass
    the cap. Returns the summary."""
    from services.acp_content_writing.seed_align import adjudicate as adj

    if model not in PRICES:
        raise ValueError(f"no price for model {model!r}: the cap cannot be kept")
    budget = Budget(cap_usd)
    statuses: Counter = Counter()
    flags: dict[str, list[bool]] = {}
    stopped = None
    prepared = [(item, *_triggers(item, t1)) for item in items]
    for path in out_paths:
        with open(path, "w", encoding="utf-8") as out:
            for item, rec, trig in prepared:
                row = {"id": item["id"], "flagged": False, "tier1_flagged": bool(rec["candidates"]),
                       "triggers": len(trig)}
                if not trig:
                    row["status"] = "no_trigger"
                elif stopped:
                    row["status"] = "not_run_cost_cap"
                else:
                    sent = trig[:adj.MAX_ITEMS]
                    estimate = worst_case_cost(sent, model)
                    if not budget.allows(estimate):
                        stopped = "cost_cap"
                        row["status"] = "not_run_cost_cap"
                    else:
                        try:
                            res = adj.adjudicate(sent, item["seed_excerpt"], client=client, stage=STAGE,
                                                 role=adj.ROLE, model_tier=model)
                            budget.charge(res.cost_usd, estimate)
                            row["status"] = "ok"
                            row["dropped"] = res.dropped
                            if res.findings:
                                row["flagged"] = True
                                row["class"] = res.findings[0][0].cls
                        except Exception as exc:  # a failed call may still be billed: count its worst case
                            budget.charge(None, estimate)
                            row["status"] = "error"
                            row["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                row["combined_flagged"] = row["flagged"] or row["tier1_flagged"]
                statuses[row["status"]] += 1
                flags.setdefault(item["id"], []).append(row["flagged"])
                out.write(json.dumps(row) + "\n")
    agree = [len(set(v)) == 1 for v in flags.values() if len(v) > 1]
    return {"items": len(items), "repeats": len(out_paths), "calls": budget.calls,
            "spent_usd": round(budget.spent, 4), "cap_usd": budget.cap, "stopped": stopped,
            "statuses": dict(sorted(statuses.items())),
            "test_retest": round(sum(agree) / len(agree), 3) if agree else None}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("dry-run", "run"))
    ap.add_argument("eval")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--cap", type=float, default=HARD_CAP_USD)
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(PRICES))
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    items = _items(a.eval, a.limit)
    if a.cmd == "dry-run":
        print(json.dumps(dry_run(items, model=a.model), indent=1), file=sys.stderr)
        return 0
    if not a.out:
        ap.error("run needs OUT.jsonl")
    if not 0 < a.cap <= HARD_CAP_USD:
        ap.error(f"--cap must be in (0, {HARD_CAP_USD}]")
    from shared.llm_client.client import LLMClient

    stem = a.out[:-6] if a.out.endswith(".jsonl") else a.out
    paths = [f"{stem}.r{k}.jsonl" for k in range(1, max(1, a.repeats) + 1)]
    summary = calibrate(items, paths, client=LLMClient(), cap_usd=a.cap, model=a.model)
    print(json.dumps(summary, indent=1), file=sys.stderr)
    return 3 if summary["stopped"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
