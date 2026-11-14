#!/usr/bin/env python3
"""AA-1077 (S280) — $0 replay: which stored short blogs the lengthening repair could reach, before it is switched on.
READ-ONLY: no model call, no write (the session is set read-only and checked). Prints 8-char ids, numbers, statuses,
reason codes and plan reasons only: never text.

S279: 25 of the 37 held audit-tenant blog pieces carry F4 length (too short). The extend repair (length_extend.py) can
only add what the piece's own source tells and the piece does not use yet. Before the setting cis.t9.blog_length_extend
is turned on for an audit tenant (and before a paid trial), this replay says, per stored blog piece, what the repair
would do with it, through the code of the checkout it runs in (the plan is the loop's `plan_after_repair`, switch on):

  stored    chars / words of the stored text, its F4 side (contract verdict) and status;
  hold      the hold reason codes (gate_eval.hold_reason_codes) and whether the piece is held for length ONLY;
  recheck   whether the post-normalizer re-check held it (writer_input.length_trace.held: approved by the loop, then cut
            under the floor by a mandatory normalizer step; the in-loop repair never sees those);
  plan      the expand plan on the stored text: `expand` (with the untold seed facts offered, the sections chosen and
            the chars it must add to reach the target), or why not (`at_or_over_floor`, `no_sections`,
            `no_untold_facts`);
  trace     what the piece's own repair rounds recorded (`length_net.expand`, the `length_extend` entry) when the switch
            was on for it, and `length_trace.extend` (S280: the switch the loop resolved for the piece).

Summary: short pieces, short held for length only, the plan reasons, `reachable` (short with an expand plan) and
`reachable_length_only` (the pieces the repair could approve if its paragraphs pass every gate: the trial's best case),
`no_material` (short, no untold fact: only the T7 seed floor helps them), `recheck_held`.

Run by the orchestrator in aa-cis-dev-api (cwd /app; container DATABASE_URL = cis_app, ssl=require), one line:
    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1077/extend_plan_replay.py SINCE=2026-11-10T05:49:46Z

Env
  SINCE     (required) ISO start; blog pieces created at or after it.
  TENANTS   (optional) comma-separated tenant slugs; default the four AA-741 audit tenants.
  EXCLUDE   (optional) comma-separated 8-char piece ids to leave out (default the S238 synthetic piece d67e3178).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any

sys.path.insert(0, os.getcwd())

AUDIT_TENANTS = "audit-atlas-hearth,audit-terra-family,audit-trail-pulse,audit-wildkind-travel"
DEFAULT_EXCLUDE = "d67e3178"
PLAN_REASONS = ("expand", "at_or_over_floor", "no_sections", "no_untold_facts", "no_seed")

PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, t.slug AS tenant, cp.status, cp.held_reason, cp.content_text,
           cp.gate_ledger, cp.repair_log, cp.writer_input, cp.seo_title, cp.meta_description, cp.slug, cp.created_at
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    WHERE t.slug = ANY($1::text[]) AND cp.channel = 'blog' AND cp.created_at >= $2
      AND cp.status IN ('approved', 'held')
    ORDER BY t.slug, cp.created_at
"""


