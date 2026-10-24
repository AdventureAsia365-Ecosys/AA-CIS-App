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
  GET/POST /pieces/{id}/label         — AA-791: a human label per piece and labeller
                                         (acp_shared.content_piece_label, migration 231; 503 until
                                         it is applied). Admin data only, never shown to a tenant.
All of them require the `admin` staff role (x-admin-role, forwarded by the BFF from the verified JWT).
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

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from api.core.staff_authz import staff_policy
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret
from services.acp_content_writing import hygiene, sentence_repair, text_integrity
from services.acp_content_writing.generate import SentenceRepairError, repair_sentences_detailed
from services.acp_content_writing.quality_gates import _length_bounds_for_channel
from services.acp_content_writing.service import grounding_from_writer_input
from services.acp_shared import place_cards, route_guard, seed_audit
from services.acp_shared.atom_seed import WRITER_FACT_KINDS
from services.acp_shared.audit_log import AdminAuditAction, PlatformAuditAction, write_audit_log
from services.acp_shared.brand_pack import fetch_brand_pack_conn
from services.acp_shared.grounding import find_novel_numeric_claims
from services.quality import registry as qr

# AA-882 (GĐ3): staff_policy narrows every route listed in api/core/staff_authz.py POLICY by role.
router = APIRouter(prefix="/admin/content-quality", tags=["admin-content-quality"],
                   dependencies=[Depends(staff_policy)])

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


# ── AA-791: human labels (acp_shared.content_piece_label, migration 231) ──────────────────────

_LABEL_TABLE = "acp_shared.content_piece_label"
LABELS = ("as_is", "light", "heavy", "reject")
_LABEL_FILTERS = LABELS + ("labelled", "unlabelled")
DEFECT_CLASSES = tuple(qr.chips()["defect_classes"])  # AA-783 criteria pack columns (fact, voice, format, ai_phrasing)
DEFECT_LEVELS = ("none", "light", "heavy")
LABELS_UNAVAILABLE = ("Labels are not available yet: migration 231 (acp_shared.content_piece_label) has not "
                      "been applied on this database.")
_CONTENT_SHA_SQL = "encode(sha256(convert_to(COALESCE(cp.content_text, ''), 'UTF8')), 'hex')"
_LATEST_LABEL_SQL = (f"(SELECT l.label FROM {_LABEL_TABLE} l WHERE l.piece_id = cp.piece_id "
                     f"ORDER BY l.labelled_at DESC LIMIT 1)")
_LABEL_COLS_SQL = (f",\n                   {_LATEST_LABEL_SQL} AS label,"
                   f"\n                   (SELECT count(*) FROM {_LABEL_TABLE} l WHERE l.piece_id = cp.piece_id)"
                   f" AS label_count")


async def _labels_enabled(conn) -> bool:
    """Migration 231 applied? Checked per request (one catalog lookup), so the label feature turns on
    the moment DB Migrate applies it — no redeploy (D8: code merges may precede the apply)."""
    return bool(await conn.fetchval(f"SELECT to_regclass('{_LABEL_TABLE}') IS NOT NULL"))


def _label_filter(where: str, params: list, label: str) -> tuple[str, list]:
    if label == "labelled":
        return f"{where} AND EXISTS (SELECT 1 FROM {_LABEL_TABLE} l WHERE l.piece_id = cp.piece_id)", params
    if label == "unlabelled":
        return f"{where} AND NOT EXISTS (SELECT 1 FROM {_LABEL_TABLE} l WHERE l.piece_id = cp.piece_id)", params
    params = [*params, label]
    return f"{where} AND {_LATEST_LABEL_SQL} = ${len(params)}", params


@router.get("/pieces")
async def list_pieces(
    request: Request,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    tenant_id: Optional[str] = None, channel: Optional[str] = None, status: Optional[str] = None,
    gate: Optional[str] = None, q: Optional[str] = None, label: Optional[str] = None,
    sort: str = "created_at", sort_dir: str = "desc",
    x_admin_secret: str = Header(None),
):
    """Every tenant's T9 pieces: final status, the blocking gates that failed (from gate_ledger),
    the warn flags, attempts and the LLM cost of the write job. AA-791: the latest human label and
    the label count, filterable by `label` — only once migration 231 is applied (`labels_enabled`);
    before that the label filter is ignored and the rows carry no label."""
    verify_admin_secret(x_admin_secret)
    if label and label not in _LABEL_FILTERS:
        raise HTTPException(status_code=422, detail=f"label must be one of {', '.join(_LABEL_FILTERS)}")
    where, params = _piece_filters(tenant_id, channel, status, gate, q)
    order = _PIECE_SORTS.get(sort, "cp.created_at")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        labels_enabled = await _labels_enabled(conn)
        label_cols = _LABEL_COLS_SQL if labels_enabled else ""
        if labels_enabled and label:
            where, params = _label_filter(where, params, label)
        total = await conn.fetchval(f"SELECT count(*) {_PIECE_FROM} WHERE {where}", *params)
        rows = await conn.fetch(
            f"""
            SELECT cp.piece_id, cp.tenant_id, t.name AS tenant_name,
                   COALESCE(cp.channel, agr.channel) AS channel, cp.status, cp.held_reason,
                   cp.attempt_number, cp.created_at, cp.job_id,
                   COALESCE(cp.seo_title, ago.name) AS title, ago.name AS angle_name, agr.goal,
                   length(cp.content_text) AS content_chars,
                   (cp.writer_input IS NOT NULL) AS has_writer_input,
                   {_FAILED_GATES_SQL}, {_COST_SQL}{label_cols}
            {_PIECE_FROM}
            WHERE {where}
            ORDER BY {order} {direction} NULLS LAST, cp.piece_id
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """, *params, limit, offset)
        # AA-863 — the seed atom's place / country and the Segment label next to each piece, with a
        # mismatch flag (seed_audit.seed_check); set-based reads for the whole page, never per row.
        seeds = await seed_audit.load_piece_seeds(conn, [r["piece_id"] for r in rows]) if rows else {}
    data = [_row(r) for r in rows]
    for d in data:
        d["seed"] = seeds.get(str(d.get("piece_id")))
    return {"data": data, "total": total, "limit": limit, "offset": offset,
            "labels_enabled": labels_enabled}


