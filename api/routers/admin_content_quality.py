"""AA-769 — /admin/content-quality: the admin home for every quality signal the tenant no longer
sees (AA-770: tenant always receives T2/T9 content; gate results, held reasons, flagged
sentences, writer inputs and per-gate stats live here, for AA admins to improve the system).

Cross-tenant by design: admin pool (aa_cis_admin, BYPASSRLS), X-Admin-Secret checked on every
route like the other /admin routers. Read-only except the place-card verdict, the place-card
merge (both audited in acp_shared.audit_log), the place-card build job, and (S234) the sentence
actions on one piece:
  POST /pieces/{id}/sentences/suggest — one cheap LLM call (stage t9_repair) proposing a rewrite of
                                         one flagged sentence; writes nothing to the piece.
  POST /pieces/{id}/sentences/apply   — deterministic: cut the sentence or put the (checked)
                                         replacement in its place, audit-logged with before/after.
  POST /requests/{id}/regenerate-angles — clone the request (same goal, atom, subject, route) and
                                         enqueue t8_angles for the clone; the old request and its
                                         pieces' angles stay as they were. Audit-logged.
All three require the `admin` staff role (x-admin-role, forwarded by the BFF from the verified JWT).
Gate results (`gate_ledger`, `flags`) are left as the writer produced them: they measure the
writer, not the admin's edit.

Counting rule (S232 lesson): `held_reason` names only the first blocking gate; every count and
filter here reads `gate_ledger` (+ `flags`), never `held_reason`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret
from services.acp_content_writing import sentence_repair, text_integrity
from services.acp_content_writing.generate import SentenceRepairError, repair_sentences_detailed
from services.acp_content_writing.quality_gates import _length_bounds_for_channel
from services.acp_content_writing.service import grounding_from_writer_input
from services.acp_shared import place_cards
from services.acp_shared.audit_log import AdminAuditAction, PlatformAuditAction, write_audit_log

router = APIRouter(prefix="/admin/content-quality", tags=["admin-content-quality"])

_PIECE_SORTS = {"created_at": "cp.created_at", "status": "cp.status", "channel": "channel",
                "tenant": "t.name", "cost_usd": "cost_usd", "attempt": "cp.attempt_number"}
_VERSION_SORTS = {"created_at": "ttv.created_at", "status": "ttv.status", "tenant": "t.name",
                  "quality_score": "ttv.quality_score", "cost_usd": "cost_usd"}


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def _row(r) -> dict:
    out = {}
    for k, v in dict(r).items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif isinstance(v, UUID):
            v = str(v)
        elif isinstance(v, Decimal):
            v = float(v)
        out[k] = _j(v) if k in ("gate_ledger", "flags", "repair_log", "discarded_attempts", "writer_input",
                                "facts", "escalate_detail", "failed_gates", "flag_gates") else v
    return out


# ── T9 pieces ──────────────────────────────────────────────────────────────────────────────────

_PIECE_FROM = """
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    LEFT JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
    LEFT JOIN acp_shared.angle_gate_option ago
        ON ago.option_id = cp.angle_gate_option_id
        OR (cp.angle_gate_option_id IS NULL AND ago.request_id = agr.request_id AND ago.chosen = true)
"""

_FAILED_GATES_SQL = """
    COALESCE((SELECT jsonb_agg(DISTINCT g->>'gate') FROM jsonb_array_elements(cp.gate_ledger) g
              WHERE (g->>'passed')::boolean = false AND COALESCE((g->>'blocking')::boolean, true)),
             '[]'::jsonb) AS failed_gates,
    COALESCE((SELECT jsonb_agg(DISTINCT g->>'gate') FROM jsonb_array_elements(COALESCE(cp.flags, '[]'::jsonb)) g),
             '[]'::jsonb) AS flag_gates
"""

_COST_SQL = """
    COALESCE((SELECT sum(l.cost_usd) FROM shared.llm_call_log l WHERE l.job_id = cp.job_id), 0) AS cost_usd
