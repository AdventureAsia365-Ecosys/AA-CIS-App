#!/usr/bin/env python3
"""AA-1077 S278 — $0 replay of the blog length margin over stored pieces. READ-ONLY: no model call, no write (the
session is set read-only and checked). Prints 8-char ids, numbers and verdicts only: never text.

Reads each blog piece's `writer_input.length_trace` (chars after each normalizer step, written since AA-1077) and says
what the S278 change would do to it:

  cuts        chars each normalizer step took (loop → stock_cut → authenticity_cut → faq_answer_fit → final_integrity);
  guard       the stored chars under the new normalizer rule (length_contract.floor_guard): an OPTIONAL step's cut
              (length_contract.NORMALIZER_STEPS "floor_guarded") that left the blog under the floor and shorter is not
              made; mandatory cuts stay. MEASURED from the trace, exact for the step the guard keeps;
  projected   the loop chars scaled by the writer's new word floor over the one the piece was written with
              (length_contract.target_words() / OLD_WORDS, default 900 = t9_write v5), minus the cuts the new rule
              still makes. A PROJECTION (the writer undershot its own 900-word floor by the same ratio), not a
              measurement: it shows which pieces the margin is meant to move, the next live run measures it;
  change      "guard" when the guarded stored chars differ, "target" when only the projection clears the floor where
              the stored text did not, "none" otherwise; "no_trace" for a piece without a length trace.

Run by the orchestrator in aa-cis-dev-api (cwd /app; container DATABASE_URL = cis_app, ssl=require), one line:
    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1077/margin_replay.py P3_SINCE=2026-11-12T00:00:00Z

Env
  P3_SINCE   (required) ISO start; blog pieces created at or after it.
  TENANTS    (optional) comma-separated tenant slugs; default the four AA-741 audit tenants.
  OLD_WORDS  (optional) the writer's word floor the pieces were written with (default 900).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

AUDIT_TENANTS = "audit-atlas-hearth,audit-terra-family,audit-trail-pulse,audit-wildkind-travel"
OLD_WORDS_DEFAULT = 900
STEPS = ("loop", "stock_cut", "authenticity_cut", "faq_answer_fit", "final_integrity")

PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, t.slug AS tenant, cp.status, cp.content_text, cp.writer_input
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    WHERE t.slug = ANY($1::text[]) AND cp.channel = 'blog' AND cp.created_at >= $2
      AND cp.status IN ('approved', 'held')
    ORDER BY t.slug, cp.created_at
"""