# Highlights: where in the final text each flagged sentence / phrase sits.
_F9_PHRASE_RE = re.compile(r'exact flagged phrase\(s\): (.+)$')
_QUOTED_RE = re.compile(r'"([^"]{3,})"')
_BRAND_RE = re.compile(r"^(.+?): '(.+)'\s*$", re.DOTALL)
_BANNED_PHRASE_RE = re.compile(r"^banned pattern /.*/ -> '(.+)'$", re.DOTALL)


def _findings(g: dict) -> list[tuple[str, str, bool, str]]:
    """(parse shape, violation, blocking, severity) per violation of a ledger / flag entry. AA-924 — a merged entry
    (F2_lexicon, F15_voice) gives each finding with its own severity, parsed in the shape of the source gate that
    found it; an older entry gives every violation with the gate's own `blocking`."""
    if isinstance(g.get("findings"), list):
        return [(f.get("source"), f.get("text"), f.get("severity") == "block", f.get("severity") or "warn")
                for f in g["findings"] if isinstance(f, dict) and isinstance(f.get("text"), str)]
    blocking = g.get("blocking", True)
    return [(g.get("gate"), v, blocking, "block" if blocking else "warn")
            for v in g.get("violations") or [] if isinstance(v, str)]


def highlights(content_text: str, gate_ledger: list, flags: list) -> list[dict]:
    """[{start, end, gate, blocking, severity, problem}] for every flagged sentence or phrase found in the
    final text. Parses the same violation shapes sentence_repair.py reads; a quote that cannot
    be found (rewritten later, or too short) is left out. AA-924 — `blocking` / `severity` are the finding's own
    (a warn stock phrase inside a blocking F2_lexicon entry reads warn); `gate` is the stored entry, `source` the
    gate that found it when they differ."""
    out: list[dict] = []
    text = content_text or ""
    for g in list(gate_ledger or []) + list(flags or []):
        if not isinstance(g, dict) or g.get("passed", False):
            continue
        gate = g.get("gate")
        for shape, v, blocking, severity in _findings(g):
            mark = {"gate": gate, "blocking": blocking, "severity": severity,
                    **({"source": shape} if shape != gate else {})}
            spans: list[tuple[str, str]] = []
            if shape == qr.F1_GROUNDING:
                m = sentence_repair._F1_QUOTE_RE.search(v)
                if m:
                    spans.append((m.group(1), v.split(" not present", 1)[0]))
            elif shape == qr.F2_BANNED_PATTERNS:
                # AA-924 — "banned pattern /…/ -> '<phrase>'" is highlighted as a phrase; the em-dash one quotes
                # its sentence
                m = _BANNED_PHRASE_RE.search(v)
                i = text.lower().find(m.group(1).lower()) if m else -1
                if i >= 0:
                    out.append({"start": i, "end": i + len(m.group(1)), **mark, "problem": "banned phrase"})
                elif not m and (m := _BRAND_RE.search(v)):
                    spans.append((m.group(2), m.group(1)))
            elif shape in (qr.F2_BRAND_FORBIDDEN, qr.F11_BRAND_NAME):
                m = _BRAND_RE.search(v)
                if m:
                    spans.append((m.group(2), m.group(1)))
            elif shape in (qr.F12_SCOPE, qr.F13_HYGIENE, qr.F13_LEAK, qr.F13_REPEAT, qr.F14_SOCIAL_SHAPE,
                           qr.F14_FIRST_LINE, qr.F15_VOICE, qr.F15_VOICE_WARN, qr.F17_TRIP_FACT, qr.F18_RARITY):
                # S234 — "<problem>: '<sentence>'"; a title/meta scope hit is not in the text. F14's
                # word-count line quotes nothing and is left out; its first line / hashtags are quoted.
                # F15_voice quotes its sentence the same way (plan C4).
                m = _BRAND_RE.search(v)
                if m and not v.startswith(("title ", "meta ")):
                    quote = m.group(2)
                    if sentence_repair.locate_sentence(text, quote) is None and quote in text:
                        i = text.find(quote)  # a short quote (a hashtag) is highlighted as a phrase
                        out.append({"start": i, "end": i + len(quote), **mark, "problem": m.group(1)[:300]})
                    else:
                        spans.append((quote, m.group(1)))
            elif shape == qr.F9_CTA_FACT and "unsupported claim(s):" in v:
                for sent, claim, why in sentence_repair._F9_CLAIM_RE.findall(v.split("unsupported claim(s):", 1)[1]):
                    spans.append((sent, f"{claim.strip()} — {why.strip()}"))
            elif shape == qr.F9_BRAND_STYLE:
                m = _F9_PHRASE_RE.search(v)
                for phrase in _QUOTED_RE.findall(m.group(1)) if m else []:
                    i = text.find(phrase)
                    if i >= 0:
                        out.append({"start": i, "end": i + len(phrase), **mark,
                                    "problem": v.split(" — ", 1)[0][:200]})
            for quote, problem in spans:
                span = sentence_repair.locate_sentence(text, quote)
                if span:
                    out.append({"start": span[0], "end": span[1], **mark, "problem": problem[:300]})
    # A non-blocking gate sits in both gate_ledger and flags: keep one highlight per span + finding. AA-924 — a
    # merged entry (F2_lexicon, F15_voice) holds several sources' findings, so two problems on one sentence are two
    # highlights: the key is the span, the entry, the source and the problem, not the span and entry alone.
    seen, unique = set(), []
    for h in sorted(out, key=lambda h: (h["start"], h["end"])):
        key = (h["start"], h["end"], h["gate"], h.get("source") or h["gate"], h["problem"])
        if key not in seen:
            seen.add(key)
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
        it["problems"].append({"gate": h["gate"], "problem": h["problem"], "blocking": h["blocking"],
                               "severity": h.get("severity"), **({"source": h["source"]} if h.get("source") else {})})
        it["blocking"] = it["blocking"] or bool(h["blocking"])
    return [items[k] for k in sorted(items)]