def _j(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _source(writer_input: dict, row: dict) -> str:
    """The grounding text the loop's extend plan reads (the writer's FactSheet render), rebuilt from writer_input the
    way the writer builds it (services.writing_memory.replay.PieceReplay, as extend_trial.py)."""
    from services.writing_memory.replay import PieceReplay
    pr = PieceReplay(writer_input, "blog", {k: row.get(k) for k in ("seo_title", "meta_description", "slug")})
    return pr.atom_text


def _own_rounds(repair_log: list) -> dict:
    """What the piece's own repair rounds recorded about the lengthening repair (switch on for it), else {}."""
    from services.acp_content_writing import length_extend as le
    out: dict = {}
    for e in repair_log or []:
        if not isinstance(e, dict):
            continue
        if isinstance(e.get("length_net"), dict):
            out["expand_reason"] = e["length_net"].get("expand")
        if isinstance(e.get(le.EXTEND_MODE), dict):
            ext = e[le.EXTEND_MODE]
            out["extend"] = {k: ext.get(k) for k in ("added", "chars_before", "chars_after", "reached_floor")}
    return out


def replay_piece(row: dict, *, source_of=_source) -> dict:
    """One piece's line (pure but for the service imports; unit-tested with `source_of` injected)."""
    from services.acp_content_writing import gate_eval, length_contract as lc, length_extend as le
    ledger = _j(row.get("gate_ledger")) or []
    repair_log = _j(row.get("repair_log")) or []
    wi = _j(row.get("writer_input")) or {}
    text = row.get("content_text") or ""
    chars = lc.measure(text)
    side = lc.side(chars)
    status = row.get("status")
    codes = gate_eval.hold_reason_codes(ledger, row.get("held_reason")) if status == "held" else []
    hold_capable = {r["source"] for r in gate_eval.failing_gates(ledger) if r["may_hold"]}
    trace = wi.get("length_trace") if isinstance(wi.get("length_trace"), dict) else {}
    out = {"piece": str(row["piece_id"])[:8], "tenant": row.get("tenant"), "status": status, "chars": chars,
           "words": lc.words(text), "side": side or "in", "codes": codes,
           "length_only": bool(status == "held" and side == lc.SHORT and hold_capable <= {le.qr.F4_EXTREME_LENGTH}
                               and lc.CODE_SHORT in codes),
           "recheck_held": bool(trace.get("held")), "switch_recorded": trace.get("extend"),
           "own_rounds": _own_rounds(repair_log)}
    if not wi.get("seed"):
        out["plan"] = "no_seed"
        return out
    plan, why = le.plan_after_repair(text, before=None, channel="blog", source=source_of(wi, row), switch=le.EXTEND_ON)
    out["plan"] = why
    if plan is not None:
        out.update(facts_offered=len(plan["facts"]), sections=len(plan["sections"]),
                   words_asked=sum(s["words"] for s in plan["sections"]), deficit_chars=plan["deficit_chars"])
    return out


def summarize(rows: list[dict]) -> dict:
    from services.acp_content_writing import length_contract as lc
    short = [r for r in rows if r["side"] == lc.SHORT]
    return {
        "pieces": len(rows), "floor_chars": lc.floor_chars(), "target_chars": lc.target_chars(),
        "status": {s: sum(r["status"] == s for r in rows) for s in ("approved", "held")},
        "short": len(short),
        "short_held_length_only": sorted(r["piece"] for r in short if r["length_only"]),
        "plan_reasons": {p: sum(r["plan"] == p for r in short) for p in PLAN_REASONS},
        "reachable": sorted(r["piece"] for r in short if r["plan"] == "expand"),
        "reachable_length_only": sorted(r["piece"] for r in short if r["plan"] == "expand" and r["length_only"]),
        "no_material": sorted(r["piece"] for r in short if r["plan"] == "no_untold_facts"),
        "recheck_held": sorted(r["piece"] for r in rows if r["recheck_held"]),
        "median_deficit_chars": _median([r["deficit_chars"] for r in short if "deficit_chars" in r]),
        "switch_recorded": {str(k): sum(r["switch_recorded"] == k for r in rows)
                            for k in sorted({r["switch_recorded"] for r in rows}, key=str)},
    }


def _median(values: list) -> Any:
    if not values:
        return None
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


async def main() -> int:
    import asyncpg
    if not os.environ.get("SINCE"):
        print("STOP SINCE is required (ISO)")
        return 2
    since = datetime.fromisoformat(os.environ["SINCE"].replace("Z", "+00:00"))
    tenants = [t.strip() for t in (os.environ.get("TENANTS") or AUDIT_TENANTS).split(",") if t.strip()]
    exclude = {p.strip() for p in (os.environ.get("EXCLUDE") or DEFAULT_EXCLUDE).split(",") if p.strip()}
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await conn.execute("SET default_transaction_read_only = on")
        assert (await conn.fetchval("SHOW default_transaction_read_only")) == "on"
        raw = [dict(r) for r in await conn.fetch(PIECES_SQL, tenants, since)]
    finally:
        await conn.close()
    rows = []
    for r in raw:
        if str(r["piece_id"])[:8] in exclude:
            continue
        try:
            rows.append(replay_piece(r))
        except Exception as e:  # noqa: BLE001 — one piece that cannot be replayed is reported, never fatal
            print(f"ERROR {str(r['piece_id'])[:8]} {type(e).__name__}: {str(e)[:160]}")
    for r in rows:
        print("PIECE " + json.dumps(r, sort_keys=True, default=str))
    print("SUMMARY " + json.dumps(summarize(rows), sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