"""


def _piece_filters(tenant_id, channel, status, gate, q) -> tuple[str, list]:
    where, params = ["true"], []
    if tenant_id:
        params.append(tenant_id)
        where.append(f"cp.tenant_id = ${len(params)}::uuid")
    if channel:
        params.append(channel)
        where.append(f"COALESCE(cp.channel, agr.channel) = ${len(params)}")
    if status:
        params.append(status)
        where.append(f"cp.status = ${len(params)}")
    if gate:
        params.append(gate)
        where.append(
            f"EXISTS (SELECT 1 FROM jsonb_array_elements(cp.gate_ledger || COALESCE(cp.flags, '[]'::jsonb)) g "
            f"WHERE g->>'gate' = ${len(params)} AND (g->>'passed')::boolean = false)")
    if q:
        params.append(f"%{q}%")
        where.append(f"(cp.seo_title ILIKE ${len(params)} OR ago.name ILIKE ${len(params)} "
                     f"OR cp.content_text ILIKE ${len(params)})")
    return " AND ".join(where), params


@router.get("/pieces")
async def list_pieces(
    request: Request,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    tenant_id: Optional[str] = None, channel: Optional[str] = None, status: Optional[str] = None,
    gate: Optional[str] = None, q: Optional[str] = None,
    sort: str = "created_at", sort_dir: str = "desc",
    x_admin_secret: str = Header(None),
):
    """Every tenant's T9 pieces: final status, the blocking gates that failed (from gate_ledger),
    the warn flags, attempts and the LLM cost of the write job."""
    verify_admin_secret(x_admin_secret)
    where, params = _piece_filters(tenant_id, channel, status, gate, q)
    order = _PIECE_SORTS.get(sort, "cp.created_at")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT count(*) {_PIECE_FROM} WHERE {where}", *params)
        rows = await conn.fetch(
            f"""
            SELECT cp.piece_id, cp.tenant_id, t.name AS tenant_name,
                   COALESCE(cp.channel, agr.channel) AS channel, cp.status, cp.held_reason,
                   cp.attempt_number, cp.created_at, cp.job_id,
                   COALESCE(cp.seo_title, ago.name) AS title, ago.name AS angle_name, agr.goal,
                   length(cp.content_text) AS content_chars,
                   (cp.writer_input IS NOT NULL) AS has_writer_input,
                   {_FAILED_GATES_SQL}, {_COST_SQL}
            {_PIECE_FROM}
            WHERE {where}
            ORDER BY {order} {direction} NULLS LAST, cp.piece_id
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """, *params, limit, offset)
    return {"data": [_row(r) for r in rows], "total": total, "limit": limit, "offset": offset}


# Highlights: where in the final text each flagged sentence / phrase sits.
_F9_PHRASE_RE = re.compile(r'exact flagged phrase\(s\): (.+)$')
_QUOTED_RE = re.compile(r'"([^"]{3,})"')
_BRAND_RE = re.compile(r"^(.+?): '(.+)'\s*$", re.DOTALL)


def highlights(content_text: str, gate_ledger: list, flags: list) -> list[dict]:
    """[{start, end, gate, blocking, problem}] for every flagged sentence or phrase found in the
    final text. Parses the same violation shapes sentence_repair.py reads; a quote that cannot
    be found (rewritten later, or too short) is left out."""
    out: list[dict] = []
    text = content_text or ""
    for g in list(gate_ledger or []) + list(flags or []):
        if not isinstance(g, dict) or g.get("passed", False):
            continue
        gate, blocking = g.get("gate"), g.get("blocking", True)
        for v in g.get("violations") or []:
            if not isinstance(v, str):
                continue
            spans: list[tuple[str, str]] = []
            if gate == "F1_grounding":
                m = sentence_repair._F1_QUOTE_RE.search(v)
                if m:
                    spans.append((m.group(1), v.split(" not present", 1)[0]))
            elif gate in ("F2_brand_forbidden", "F11_brand_name"):
                m = _BRAND_RE.search(v)
                if m:
                    spans.append((m.group(2), m.group(1)))
            elif gate in ("F12_scope", "F13_hygiene", "F13_leak", "F13_repeat", "F14_social_shape", "F14_first_line",
                          "F15_voice", "F15_voice_warn", "F17_trip_fact", "F18_rarity"):
                # S234 — "<problem>: '<sentence>'"; a title/meta scope hit is not in the text. F14's
                # word-count line quotes nothing and is left out; its first line / hashtags are quoted.
                # F15_voice quotes its sentence the same way (plan C4).
                m = _BRAND_RE.search(v)
                if m and not v.startswith(("title ", "meta ")):
                    quote = m.group(2)
                    if sentence_repair.locate_sentence(text, quote) is None and quote in text:
                        i = text.find(quote)  # a short quote (a hashtag) is highlighted as a phrase
                        out.append({"start": i, "end": i + len(quote), "gate": gate, "blocking": blocking,
                                    "problem": m.group(1)[:300]})
                    else:
                        spans.append((quote, m.group(1)))
            elif gate == "F9_cta_fact" and "unsupported claim(s):" in v:
                for sent, claim, why in sentence_repair._F9_CLAIM_RE.findall(v.split("unsupported claim(s):", 1)[1]):
                    spans.append((sent, f"{claim.strip()} — {why.strip()}"))
            elif gate == "F9_brand_style":
                m = _F9_PHRASE_RE.search(v)
                for phrase in _QUOTED_RE.findall(m.group(1)) if m else []:
                    i = text.find(phrase)
                    if i >= 0:
                        out.append({"start": i, "end": i + len(phrase), "gate": gate, "blocking": blocking,
                                    "problem": v.split(" — ", 1)[0][:200]})
            for quote, problem in spans:
                span = sentence_repair.locate_sentence(text, quote)
                if span:
                    out.append({"start": span[0], "end": span[1], "gate": gate, "blocking": blocking,
                                "problem": problem[:300]})
    # A non-blocking gate sits in both gate_ledger and flags: keep one highlight per span + gate.
    seen, unique = set(), []
    for h in sorted(out, key=lambda h: (h["start"], h["end"])):
        if (h["start"], h["end"], h["gate"]) not in seen:
            seen.add((h["start"], h["end"], h["gate"]))
            unique.append(h)
    # S234 — edit-ready: the exact span text (the client sends it back so a stale offset can never
    # cut the wrong words) and the whole sentence around a phrase hit, which is what cut/rewrite act on.
    for h in unique:
        ss, se = sentence_repair.expand_to_sentence(text, h["start"], h["end"])
        sentence = _plain(text[ss:se])
        h.update(text=text[h["start"]:h["end"]], sentence_start=ss, sentence_end=se, sentence=sentence,
                 key=_sentence_key(sentence))
    return unique


def _plain(span: str) -> str:
    return " ".join(re.sub(sentence_repair._TAG, " ", span).split())


def _sentence_key(sentence: str) -> str:
    return hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:12]


def flagged_sentences(content_text: str, hls: list[dict]) -> list[dict]:
    """One actionable item per flagged sentence, for the drawer's Cut / Rewrite list: highlights on
    the same sentence (an F1 number and an F9 phrase, say) merge into one item listing every gate
    and problem. `start`/`end`/`text` are the sentence span the actions send back."""
    items: dict[tuple[int, int], dict] = {}
    for h in hls:
        span = (h["sentence_start"], h["sentence_end"])
        it = items.get(span)
        if it is None:
            it = items[span] = {"key": h["key"], "start": span[0], "end": span[1],
                                "text": content_text[span[0]:span[1]], "sentence": h["sentence"],
                                "gates": [], "problems": [], "blocking": False}
        if h["gate"] not in it["gates"]:
            it["gates"].append(h["gate"])
        it["problems"].append({"gate": h["gate"], "problem": h["problem"], "blocking": h["blocking"]})
        it["blocking"] = it["blocking"] or bool(h["blocking"])
    return [items[k] for k in sorted(items)]


_PIECE_DETAIL_SQL = f"""
    SELECT cp.piece_id, cp.tenant_id, t.name AS tenant_name, COALESCE(cp.channel, agr.channel) AS channel,
           cp.status, cp.held_reason, cp.attempt_number, cp.created_at, cp.job_id,
           cp.content_text, cp.seo_title, cp.meta_description, cp.slug, cp.gate_ledger, cp.flags,
           cp.repair_log, cp.discarded_attempts, cp.writer_input, cp.route_hub_name,
           agr.goal, agr.atom_id, agr.request_id,
           ago.name AS angle_name, ago.why_it_works AS angle_why, ago.best_final_style AS angle_style,
           EXISTS (SELECT 1 FROM acp_shared.publish_log pl
                   WHERE pl.piece_id = cp.piece_id AND pl.status = 'published') AS published,
           {_COST_SQL}
    {_PIECE_FROM}
    WHERE cp.piece_id = $1::uuid