# AA-802 — when the tenant last took a copy of the piece away: published to a channel
# (publish_log; WordPress/Facebook are never re-synced) or exported (the AA-613 audit row).
_DELIVERED_SQL = """
    (SELECT max(COALESCE(pl.published_at, pl.created_at)) FROM acp_shared.publish_log pl
     WHERE pl.piece_id = cp.piece_id AND pl.status = 'published') AS published_at,
    (SELECT max(al.created_at) FROM acp_shared.audit_log al
     WHERE al.resource_type = 'content_piece' AND al.resource_id = cp.piece_id::text
       AND al.action = 'content_piece.exported') AS exported_at
"""

_PIECE_DETAIL_SQL = f"""
    SELECT cp.piece_id, cp.tenant_id, t.name AS tenant_name, COALESCE(cp.channel, agr.channel) AS channel,
           cp.status, cp.held_reason, cp.attempt_number, cp.created_at, cp.job_id,
           cp.content_text, cp.seo_title, cp.meta_description, cp.slug, cp.gate_ledger, cp.flags,
           cp.repair_log, cp.discarded_attempts, cp.writer_input, cp.route_hub_name,
           agr.goal, agr.atom_id, agr.request_id,
           ago.name AS angle_name, ago.why_it_works AS angle_why, ago.best_final_style AS angle_style,
           EXISTS (SELECT 1 FROM acp_shared.publish_log pl
                   WHERE pl.piece_id = cp.piece_id AND pl.status = 'published') AS published,
           {_DELIVERED_SQL},
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
    WHERE tenant_id = $1 AND resource_type = $4 AND resource_id = $2 AND action LIKE $3
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
                             AdminAuditAction.ADMIN_SENTENCE_PREFIX + "%", "content_piece")
    piece = _row(row)
    piece["highlights"] = highlights(piece["content_text"], piece["gate_ledger"], piece["flags"])
    piece["flagged_sentences"] = flagged_sentences(piece["content_text"] or "", piece["highlights"])
    piece["cost_by_stage"] = [_row(r) for r in cost_rows]
    piece["other_pieces_for_request"] = [_row(r) for r in siblings]
    piece["admin_edits"] = [{**_row(r), "details": _j(r["details"]) or {}} for r in edits]
    piece["exported"] = row["exported_at"] is not None
    piece["tenant_copy"] = tenant_copy_state(row["published_at"], row["exported_at"],
                                             [r["created_at"] for r in edits])
    piece["seed"] = (await seed_audit.load_piece_seeds(conn, [row["piece_id"]])).get(str(row["piece_id"]))  # AA-863
    return piece


def tenant_copy_state(published_at, exported_at, edit_times) -> dict:
    """AA-802 — does the tenant hold a copy older than the text we now have? `delivered_at` is the
    latest publish or export; `older_copy` is true when an admin edit came after it (the published
    post / the downloaded file still has the old sentence — nothing is re-synced). One function for
    the T9 piece and the T2 tour version."""
    marks = [t for t in (published_at, exported_at) if t is not None]
    delivered_at = max(marks) if marks else None
    later = [t for t in edit_times if t is not None and delivered_at is not None and t > delivered_at]
    return {"published_at": published_at.isoformat() if published_at else None,
            "exported_at": exported_at.isoformat() if exported_at else None,
            "delivered_at": delivered_at.isoformat() if delivered_at else None,
            "older_copy": bool(later), "edits_after_delivery": len(later)}


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
    piece detail. A published piece is edited here only — WordPress is not re-synced; the detail's
    `tenant_copy.older_copy` (AA-802) then tells the admin the tenant holds the older text."""
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
                               WHERE pl.piece_id = cp.piece_id AND pl.status = 'published') AS published,
                       EXISTS (SELECT 1 FROM acp_shared.audit_log al
                               WHERE al.resource_type = 'content_piece' AND al.resource_id = cp.piece_id::text
                                 AND al.action = 'content_piece.exported') AS exported
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
                warnings.append({"gate": qr.F13_REPEAT, "violations": broken, "blocking": False})
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
                         "published": bool(row["published"]), "exported": bool(row.get("exported")),
                         "warnings": warnings},
            )
        piece = await _load_piece_detail(conn, piece_id)
    # S234 review: F12_scope / F13 hygiene hits on the admin's text are warnings, not refusals.
    return {**piece, "warnings": warnings}


# ── S234: regenerate the 3 T8 angles of one request (admin only) ─────────────────────────────

_REGENERABLE_STATUSES = ("pending_choice", "approved", "reusable")


async def _regenerate_route_segment_ids(conn, request_id: str) -> Optional[list[str]]:
    """AA-914 — the Route Segments the regenerated request carries: the Subject's Route when it is
    still the current version (`superseded_at IS NULL`), else the old request's snapshot — either
    way only the Segments the trip has a live atom in (route_guard), so a stale foreign id is never
    copied forward. None for a Segment / atom request (no Route). 409 when no Segment is left."""
    src = await conn.fetchrow(
        """
        SELECT r.tenant_id, r.trip_id, r.route_segment_ids, s.route_id
        FROM acp_shared.angle_gate_request r
        LEFT JOIN acp_shared.subject s ON s.subject_id = r.subject_id
        WHERE r.request_id = $1::uuid
        """, request_id)
    if src is None or not route_guard.parse_segment_ids(src["route_segment_ids"]):
        return None
    route = await route_guard.current_route(conn, src["route_id"])
    kept = await route_guard.trip_route_segment_ids(
        conn, src["trip_id"], route["segment_ids"] if route else src["route_segment_ids"],
        tenant_id=src["tenant_id"], request_id=request_id, consumer="admin_regenerate",
    )
    if not kept:
        raise HTTPException(status_code=409, detail="None of this request's Route Segments is still on its trip "
                                                    "— pick the Route again from the Slate")
    return kept


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
            route_segment_ids = await _regenerate_route_segment_ids(conn, request_id)
            new_id = str(uuid4())
            await conn.execute(
                """
                INSERT INTO acp_shared.angle_gate_request
                    (request_id, tenant_id, atom_id, trip_id, channel, goal, status, cta, subject_id,
                     route_segment_ids)
                SELECT $2::uuid, tenant_id, atom_id, trip_id, channel, goal, 'pending_goal', cta, subject_id,
                       $3::jsonb
                FROM acp_shared.angle_gate_request WHERE request_id = $1::uuid
                """, request_id, new_id,
                json.dumps(route_segment_ids) if route_segment_ids is not None else None)
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


