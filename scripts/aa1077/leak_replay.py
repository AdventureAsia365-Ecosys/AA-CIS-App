#!/usr/bin/env python3
"""AA-1077 round 2 (S278 review) — $0 replay of the widened F13_leak pattern over stored APPROVED T9 pieces. READ-ONLY:
no model call, no write (the session is set read-only and checked). Prints 8-char ids, channels and the matched
phrase only (a few words of the pattern itself): never the piece text.

An approved piece passed F13_leak under main's pattern (OLD_LEAK_PATTERN below, frozen here), so every sentence the
deployed `hygiene.LEAK_RE` matches and the old one does not is a NEW hit: a blocking F13_leak, a borrowed-sentence
delete and a refused expand paragraph the round-2 widening would cause on a piece that shipped. The review asked for
this count before the trial: the widening is meant to catch "the source notes name…" / "the itinerary says…", not
reader-facing copy ("The itinerary gives you a free afternoon…").

Run by the orchestrator AFTER the code deploy (the container must carry the new hygiene.LEAK_RE; the script stops
when it still carries the old one), in aa-cis-dev-api (cwd /app; container DATABASE_URL = cis_app, ssl=require):
    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1077/leak_replay.py

Env
  SINCE    (optional) ISO start; approved pieces created at or after it (default: all).
  TENANTS  (optional) comma-separated tenant slugs (default: every tenant).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.getcwd())

# main's LEAK_RE before AA-1077 round 2, verbatim (hygiene.py): the baseline every approved piece passed.
OLD_LEAK_PATTERN = (
    r"\b(?:content seed|the seed|this seed|source itinerary|place background|the itinerary calls it"
    r"|other trips that include|for context only|source notes"
    r"|agency fact(?!\s*-?\s*sheets?\b)|platform fact(?!\s*-?\s*sheets?\b))\b|\[moment\b|\bscope:"
    r"|^\W*source:")
OLD_LEAK_RE = re.compile(OLD_LEAK_PATTERN, re.I)

PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, t.slug AS tenant, cp.channel, cp.content_text
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    WHERE cp.status = 'approved' AND cp.created_at >= $1
      AND (cardinality($2::text[]) = 0 OR t.slug = ANY($2::text[]))
    ORDER BY cp.created_at
"""


def new_hits(text: str, new_re: re.Pattern) -> list[str]:
    """The matched phrases (lower-cased) of every body sentence `new_re` flags and OLD_LEAK_RE does not — read the
    way gate F13_leak reads them (hygiene.sentence_spans, prose / bullet / FAQ sentences)."""
    from services.acp_content_writing.hygiene import sentence_spans
    out = []
    for s in sentence_spans(text or ""):
        if s["kind"] not in ("prose", "bullet", "faq_a", "faq_q"):
            continue
        m = new_re.search(s["clean"])
        if m and not OLD_LEAK_RE.search(s["clean"]):
            out.append(m.group(0).lower())
    return out


def summarize(rows: list[dict], new_re: re.Pattern) -> tuple[list[dict], dict]:
    """(per-piece lines for pieces with a new hit, summary). Pure: unit-tested on invented texts."""
    lines, phrases = [], Counter()
    channels: Counter = Counter()
    for r in rows:
        hits = new_hits(r.get("content_text") or "", new_re)
        channels[r.get("channel") or "-"] += 1
        if hits:
            phrases.update(hits)
            lines.append({"piece": str(r["piece_id"])[:8], "tenant": r.get("tenant"), "channel": r.get("channel"),
                          "hits": len(hits), "phrases": sorted(set(hits))})
    return lines, {"approved_pieces": len(rows), "by_channel": dict(channels), "pieces_with_new_hit": len(lines),
                   "new_hit_sentences": sum(x["hits"] for x in lines), "phrases": dict(phrases.most_common(20))}


async def main() -> int:
    import asyncpg

    from services.acp_content_writing.hygiene import LEAK_RE
    if LEAK_RE.pattern == OLD_LEAK_PATTERN:
        print("STOP the deployed hygiene.LEAK_RE is main's old pattern: run after the round-2 deploy")
        return 2
    since = datetime.fromisoformat((os.environ.get("SINCE") or "2000-01-01T00:00:00Z").replace("Z", "+00:00"))
    tenants = [t.strip() for t in (os.environ.get("TENANTS") or "").split(",") if t.strip()]
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await conn.execute("SET default_transaction_read_only = on")
        assert (await conn.fetchval("SHOW default_transaction_read_only")) == "on"
        rows = [dict(r) for r in await conn.fetch(PIECES_SQL, since, tenants)]
    finally:
        await conn.close()
    lines, summary = summarize(rows, LEAK_RE)
    for x in lines:
        print(f"PIECE {x['piece']} {x['tenant']} {x['channel']} hits={x['hits']} phrases={json.dumps(x['phrases'])}")
    print("SUMMARY " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
