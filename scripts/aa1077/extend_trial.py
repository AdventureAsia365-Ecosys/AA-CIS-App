#!/usr/bin/env python3
"""AA-1077 — paid trial of the extend repair on stored length-only blog pieces, with a hard cost cap. It never writes
a piece: the database session that reads the pieces is read-only (checked); the only writes are the gateway's own
llm_call_log rows of the calls it makes. Prints 8-char ids, numbers, gate names and reason codes only: never text.

Per piece (the 8-char ids from remeasure_length.py's `length_only` list):
  1. its stored inputs rebuilt the way the writer builds them (services.writing_memory.replay.PieceReplay: grounding
     text, hygiene and trip-fact context, CTA, brand pack);
  2. BASE: the T10 gates on the stored text (stored F9 verdicts, $0) — the F4 reading of the stored text;
  3. the extend repair exactly as the loop runs it (length_extend.plan_for + length_extend.extend with
     generate.extend_sections: ONE writer call on stage `t9_write`, quality_signal mode `length_extend`,
     content_piece_id = the piece; billed fields recorded);
  4. AFTER: the T10 gates on the extended text with a LIVE F9 judge (stage t10_judge; `F9=stored` keeps the stored
     verdicts at $0) — would the piece be approved, and which hold-capable gates are NEW against BASE.
Cap: CAP_USD (required, e.g. 0.10). Before each piece the run stops when spent + the largest cost seen so far (0.05
before the first) would pass the cap; the run total is printed. Read the ledger afterwards:
    SELECT stage, count(*), sum(cost_usd) FROM shared.llm_call_log
    WHERE created_at >= <run start> AND content_piece_id = ANY(<the pieces>) GROUP BY stage;

Run by the orchestrator in aa-cis-dev-worker or -api (cwd /app; container DATABASE_URL = cis_app), one line:
    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1077/extend_trial.py PIECES=aaaaaaaa,bbbbbbbb,cccccccc CAP_USD=0.10
Env
  PIECES   (required) comma-separated 8-char piece ids.
  CAP_USD  (required) hard cap for the run, USD.
  F9       live (default) | stored.
  TENANTS  (optional) tenant slugs the pieces must belong to; default the four AA-741 audit tenants.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

AUDIT_TENANTS = "audit-atlas-hearth,audit-terra-family,audit-trail-pulse,audit-wildkind-travel"
FIRST_COST_GUESS = 0.05
# A live F9 judge call's cost is not returned by the gates; this guess is added to the spend of each live piece (the
# phase 2 t10_judge mean was ~0.0016 USD per call). The ledger read after the run gives the true total.
F9_COST_GUESS = 0.003

PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.tenant_id::text AS tenant_id, t.slug AS tenant, cp.status,
           cp.content_text, cp.gate_ledger, cp.writer_input, cp.seo_title, cp.meta_description, cp.slug
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    WHERE t.slug = ANY($1::text[]) AND cp.channel = 'blog' AND left(cp.piece_id::text, 8) = ANY($2::text[])
"""