# ── AA-791: label one piece (admin only) ──────────────────────────────────────────────────────

class PieceLabelBody(BaseModel):
    label: str
    defects: dict[str, str] = Field(default_factory=dict)
    minutes_to_fix: Optional[int] = Field(None, ge=0, le=600)
    reason: Optional[str] = Field(None, max_length=4000)


def _require_label_admin(x_admin_role: Optional[str]) -> None:
    if x_admin_role != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can see or set piece labels")


def _label_row(r) -> dict:
    out = _row(r)
    out["defects"] = _j(out.get("defects")) or {}
    return out


_PIECE_LABELS_SQL = f"""
    SELECT l.labelled_by, l.label, l.defects, l.minutes_to_fix, l.reason, l.labelled_at,
           (l.content_sha256 IS DISTINCT FROM {_CONTENT_SHA_SQL}) AS text_changed_since_label
    FROM {_LABEL_TABLE} l JOIN acp_shared.content_piece cp ON cp.piece_id = l.piece_id
    WHERE l.piece_id = $1::uuid
    ORDER BY l.labelled_at DESC
"""


@router.get("/pieces/{piece_id}/label")
async def get_piece_label(piece_id: str, request: Request, x_admin_secret: str = Header(None),
                          x_admin_user_id: Optional[str] = Header(None), x_admin_role: Optional[str] = Header(None)):
    """Every human label on this piece (newest first) and the caller's own (`mine`). 503 until
    migration 231 is applied."""
    verify_admin_secret(x_admin_secret)
    _require_label_admin(x_admin_role)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        if not await _labels_enabled(conn):
            raise HTTPException(status_code=503, detail=LABELS_UNAVAILABLE)
        if not await conn.fetchval("SELECT 1 FROM acp_shared.content_piece WHERE piece_id = $1::uuid", piece_id):
            raise HTTPException(status_code=404, detail="Piece not found")
        rows = await conn.fetch(_PIECE_LABELS_SQL, piece_id)
    labels = [_label_row(r) for r in rows]
    me = f"admin:{x_admin_user_id}" if x_admin_user_id else None
    return {"piece_id": piece_id, "labels": labels,
            "mine": next((x for x in labels if me and x["labelled_by"] == me), None),
            "choices": {"label": list(LABELS), "defect_classes": list(DEFECT_CLASSES),
                        "defect_levels": list(DEFECT_LEVELS)}}


