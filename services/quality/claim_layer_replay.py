"""AA-1050 (S294 D6) — the $0 carry-over replay of the dormant claim layers: how many published decisions each layer
would have changed. Read only, no model call, no write; the output is counts and ids only (no text).

The layers (inventory: docs/implementation-notes/AA-1050.md, "Carry-over inventory"):

  t10_claims        the F9 v2 claim-extraction stage (claim_rules.EXTRACTION_SYSTEM): a route row and a contract-smoke
                    case, no production caller. Measured from the ledger: calls of the stage in the window, and how
                    many of them were tied to a piece, a request or a tour (a decision path would set one).
  seed_align_tier1  AA-789 (services/acp_content_writing/seed_align/modes.py, settings
                    cis.quality_modes.t10_seed_align*):
                    deterministic (spaCy). Replayed on the final text of each stored piece with EVERY built class in
                    repair mode: a candidate that `modes.apply` adds to the STORED final ledger is a decision the layer
                    would have changed (the piece shipped as written; with the layer on it would have had a sentence
                    repair). Hold: none can (HOLD_PROMOTED is empty); the hold-capable fires are counted for G3.
                    The stored shadow records (writer_input.seed_align) are counted too.
  t10_adjudicate    AA-1000 tier 2 (seed_align/adjudicate.py, settings cis.quality_modes.t10_adjudicate*): an LLM judge.
                    A verdict cannot be replayed at $0, so it is measured from what is stored (writer_input.seed_align
                    [*].tier2: a `live` finding on a stored piece is a decision it changed) plus the deterministic
                    triggers it would send to the judge (the paid calls it would make, never made here).
  claim_shadow      AA-1050 W4 (services/quality/claim_shadow.py, env CLAIM_SHADOW): the instrument this issue promotes,
                    not a removal candidate. Its stored verdicts (shared.claim_shadow_verdict) say how many approved
                    objects it would have held or sent to review.

A layer is `zero_diff` when the window holds no decision it would have changed. Default rule (tech lead S294 D6): a
zero-diff layer that cannot pass W2 gets a removal PR; the adjudicator stays off (never removed) until TL-11 is lifted.

Run in a CIS container (services/ is in the image; this file needs only modules already on main), with the
container's DATABASE_URL (cis_app); the orchestrator's ecsx wrapper passes VAR=value pairs as the environment:

    tools/ecs/ecsx.sh apps/AA-CIS-App/services/quality/claim_layer_replay.py REPLAY_DAYS=90 REPLAY_LIMIT=300

or ``python -m services.quality.claim_layer_replay --days 90 --limit 300 [--out result.json]``. Sessions are read
only (default_transaction_read_only); ssl required unless REPLAY_DB_SSL=disable (local tests). The JSON goes to
stdout between REPLAY-BEGIN / REPLAY-END.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from typing import Any, Callable, Optional

SCHEMA = "aa1050-claim-layer-replay/1"
LAYERS = ("t10_claims", "seed_align_tier1", "t10_adjudicate", "claim_shadow")
LEDGER_STAGES = ("t10_claims", "t10_adjudicate", "claim_shadow")
SETTING_PREFIX = "cis.quality_modes.t10_"
DEFAULT_DAYS = 90
DEFAULT_LIMIT = 300
MAX_DAYS = 366
MAX_LIMIT = 1000
SAMPLE_IDS = 10

# Every stored T9 piece, approved or held, with a stored writer input (the gate inputs are rebuilt from it:
# writing_memory.replay.PieceReplay): the population of the R0 replay's _PIECES_SQL. Each row is one write session and
# already holds that session's final attempt (attempt_number counts T9's internal retries, 1-2); since migration 125 a
# reopened request gets a new row, and every approved or held row is a decision that shipped, so none is collapsed.
PIECES_SQL = """
SELECT piece_id::text AS piece_id, channel, status, content_text, seo_title, meta_description, slug, gate_ledger,
       writer_input
