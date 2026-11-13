#!/usr/bin/env python3
"""AA-1077 (S279) — $0 replay of the T7 Blog seed floor sized from the F4 blog floor. READ-ONLY: no model call, no
write (the session is set read-only and checked). Prints 8-char ids, numbers, statuses and reason codes only: never
text.

Per stored blog piece (acp_shared.content_piece, channel blog, created at or after SINCE) of the named tenants:
  t7_words   the T7 Blog seed estimate of its pick NOW (`blog_seed.blog_seed_words` on the request's atom, tour and
             walk: the measure the slate compares with the floor);
  t9_words   the seed words the writer actually got (writer_input `seed` + `route_moments`), the same word rule;
  chars      the stored length as F4 reads it (`length_contract.measure`) and its side of the band;
  ratio      chars / t7_words (t9_words when the estimate is 0): blog characters per seed word;
  verdict    what the floor in force (env + registry, `blog_seed.seed_floor()` with the flat MIN_SEED) and the floor at
             the MEASURED ratio say about the pick: keep, or the skip reason (seed_below_min / seed_below_length_floor).
Per Subject (tenant x subject) the newest piece decides.

The measured ratio: the median ratio of the material-bound pieces (stored under the F4 floor: the writer stopped
because the seed ran out, S278 extend step) of the newest writer prompt version that has any, else of all versions.
A piece that reached the floor only shows the writer's target, not what its seed could carry, so it is not used.
SUMMARY prints the ratio, the sized floor it gives, and per floor how many Subjects are kept / skipped, how many of
the short ones the floor skips (wanted) and how many approved ones it skips (lost topics).

Run by the orchestrator in aa-cis-dev-api (cwd /app; container DATABASE_URL = cis_app, ssl=require), S3-mediated
ECS exec (`scripts/` is not in the image; it imports services.* only):
    SINCE=2026-11-10T05:49:46Z MIN_SEED=600 python3 /tmp/seed_floor_replay.py

Env
  SINCE     (required) ISO start; blog pieces created at or after it.
  TENANTS   (optional) comma-separated tenant slugs; default the four AA-741 audit tenants.
  MIN_SEED  (optional) the flat Blog minimum seed to evaluate (default T7_BLOG_MIN_SEED_WORDS, else 600).
  RATIO     (optional) chars per seed word for the "configured" floor (default T7_BLOG_CHARS_PER_SEED_WORD, else the
            registry value).
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
from datetime import datetime
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

AUDIT_TENANTS = "audit-atlas-hearth,audit-terra-family,audit-trail-pulse,audit-wildkind-travel"
DEFAULT_FLAT = 600

PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.tenant_id::text AS tenant_id, t.slug AS tenant, cp.status,
           cp.content_text, cp.writer_input, cp.created_at,
           r.atom_id, r.trip_id::text AS tour_id, r.route_segment_ids, r.subject_id::text AS subject_id,
           s.segment_id
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id
    LEFT JOIN acp_shared.subject s ON s.subject_id = r.subject_id
    WHERE t.slug = ANY($1::text[]) AND r.channel = 'blog' AND cp.created_at >= $2
      AND cp.status IN ('approved', 'held')
    ORDER BY t.slug, cp.created_at
"""