@router.post("/pieces/{piece_id}/label")
async def set_piece_label(piece_id: str, body: PieceLabelBody, request: Request, x_admin_secret: str = Header(None),
                          x_admin_user_id: Optional[str] = Header(None), x_admin_role: Optional[str] = Header(None)):
    """Save the caller's label on one approved/held piece (AA-783 criteria: as_is / light / heavy /
    reject, per-class defects none/light/heavy, minutes to fix, reason). One row per piece and
    labeller: a second save overwrites the caller's own row. The sha256 of the text being labelled is
    stored, so a later admin edit shows as `text_changed_since_label`. 503 until migration 231."""
    verify_admin_secret(x_admin_secret)
    _require_label_admin(x_admin_role)
    if not x_admin_user_id:
        raise HTTPException(status_code=400, detail="No admin identity on the request — sign in again")
    if body.label not in LABELS:
        raise HTTPException(status_code=422, detail=f"label must be one of {', '.join(LABELS)}")
    bad = {k: v for k, v in body.defects.items() if k not in DEFECT_CLASSES or v not in DEFECT_LEVELS}
    if bad:
        raise HTTPException(status_code=422, detail=f"defects take {', '.join(DEFECT_CLASSES)} with a value in "
                                                    f"{', '.join(DEFECT_LEVELS)} (got {bad})")
    reason = (body.reason or "").strip() or None
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        if not await _labels_enabled(conn):
            raise HTTPException(status_code=503, detail=LABELS_UNAVAILABLE)
        status = await conn.fetchval("SELECT status FROM acp_shared.content_piece WHERE piece_id = $1::uuid", piece_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Piece not found")
        if status not in _EDITABLE_STATUSES:
            raise HTTPException(status_code=409, detail=f"A {status} piece has no final text to label")
        await conn.execute(
            f"""
            INSERT INTO {_LABEL_TABLE} AS l (piece_id, labelled_by, label, defects, minutes_to_fix, reason,
                                             content_sha256, labelled_at)
            SELECT cp.piece_id, $2, $3, $4::jsonb, $5, $6, {_CONTENT_SHA_SQL}, now()
            FROM acp_shared.content_piece cp WHERE cp.piece_id = $1::uuid
            ON CONFLICT (piece_id, labelled_by) DO UPDATE SET
                label = EXCLUDED.label, defects = EXCLUDED.defects, minutes_to_fix = EXCLUDED.minutes_to_fix,
                reason = EXCLUDED.reason, content_sha256 = EXCLUDED.content_sha256, labelled_at = now()
            """, piece_id, f"admin:{x_admin_user_id}", body.label, json.dumps(body.defects), body.minutes_to_fix,
            reason)
        rows = await conn.fetch(_PIECE_LABELS_SQL, piece_id)
    labels = [_label_row(r) for r in rows]
    me = f"admin:{x_admin_user_id}"
    return {"piece_id": piece_id, "labels": labels, "mine": next((x for x in labels if x["labelled_by"] == me), None)}


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


# ── AA-802: silent correction of a T2 tour version (admin only) ────────────────────────────────
#
# AA-770: the tenant always receives the T2 rewrite; a low score (needs_review) or a T3 escalation
# is admin-only. An admin fixes a sentence of a version the tenant has not decided on yet, and the
# tenant's copy changes silently: the row is updated IN PLACE (same id, version_number, status,
# edit_source, edited_at), so the tenant sees no new version, no "edited" mark and no flag. The
# version history is the audit row (sentence before/after plus the whole section before/after).
# A new tenant_tour_versions row was rejected: it counts as a rewrite in the tenant's monthly quota
# (v_tenant_monthly_usage counts distinct ttv.id), shows up in the tenant's version list, and moves
# the version id the portal has open.

_T2_EDITABLE_STATUSES = ("ai_generated", "needs_review")  # finished AI text, no tenant decision yet
_T2_TEXT_FIELDS = ("subtitle", "summary", "highlights", "itineraries")  # T3's gated prose fields
_T2_SOURCE_COLUMNS = ("aa_name", "aa_subtitle", "aa_summary", "aa_description", "aa_highlights",
                      "aa_itineraries", "seo_title", "seo_meta", "country", "duration")

_VERSION_DETAIL_SQL = """
    SELECT ttv.id, ttv.tenant_id, ttv.published_tour_id, t.name AS tenant_name, pt.aa_name AS tour_name,
           ttv.version_number, ttv.status, ttv.quality_score, ttv.qa_status, ttv.qa_auto_passed,
           ttv.qa_repair_count, ttv.qa_checked_at, ttv.rewritten_content, ttv.edit_source, ttv.created_at,
           ttv.job_id,
           (SELECT max(v2.version_number) FROM gold_aa_internal.tenant_tour_versions v2
            WHERE v2.tenant_id = ttv.tenant_id AND v2.published_tour_id = ttv.published_tour_id
              AND v2.status <> 'failed')  -- a failed rewrite has no text: the older one stays the copy
               AS latest_version_number,
           (SELECT max(al.created_at) FROM acp_shared.audit_log al
            WHERE al.resource_type = 'tenant_tour_version' AND al.resource_id = ttv.id::text
              AND al.action = 'tenant_tour_version.exported') AS exported_at,
           pt.aa_name, pt.aa_subtitle, pt.aa_summary, pt.aa_description, pt.aa_highlights, pt.aa_itineraries,
           pt.seo_title, pt.seo_meta, rt.country, rt.duration
    FROM gold_aa_internal.tenant_tour_versions ttv
    JOIN shared.tenants t ON t.tenant_id = ttv.tenant_id
    JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    WHERE ttv.id = $1::uuid
"""

# The write targets the tenant's row by id AND tenant_id AND a still-editable status. An UPDATE
# that touches 0 rows (the tenant approved meanwhile, or a future FORCE RLS hides the row from the
# admin role, D2) is a 409, never a silent success.
_VERSION_UPDATE_SQL = """
    UPDATE gold_aa_internal.tenant_tour_versions SET rewritten_content = $3::jsonb
    WHERE id = $1::uuid AND tenant_id = $2::uuid AND status = ANY($4::text[])
"""


def t2_edit_block(status: Optional[str], version_number: Optional[int],
                  latest_version_number: Optional[int], edit_source: Optional[str] = None) -> Optional[str]:
    """Why an admin may NOT correct this version in place (None = editable). Only finished AI text
    the tenant has not approved / rejected, and only the tenant's latest version of the tour (an
    older one is no longer the copy the tenant works from). A tenant's own inline edit is a new
    `pending` version with edit_source 'tenant_edit' (v1_tours.update_version): their words, never
    corrected by an admin."""
    if edit_source == "tenant_edit":
        return "The tenant wrote this version themselves (inline edit), so an admin edit is not applied."
    if status not in _T2_EDITABLE_STATUSES:
        if status in ("approved", "rejected"):
            return f"The tenant {status} this version, so it is theirs now; an admin edit is not applied."
        return f"A {status} version has no finished text to correct."
    if latest_version_number and version_number is not None and version_number < latest_version_number:
        return (f"The tenant has a newer version (v{latest_version_number}) of this tour; "
                "correct that one instead.")
    return None


def _rewritten(row) -> dict:
    rc = _j(row["rewritten_content"])
    return rc if isinstance(rc, dict) else {}


def t2_sections(rc: dict) -> list[dict]:
    """The prose a tenant reads in a version, one entry per text an admin can edit: subtitle,
    summary, itineraries (strings) and each highlight (`index` into the list). Each carries its
    sentences with code-point offsets into that text. Other shapes (an itinerary stored as a list
    of day objects) are left out: not editable here."""
    out: list[dict] = []
    for field in _T2_TEXT_FIELDS:
        value = rc.get(field)
        if field == "highlights" and isinstance(value, list):
            texts = [(i, v) for i, v in enumerate(value) if isinstance(v, str) and v.strip()]
        elif isinstance(value, str) and value.strip():
            texts = [(None, value)]
        else:
            texts = []
        for index, text in texts:
            sentences = []
            for sp in hygiene.sentence_spans(text):
                if sp["kind"] in ("heading", "hashtags") or not sp["clean"]:
                    continue
                sentences.append({"start": sp["start"], "end": sp["end"], "text": sp["text"],
                                  "sentence": sp["clean"], "key": _sentence_key(f"{field}:{index}:{sp['clean']}"),
                                  "problems": []})
            out.append({"field": field, "index": index, "text": text, "sentences": sentences})
    return out


def _t2_pack(pack_or_words) -> dict:
    """A brand pack, or (the pre-AA-800 call shape) just the tenant's forbidden words."""
    if isinstance(pack_or_words, dict):
        return pack_or_words
    return {"forbidden_words": list(pack_or_words or [])}


def t2_flag(sections: list[dict], tour: dict, pack_or_words) -> list[dict]:
    """Mark each sentence with what T3 would flag on it NOW (deterministic, no LLM): a number the
    source tour lacks (`find_novel_numeric_claims`, T3's own reading), a service-level claim the source
    does not support (AA-800) and a forbidden word. Candidates only: T3's Jev step may have cleared a
    number. Returns the flagged sentences (the drawer's Cut / Rewrite / Edit list).

    AA-800: the forbidden words are the tenant's layered list, the same one T3 validates with
    (signature_vocabulary.tenant_blocked_words): its signature words are not flagged, and the stock
    superlatives only when the version holds more than one."""
    from services.acp_shared.signature_vocabulary import service_level_claims, tenant_blocked_words
    from services.content_generation.forbidden_words import has_word
    source_texts = [str(v) for v in tour.values() if v]
    whole = "\n".join(sec["text"] for sec in sections)
    words = tenant_blocked_words(_t2_pack(pack_or_words), whole)
    flagged = []
    for sec in sections:
        for s in sec["sentences"]:
            novel = find_novel_numeric_claims(s["sentence"], source_texts)
            if novel:
                s["problems"].append({"gate": qr.T3_GROUNDING, "blocking": True,
                                      "problem": f"number(s) {novel} not in the source tour"})
            claims = [c["phrase"] for c in service_level_claims(s["sentence"], *source_texts)]
            if claims:
                s["problems"].append({"gate": qr.T3_GROUNDING, "blocking": True,
                                      "problem": f"service claim(s) {claims} not in the source tour"})
            hits = [w for w in words if has_word(s["sentence"], w)]
            if hits:
                s["problems"].append({"gate": qr.T2_FORBIDDEN_WORD, "blocking": True,
                                      "problem": f"forbidden word(s): {', '.join(hits)}"})
            if s["problems"]:
                flagged.append({**s, "field": sec["field"], "index": sec["index"],
                                "gates": list(dict.fromkeys(p["gate"] for p in s["problems"])),
                                "blocking": True})
    return flagged


def t2_check_replacement(replacement: str, tour: dict, pack: dict, field_text: str,
                         span: tuple[int, int]) -> list[dict]:
    """The deterministic checks on an admin's (or the model's) sentence for a T2 version, same shape
    as `sentence_repair.check_replacement` (which runs first, against the source tour): F1 / F2 /
    brand-forbidden / F11 / a near-verbatim neighbour block. On top, T2's own rules block: a number
    T3's plain reading finds outside the source, and a word on the tenant's layered forbidden list
    (AA-800: platform truth, undeclared positioning words; a second stock superlative is caught by the
    whole-version T3 check at apply time)."""
    from services.acp_shared.signature_vocabulary import service_level_claims, tenant_blocked_words
    from services.content_generation.forbidden_words import has_word
    source_texts = [str(v) for v in tour.values() if v]
    checks = sentence_repair.check_replacement(replacement, "\n".join(source_texts), pack,
                                               text=field_text, span=span)
    sentence = " ".join((replacement or "").split())
    if not sentence:
        return checks
    novel = find_novel_numeric_claims(sentence, source_texts)
    if novel and not any(c["gate"] == qr.F1_GROUNDING for c in checks):
        checks.append({"gate": qr.T3_GROUNDING, "blocking": True,
                       "violations": [f"number(s) {novel} not in the source tour"]})
    claims = [c["phrase"] for c in service_level_claims(sentence, *source_texts)]
    if claims:
        checks.append({"gate": qr.T3_GROUNDING, "blocking": True,
                       "violations": [f"service claim(s) {claims} not in the source tour"]})
    hits = [w for w in tenant_blocked_words({**(pack or {}), "forbidden_words": []}) if has_word(sentence, w)]
    if hits:
        checks.append({"gate": qr.T2_FORBIDDEN_WORD, "blocking": True,
                       "violations": [f"forbidden word(s): {', '.join(hits)}"]})
    return checks


def _t2_field_text(rc: dict, field: str, index: Optional[int]) -> str:
    if field not in _T2_TEXT_FIELDS:
        raise HTTPException(status_code=422, detail=f"field must be one of {', '.join(_T2_TEXT_FIELDS)}")
    value = rc.get(field)
    if field == "highlights" and isinstance(value, list):
        if index is None or index >= len(value) or not isinstance(value[index], str):
            raise HTTPException(status_code=409,
                                detail="The version changed since it was loaded — reload and try again")
        return value[index]
    if index is not None or not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"The {field} of this version cannot be edited here")
    return value