FROM acp_shared.content_piece
WHERE status IN ('approved', 'held') AND writer_input IS NOT NULL AND created_at >= now() - make_interval(days => $1)
ORDER BY created_at DESC, piece_id
LIMIT $2
"""
LEDGER_SQL = """
SELECT stage, count(*) AS calls,
       count(*) FILTER (WHERE content_piece_id IS NOT NULL OR angle_gate_request_id IS NOT NULL
                        OR tour_id IS NOT NULL) AS object_calls,
       count(DISTINCT content_piece_id) AS pieces,
       count(*) FILTER (WHERE is_shadow) AS shadow_calls,
       coalesce(sum(cost_usd), 0)::float AS cost_usd,
       max(created_at) AS last_at
FROM shared.llm_call_log
WHERE stage = ANY($1::text[]) AND created_at >= now() - make_interval(days => $2)
GROUP BY stage
"""
ROUTES_SQL = """
SELECT stage, role, is_active, model_id, disabled_reason IS NOT NULL AS has_disabled_reason
FROM shared.llm_role_config WHERE stage = ANY($1::text[])
"""
# The platform value in force of each layer setting (no free-text column is read).
SETTINGS_SQL = """
SELECT d.key, d.default_value, v.value, v.version
FROM shared.setting_definition d
LEFT JOIN LATERAL (
    SELECT value, version FROM shared.setting_value
    WHERE key = d.key AND tenant_id IS NULL AND status = 'approved' AND effective_from <= now()
    ORDER BY version DESC LIMIT 1
) v ON true
WHERE d.key LIKE $1
ORDER BY d.key
"""
# The W4 shadow's stored verdicts with the stored decision of their object.
SHADOW_SQL = """
SELECT s.surface, s.shadow_action, s.verdict, s.agreement, s.pinned, s.cached,
       coalesce(p.status, v.status) AS object_status,
       coalesce(s.piece_id::text, s.version_id::text) AS object_id