def _j(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def t9_seed_words(writer_input: Optional[dict]) -> int:
    """Seed words the writer got: its seed plus every route moment's seed (the audit's measure, blog_seed.WORD_RE)."""
    from services.acp_shared import blog_seed
    wi = writer_input or {}
    moments = wi.get("route_moments") or []
    return blog_seed.seed_words(wi.get("seed")) + sum(
        blog_seed.seed_words(m.get("seed")) for m in moments if isinstance(m, dict))


def piece_line(row: dict, t7_words: Optional[int]) -> dict:
    """One piece's numbers (pure but for the service imports; unit-tested)."""
    from services.acp_content_writing import length_contract as lc
    wi = _j(row.get("writer_input")) or {}
    chars = lc.measure(row.get("content_text"))
    t9 = t9_seed_words(wi)
    base = t7_words or t9
    return {"piece": str(row["piece_id"])[:8], "tenant": row.get("tenant"), "status": row.get("status"),
            "subject": (row.get("subject_id") or "-")[:8], "segment": row.get("segment_id"),
            "tour": (row.get("tour_id") or "-")[:8], "writer": wi.get("writer_prompt_version") or "-",
            "chars": chars, "side": lc.side(chars) or "in", "t7_words": t7_words, "t9_words": t9,
            "ratio": round(chars / base, 3) if base else None, "created_at": str(row.get("created_at"))}


def measured_ratio(lines: list[dict]) -> dict:
    """The median ratio of the material-bound (short) pieces of the newest writer version that has any, else of every
    short piece; None without a short piece. Pure."""
    short = [p for p in lines if p["side"] == "short" and p["ratio"]]
    if not short:
        return {"ratio": None, "n": 0, "writer": None}
    newest: dict[str, str] = {}
    for p in lines:
        newest[p["writer"]] = max(newest.get(p["writer"], ""), p["created_at"])
    for writer in sorted(newest, key=newest.get, reverse=True):
        pick = [p["ratio"] for p in short if p["writer"] == writer]
        if pick:
            return {"ratio": round(statistics.median(pick), 3), "n": len(pick), "writer": writer,
                    "min": min(pick), "max": max(pick)}
    pick = [p["ratio"] for p in short]
    return {"ratio": round(statistics.median(pick), 3), "n": len(pick), "writer": "all"}


def floor_at(flat: int, ratio: Optional[float]) -> dict:
    """The floor `blog_seed.seed_floor()` would give for this flat floor and ratio (pure)."""
    from services.acp_content_writing import length_contract as lc
    from services.acp_shared import blog_seed
    sized = blog_seed.sized_min_seed_words(ratio) if (flat > 0 and ratio) else 0
    return {"min_seed_words": max(flat, sized), "flat": flat, "sized": sized,
            "chars_per_seed_word": ratio or 0.0, "floor_chars": lc.BLOG_FLOOR_CHARS}


def subjects(lines: list[dict]) -> list[dict]:
    """The newest piece per (tenant, subject) (a piece without a subject stands alone). Pure."""
    out: dict[tuple, dict] = {}
    for p in sorted(lines, key=lambda x: x["created_at"]):
        key = (p["tenant"], p["subject"] if p["subject"] != "-" else p["piece"])
        out[key] = p
    return list(out.values())


def verdicts(subs: list[dict], floor: dict) -> dict:
    """Keep / skip per Subject under `floor` (on the T7 estimate; the writer's seed when there is none). Pure."""
    from services.acp_shared import blog_seed
    rows = []
    for p in subs:
        words = p["t7_words"] or p["t9_words"]
        rows.append({"subject": p["subject"], "tenant": p["tenant"], "tour": p["tour"], "piece": p["piece"],
                     "status": p["status"], "side": p["side"], "seed_words": words,
                     "verdict": blog_seed.skip_reason(words, floor) or "keep"})
    skipped = [r for r in rows if r["verdict"] != "keep"]
    return {"floor": floor, "kept": len(rows) - len(skipped), "skipped": len(skipped),
            "short_skipped": sum(1 for r in skipped if r["side"] == "short"),
            "short_total": sum(1 for r in rows if r["side"] == "short"),
            "approved_skipped": sum(1 for r in skipped if r["status"] == "approved"),
            "rows": rows}


def summarize(lines: list[dict], flat: int, configured_ratio: float) -> dict:
    m = measured_ratio(lines)
    subs = subjects(lines)
    out = {"pieces": len(lines), "subjects": len(subs), "measured": m,
           "configured": verdicts(subs, floor_at(flat, configured_ratio)),
           "flat_only": verdicts(subs, floor_at(flat, None))}
    if m["ratio"]:
        out["at_measured"] = verdicts(subs, floor_at(flat, m["ratio"]))
        out["recommended_env"] = {"T7_BLOG_CHARS_PER_SEED_WORD": m["ratio"],
                                  "sized_min_seed_words": out["at_measured"]["floor"]["sized"]}
    return out


async def main() -> int:
    import asyncpg
    from services.acp_shared import blog_seed
    if not os.environ.get("SINCE"):
        print("STOP SINCE is required (ISO)")
        return 2
    since = datetime.fromisoformat(os.environ["SINCE"].replace("Z", "+00:00"))
    tenants = [t.strip() for t in (os.environ.get("TENANTS") or AUDIT_TENANTS).split(",") if t.strip()]
    flat = int(os.environ.get("MIN_SEED") or blog_seed.min_seed_words() or DEFAULT_FLAT)
    ratio = float(os.environ["RATIO"]) if os.environ.get("RATIO") else blog_seed.chars_per_seed_word()
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    lines = []
    try:
        await conn.execute("SET default_transaction_read_only = on")
        assert (await conn.fetchval("SHOW default_transaction_read_only")) == "on"
        for row in [dict(r) for r in await conn.fetch(PIECES_SQL, tenants, since)]:
            walk = _j(row.get("route_segment_ids")) or None
            try:
                t7 = await blog_seed.blog_seed_words(conn, row["tenant_id"], row["atom_id"], row.get("tour_id"),
                                                     list(walk) if walk else None)
            except Exception as exc:          # one unreadable pick is a line, not a stop
                print(f"WARN {str(row['piece_id'])[:8]} t7 estimate failed: {type(exc).__name__}")
                t7 = None
            lines.append(piece_line(row, t7))
    finally:
        await conn.close()
    for p in lines:
        print(f"PIECE {p['piece']} {p['tenant']} tour={p['tour']} subject={p['subject']} status={p['status']} "
              f"writer={p['writer']} chars={p['chars']} side={p['side']} t7_words={p['t7_words']} "
              f"t9_words={p['t9_words']} ratio={p['ratio']}")
    summary = summarize(lines, flat, ratio)
    for arm in ("configured", "at_measured", "flat_only"):
        for r in (summary.get(arm) or {}).get("rows", []):
            print(f"SUBJECT {arm} {r['subject']} {r['tenant']} tour={r['tour']} status={r['status']} side={r['side']} "
                  f"seed={r['seed_words']} floor={summary[arm]['floor']['min_seed_words']} -> {r['verdict']}")
    print("SUMMARY " + json.dumps({k: ({kk: vv for kk, vv in v.items() if kk != "rows"} if isinstance(v, dict) else v)
                                   for k, v in summary.items()}, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