def _j(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def step_cuts(chars: dict) -> list[tuple[str, int, int]]:
    """[(step, chars before it, chars after it)] for each normalizer step the trace holds, in production order."""
    out, prev = [], chars.get("loop")
    for step in STEPS[1:]:
        if step not in chars or prev is None:
            continue
        out.append((step, int(prev), int(chars[step])))
        prev = chars[step]
    return out


def replay_trace(chars: dict, *, old_words: int = OLD_WORDS_DEFAULT) -> Optional[dict]:
    """The replay of one length trace (pure; unit-tested with the S278 numbers). None without a `loop` reading."""
    from services.acp_content_writing import length_contract as lc
    if not isinstance(chars, dict) or chars.get("loop") is None:
        return None
    floor = lc.floor_chars()
    loop = int(chars["loop"])
    cuts = step_cuts(chars)
    stored = cuts[-1][2] if cuts else loop
    guarded, kept, mandatory_cut = loop, [], 0
    for step, before, after in cuts:
        cut = before - after
        optional = lc.NORMALIZER_STEPS.get(step) == "floor_guarded"
        # the guard reads the text as it stands after the earlier steps under the NEW rule
        new_after = guarded - cut
        if optional and cut > 0 and new_after < floor and new_after < guarded:
            kept.append(step)
            continue
        guarded = new_after
        if not optional:
            mandatory_cut += cut
    projected = round(loop * lc.target_words() / old_words) - (loop - guarded)
    change = ("guard" if guarded != stored else
              "target" if stored < floor <= projected else "none")
    return {"loop": loop, "stored": stored, "cuts": {s: b - a for s, b, a in cuts if b != a},
            "guarded": guarded, "floor_kept": kept, "projected": projected, "mandatory_cut": mandatory_cut,
            "stored_verdict": "in" if stored >= floor else lc.CODE_SHORT,
            "guarded_verdict": "in" if guarded >= floor else lc.CODE_SHORT,
            "projected_verdict": "in" if projected >= floor else lc.CODE_SHORT, "change": change}


def replay_piece(row: dict, *, old_words: int = OLD_WORDS_DEFAULT) -> dict:
    from services.acp_content_writing import length_contract as lc
    trace = (_j(row.get("writer_input")) or {}).get("length_trace")
    out = {"piece": str(row["piece_id"])[:8], "tenant": row.get("tenant"), "status": row.get("status"),
           "stored_chars": lc.measure(row.get("content_text") or "")}
    r = replay_trace((trace or {}).get("chars") if isinstance(trace, dict) else None, old_words=old_words)
    if r is None:
        out.update(change="no_trace")
        return out
    out.update(r, attempt=trace.get("attempt"))
    return out


def summarize(rows: list[dict]) -> dict:
    from services.acp_content_writing import length_contract as lc
    traced = [r for r in rows if r["change"] != "no_trace"]
    return {
        "pieces": len(rows), "traced": len(traced), "floor_chars": lc.floor_chars(),
        "target_chars": lc.target_chars(), "target_words": lc.target_words(),
        "short_stored": sorted(r["piece"] for r in traced if r["stored_verdict"] != "in"),
        "short_guarded": sorted(r["piece"] for r in traced if r["guarded_verdict"] != "in"),
        "short_projected": sorted(r["piece"] for r in traced if r["projected_verdict"] != "in"),
        "changed_by_guard": sorted(r["piece"] for r in traced if r["change"] == "guard"),
        "cleared_by_target": sorted(r["piece"] for r in traced if r["change"] == "target"),
        "largest_cut": max((max(r["cuts"].values(), default=0) for r in traced), default=0),
    }


async def main() -> int:
    import asyncpg
    if not os.environ.get("P3_SINCE"):
        print("STOP P3_SINCE is required (ISO)")
        return 2
    since = datetime.fromisoformat(os.environ["P3_SINCE"].replace("Z", "+00:00"))
    tenants = [t.strip() for t in (os.environ.get("TENANTS") or AUDIT_TENANTS).split(",") if t.strip()]
    old_words = int(os.environ.get("OLD_WORDS") or OLD_WORDS_DEFAULT)
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await conn.execute("SET default_transaction_read_only = on")
        assert (await conn.fetchval("SHOW default_transaction_read_only")) == "on"
        raw = [dict(r) for r in await conn.fetch(PIECES_SQL, tenants, since)]
    finally:
        await conn.close()
    rows = [replay_piece(r, old_words=old_words) for r in raw]
    for r in rows:
        if r["change"] == "no_trace":
            print(f"PIECE {r['piece']} {r['tenant']} status={r['status']} stored={r['stored_chars']} no_trace")
            continue
        cuts = ",".join(f"{k}:{v}" for k, v in r["cuts"].items()) or "-"
        print(f"PIECE {r['piece']} {r['tenant']} status={r['status']} attempt={r.get('attempt')} loop={r['loop']} "
              f"cuts={cuts} stored={r['stored']} ({r['stored_verdict']}) guarded={r['guarded']} "
              f"({r['guarded_verdict']}) kept={','.join(r['floor_kept']) or '-'} projected={r['projected']} "
              f"({r['projected_verdict']}) change={r['change']}")
    print("SUMMARY " + json.dumps(summarize(rows), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