FROM shared.claim_shadow_verdict s
LEFT JOIN acp_shared.content_piece p ON p.piece_id = s.piece_id
LEFT JOIN gold_aa_internal.tenant_tour_versions v ON v.id = s.version_id
WHERE s.created_at >= now() - make_interval(days => $1)
"""
# Stored decisions that count as published: a T9 piece approved, a T2 version approved or AI-written and shown.
PUBLISHED = frozenset({"approved", "ai_generated"})


def _json(v: Any, default: Any = None) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return default if v is None else v


def _sample(ids) -> list[str]:
    return sorted(str(i) for i in ids)[:SAMPLE_IDS]


# ── t10_claims and the ledger ────────────────────────────────────────────────────────────────────────────────────────

def ledger_summary(rows: list[dict]) -> dict:
    """stage -> {calls, object_calls, pieces, shadow_calls, cost_usd, last_at} (a stage with no row: zeros)."""
    out = {st: {"calls": 0, "object_calls": 0, "pieces": 0, "shadow_calls": 0, "cost_usd": 0.0, "last_at": None}
           for st in LEDGER_STAGES}
    for r in rows:
        st = r.get("stage")
        if st in out:
            out[st] = {"calls": int(r.get("calls") or 0), "object_calls": int(r.get("object_calls") or 0),
                       "pieces": int(r.get("pieces") or 0), "shadow_calls": int(r.get("shadow_calls") or 0),
                       "cost_usd": round(float(r.get("cost_usd") or 0.0), 6),
                       "last_at": None if r.get("last_at") is None else str(r["last_at"])}
    return out


def t10_claims_layer(ledger: dict, routes: dict) -> dict:
    """The extraction stage changed a decision only if a call of it was tied to an object: no production code calls
    it, the contract smoke sends it with no object ids."""
    led = ledger.get("t10_claims") or {}
    diffs = int(led.get("object_calls") or 0)
    return {"decisions_changed": diffs, "zero_diff": diffs == 0, "ledger": led, "route": routes.get("t10_claims"),
            "measured_by": "ledger: calls of stage t10_claims tied to a piece, request or tour"}


# ── seed-align tier 1 and the tier-2 adjudicator over stored pieces ──────────────────────────────────────────────────

def stored_records(writer_input: Optional[dict]) -> tuple[list[dict], list[dict]]:
    """(tier-1 records, tier-2 records) of the attempts stored in writer_input.seed_align."""
    t1, t2 = [], []
    for e in (writer_input or {}).get("seed_align") or []:
        if not isinstance(e, dict):
            continue
        t1.append(e)
        if isinstance(e.get("tier2"), dict):
            t2.append(e["tier2"])
    return t1, t2


def strip_sa(legacy: list[dict]) -> list[dict]:
    """The legacy ledger without `sa:` findings (a piece written while a class was in repair carries them already),
    each result re-read as passed when only `sa:` findings had failed it."""
    from services.acp_content_writing.seed_align import modes as sa_modes

    out = []
    for g in legacy:
        vs = [v for v in g.get("violations") or [] if not str(v).startswith(sa_modes.FINDING_PREFIX)]
        if len(vs) == len(g.get("violations") or []):
            out.append(g)
            continue
        base = sa_modes.base_blocking(g)
        out.append({**{k: v for k, v in g.items() if k != sa_modes.BASE_BLOCKING_KEY}, "violations": vs,
                    "passed": not vs or not base, "blocking": base if vs else g.get("blocking", True)})
    return out


def added_findings(before: list[dict], after: list[dict]) -> list[str]:
    """The finding texts `modes.apply` added (same order and length of results on both sides)."""
    out: list[str] = []
    for b, a in zip(before, after):
        if a is not b:
            out += [v for v in a.get("violations") or [] if v not in (b.get("violations") or [])]
    return out


class PieceTools:
    """The production functions the replay calls, injectable for tests (they need spaCy and the full gate inputs)."""

    def __init__(self, *, inputs: Optional[Callable] = None, tier1: Optional[Callable] = None,
                 triggers: Optional[Callable] = None, expand: Optional[Callable] = None,
                 apply: Optional[Callable] = None):
        self.inputs = inputs or _rebuild_inputs
        self.tier1 = tier1 or _tier1_all_repair
        self.triggers = triggers or _tier2_triggers
        self.expand = expand or _expand
        self.apply = apply or _apply


def _rebuild_inputs(piece: dict) -> dict:
    """trip_ctx / hygiene_ctx / brief_texts / seed the writer built for the piece (R0's PieceReplay rebuild)."""
    from services.acp_content_writing.seed_align import modes as sa_modes
    from services.writing_memory.replay import PieceReplay

    pr = PieceReplay(piece.get("writer_input") or {}, piece.get("channel") or "blog",
                     {k: piece.get(k) for k in ("seo_title", "meta_description", "slug")})
    kw = pr._kw
    return {"trip_ctx": kw.get("trip_ctx"), "hygiene_ctx": kw.get("hygiene_ctx"), "brief_texts": kw.get("brief_texts"),
            "seed": sa_modes._seed_of(kw.get("trip_ctx"), kw.get("hygiene_ctx"))}


def _tier1_all_repair(text: str, inputs: dict):
    from services.acp_content_writing.seed_align import checks as sa_checks
    from services.acp_content_writing.seed_align import modes as sa_modes

    return sa_modes.run(text, trip_ctx=inputs["trip_ctx"], hygiene_ctx=inputs["hygiene_ctx"],
                        brief_texts=inputs["brief_texts"], modes={c: sa_modes.REPAIR for c in sa_checks.BUILT_CLASSES})


def _tier2_triggers(text: str, inputs: dict, tier1_record: Optional[dict]) -> list:
    from services.acp_content_writing.seed_align import adjudicate as adj
    from services.acp_content_writing.seed_align import modes as sa_modes

    briefs = sa_modes._brief_lines(inputs["brief_texts"], inputs["trip_ctx"], inputs["hygiene_ctx"])
    return adj.find_triggers(text, inputs.get("seed"), tier1_record, classes=adj.CLASSES, brief_texts=briefs)


def _expand(ledger: list) -> list[dict]:
    from services.acp_content_writing.quality_gates import expand_ledger

    return expand_ledger([g for g in ledger or [] if isinstance(g, dict)])


def _apply(legacy: list[dict], sa_run) -> list[dict]:
    from services.acp_content_writing.seed_align import modes as sa_modes

    return sa_modes.apply(legacy, sa_run)


def hold_capable(cls: Optional[str]) -> tuple[bool, bool]:
    """(hold-capable class, promoted) of an `sa:` class."""
    from services.acp_content_writing.seed_align import modes as sa_modes

    return cls in sa_modes.HOLD_CAPABLE, cls in sa_modes.HOLD_PROMOTED


def _new_counters() -> tuple[dict, dict]:
    t1 = {"pieces": 0, "replayed": 0, "errors": Counter(), "status": Counter(), "candidates_by_class": Counter(),
          "pieces_with_candidate": 0, "decisions_changed": 0, "decisions_changed_by_status": Counter(),
          "changed_ids": set(), "hold_capable_fires": Counter(), "holds": 0,
          "stored": {"pieces_with_record": 0, "attempts": 0, "status": Counter(), "candidates_by_mode": Counter(),
                     "live_findings_on_published": 0}}
    t2 = {"pieces_with_trigger": 0, "triggers_by_class": Counter(), "would_send": 0, "trigger_errors": 0,
          "stored": {"pieces_with_record": 0, "attempts": 0, "status": Counter(), "findings": 0,
                     "live_on_published": 0, "live_ids": set(), "cost_usd": 0.0}}
    return t1, t2


def _count_stored_tier1(st: dict, records: list[dict], published: bool, legacy: list[dict]) -> None:
    from services.acp_content_writing.seed_align import modes as sa_modes

    if records:
        st["pieces_with_record"] += 1
        st["attempts"] += len(records)
    for r in records:
        st["status"][str(r.get("status") or "none")] += 1
        for c in r.get("candidates") or []:
            if isinstance(c, dict):
                st["candidates_by_mode"][str(c.get("mode"))] += 1
    if published and any(str(v).startswith(sa_modes.FINDING_PREFIX)
                         for g in legacy for v in g.get("violations") or []):
        st["live_findings_on_published"] += 1


def _count_stored_tier2(st: dict, records: list[dict], published: bool, piece_id) -> None:
    if records:
        st["pieces_with_record"] += 1
        st["attempts"] += len(records)
    for r in records:
        st["status"][str(r.get("status") or "none")] += 1
        st["findings"] += len(r.get("findings") or [])
        st["cost_usd"] += float(r.get("cost_usd") or 0.0)
    if published and any(r.get("live") for r in records):
        st["live_on_published"] += 1
        st["live_ids"].add(piece_id)


def _count_tier1_run(t1: dict, record: dict, before: list[dict], after: list[dict], piece: dict) -> None:
    t1["replayed"] += 1
    t1["status"][str(record.get("status") or "none")] += 1
    cands = [c for c in record.get("candidates") or [] if isinstance(c, dict)]
    t1["pieces_with_candidate"] += bool(cands)
    for c in cands:
        t1["candidates_by_class"][str(c.get("cls"))] += 1
        capable, promoted = hold_capable(c.get("cls"))
        if capable:
            t1["hold_capable_fires"][str(c.get("cls"))] += 1
        t1["holds"] += int(capable and promoted)
    if added_findings(before, after):
        status = piece.get("status")
        t1["decisions_changed_by_status"][str(status)] += 1
        if status in PUBLISHED:
            t1["decisions_changed"] += 1
            t1["changed_ids"].add(piece.get("piece_id"))


def _count_triggers(t2: dict, trig: list) -> None:
    from services.acp_content_writing.seed_align import adjudicate as adj

    if not trig:
        return
    t2["pieces_with_trigger"] += 1
    for t in trig:
        t2["triggers_by_class"][str(getattr(t, "cls", "unknown"))] += 1
    t2["would_send"] += min(len(trig), adj.MAX_ITEMS)


def replay_pieces(pieces: list[dict], tools: Optional[PieceTools] = None) -> dict:
    """Tier 1 (all classes in repair) and the tier-2 triggers over the final text of each stored piece."""
    tools = tools or PieceTools()
    t1, t2 = _new_counters()
    for piece in pieces:
        t1["pieces"] += 1
        wi = _json(piece.get("writer_input"), {}) or {}
        piece = {**piece, "writer_input": wi}
        published = piece.get("status") in PUBLISHED
        legacy = tools.expand(_json(piece.get("gate_ledger"), []) or [])
        rec1, rec2 = stored_records(wi)
        _count_stored_tier1(t1["stored"], rec1, published, legacy)
        _count_stored_tier2(t2["stored"], rec2, published, piece.get("piece_id"))
        text = piece.get("content_text") or ""
        try:
            inputs = tools.inputs(piece)
            run = tools.tier1(text, inputs)
        except Exception as e:  # noqa: BLE001 — a piece whose input cannot be rebuilt is counted, never fatal
            t1["errors"][type(e).__name__] += 1
            continue
        record = getattr(run, "record", None) or {}
        before = strip_sa(legacy)
        _count_tier1_run(t1, record, before, tools.apply(before, run), piece)
        try:
            _count_triggers(t2, tools.triggers(text, inputs, record))
        except Exception:  # noqa: BLE001
            t2["trigger_errors"] += 1
    return {"tier1": _plain(t1), "tier2": _plain(t2)}


def _plain(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and not isinstance(v, Counter):
            out[k] = _plain(v)
        elif isinstance(v, Counter):
            out[k] = dict(sorted(v.items()))
        elif isinstance(v, set):
            out[k.replace("_ids", "_sample")] = _sample(v)
            out[k.replace("_ids", "_count")] = len(v)
        elif isinstance(v, float):
            out[k] = round(v, 6)
        else:
            out[k] = v
    return out


def seed_align_layer(replayed: dict) -> dict:
    t1 = replayed["tier1"]
    diffs = int(t1["decisions_changed"])
    return {"decisions_changed": diffs, "zero_diff": diffs == 0, "holds": t1["holds"], "replay": t1,
            "measured_by": "tier 1 re-run on each stored final text, every built class in repair; a finding added to "
                           "the stored final ledger of a published piece is a changed decision"}


def adjudicate_layer(replayed: dict, ledger: dict) -> dict:
    t2 = replayed["tier2"]
    diffs = int(t2["stored"]["live_on_published"])
    return {"decisions_changed": diffs, "zero_diff": diffs == 0, "measurable_at_0_usd": False,
            "replay": t2, "ledger": ledger.get("t10_adjudicate"),
            "measured_by": "stored tier-2 records (a live finding on a published piece); verdicts on the triggers "
                           "need paid calls, so a zero here only says the layer never ran live"}


# ── the W4 claim shadow ──────────────────────────────────────────────────────────────────────────────────────────────

def shadow_layer(rows: list[dict], ledger: dict) -> dict:
    by_action, by_surface, would = Counter(), Counter(), Counter()
    ids: set = set()
    for r in rows:
        by_action[str(r.get("shadow_action"))] += 1
        by_surface[str(r.get("surface"))] += 1
        if r.get("shadow_action") in ("hold", "review") and r.get("object_status") in PUBLISHED:
            would[str(r.get("shadow_action"))] += 1
            ids.add(r.get("object_id"))
    diffs = len(ids)
    return {"decisions_changed": diffs, "zero_diff": diffs == 0, "rows": len(rows),
            "by_action": dict(sorted(by_action.items())), "by_surface": dict(sorted(by_surface.items())),
            "would_change_by_action": dict(sorted(would.items())), "changed_sample": _sample(ids),
            "ledger": ledger.get("claim_shadow"), "removal_candidate": False,
            "measured_by": "stored shadow verdicts: hold or review on an object whose stored decision is published"}


# ── settings and the decision ────────────────────────────────────────────────────────────────────────────────────────

def settings_in_force(rows: list[dict]) -> dict:
    """key -> value in force (the approved platform value, else the default)."""
    out = {}
    for r in rows:
        val = _json(r.get("value")) if r.get("value") is not None else _json(r.get("default_value"))
        out[r["key"]] = {"value": val, "source": "approved" if r.get("value") is not None else "default"}
    return out


def recommendation(layers: dict) -> dict:
    """The D6 default per layer (the tech lead decides): remove a zero-diff layer that has no W2 pass record."""
    rec = {}
    claims = layers["t10_claims"]
    rec["t10_claims"] = ("remove: ship the prepared removal (route switched off, smoke case dropped)"
                         if claims["zero_diff"] else "keep: a call was tied to an object; read the ledger ids first")
    sa = layers["seed_align_tier1"]
    rec["seed_align_tier1"] = (
        "zero diffs, but tier 2 (kept off, not removed, D6) reads tier 1's record: removing tier 1 removes the "
        "adjudicator with it; decide both together" if sa["zero_diff"] else
        f"{sa['decisions_changed']} published pieces would have had a sentence repair: label them before a "
        "delete or a promote")
    rec["t10_adjudicate"] = "keep off until TL-11 is lifted (D6); no removal"
    rec["claim_shadow"] = "keep: the W4 instrument this issue promotes"
    return rec


async def _fetch(conn, sql: str, *args) -> list[dict]:
    return [dict(r) for r in await conn.fetch(sql, *args)]


async def run(days: int, limit: int, *, dsn: Optional[str] = None, tools: Optional[PieceTools] = None) -> dict:
    import asyncpg

    ssl = None if os.environ.get("REPLAY_DB_SSL") == "disable" else "require"
    conn = await asyncpg.connect(dsn or os.environ["DATABASE_URL"], ssl=ssl,
                                 server_settings={"default_transaction_read_only": "on"})
    try:
        pieces = await _fetch(conn, PIECES_SQL, days, limit)
        ledger = ledger_summary(await _fetch(conn, LEDGER_SQL, list(LEDGER_STAGES), days))
        routes = {r["stage"]: {k: r[k] for k in ("role", "is_active", "model_id", "has_disabled_reason")}
                  for r in await _fetch(conn, ROUTES_SQL, list(LEDGER_STAGES))}
        settings = settings_in_force(await _fetch(conn, SETTINGS_SQL, SETTING_PREFIX + "%"))
        try:
            shadow_rows = await _fetch(conn, SHADOW_SQL, days)
        except asyncpg.UndefinedTableError:
            shadow_rows = []
    finally:
        await conn.close()
    # CPU-bound (spaCy, the gate inputs): off the event loop, like the R0 replay
    replayed = await asyncio.to_thread(replay_pieces, pieces, tools)
    layers = {"t10_claims": t10_claims_layer(ledger, routes),
              "seed_align_tier1": seed_align_layer(replayed),
              "t10_adjudicate": adjudicate_layer(replayed, ledger),
              "claim_shadow": shadow_layer(shadow_rows, ledger)}
    return {"schema": SCHEMA, "cost_usd": 0, "window_days": days, "piece_limit": limit, "pieces": len(pieces),
            "settings": settings, "routes": routes, "layers": layers, "recommendation": recommendation(layers)}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def parse_args(argv: Optional[list[str]] = None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--days", type=int, default=_int_env("REPLAY_DAYS", DEFAULT_DAYS))
    p.add_argument("--limit", type=int, default=_int_env("REPLAY_LIMIT", DEFAULT_LIMIT))
    p.add_argument("--out", default=os.environ.get("REPLAY_OUT"))
    a = p.parse_args(argv)
    if not (1 <= a.days <= MAX_DAYS and 1 <= a.limit <= MAX_LIMIT):
        p.error(f"--days in 1..{MAX_DAYS}, --limit in 1..{MAX_LIMIT}")
    return a


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    result = asyncio.run(run(args.days, args.limit))
    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    print("REPLAY-BEGIN")
    print(text)
    print("REPLAY-END")
    return 0


if __name__ == "__main__":
    sys.exit(main())