def _j(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def may_spend(spent: float, largest: float, cap: float) -> bool:
    """The next piece may run only when the spend so far plus the largest cost seen (a first guess before any) stays
    within the cap."""
    return spent + (largest or FIRST_COST_GUESS) <= cap + 1e-9


def hold_capable(ledger: list) -> set:
    from services.acp_content_writing import gate_eval
    return {r["source"] for r in gate_eval.failing_gates(ledger) if r["may_hold"]}


def trial_piece(row: dict, *, call, live_f9: bool) -> dict:
    """One piece: base gates, the extend repair, gates after. `call` is generate.extend_sections (mocked in tests)."""
    from services.acp_content_writing import gate_eval, hygiene, length_contract as lc, length_extend
    from services.acp_content_writing.quality_gates import hold_failure
    from services.writing_memory.replay import PieceReplay, _stored_f9
    wi = _j(row.get("writer_input")) or {}
    out: dict = {"piece": str(row["piece_id"])[:8], "tenant": row.get("tenant"), "status": row.get("status"),
                 "cost_usd": 0.0}
    if not wi.get("seed"):
        out["skip"] = "no writer_input seed"
        return out
    pr = PieceReplay(wi, "blog", {k: row.get(k) for k in ("seo_title", "meta_description", "slug")})
    text = row.get("content_text") or ""
    stored_f9 = _stored_f9(_j(row.get("gate_ledger")) or [])
    base = pr.run(text, pack=pr.stored_pack, f9=stored_f9, stock_blocking=False)
    out.update(chars_before=lc.measure(text), words_before=lc.words(text),
               base_codes=gate_eval.hold_reason_codes(base), base_hold=(hold_failure(base) or {}).get("gate"))
    plan = length_extend.plan_for(text, base, channel="blog", source=pr.atom_text)
    if plan is None:
        out["skip"] = "no extend plan (not short, no body section or no untold seed fact)"
        return out
    hyg = pr._kw["hygiene_ctx"]
    brand_name = (pr.stored_pack or {}).get("brand_name")
    new_text, cost, entry = length_extend.extend(
        text, plan, call=call, source=pr.atom_text, cta=pr.cta, ignore=hygiene.brand_words(brand_name),
        moments=hyg.moments, trip_ctx=pr._kw["trip_ctx"],
        voice=", ".join((pr.stored_pack or {}).get("voice") or []) or None, channel="blog",
        tenant_id=row.get("tenant_id"), content_piece_id=str(row["piece_id"]), attempt=None)
    out["cost_usd"] = round(cost, 6)
    out["extend"] = {k: entry.get(k) for k in ("sections", "facts_offered", "words_asked", "added", "chars_after",
                                               "reached_floor", "error", "model")}
    out["extend"]["refused"] = [r["why"][:80] for r in entry.get("refused") or []]
    after = pr.run(new_text, pack=pr.stored_pack, f9=None if live_f9 else stored_f9, stock_blocking=False)
    out.update(chars_after=lc.measure(new_text), words_after=lc.words(new_text),
               after_codes=gate_eval.hold_reason_codes(after), after_hold=(hold_failure(after) or {}).get("gate"),
               new_hold_gates=sorted(hold_capable(after) - hold_capable(base)))
    out["would_be"] = "approved" if out["after_hold"] is None else "held"
    return out


def summarize(rows: list[dict], spent: float, cap: float, stopped: list[str]) -> dict:
    done = [r for r in rows if "would_be" in r]
    return {"pieces": len(rows), "trialled": len(done), "skipped": sorted(r["piece"] for r in rows if r.get("skip")),
            "stopped_by_cap": stopped, "would_be_approved": sum(r["would_be"] == "approved" for r in done),
            "reached_floor": sum(bool((r.get("extend") or {}).get("reached_floor")) for r in done),
            "pieces_with_new_hold_gates": sorted(r["piece"] for r in done if r["new_hold_gates"]),
            "spent_usd": round(spent, 6), "cap_usd": cap}


async def main() -> int:
    import asyncpg
    from services.acp_content_writing.generate import extend_sections
    pieces = [p.strip()[:8] for p in (os.environ.get("PIECES") or "").split(",") if p.strip()]
    try:
        cap = float(os.environ.get("CAP_USD") or "")
    except ValueError:
        cap = 0.0
    if not pieces or cap <= 0:
        print("STOP PIECES (8-char ids) and CAP_USD (> 0) are required")
        return 2
    live_f9 = (os.environ.get("F9") or "live").strip().lower() != "stored"
    from services.acp_content_writing import length_extend
    os.environ[length_extend.EXTEND_ENV] = length_extend.EXTEND_ON   # this process only: the trial IS the extend
    tenants = [t.strip() for t in (os.environ.get("TENANTS") or AUDIT_TENANTS).split(",") if t.strip()]
    started = datetime.now(timezone.utc).isoformat()
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await conn.execute("SET default_transaction_read_only = on")
        assert (await conn.fetchval("SHOW default_transaction_read_only")) == "on"
        raw = {str(r["piece_id"])[:8]: dict(r) for r in await conn.fetch(PIECES_SQL, tenants, pieces)}
    finally:
        await conn.close()
    print(f"START {started} pieces={len(pieces)} found={len(raw)} cap_usd={cap} f9={'live' if live_f9 else 'stored'}")
    rows, spent, largest, stopped = [], 0.0, 0.0, []
    for p in pieces:
        if p not in raw:
            rows.append({"piece": p, "skip": "not found in the tenants"})
            continue
        if not may_spend(spent, largest, cap):
            stopped.append(p)
            continue
        try:
            r = await asyncio.to_thread(trial_piece, raw[p], call=extend_sections, live_f9=live_f9)
        except Exception as e:  # noqa: BLE001 — one piece that cannot be trialled is reported, never fatal
            r = {"piece": p, "skip": f"{type(e).__name__}: {str(e)[:160]}", "cost_usd": 0.0}
        piece_cost = float(r.get("cost_usd") or 0.0) + (F9_COST_GUESS if live_f9 and "would_be" in r else 0.0)
        spent += piece_cost
        largest = max(largest, piece_cost)
        rows.append(r)
        print("PIECE " + json.dumps(r, sort_keys=True, default=str))
    print("SUMMARY " + json.dumps(summarize(rows, spent, cap, stopped), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