def _t2_with_field(rc: dict, field: str, index: Optional[int], new_text: str) -> dict:
    out = dict(rc)
    if field == "highlights" and isinstance(rc.get(field), list):
        items = list(rc[field])
        if new_text.strip():
            items[index] = new_text
        else:
            del items[index]  # a cut that empties a highlight drops the item
        out[field] = items
    else:
        out[field] = new_text
    return out


async def _load_version_detail(conn, version_id: str) -> dict:
    """GET /tour-versions/{id} and the apply response share this one read."""
    from services.jobs.t2_rewrite_job import source_tour_dict
    row = await conn.fetchrow(_VERSION_DETAIL_SQL, version_id)
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
    edits = await conn.fetch(_ADMIN_EDITS_SQL, str(row["tenant_id"]), str(row["id"]),
                             AdminAuditAction.TOUR_SENTENCE_PREFIX + "%", "tenant_tour_version")
    pack = await fetch_brand_pack_conn(conn, row["tenant_id"])
    rc = _rewritten(row)
    sections = t2_sections(rc)
    out = {k: v for k, v in _row(row).items() if k not in _T2_SOURCE_COLUMNS}
    out["rewritten_content"] = rc or _j(row["rewritten_content"])
    out["reviews"] = [_row(r) for r in reviews]
    out["cost_by_stage"] = [_row(r) for r in cost_rows]
    out["flagged_sentences"] = t2_flag(sections, source_tour_dict(row), pack)
    out["sections"] = sections
    out["admin_edits"] = [{**_row(r), "details": _j(r["details"]) or {}} for r in edits]
    block = t2_edit_block(row["status"], row["version_number"], row["latest_version_number"],
                          row["edit_source"])
    out["editable"], out["edit_block_reason"] = block is None, block
    out["tenant_copy"] = tenant_copy_state(None, row["exported_at"], [r["created_at"] for r in edits])
    return out


