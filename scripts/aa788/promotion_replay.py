#!/usr/bin/env python3
"""AA-788 — the expected effect of the three T10 promotions on recent T9 pieces. Read-only, $0, no model call.

Measured before anyone sets a promoted mode (they all default to today's behaviour):
  T10_F12_SCOPE_MODE=block        a body F12_scope finding blocks (one sentence repair), held when it is left;
  T10_F9_BRAND_STYLE_MODE=repair  attempt 1's F9_brand_style blocks for one sentence repair, never a hold;
  T10_F14_FLOOR_MODE              the AA-780 under-floor block (already on; `warn` is its kill switch).

Two readings per piece (`acp_shared.content_piece`, status approved / held, the last DAYS days, newest LIMIT):
  stored   the check results the gates wrote: the final `gate_ledger` and attempt 1's ledger (its discarded attempt,
           or the repair_log snapshot, or the final ledger of a piece that stopped at attempt 1). How often each check
           failed, and for F12 whether the finding was in the body (would block) or only the title / meta (stays a
           warning). F9_brand_style counts attempt-1 failures whose phrase sits in a body sentence of attempt 1's
           text: each one is one extra t9_repair call plus one F9 judge call when the mode is on.
  replay   the deterministic gates re-run on the stored final text (and on attempt 1's text when it is stored) with
           the piece's stored inputs (services.writing_memory.replay.PieceReplay) and its STORED F9 verdicts — no judge
           call — once in the default modes and once promoted. A piece the promoted run would hold and the default run
           would not is a NEW HOLD — an upper bound, because a live run first gives the finding its sentence repair.
New holds are also counted on the pieces the panel labelled as_is / light (`acp_shared.content_piece_label`, the
issue's Done-when: no new holds on panel-light pieces).

Output: one JSON document on stdout — counts, the F12 finding kinds and the places they name (catalogue geography, no
tenant copy), and the piece ids of the new holds. Never piece text.

Runs in aa-cis-dev-api / -worker AFTER the AA-788 deploy (it reads the promoted modes from the image), as cis_app
(container DATABASE_URL, ssl=require) in a read-only session; exec'd alone (tools/ecs/ecsx.sh: cwd /app, /app on
sys.path), so it imports nothing from scripts/:
    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa788/promotion_replay.py DAYS=60 LIMIT=400
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Optional

sys.path.insert(0, os.getcwd())

DEFAULT_DAYS = 60
DEFAULT_LIMIT = 400
LIGHT_LABELS = frozenset({"as_is", "light"})
PROMOTED_ENV = {"T10_F12_SCOPE_MODE": "block", "T10_F9_BRAND_STYLE_MODE": "repair"}
DEFAULT_ENV = {"T10_F12_SCOPE_MODE": "warn", "T10_F9_BRAND_STYLE_MODE": "warn"}
_SEO_PREFIXES = ("title ", "meta ")
_PLACE_RE = re.compile(r"^(?:title |meta )?names '([^']+)'")

PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.tenant_id::text AS tenant_id,
           COALESCE(cp.channel, r.channel) AS channel, cp.status, cp.attempt_number, cp.created_at,
           cp.content_text, cp.gate_ledger, cp.repair_log, cp.discarded_attempts, cp.writer_input,
           cp.seo_title, cp.meta_description, cp.slug
    FROM acp_shared.content_piece cp
    LEFT JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id
    WHERE cp.status IN ('approved', 'held') AND cp.created_at > now() - make_interval(days => $1)
    ORDER BY cp.created_at DESC
    LIMIT $2
"""
LABELS_SQL = "SELECT piece_id::text AS piece_id, label FROM acp_shared.content_piece_label"


# ── pure parts (unit-tested) ───────────────────────────────────────────────────────────────────────────────────────