"""

# S234 — the drawer's cost includes calls made for this piece outside its write job (the admin
# rewrite suggestions, logged with content_piece_id).
_PIECE_COST_BY_STAGE_SQL = """
    SELECT stage, role, count(*) AS calls, sum(cost_usd) AS cost_usd
    FROM shared.llm_call_log WHERE job_id = $1 OR content_piece_id = $2::uuid
    GROUP BY stage, role ORDER BY sum(cost_usd) DESC NULLS LAST
"""

_ADMIN_EDITS_SQL = """
    SELECT action, actor, details, created_at FROM acp_shared.audit_log
    WHERE tenant_id = $1 AND resource_type = 'content_piece' AND resource_id = $2 AND action LIKE $3
    ORDER BY created_at DESC LIMIT 50
"""


async def _load_piece_detail(conn, piece_id: str) -> dict:
    """GET /pieces/{id} and the apply response share this one read."""
    row = await conn.fetchrow(_PIECE_DETAIL_SQL, piece_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Piece not found")
    cost_rows = await conn.fetch(_PIECE_COST_BY_STAGE_SQL, row["job_id"], piece_id)
    siblings = await conn.fetch(
        """
        SELECT piece_id, status, attempt_number, created_at FROM acp_shared.content_piece
        WHERE angle_gate_request_id = $1 AND piece_id <> $2::uuid ORDER BY created_at
        """, row["request_id"], piece_id)
    edits = await conn.fetch(_ADMIN_EDITS_SQL, str(row["tenant_id"]), str(row["piece_id"]),
                             AdminAuditAction.ADMIN_SENTENCE_PREFIX + "%")
    piece = _row(row)
    piece["highlights"] = highlights(piece["content_text"], piece["gate_ledger"], piece["flags"])
    piece["flagged_sentences"] = flagged_sentences(piece["content_text"] or "", piece["highlights"])
    piece["cost_by_stage"] = [_row(r) for r in cost_rows]
    piece["other_pieces_for_request"] = [_row(r) for r in siblings]
    piece["admin_edits"] = [{**_row(r), "details": _j(r["details"]) or {}} for r in edits]
    return piece


@router.get("/pieces/{piece_id}")
async def get_piece(piece_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        return await _load_piece_detail(conn, piece_id)


# ── S234: cut / rewrite one flagged sentence (admin only) ─────────────────────────────────────

_EDITABLE_STATUSES = ("approved", "held")  # same boundary as the tenant hand-edit (AA-569)
_SUGGEST_MAX_TOKENS = 400  # one sentence of JSON back
_SUGGEST_STAGE = "t9_repair"


def _require_admin_role(x_admin_role: Optional[str]) -> None:
    """Defense in depth behind the BFF's own check: the sentence actions change a tenant's piece,
    so only the `admin` staff role (never reviewer / content) may call them."""
    if x_admin_role != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can edit a piece")


def _resolve(content_text: str, start: int, end: int, text: str) -> tuple[int, int]:
    span = sentence_repair.resolve_span(content_text, start, end, text)
    if span is None:
        raise HTTPException(status_code=409, detail="The piece changed since it was loaded — reload and try again")
    return span


class SentenceSuggestBody(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=5000)
    problem: str = Field("", max_length=1000)
    note: Optional[str] = Field(None, max_length=200)


@router.post("/pieces/{piece_id}/sentences/suggest")
async def suggest_sentence(piece_id: str, body: SentenceSuggestBody, request: Request,
                           x_admin_secret: str = Header(None), x_admin_role: Optional[str] = Header(None)):
    """A proposed rewrite of one sentence: ONE call on the `t9_repair` stage (Haiku, max 400
    tokens, about $0.001-0.006), logged to llm_call_log with this piece's id. Nothing is saved —
    the admin reviews (or edits) it and sends it to /apply. The suggestion is run through the same
    deterministic checks /apply uses, so the drawer can show what would block it."""
    verify_admin_secret(x_admin_secret)
    _require_admin_role(x_admin_role)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cp.content_text, cp.status, cp.tenant_id, cp.writer_input, cp.angle_gate_request_id,
                   COALESCE(cp.channel, agr.channel) AS channel
            FROM acp_shared.content_piece cp
            LEFT JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
            WHERE cp.piece_id = $1::uuid
            """, piece_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Piece not found")
    wi = _j(row["writer_input"])
    if not isinstance(wi, dict) or not wi:
        raise HTTPException(status_code=409, detail="No writer input stored for this piece (written before "
                                                    "AA-769), so there is no source to rewrite from. Cut or "
                                                    "edit it by hand instead.")
    start, end = _resolve(row["content_text"] or "", body.start, body.end, body.text)
    sentence = _plain(row["content_text"][start:end])
    problem = body.problem.strip() or "The editor wants this sentence rewritten."
    if body.note and body.note.strip():
        problem += f" Editor note: {body.note.strip()}"
    grounding = grounding_from_writer_input(wi)
    try:
        reps, cost, model = await asyncio.to_thread(
            repair_sentences_detailed,
            sentences=[{"sentence": sentence, "problem": problem,
                        # S234 — its neighbours, so the rewrite does not restate them (same as T9)
                        "context": sentence_repair.neighbours(row["content_text"], {"start": start, "end": end})}],
            content_seed=grounding, facts_text=None, channel=row["channel"] or "unknown",
            tenant_id=str(row["tenant_id"]), angle_gate_request_id=str(row["angle_gate_request_id"]),
            stage=_SUGGEST_STAGE, mode="admin_suggest", attempt=None, content_piece_id=piece_id,
            max_tokens=_SUGGEST_MAX_TOKENS,
        )
    except SentenceRepairError as e:
        raise HTTPException(status_code=502, detail=f"The model reply could not be read: {str(e)[:200]}")
    replacement = sentence_repair.sanitize_replacement(reps[0], row["channel"])
    return {"start": start, "end": end, "sentence": sentence, "replacement": replacement,
            "checks": sentence_repair.check_replacement(replacement, grounding, wi.get("brand_pack"),
                                                        writer_input=wi, text=row["content_text"],
                                                        span=(start, end)),
            "model": model, "cost_usd": cost}


