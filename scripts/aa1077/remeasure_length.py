#!/usr/bin/env python3
"""AA-1077 — $0 re-measure of stored blog pieces against the blog length contract. READ-ONLY: no model call, no write
(the session is set read-only and checked). Prints 8-char ids, numbers, statuses and reason codes only: never text.

Per piece (acp_shared.content_piece, channel blog):
  stored    chars as F4 reads them (tag-stripped) and words of the stored text;
  f4_ledger the F4 reading on the piece's own ledger: 'in' (passed) or the number in its violation;
  before    chars before the post-loop normalizer: `writer_input.length_trace` of a piece written after AA-1077
            (`loop`), else the ledger number of a failed F4 (the reading the gates made), else '-' (passed, not kept);
  after     the stored chars (the normalizer's output is what is stored);
  verdict   the length contract's verdict on the stored text (in / length_short / length_long) and the status the
            AA-1077 re-check (service._recheck_length, the same function the normalizer runs) gives the piece;
  codes     the hold reason codes now (gate_eval.hold_reason_codes on the re-checked ledger: length_short /
            length_long) next to the label stored with the piece (old pieces: `length`).
Summary: approved pieces stored under the floor as stored, and after the re-check (must be 0); the length-only pieces
(held, hold-capable failures = F4 short only) with their 8-char ids: the extend trial's input (extend_trial.py).

Run by the orchestrator in aa-cis-dev-api (cwd /app; container DATABASE_URL = cis_app, ssl=require), one line:
    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1077/remeasure_length.py P2_SINCE=2026-11-10T05:49:46Z

Env
  P2_SINCE  (required) ISO start; blog pieces created at or after it.
  TENANTS   (optional) comma-separated tenant slugs; default the four AA-741 audit tenants.
  EXCLUDE   (optional) comma-separated 8-char piece ids to leave out (default the S238 synthetic piece d67e3178).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

AUDIT_TENANTS = "audit-atlas-hearth,audit-terra-family,audit-trail-pulse,audit-wildkind-travel"
DEFAULT_EXCLUDE = "d67e3178"
_NUM_RE = re.compile(r"(?:only |is )(\d+) chars")

PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, t.slug AS tenant, cp.status, cp.held_reason, cp.content_text,
           cp.gate_ledger, cp.repair_log, cp.writer_input, cp.created_at
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    WHERE t.slug = ANY($1::text[]) AND cp.channel = 'blog' AND cp.created_at >= $2
      AND cp.status IN ('approved', 'held', 'failed')
    ORDER BY t.slug, cp.created_at
"""