def _j(value: Any) -> Any:
    """A jsonb column as asyncpg returns it (text without a codec) → Python."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _entries(ledger: Any) -> list[dict]:
    from services.acp_content_writing.quality_gates import expand_ledger
    return [g for g in expand_ledger([g for g in (ledger or []) if isinstance(g, dict)]) if g.get("gate")]


def _gate(ledger: Any, gate: str) -> Optional[dict]:
    return next((g for g in _entries(ledger) if g["gate"] == gate), None)


def attempt_one(piece: dict) -> tuple[Optional[str], Optional[list]]:
    """(attempt 1's text or None, attempt 1's ledger or None): the final text and ledger of a piece that stopped at
    attempt 1, else its discarded attempt 1, else (no text) the repair_log snapshot of attempt 1."""
    if int(piece.get("attempt_number") or 1) == 1:
        return piece.get("content_text"), _j(piece.get("gate_ledger"))
    for d in _j(piece.get("discarded_attempts")) or []:
        if isinstance(d, dict) and d.get("attempt_number") == 1:
            return d.get("content_text"), d.get("gate_ledger")
    for e in _j(piece.get("repair_log")) or []:
        if isinstance(e, dict) and e.get("attempt") == 1 and isinstance(e.get("gate_ledger"), list):
            return None, e["gate_ledger"]
    return None, None


def f12_where(entry: Optional[dict]) -> Optional[str]:
    """'body' (a body finding: blocks in block mode), 'title_meta' (only title / meta findings: stays a warning) or
    None (passed / not run)."""
    if not entry or entry.get("passed"):
        return None
    vs = [str(v) for v in entry.get("violations") or []]
    return "body" if any(not v.startswith(_SEO_PREFIXES) for v in vs) else "title_meta"


def f12_finding_kind(violation: str) -> tuple[str, Optional[str]]:
    """(kind, place) of one F12_scope finding (hygiene.find_issues wording)."""
    v = str(violation)
    m = _PLACE_RE.match(v)
    if m:
        return ("title_meta_place" if v.startswith(_SEO_PREFIXES) else "body_place"), m.group(1)
    if v.startswith("states the whole trip's length"):
        return "trip_length", None
    if "but the trip goes on" in v:
        return "journey_end", None
    return "other", None


def is_floor_block(entry: Optional[dict]) -> bool:
    return bool(entry and not entry.get("passed") and entry.get("blocking", True)
                and any(" under the " in str(v) for v in entry.get("violations") or []))


def stored_stats(piece: dict, brand_style_sentences: Callable[[str, str], list]) -> dict:
    """What the stored check results of one piece say about the three checks."""
    from services.quality import registry as qr
    final = _j(piece.get("gate_ledger"))
    a1_text, a1_ledger = attempt_one(piece)
    f12_final, f12_a1 = _gate(final, qr.F12_SCOPE), _gate(a1_ledger, qr.F12_SCOPE)
    style_a1 = _gate(a1_ledger, qr.F9_BRAND_STYLE)
    style_final = _gate(final, qr.F9_BRAND_STYLE)
    a1_style_fail = bool(style_a1 and not style_a1.get("passed"))
    locatable = None
    if a1_style_fail and a1_text:
        locatable = any(brand_style_sentences(a1_text, v) for v in style_a1.get("violations") or [])
    f14_final = _gate(final, qr.F14_SOCIAL_SHAPE)
    return {
        "has_attempt1": a1_ledger is not None,
        "f12_final": f12_where(f12_final),
        "f12_attempt1": f12_where(f12_a1),
        "f12_findings": [f12_finding_kind(v) for v in (f12_final or {}).get("violations") or []],
        "f9_style_final_fail": bool(style_final and not style_final.get("passed")),
        "f9_style_attempt1_fail": a1_style_fail,
        "f9_style_attempt1_locatable": locatable,
        "f14_floor_attempt1": is_floor_block(_gate(a1_ledger, qr.F14_SOCIAL_SHAPE)),
        "f14_under_final": bool(f14_final and not f14_final.get("passed")
                                and any(" under the " in str(v) for v in f14_final.get("violations") or [])),
    }


def majority_label(labels: list[str]) -> Optional[str]:
    if not labels:
        return None
    c = Counter(labels).most_common()
    return c[0][0] if len(c) == 1 or c[0][1] > c[1][1] else None


def summarize(rows: list[dict], labels: dict[str, list[str]]) -> dict:
    """rows: {piece_id, status, channel, stored: stored_stats(), replay: replay result or None}."""
    n = len(rows)
    by_channel = Counter(r.get("channel") or "?" for r in rows)
    st = [r["stored"] for r in rows]
    kinds, places = Counter(), Counter()
    for s in st:
        for kind, place in s["f12_findings"]:
            kinds[kind] += 1
            if place:
                places[place] += 1
    replayed = [r for r in rows if r.get("replay") and not r["replay"].get("error")]
    new_holds = [r for r in replayed if r["replay"]["promoted_hold"] and not r["replay"]["base_hold"]]
    light = {pid for pid, ls in labels.items() if majority_label(ls) in LIGHT_LABELS}
    # attempt 1 passed in the default modes and the promoted run blocks it: one extra repair (and judge) round
    extra_repairs = Counter(r["replay"]["a1_promoted_first"] for r in replayed if r["replay"].get("a1_ran")
                            and r["replay"].get("a1_promoted_first") and not r["replay"].get("a1_base_first"))

    def pct(k: int, d: int) -> Optional[float]:
        return round(100.0 * k / d, 1) if d else None
    return {
        "pieces": n, "by_channel": dict(by_channel), "by_status": dict(Counter(r["status"] for r in rows)),
        "stored": {
            "with_attempt1_ledger": sum(s["has_attempt1"] for s in st),
            "f12_final_body": sum(s["f12_final"] == "body" for s in st),
            "f12_final_title_meta_only": sum(s["f12_final"] == "title_meta" for s in st),
            "f12_final_body_not_held": sum(s["f12_final"] == "body" and r["status"] != "held"
                                           for s, r in zip(st, rows)),
            "f12_attempt1_body": sum(s["f12_attempt1"] == "body" for s in st),
            "f12_finding_kinds": dict(kinds), "f12_places": dict(places.most_common(30)),
            "f9_style_attempt1_fail": sum(s["f9_style_attempt1_fail"] for s in st),
            "f9_style_attempt1_locatable": sum(bool(s["f9_style_attempt1_locatable"]) for s in st),
            "f9_style_attempt1_text_missing": sum(s["f9_style_attempt1_fail"]
                                                  and s["f9_style_attempt1_locatable"] is None for s in st),
            "f9_style_final_fail": sum(s["f9_style_final_fail"] for s in st),
            "f14_floor_attempt1": sum(s["f14_floor_attempt1"] for s in st),
            "f14_under_final": sum(s["f14_under_final"] for s in st),
        },
        "replay": {
            "replayed": len(replayed), "errors": sum(1 for r in rows if r.get("replay") and r["replay"].get("error")),
            "no_writer_input": sum(1 for r in rows if r.get("replay") is None),
            "new_holds": len(new_holds), "new_hold_rate_pct": pct(len(new_holds), len(replayed)),
            "new_holds_by_gate": dict(Counter(r["replay"]["promoted_hold"] for r in new_holds)),
            "new_holds_on_panel_light": sorted(r["piece_id"] for r in new_holds if r["piece_id"] in light),
            "labelled_light_replayed": sum(1 for r in replayed if r["piece_id"] in light),
            "new_hold_piece_ids": sorted(r["piece_id"] for r in new_holds),
            "attempt1_replayed": sum(1 for r in replayed if r["replay"].get("a1_ran")),
            "attempt1_extra_repairs_by_gate": dict(extra_repairs),
        },
    }


def first_blocking(ledger: list) -> Optional[str]:
    """The source gate of the first failed blocking result (run_quality_gates' first_failure), or None."""
    return next((g["gate"] for g in _entries(ledger) if not g["passed"] and g.get("blocking", True)), None)


@contextmanager
def env(values: dict):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def replay_piece(piece: dict, run: Callable[[str, list, bool], list]) -> Optional[dict]:
    """The default vs promoted runs of one piece's stored final text (attempt ≥ 2 → stock_blocking False, attempt 1 →
    True) and, when stored, attempt 1's text. `run(text, f9_ledger, stock_blocking)` → ledger. None without inputs."""
    from services.acp_content_writing import quality_gates as qg
    wi = _j(piece.get("writer_input"))
    if not isinstance(wi, dict) or not wi.get("seed"):
        return None
    out: dict = {"error": None, "a1_ran": False}
    final = _j(piece.get("gate_ledger")) or []
    stock = int(piece.get("attempt_number") or 1) == 1
    try:
        with env(DEFAULT_ENV):
            base = run(piece.get("content_text") or "", final, stock)
        with env(PROMOTED_ENV):
            promoted = run(piece.get("content_text") or "", final, stock)
        out["base_hold"] = (qg.hold_failure(base) or {}).get("gate")
        out["promoted_hold"] = (qg.hold_failure(promoted) or {}).get("gate")
        a1_text, a1_ledger = attempt_one(piece)
        if stock:  # the final text IS attempt 1
            b1, p1 = base, promoted
        elif a1_text and a1_ledger:
            with env(DEFAULT_ENV):
                b1 = run(a1_text, a1_ledger, True)
            with env(PROMOTED_ENV):
                p1 = run(a1_text, a1_ledger, True)
        else:
            return out
        out.update(a1_ran=True, a1_base_first=first_blocking(b1), a1_promoted_first=first_blocking(p1))
    except Exception as e:  # noqa: BLE001 — one piece that cannot be replayed is counted, never fatal
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


def make_runner(piece: dict) -> Callable[[str, list, bool], list]:
    from services.writing_memory.replay import PieceReplay, _stored_f9
    pr = PieceReplay(_j(piece.get("writer_input")) or {}, piece.get("channel") or "blog",
                     {k: piece.get(k) for k in ("seo_title", "meta_description", "slug")})

    def run(text: str, f9_ledger: list, stock_blocking: bool) -> list:
        return pr.run(text, pack=pr.stored_pack, f9=_stored_f9(f9_ledger), stock_blocking=stock_blocking)
    return run


# ── DB + main ──────────────────────────────────────────────────────────────────────────────────────────────────────

async def main() -> int:
    import asyncpg
    from services.acp_content_writing import quality_gates as qg
    if not hasattr(qg, "f12_scope_mode"):
        print(json.dumps({"error": "this image predates AA-788 (no promoted modes): run after the deploy"}))
        return 2
    days = int(os.environ.get("DAYS") or DEFAULT_DAYS)
    limit = int(os.environ.get("LIMIT") or DEFAULT_LIMIT)
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await conn.execute("SET default_transaction_read_only = on")
        assert (await conn.fetchval("SHOW default_transaction_read_only")) == "on"
        pieces = [dict(r) for r in await conn.fetch(PIECES_SQL, days, limit)]
        labels: dict[str, list[str]] = {}
        for r in await conn.fetch(LABELS_SQL):
            labels.setdefault(r["piece_id"], []).append(r["label"])
    finally:
        await conn.close()
    rows = []
    for p in pieces:
        row = {"piece_id": p["piece_id"], "status": p["status"], "channel": p.get("channel"),
               "stored": stored_stats(p, qg.brand_style_sentences), "replay": None}
        try:
            row["replay"] = replay_piece(p, make_runner(p)) if _j(p.get("writer_input")) else None
        except Exception as e:  # noqa: BLE001 — the inputs of an old piece may not rebuild
            row["replay"] = {"error": f"inputs: {type(e).__name__}: {str(e)[:160]}"}
        rows.append(row)
    report = {"name": "aa788-promotion-replay", "created_at": datetime.now(timezone.utc).isoformat(),
              "days": days, "limit": limit, "modes_promoted": PROMOTED_ENV, **summarize(rows, labels)}
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