@router.get("/tour-versions/{version_id}")
async def get_tour_version(version_id: str, request: Request, x_admin_secret: str = Header(None)):
    """One T2 version for the admin: scores / QA / escalations / cost (never shown to the tenant),
    plus (AA-802) its editable sections, the sentences T3 would flag now, the admin edit history,
    whether it can be corrected in place and whether the tenant already downloaded a copy."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        return await _load_version_detail(conn, version_id)


class TourSentenceSuggestBody(SentenceSuggestBody):
    field: str = Field(max_length=40)
    index: Optional[int] = Field(None, ge=0)  # the highlight's position; None for a text field


class TourSentenceApplyBody(SentenceApplyBody):
    field: str = Field(max_length=40)
    index: Optional[int] = Field(None, ge=0)


_TOUR_ACTION_BY_MODE = {"cut": AdminAuditAction.TOUR_SENTENCE_CUT,
                        "rewrite": AdminAuditAction.TOUR_SENTENCE_REWRITTEN,
                        "manual": AdminAuditAction.TOUR_SENTENCE_EDITED}


@router.post("/tour-versions/{version_id}/sentences/suggest")
async def suggest_tour_sentence(version_id: str, body: TourSentenceSuggestBody, request: Request,
                                x_admin_secret: str = Header(None), x_admin_role: Optional[str] = Header(None)):
    """AA-802 — a proposed rewrite of one sentence of a T2 version: ONE call on the cheap
    `t9_repair` stage (mode `admin_suggest_tour`, logged to llm_call_log with the tenant), with the
    source tour as SOURCE. Nothing is saved; the suggestion goes through the checks /apply runs."""
    verify_admin_secret(x_admin_secret)
    _require_admin_role(x_admin_role)
    from services.content_generation.grounding import source_text
    from services.jobs.t2_rewrite_job import source_tour_dict
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_VERSION_DETAIL_SQL, version_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Version not found")
        pack = await fetch_brand_pack_conn(conn, row["tenant_id"])
    field_text = _t2_field_text(_rewritten(row), body.field, body.index)
    start, end = _resolve(field_text, body.start, body.end, body.text)
    sentence = _plain(field_text[start:end])
    problem = body.problem.strip() or "The editor wants this sentence rewritten."
    if body.note and body.note.strip():
        problem += f" Editor note: {body.note.strip()}"
    tour = source_tour_dict(row)
    seed = source_text(tour)
    if pack.get("brand_name"):
        seed = f"BRAND: {pack['brand_name']}\n\n{seed}"
    try:
        reps, cost, model = await asyncio.to_thread(
            repair_sentences_detailed,
            sentences=[{"sentence": sentence, "problem": problem,
                        "context": sentence_repair.neighbours(field_text, {"start": start, "end": end})}],
            content_seed=seed, facts_text=None, channel="tour", tenant_id=str(row["tenant_id"]),
            angle_gate_request_id=None, stage=_SUGGEST_STAGE, mode="admin_suggest_tour", attempt=None,
            content_piece_id=None, max_tokens=_SUGGEST_MAX_TOKENS,
        )
    except SentenceRepairError as e:
        raise HTTPException(status_code=502, detail=f"The model reply could not be read: {str(e)[:200]}")
    replacement = sentence_repair.sanitize_replacement(reps[0], None)
    return {"field": body.field, "index": body.index, "start": start, "end": end, "sentence": sentence,
            "replacement": replacement,
            "checks": t2_check_replacement(replacement, tour, pack, field_text, (start, end)),
            "model": model, "cost_usd": cost}


@router.post("/tour-versions/{version_id}/sentences/apply")
async def apply_tour_sentence(version_id: str, body: TourSentenceApplyBody, request: Request,
                              x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
                              x_admin_role: Optional[str] = Header(None)):
    """AA-802 — cut one sentence of a T2 version, or put a replacement in its place, in the
    tenant's copy (silent correction). Deterministic, no LLM. In one transaction: lock the row,
    refuse unless `t2_edit_block` allows it, resolve the span in the section (409 when it moved
    ambiguously), run `t2_check_replacement` (422 on a blocking hit; F12/F13 hits are warnings),
    splice + tidy, refuse an emptied text section and anything that adds a T3 hard code
    (validate_node: forbidden word, missing field…), UPDATE the row by id + tenant_id + editable
    status (0 rows → 409) and write the audit row. Returns the refreshed detail + `warnings`."""
    verify_admin_secret(x_admin_secret)
    _require_admin_role(x_admin_role)
    from services.acp_produce.tenant_pipeline import _t3_structural_issues
    from services.jobs.t2_rewrite_job import brand_rules_from_pack, source_tour_dict
    if body.mode not in _TOUR_ACTION_BY_MODE:
        raise HTTPException(status_code=422, detail="mode must be cut, rewrite or manual")
    replacement = body.replacement.strip()
    if body.mode == "cut":
        replacement = ""
    elif not replacement:
        raise HTTPException(status_code=422, detail="An empty replacement is a cut — use mode 'cut'")
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(_VERSION_DETAIL_SQL + "    FOR UPDATE OF ttv\n", version_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Version not found")
            block = t2_edit_block(row["status"], row["version_number"], row["latest_version_number"],
                          row["edit_source"])
            if block:
                raise HTTPException(status_code=409, detail=block)
            rc = _rewritten(row)
            old_text = _t2_field_text(rc, body.field, body.index)
            start, end = _resolve(old_text, body.start, body.end, body.text)
            replacement = sentence_repair.sanitize_replacement(replacement, None)
            if body.mode != "cut" and not replacement:
                raise HTTPException(status_code=422, detail="The replacement is empty once cleaned — use mode 'cut'")
            pack = await fetch_brand_pack_conn(conn, row["tenant_id"])
            tour = source_tour_dict(row)
            warnings: list[dict] = []
            if replacement:
                checks = t2_check_replacement(replacement, tour, pack, old_text, (start, end))
                failed = [c for c in checks if c["blocking"]]
                warnings = [c for c in checks if not c["blocking"]]
                if failed:
                    detail = "; ".join(f"{f['gate']}: {' | '.join(f['violations'])}" for f in failed)
                    raise HTTPException(status_code=422, detail=f"The new sentence fails a check — {detail}")
            new_text = sentence_repair.apply_edit(old_text, start, end, replacement)
            if body.field != "highlights" and not new_text.strip():
                raise HTTPException(status_code=422, detail=f"This would leave the {body.field} empty")
            new_rc = _t2_with_field(rc, body.field, body.index, new_text)
            if body.field == "highlights" and not new_rc.get("highlights"):
                raise HTTPException(status_code=422, detail="This would leave the tour with no highlights")
            broken = text_integrity.edit_findings(old_text, new_text, None, pack.get("brand_name"))
            if broken:
                warnings.append({"gate": qr.F13_REPEAT, "violations": broken, "blocking": False})
            rules = brand_rules_from_pack(pack)  # AA-800: T3 reads the signature words too
            added = sorted(set(_t3_structural_issues(new_rc, tour, rules))
                           - set(_t3_structural_issues(rc, tour, rules)))
            if added:
                raise HTTPException(status_code=422, detail=f"This edit adds a T3 problem — {', '.join(added)}")
            res = await conn.execute(_VERSION_UPDATE_SQL, version_id, str(row["tenant_id"]),
                                     json.dumps(new_rc), list(_T2_EDITABLE_STATUSES))
            if not str(res).endswith(" 1"):
                raise HTTPException(status_code=409, detail="The version was not updated (it changed or is no "
                                                            "longer editable) — reload and try again")
            await write_audit_log(
                conn, tenant_id=str(row["tenant_id"]), actor=f"admin:{x_admin_user_id or 'unknown'}",
                action=_TOUR_ACTION_BY_MODE[body.mode], resource_type="tenant_tour_version",
                resource_id=str(row["id"]),  # canonical form: the detail reads by ttv.id::text
                details={"field": body.field, "index": body.index, "start": start, "end": end,
                         "before": old_text[start:end], "after": replacement, "mode": body.mode,
                         "gate": body.gate, "problem": body.problem, "suggestion": body.suggestion,
                         "suggestion_model": body.suggestion_model,
                         "suggestion_cost_usd": body.suggestion_cost_usd,
                         "version_number": row["version_number"], "status": row["status"],
                         "section_before": old_text, "section_after": new_text,
                         # the whole version before / after: the history holds both versions
                         # (AA-802 Done-when), not only the section that changed
                         "content_before": rc, "content_after": new_rc,
                         "exported": row["exported_at"] is not None, "warnings": warnings},
            )
        detail = await _load_version_detail(conn, version_id)
    return {**detail, "warnings": warnings}


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


# ── AA-774 (S235): correct one card's facts (admin only) ────────────────────────────────────────

_CARD_DETAIL_SQL = """
    SELECT pc.card_id, pc.display_name, pc.locality, pc.region, pc.country, pc.naming_note, pc.facts,
           pc.status, pc.cost_usd, pc.verified_by, pc.verified_at, pc.created_at, pc.updated_at,
           (SELECT count(*) FROM acp_shared.atom_place_card a WHERE a.card_id = pc.card_id) AS atoms
    FROM acp_shared.place_card pc
    WHERE pc.card_id = $1::uuid