def _j(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _f4(ledger: list) -> Optional[dict]:
    from services.quality import registry as qr
    return next((g for g in ledger or [] if isinstance(g, dict) and g.get("gate") == qr.F4_EXTREME_LENGTH), None)


def f4_reading(ledger: list) -> str:
    """'in' for a passed F4, the number of a failed one, '-' when there is no F4 entry or no number."""
    g = _f4(ledger)
    if g is None:
        return "-"
    if g.get("passed"):
        return "in"
    for v in g.get("violations") or []:
        m = _NUM_RE.search(str(v))
        if m:
            return m.group(1)
    return "-"


def before_normalizer(writer_input: dict, ledger: list) -> str:
    """Chars before the normalizer: the AA-1077 length trace when stored, else a failed F4's reading, else '-'."""
    trace = (writer_input or {}).get("length_trace")
    if isinstance(trace, dict) and isinstance(trace.get("chars"), dict) and trace["chars"].get("loop") is not None:
        return str(trace["chars"]["loop"])
    reading = f4_reading(ledger)
    return reading if reading.isdigit() else "-"


def stored_label(repair_log: list) -> list:
    label = next((e for e in reversed(repair_log or []) if isinstance(e, dict) and "reason_codes" in e), None)
    return list((label or {}).get("reason_codes") or [])


def measure_piece(row: dict) -> dict:
    """One piece's line (pure but for the service import; unit-tested)."""
    from services.acp_content_writing import gate_eval, length_contract as lc
    from services.acp_content_writing import service as svc
    from services.acp_content_writing.quality_gates import hold_failure
    ledger = _j(row.get("gate_ledger")) or []
    repair_log = _j(row.get("repair_log")) or []
    text = row.get("content_text") or ""
    status = row.get("status")
    out = {"piece": str(row["piece_id"])[:8], "tenant": row.get("tenant"), "status": status,
           "stored_chars": lc.measure(text), "words": lc.words(text), "f4_ledger": f4_reading(ledger),
           "before": before_normalizer(_j(row.get("writer_input")) or {}, ledger), "label": stored_label(repair_log)}
    out["after"] = out["stored_chars"]
    if status == "failed" or not text:
        out.update(verdict="empty", new_status=status, codes=[], length_only=False)
        return out
    new_ledger, new_status, _reason, entry = svc._recheck_length(text, ledger, status, row.get("held_reason"),
                                                                trace={})
    codes = gate_eval.hold_reason_codes(new_ledger, row.get("held_reason")) if new_status == "held" else []
    hold_capable = {r["source"] for r in gate_eval.failing_gates(new_ledger) if r["may_hold"]}
    out.update(verdict=entry["verdict"], new_status=new_status, codes=codes,
               length_only=(new_status == "held" and codes == [lc.CODE_SHORT]
                            and hold_capable == {svc.qr.F4_EXTREME_LENGTH}),
               hold_gate=(hold_failure(new_ledger) or {}).get("gate"))
    return out


def summarize(rows: list[dict]) -> dict:
    from services.acp_content_writing import length_contract as lc
    floor = lc.floor_chars()
    measured = [r for r in rows if r["verdict"] != "empty"]
    return {
        "pieces": len(rows), "floor_chars": floor,
        "status_stored": {s: sum(r["status"] == s for r in rows) for s in ("approved", "held", "failed")},
        "status_after_recheck": {s: sum(r["new_status"] == s for r in rows) for s in ("approved", "held", "failed")},
        "approved_under_floor_stored": sorted(r["piece"] for r in measured
                                              if r["status"] == "approved" and r["stored_chars"] < floor),
        "approved_under_floor_after": sorted(r["piece"] for r in measured
                                             if r["new_status"] == "approved" and r["stored_chars"] < floor),
        "held_by_recheck": sorted(r["piece"] for r in measured if r["status"] == "approved"
                                  and r["new_status"] == "held"),
        "verdicts": {v: sum(r["verdict"] == v for r in rows) for v in ("in", "length_short", "length_long", "empty")},
        "length_only": sorted(r["piece"] for r in rows if r.get("length_only")),
        "ledger_vs_stored_drift": sum(1 for r in measured if r["f4_ledger"].isdigit()
                                      and int(r["f4_ledger"]) != r["stored_chars"]),
    }


async def main() -> int:
    import asyncpg
    if not os.environ.get("P2_SINCE"):
        print("STOP P2_SINCE is required (ISO)")
        return 2
    since = datetime.fromisoformat(os.environ["P2_SINCE"].replace("Z", "+00:00"))
    tenants = [t.strip() for t in (os.environ.get("TENANTS") or AUDIT_TENANTS).split(",") if t.strip()]
    exclude = {p.strip() for p in (os.environ.get("EXCLUDE") or DEFAULT_EXCLUDE).split(",") if p.strip()}
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await conn.execute("SET default_transaction_read_only = on")
        assert (await conn.fetchval("SHOW default_transaction_read_only")) == "on"
        raw = [dict(r) for r in await conn.fetch(PIECES_SQL, tenants, since)]
    finally:
        await conn.close()
    rows = [measure_piece(r) for r in raw if str(r["piece_id"])[:8] not in exclude]
    for r in rows:
        print(f"PIECE {r['piece']} {r['tenant']} status={r['status']} -> {r['new_status']} verdict={r['verdict']} "
              f"stored={r['stored_chars']} words={r['words']} f4_ledger={r['f4_ledger']} before={r['before']} "
              f"after={r['after']} codes={','.join(r['codes']) or '-'} label={','.join(r['label']) or '-'}"
              f"{' LENGTH_ONLY' if r.get('length_only') else ''}")
    print("SUMMARY " + json.dumps(summarize(rows), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
