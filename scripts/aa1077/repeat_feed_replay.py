#!/usr/bin/env python3
"""AA-1077 (S279, lane f13-drop) — $0 replay of the repeat drop fed by both F13 gates over stored blog pieces.
READ-ONLY: no model call, no write (the pool's sessions are read only and checked). Prints 8-char ids, gate names,
outcome codes and numbers only: never the piece text.

Per stored blog piece (acp_shared.content_piece, channel blog), on its stored final text:
  old arm   `repeat_drop.drop_repeats(sources=(F13_repeat,))` — the #1468 behaviour;
  new arm   `repeat_drop.drop_repeats()` — F13_repeat AND the F13_hygiene repeat warnings, with a per-candidate trace
            (source gate, outcome code, best Dice share, chars left);
  gates     the deterministic T10 gates re-run on the stored text and on the new arm's text with the inputs rebuilt
            from `writer_input` (services.writing_memory.replay.PieceReplay; the stored F9 verdicts are passed through,
            F9 / F10 are not compared). A gate verdict other than F13_repeat / F13_hygiene that changes is a defect of
            the drop (SUMMARY `other_verdict_changes`, exit 1).
SUMMARY: pieces with an F13_hygiene repeat warning and the pieces where every such warning disappears; the pieces the
hygiene feed changes beyond the old arm; outcomes per source gate; the Dice shares of the refused (not near-exact)
candidates in bands (evidence for the setting `cis.t9.repeat_drop_near_exact`); pieces whose blocking failures all
clear; the share and floor in force.

Run by the orchestrator AFTER the code deploy, in aa-cis-dev-api (cwd /app; container DATABASE_URL = cis_app,
ssl=require), one line:
    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1077/repeat_feed_replay.py SINCE=2026-11-10T05:49:46Z

Env
  SINCE    (optional) ISO start; blog pieces created at or after it (default 2026-11-10T05:49:46Z, phase 2).
  TENANTS  (optional) comma-separated tenant slugs; default the four AA-741 audit tenants; '*' = every tenant.
  LIMIT    (optional) newest pieces at most (default 300).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Optional

sys.path.insert(0, os.getcwd())

AUDIT_TENANTS = "audit-atlas-hearth,audit-terra-family,audit-trail-pulse,audit-wildkind-travel"
DEFAULT_SINCE = "2026-11-10T05:49:46Z"
DEFAULT_LIMIT = 300
DICE_BANDS = (0.5, 0.6, 0.7, 0.8, 0.9)   # report bands of a refused candidate's best Dice share (not a gate value)

PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, t.slug AS tenant, cp.status, cp.channel, cp.content_text,
           cp.gate_ledger, cp.writer_input, cp.seo_title, cp.meta_description, cp.slug, cp.created_at
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    WHERE cp.channel = 'blog' AND cp.created_at >= $1 AND cp.status IN ('approved', 'held')
      AND (cardinality($2::text[]) = 0 OR t.slug = ANY($2::text[]))
    ORDER BY cp.created_at DESC
    LIMIT $3
"""