class SentenceApplyBody(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=5000)  # the span the admin saw
    replacement: str = Field("", max_length=3000)  # "" = cut
    mode: str  # cut | rewrite (the suggestion as-is) | manual (the admin's own text)
    gate: Optional[str] = Field(None, max_length=100)
    problem: Optional[str] = Field(None, max_length=1000)
    suggestion: Optional[str] = Field(None, max_length=3000)
    suggestion_model: Optional[str] = Field(None, max_length=200)
    suggestion_cost_usd: Optional[float] = None


_ACTION_BY_MODE = {"cut": AdminAuditAction.SENTENCE_CUT, "rewrite": AdminAuditAction.SENTENCE_REWRITTEN,
                   "manual": AdminAuditAction.SENTENCE_EDITED}


@router.post("/pieces/{piece_id}/sentences/apply")
async def apply_sentence(piece_id: str, body: SentenceApplyBody, request: Request,
                         x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
                         x_admin_role: Optional[str] = Header(None)):
    """Cut one sentence, or put a replacement in its place. Deterministic, no LLM. In one
    transaction: lock the piece, check the span text still matches (else its one occurrence, else
    409), run the replacement through F1 / F2 / brand-forbidden / F11 and a near-verbatim
    neighbour (422 with the violations); F12_scope / F13_hygiene hits are returned as `warnings`
    and saved anyway (S234 review: F12 is warn-only until its replay shows no false positive),
    splice + tidy, refuse a cut that empties the piece or drops it under the channel's minimum
    length, UPDATE content_text and write the audit row with before/after. What the edit newly
    breaks (`text_integrity.edit_findings`: a second / reworded CTA, a fragment, a repeat) is an
    F13_repeat warning — the final integrity pass does not run here. Returns the refreshed
    piece detail. A published piece is edited here only — WordPress is not re-synced."""
    verify_admin_secret(x_admin_secret)
    _require_admin_role(x_admin_role)
    if body.mode not in _ACTION_BY_MODE:
        raise HTTPException(status_code=422, detail="mode must be cut, rewrite or manual")
    replacement = body.replacement.strip()
    if body.mode == "cut":
        replacement = ""
    elif not replacement:
        raise HTTPException(status_code=422, detail="An empty replacement is a cut — use mode 'cut'")
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT cp.content_text, cp.status, cp.tenant_id, cp.writer_input,
                       COALESCE(cp.channel, (SELECT agr.channel FROM acp_shared.angle_gate_request agr
                                             WHERE agr.request_id = cp.angle_gate_request_id)) AS channel,
                       EXISTS (SELECT 1 FROM acp_shared.publish_log pl
                               WHERE pl.piece_id = cp.piece_id AND pl.status = 'published') AS published
                FROM acp_shared.content_piece cp
                WHERE cp.piece_id = $1::uuid
                FOR UPDATE OF cp
                """, piece_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Piece not found")
            if row["status"] not in _EDITABLE_STATUSES:
                raise HTTPException(status_code=409, detail=f"A {row['status']} piece cannot be edited")
            old_text = row["content_text"] or ""
            start, end = _resolve(old_text, body.start, body.end, body.text)
            warnings: list[dict] = []
            wi = _j(row["writer_input"])
            wi = wi if isinstance(wi, dict) else {}
            # The admin's text (or the model's) gets the same tenant-facing clean-up the
            # pipeline runs before persisting: no citation tag / source label, no markdown on
            # plain-text channels. Checked and saved in that form.
            replacement = sentence_repair.sanitize_replacement(replacement, row["channel"])
            if body.mode != "cut" and not replacement:
                raise HTTPException(status_code=422, detail="The replacement is empty once cleaned — use mode 'cut'")
            if replacement:
                # No stored source (piece written before AA-769): a number already in the rest of
                # the piece is not new, so the piece minus this sentence stands in for the source.
                grounding = (grounding_from_writer_input(wi) if wi
                             else old_text[:start] + " " + old_text[end:])
                checks = sentence_repair.check_replacement(replacement, grounding, wi.get("brand_pack"),
                                                           writer_input=wi, text=old_text, span=(start, end))
                failed = [c for c in checks if c["blocking"]]
                warnings = [c for c in checks if not c["blocking"]]
                if failed:
                    detail = "; ".join(f"{f['gate']}: {' | '.join(f['violations'])}" for f in failed)
                    raise HTTPException(status_code=422, detail=f"The new sentence fails a check — {detail}")
            new_text = sentence_repair.apply_edit(old_text, start, end, replacement)
            # S234 round 4 review — the pipeline's final integrity pass does not run on a hand edit:
            # what the edit newly breaks (a second or reworded CTA, a lower-case fragment left by a
            # cut, a repeat) is returned as an F13_repeat warning (saved anyway — the admin decides)
            broken = text_integrity.edit_findings(old_text, new_text, wi.get("cta"),
                                                  (wi.get("brand_pack") or {}).get("brand_name"))
            if broken:
                warnings.append({"gate": "F13_repeat", "violations": broken, "blocking": False})
            min_chars, _ = _length_bounds_for_channel(row["channel"])
            if not new_text.strip():
                raise HTTPException(status_code=422, detail="This would leave the piece empty")
            if len(new_text) < min_chars <= len(old_text):
                raise HTTPException(status_code=422, detail=f"This would take the piece under the {min_chars}-char "
                                                            f"minimum for {row['channel']}")
            await conn.execute(
                "UPDATE acp_shared.content_piece SET content_text = $2 WHERE piece_id = $1::uuid",
                piece_id, new_text)
            await write_audit_log(
                conn, tenant_id=str(row["tenant_id"]), actor=f"admin:{x_admin_user_id or 'unknown'}",
                action=_ACTION_BY_MODE[body.mode], resource_type="content_piece", resource_id=piece_id,
                details={"start": start, "end": end, "before": old_text[start:end], "after": replacement,
                         "mode": body.mode, "gate": body.gate, "problem": body.problem,
                         "suggestion": body.suggestion, "suggestion_model": body.suggestion_model,
                         "suggestion_cost_usd": body.suggestion_cost_usd,
                         "length_before": len(old_text), "length_after": len(new_text),
                         "published": bool(row["published"]), "warnings": warnings},
            )
        piece = await _load_piece_detail(conn, piece_id)
    # S234 review: F12_scope / F13 hygiene hits on the admin's text are warnings, not refusals.
    return {**piece, "warnings": warnings}


# ── S234: regenerate the 3 T8 angles of one request (admin only) ─────────────────────────────

_REGENERABLE_STATUSES = ("pending_choice", "approved", "reusable")


@router.post("/requests/{request_id}/regenerate-angles", status_code=202)
async def regenerate_angles(request_id: str, request: Request, x_admin_secret: str = Header(None),
                            x_admin_user_id: Optional[str] = Header(None),
                            x_admin_role: Optional[str] = Header(None)):
    """New angles for a request whose stored ones are bad (S234: both route-blog requests anchor
    on the ruins), with the same goal — about $0.02 (one Sonnet angle call plus up to 4 Jev answer
    checks), no new stage.

    The old request is never touched (S234 review): its option rows are what its pieces'
    `angle_gate_option_id` joins to (tenant wizard resume, My Content, the admin list/drawer), so
    overwriting them would relabel every old piece with the new angle and make the wizard resume
    onto an old piece. Instead, in one transaction: lock the old request, refuse while a
    regenerate for it is still generating, insert a NEW request cloned from it (same tenant, atom,
    trip, channel, CTA, subject, route_segment_ids and goal; status 'pending_goal' with the goal
    set — the tenant flow's own "generating" state), enqueue `t8_angles` for the new request, and
    audit both ids. The job lands the new request on 'pending_choice' with its own 3 option rows;
    if it fails, its on_terminal hook clears the goal on the new request only."""
    verify_admin_secret(x_admin_secret)
    _require_admin_role(x_admin_role)
    from uuid import uuid4

    from services.acp_angle_gate.service import T8_ANGLES_KIND
    from shared.jobs.registry import enqueue
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            req = await conn.fetchrow(
                """
                SELECT request_id, tenant_id, goal, status FROM acp_shared.angle_gate_request
                WHERE request_id = $1::uuid FOR UPDATE
                """, request_id)
            if req is None:
                raise HTTPException(status_code=404, detail="Request not found")
            if not req["goal"] or req["status"] not in _REGENERABLE_STATUSES:
                raise HTTPException(status_code=409, detail=f"A {req['status']} request without generated "
                                                            "angles cannot be regenerated")
            busy = await conn.fetchval(
                """
                SELECT count(*) FROM shared.job
                WHERE kind = $2 AND payload->>'regenerated_from' = $1 AND status IN ('queued', 'running')
                """, request_id, T8_ANGLES_KIND)
            if busy:
                raise HTTPException(status_code=409, detail="New angles are already being generated for this "
                                                            "request — try again when they finish")
            previous = await conn.fetch(
                """
                SELECT idx, name, best_final_style, recommended, chosen FROM acp_shared.angle_gate_option
                WHERE request_id = $1::uuid ORDER BY idx
                """, request_id)
            new_id = str(uuid4())
            await conn.execute(
                """
                INSERT INTO acp_shared.angle_gate_request
                    (request_id, tenant_id, atom_id, trip_id, channel, goal, status, cta, subject_id,
                     route_segment_ids)
                SELECT $2::uuid, tenant_id, atom_id, trip_id, channel, goal, 'pending_goal', cta, subject_id,
                       route_segment_ids
                FROM acp_shared.angle_gate_request WHERE request_id = $1::uuid
                """, request_id, new_id)
            actor = f"admin:{x_admin_user_id or 'unknown'}"
            job_id, _ = await enqueue(
                conn, T8_ANGLES_KIND,
                {"request_id": new_id, "tenant_id": str(req["tenant_id"]), "goal": req["goal"],
                 "regenerated_from": request_id},
                created_by=actor,
            )
            await write_audit_log(
                conn, tenant_id=str(req["tenant_id"]), actor=actor, action=AdminAuditAction.ANGLES_REGENERATED,
                resource_type="angle_gate_request", resource_id=request_id,
                details={"new_request_id": new_id, "job_id": job_id, "goal": req["goal"],
                         "previous_status": req["status"], "previous_angles": [_row(r) for r in previous]},
            )
    return {"request_id": new_id, "regenerated_from": request_id, "job_id": job_id, "status": "pending_goal",
            "goal": req["goal"]}


@router.get("/stats")
async def stats(request: Request, days: int = Query(30, ge=1, le=365), tenant_id: Optional[str] = None,
                x_admin_secret: str = Header(None)):
    """Per-gate runs / fails / blocking fails on final pieces, and per-tenant outcomes."""
    verify_admin_secret(x_admin_secret)
    params: list = [days]
    tenant_clause = ""
    if tenant_id:
        params.append(tenant_id)
        tenant_clause = "AND cp.tenant_id = $2::uuid"
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        gates = await conn.fetch(
            f"""
            SELECT g->>'gate' AS gate, COALESCE(cp.channel, 'unknown') AS channel,
                   count(*) AS runs,
                   count(*) FILTER (WHERE (g->>'passed')::boolean = false) AS failed,
                   count(*) FILTER (WHERE (g->>'passed')::boolean = false
                                      AND COALESCE((g->>'blocking')::boolean, true)) AS blocking_failed
            FROM acp_shared.content_piece cp, jsonb_array_elements(cp.gate_ledger) g
            WHERE cp.created_at > now() - make_interval(days => $1) AND cp.status <> 'processing' {tenant_clause}
            GROUP BY 1, 2 ORDER BY failed DESC, gate
            """, *params)
        tenants = await conn.fetch(
            f"""
            SELECT t.name AS tenant_name, cp.tenant_id,
                   count(*) AS pieces,
                   count(*) FILTER (WHERE cp.status = 'approved') AS approved,
                   count(*) FILTER (WHERE cp.status = 'held') AS held,
                   count(*) FILTER (WHERE cp.status = 'failed') AS failed,
                   COALESCE(sum((SELECT sum(l.cost_usd) FROM shared.llm_call_log l WHERE l.job_id = cp.job_id)), 0)
                       AS cost_usd
            FROM acp_shared.content_piece cp JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
            WHERE cp.created_at > now() - make_interval(days => $1) {tenant_clause}
            GROUP BY 1, 2 ORDER BY pieces DESC
            """, *params)
    return {"days": days, "gates": [_row(r) for r in gates], "tenants": [_row(r) for r in tenants]}


# ── T2 tenant tour versions ────────────────────────────────────────────────────────────────────

@router.get("/tour-versions")
async def list_tour_versions(
    request: Request,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    tenant_id: Optional[str] = None, status: Optional[str] = None, q: Optional[str] = None,
    sort: str = "created_at", sort_dir: str = "desc",
    x_admin_secret: str = Header(None),
):
    """Every tenant's T2 tour versions with the scores and QA state the tenant no longer sees."""
    verify_admin_secret(x_admin_secret)
    where, params = ["true"], []
    if tenant_id:
        params.append(tenant_id)
        where.append(f"ttv.tenant_id = ${len(params)}::uuid")
    if status:
        params.append(status)
        where.append(f"ttv.status = ${len(params)}")
    if q:
        params.append(f"%{q}%")
        where.append(f"pt.aa_name ILIKE ${len(params)}")
    w = " AND ".join(where)
    order = _VERSION_SORTS.get(sort, "ttv.created_at")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    base = """
        FROM gold_aa_internal.tenant_tour_versions ttv
        JOIN shared.tenants t ON t.tenant_id = ttv.tenant_id
        JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
    """
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT count(*) {base} WHERE {w}", *params)
        rows = await conn.fetch(
            f"""
            SELECT ttv.id, ttv.tenant_id, t.name AS tenant_name, pt.aa_name AS tour_name, ttv.version_number,
                   ttv.status, ttv.quality_score, ttv.qa_status, ttv.qa_auto_passed, ttv.qa_repair_count,
                   ttv.edit_source, ttv.created_at, ttv.job_id,
                   COALESCE((SELECT sum(l.cost_usd) FROM shared.llm_call_log l WHERE l.job_id = ttv.job_id), 0)
                       AS cost_usd,
                   (SELECT count(*) FROM silver_aa_internal.review_queue rq
                    WHERE rq.tenant_tour_version_id = ttv.id) AS escalations
            {base}
            WHERE {w}
            ORDER BY {order} {direction} NULLS LAST, ttv.id
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """, *params, limit, offset)
    return {"data": [_row(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/tour-versions/{version_id}")
async def get_tour_version(version_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT ttv.id, t.name AS tenant_name, pt.aa_name AS tour_name, ttv.version_number, ttv.status,
                   ttv.quality_score, ttv.qa_status, ttv.qa_auto_passed, ttv.qa_repair_count, ttv.qa_checked_at,
                   ttv.rewritten_content, ttv.edit_source, ttv.created_at, ttv.job_id
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN shared.tenants t ON t.tenant_id = ttv.tenant_id
            JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
            WHERE ttv.id = $1::uuid
            """, version_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Version not found")
        reviews = await conn.fetch(
            """
            SELECT failure_summary, escalate_detail, review_status, created_at
            FROM silver_aa_internal.review_queue WHERE tenant_tour_version_id = $1::uuid ORDER BY created_at
            """, version_id)
        cost_rows = await conn.fetch(
            """
            SELECT stage, role, count(*) AS calls, sum(cost_usd) AS cost_usd
            FROM shared.llm_call_log WHERE job_id = $1 GROUP BY stage, role ORDER BY sum(cost_usd) DESC NULLS LAST
            """, row["job_id"]) if row["job_id"] else []
    out = _row(row)
    out["rewritten_content"] = _j(out.get("rewritten_content"))
    out["reviews"] = [_row(r) for r in reviews]
    out["cost_by_stage"] = [_row(r) for r in cost_rows]
    return out


# ── Place cards (AA-772) ───────────────────────────────────────────────────────────────────────

@router.get("/place-cards")
async def list_place_cards(
    request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    status: Optional[str] = None, q: Optional[str] = None, x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    where, params = ["true"], []
    if status:
        params.append(status)
        where.append(f"pc.status = ${len(params)}")
    if q:
        params.append(f"%{q}%")
        where.append(f"(pc.display_name ILIKE ${len(params)} OR pc.locality ILIKE ${len(params)} "
                     f"OR pc.country ILIKE ${len(params)})")
    w = " AND ".join(where)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT count(*) FROM acp_shared.place_card pc WHERE {w}", *params)
        rows = await conn.fetch(
            f"""
            SELECT pc.card_id, pc.display_name, pc.locality, pc.region, pc.country, pc.naming_note, pc.facts,
                   pc.status, pc.cost_usd, pc.verified_by, pc.verified_at, pc.created_at,
                   (SELECT count(*) FROM acp_shared.atom_place_card a WHERE a.card_id = pc.card_id) AS atoms
            FROM acp_shared.place_card pc
            WHERE {w}
            ORDER BY pc.created_at DESC, pc.card_id
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """, *params, limit, offset)
    return {"data": [_row(r) for r in rows], "total": total, "limit": limit, "offset": offset}


class PlaceCardVerdict(BaseModel):
    status: str  # verified | rejected | auto_checked


@router.get("/place-cards/duplicates")
async def list_place_card_duplicates(request: Request, x_admin_secret: str = Header(None)):
    """S234 — candidate duplicate clusters (same country + same core name, whole-word name
    containment, or an alias), for the admin to merge by hand. Never merged automatically:
    'Punakha' and 'Punakha Dzong' are two places."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        cards = await conn.fetch(
            """
            SELECT pc.card_id, pc.display_name, pc.locality, pc.region, pc.country, pc.status, pc.facts,
                   pc.cost_usd, pc.created_at,
                   (SELECT count(*) FROM acp_shared.atom_place_card a WHERE a.card_id = pc.card_id) AS atoms
            FROM acp_shared.place_card pc
            ORDER BY pc.country, pc.display_name, pc.created_at
            """)
        aliases = await conn.fetch("SELECT alias_norm, country_norm, card_id FROM acp_shared.place_card_alias")
    rows = [_row(c) for c in cards]
    for r in rows:
        r["facts"] = len(r.get("facts") or [])
    clusters = place_cards.duplicate_clusters(rows, [dict(a) for a in aliases])
    return {"data": clusters, "total": len(clusters)}


@router.patch("/place-cards/{card_id}")
async def set_place_card_status(card_id: str, body: PlaceCardVerdict, request: Request,
                                x_admin_secret: str = Header(None),
                                x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    if body.status not in ("verified", "rejected", "auto_checked"):
        raise HTTPException(status_code=422, detail="status must be verified, rejected or auto_checked")
    pool = request.app.state.pool
    actor = x_admin_user_id or "admin"
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            UPDATE acp_shared.place_card
            SET status = $2, verified_by = $3, verified_at = CASE WHEN $2 = 'auto_checked' THEN NULL ELSE now() END,
                updated_at = now()
            WHERE card_id = $1::uuid
            RETURNING card_id, status, verified_by, verified_at, display_name, country
            """, card_id, body.status, actor)
        if row is not None:
            await write_audit_log(
                conn, tenant_id=None, actor=f"admin:{actor}", action=PlatformAuditAction.PLACE_CARD_STATUS,
                resource_type="place_card", resource_id=card_id,
                details={"status": body.status, "display_name": row["display_name"], "country": row["country"]})
    if row is None:
        raise HTTPException(status_code=404, detail="Place card not found")
    return _row(row)


class PlaceCardMerge(BaseModel):
    from_ids: list[str]


@router.post("/place-cards/{keep_id}/merge")
async def merge_place_cards(keep_id: str, body: PlaceCardMerge, request: Request,
                            x_admin_secret: str = Header(None),
                            x_admin_user_id: Optional[str] = Header(None)):
    """S234 — merge duplicate cards into `keep_id` in one transaction: atoms re-linked, names kept
    as aliases (the next atom naming them links with no LLM call), facts/cost added, merged cards
    deleted, one audit_log row."""
    verify_admin_secret(x_admin_secret)
    try:
        ids = [str(UUID(i)) for i in [keep_id, *body.from_ids]]
    except ValueError:
        raise HTTPException(status_code=422, detail="card ids must be UUIDs")
    actor = x_admin_user_id or "admin"
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        try:
            out = await place_cards.merge_cards(conn, ids[0], ids[1:])
        except place_cards.MergeError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        await write_audit_log(
            conn, tenant_id=None, actor=f"admin:{actor}", action=PlatformAuditAction.PLACE_CARD_MERGE,
            resource_type="place_card", resource_id=ids[0], details=out)
    return out


class PlaceCardBuild(BaseModel):
    tour_id: str
    force: bool = False  # ignore stored misses (place_card_miss) for this tour


@router.post("/place-cards/build")
async def build_place_cards(body: PlaceCardBuild, request: Request, x_admin_secret: str = Header(None),
                            x_admin_user_id: Optional[str] = Header(None)):
    """Enqueue `place_cards_build` for one tour (missing cards only; ≤40 paid drafts, one per
    distinct place). `force` retries places whose last draft missed."""
    verify_admin_secret(x_admin_secret)
    from shared.jobs import queue as job_queue
    payload = {"tour_id": body.tour_id, **({"force": True} if body.force else {})}
    job_id, created = await job_queue.enqueue(
        request.app.state.pool, "place_cards_build", payload,
        max_attempts=2,
        created_by=x_admin_user_id or "admin",
    )
    return {"job_id": job_id, "created": created}