"""


def _card_detail(row) -> dict:
    """One card for the admin editor: the stored facts array unchanged, plus `fact_rows` — each fact
    with its `index` in that array and whether the T9 writer seed shows it (WRITER_FACT_KINDS)."""
    out = _row(row)
    facts = out.get("facts") if isinstance(out.get("facts"), list) else []
    out["fact_rows"] = [
        {"index": i, "kind": f.get("kind") or "what_it_is", "text": f.get("text") or "",
         "in_writer_seed": (f.get("kind") or "what_it_is") in WRITER_FACT_KINDS}
        for i, f in enumerate(facts) if isinstance(f, dict)]
    out["fact_kinds"] = list(place_cards.FACT_KINDS)
    return out


def _card_uuid(card_id: str) -> str:
    try:
        return str(UUID(card_id))
    except ValueError:
        raise HTTPException(status_code=422, detail="card_id must be a UUID")


@router.get("/place-cards/{card_id}")
async def get_place_card(card_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    cid = _card_uuid(card_id)
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(_CARD_DETAIL_SQL, cid)
    if row is None:
        raise HTTPException(status_code=404, detail="Place card not found")
    return _card_detail(row)


class FactEdit(BaseModel):
    index: int = Field(ge=0)
    kind: Optional[str] = None
    text: Optional[str] = Field(None, max_length=place_cards.MAX_FACT_TEXT)
    drop: bool = False
    expected_text: Optional[str] = None  # the text the admin loaded; mismatch → 409


class PlaceCardFactsEdit(BaseModel):
    edits: list[FactEdit] = Field(min_length=1, max_length=place_cards.MAX_FACTS * 2)
    reason: str = Field(min_length=3, max_length=500)


@router.patch("/place-cards/{card_id}/facts")
async def edit_place_card_facts(card_id: str, body: PlaceCardFactsEdit, request: Request,
                                x_admin_secret: str = Header(None),
                                x_admin_user_id: Optional[str] = Header(None),
                                x_admin_role: Optional[str] = Header(None)):
    """AA-774 (S235) — re-kind, re-word or drop facts on one card (one transaction, one audit row
    with before/after per changed fact). A fact re-kinded to visiting/practical stays on the card but
    leaves the T9 writer seed (atom_seed.writer_facts). Status / verified_by are not touched."""
    verify_admin_secret(x_admin_secret)
    if x_admin_role != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can edit place-card facts")
    cid = _card_uuid(card_id)
    reason = " ".join(body.reason.split())
    if len(reason) < 3:
        raise HTTPException(status_code=422, detail="reason is required")
    actor = x_admin_user_id or "admin"
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        cur = await conn.fetchrow(
            "SELECT card_id, display_name, country, facts FROM acp_shared.place_card "
            "WHERE card_id = $1::uuid FOR UPDATE", cid)
        if cur is None:
            raise HTTPException(status_code=404, detail="Place card not found")
        try:
            facts, changes = place_cards.apply_fact_edits(
                cur["facts"], [e.model_dump(exclude_none=True) for e in body.edits])
        except place_cards.FactEditConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        except place_cards.FactEditError as e:
            raise HTTPException(status_code=422, detail=str(e))
        await conn.execute(
            "UPDATE acp_shared.place_card SET facts = $2::jsonb, updated_at = now() WHERE card_id = $1::uuid",
            cid, json.dumps(facts))
        await write_audit_log(
            conn, tenant_id=None, actor=f"admin:{actor}", action=PlatformAuditAction.PLACE_CARD_FACTS_EDITED,
            resource_type="place_card", resource_id=cid,
            details={"reason": reason, "display_name": cur["display_name"], "country": cur["country"],
                     "facts_before": len(place_cards.stored_facts(cur["facts"])), "facts_after": len(facts),
                     "changes": changes})
        row = await conn.fetchrow(_CARD_DETAIL_SQL, cid)
    return _card_detail(row)


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