def _j(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def repeat_counts(text: str, hyg) -> dict[str, int]:
    """{F13_repeat: findings, F13_hygiene: repeat warnings} of `text` (hygiene.find_issues)."""
    from services.acp_content_writing import hygiene, repeat_drop
    from services.quality import registry as qr
    found = hygiene.find_issues(text or "", hyg)
    return {qr.F13_REPEAT: len(found["repeat"]),
            qr.F13_HYGIENE: sum(f.startswith(repeat_drop.HYGIENE_REPEAT_PREFIX) for f in found["hygiene"])}


def verdict_changes(before: list, after: list) -> list[str]:
    """Gates (legacy view, F9 / F10 excluded) whose passed verdict differs between the two ledgers."""
    from services.writing_memory.replay import baseline_compare
    return sorted(g for g, (b, a) in baseline_compare(before, after).items() if b != a)


def piece_result(piece: str, text: str, hyg, gates: Callable[[str], list]) -> dict:
    """One piece: both arms, the trace and the gate verdicts before / after the new arm. `gates(text)` returns the
    replayed ledger (PieceReplay.run on Dev; a fake in tests). Pure but for `gates`."""
    from services.acp_content_writing import repeat_drop
    from services.quality import registry as qr
    from services.writing_memory.replay import blocking_failed
    old_text, _ = repeat_drop.drop_repeats(text, hyg, sources=(qr.F13_REPEAT,))
    trace: list[dict] = []
    new_text, _ = repeat_drop.drop_repeats(text, hyg, trace=trace)
    before = gates(text)
    after = gates(new_text) if new_text != text else before
    changed = verdict_changes(before, after)
    return {"piece": piece, "trace": trace, "drops": sum(t["outcome"] == repeat_drop.DROPPED for t in trace),
            "feed_changed": new_text != old_text, "old_changed": old_text != text,
            "counts_before": repeat_counts(text, hyg), "counts_after": repeat_counts(new_text, hyg),
            "f13_changes": [g for g in changed if g in (qr.F13_REPEAT, qr.F13_HYGIENE)],
            "other_changes": [g for g in changed if g not in (qr.F13_REPEAT, qr.F13_HYGIENE)],
            "blocking_before": sorted(blocking_failed(before)), "blocking_after": sorted(blocking_failed(after))}


def dice_band(d: Optional[float]) -> str:
    """'<0.5', '0.5-0.6', …, '0.8-0.9', '>=0.9' (a report label; None = under 4 copy words)."""
    if d is None:
        return "n/a"
    if d < DICE_BANDS[0]:
        return f"<{DICE_BANDS[0]}"
    for lo, hi in zip(DICE_BANDS, DICE_BANDS[1:]):
        if lo <= d < hi:
            return f"{lo}-{hi}"
    return f">={DICE_BANDS[-1]}"


def summarize(results: list[dict], errors: list[str]) -> dict:
    """The SUMMARY document (pure)."""
    from services.acp_content_writing import repeat_drop
    from services.quality import registry as qr
    hyg_g = qr.F13_HYGIENE
    with_hyg = [r for r in results if r["counts_before"][hyg_g]]
    outcomes = {s: Counter() for s in repeat_drop.SOURCES}
    refused_dice = {s: Counter() for s in repeat_drop.SOURCES}
    for r in results:
        for t in r["trace"]:
            outcomes[t["source"]][t["outcome"]] += 1
            if t["outcome"] == repeat_drop.NOT_NEAR_EXACT:
                refused_dice[t["source"]][dice_band(t["dice"])] += 1
    return {
        "pieces": len(results), "errors": len(errors),
        "pieces_with_hygiene_repeat": len(with_hyg),
        "hygiene_repeat_warnings": sum(r["counts_before"][hyg_g] for r in results),
        "pieces_hygiene_repeats_gone": sorted(r["piece"] for r in with_hyg if r["counts_after"][hyg_g] == 0),
        "pieces_hygiene_repeats_fewer": sorted(r["piece"] for r in with_hyg
                                               if 0 < r["counts_after"][hyg_g] < r["counts_before"][hyg_g]),
        "pieces_feed_changed": sorted(r["piece"] for r in results if r["feed_changed"]),
        "pieces_old_arm_changed": sorted(r["piece"] for r in results if r["old_changed"]),
        "outcomes": {s: dict(c) for s, c in outcomes.items()},
        "refused_dice_bands": {s: dict(c) for s, c in refused_dice.items()},
        "f13_verdict_changes": sorted(r["piece"] for r in results if r["f13_changes"]),
        "other_verdict_changes": {r["piece"]: r["other_changes"] for r in results if r["other_changes"]},
        "blocking_cleared": sorted(r["piece"] for r in results if r["blocking_before"] and not r["blocking_after"]),
    }


def replay_row(row: dict) -> dict:
    """One stored piece through PieceReplay (the writer's own gate inputs, the stored F9 verdicts)."""
    from services.writing_memory.replay import PieceReplay, _stored_f9
    pr = PieceReplay(_j(row.get("writer_input")) or {}, row.get("channel") or "blog",
                     {k: row.get(k) for k in ("seo_title", "meta_description", "slug")})
    f9 = _stored_f9(_j(row.get("gate_ledger")) or [])

    def gates(text: str) -> list:
        return pr.run(text, pack=pr.stored_pack, f9=f9, stock_blocking=False)

    return piece_result(str(row["piece_id"])[:8], row.get("content_text") or "", pr._kw["hygiene_ctx"], gates)


def _line(r: dict, tenant: str, status: str) -> str:
    from services.quality import registry as qr
    trace = ",".join(f"{t['source']}:{t['outcome']}:{t['dice']}" for t in r["trace"]) or "-"
    cb, ca = r["counts_before"], r["counts_after"]
    return (f"PIECE {r['piece']} {tenant} status={status} "
            f"repeat={cb[qr.F13_REPEAT]}->{ca[qr.F13_REPEAT]} "
            f"hygiene_repeat={cb[qr.F13_HYGIENE]}->{ca[qr.F13_HYGIENE]} "
            f"drops={r['drops']} feed_changed={r['feed_changed']} "
            f"blocking={','.join(r['blocking_before']) or '-'}->{','.join(r['blocking_after']) or '-'} "
            f"other_changes={','.join(r['other_changes']) or '-'} candidates={trace}")


async def main() -> int:
    import asyncpg

    from services.acp_content_writing import length_contract as lc
    from services.acp_content_writing import repeat_drop
    from shared import settings
    since = datetime.fromisoformat((os.environ.get("SINCE") or DEFAULT_SINCE).replace("Z", "+00:00"))
    raw_tenants = os.environ.get("TENANTS") or AUDIT_TENANTS
    tenants = [] if raw_tenants.strip() == "*" else [t.strip() for t in raw_tenants.split(",") if t.strip()]
    limit = int(os.environ.get("LIMIT") or DEFAULT_LIMIT)
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl="require", min_size=1, max_size=1,
                                     server_settings={"default_transaction_read_only": "on"})
    try:
        async with pool.acquire() as conn:
            assert (await conn.fetchval("SHOW default_transaction_read_only")) == "on"
            raw = [dict(r) for r in await conn.fetch(PIECES_SQL, since, tenants, limit)]
        fresh = await settings.ensure_fresh(pool)
    finally:
        await pool.close()
    results, errors = [], []
    for row in raw:
        try:
            r = replay_row(row)
        except Exception as e:  # noqa: BLE001 — a piece whose stored input cannot be rebuilt is counted, never fatal
            errors.append(f"{str(row['piece_id'])[:8]} {type(e).__name__}: {str(e)[:120]}")
            continue
        results.append(r)
        print(_line(r, row.get("tenant"), row.get("status")))
    for e in errors:
        print(f"ERROR {e}")
    summary = summarize(results, errors)
    summary.update(settings_fresh=fresh, near_exact_share=repeat_drop.near_exact_share(), floor_chars=lc.floor_chars(),
                   since=since.isoformat(), tenants=tenants or "*")
    print("SUMMARY " + json.dumps(summary, sort_keys=True, default=str))
    return 1 if summary["other_verdict_changes"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
