"""AA-921 WM-27 / WM-29 — Pinned Exemplars (app ADR 0010, S245 decisions E3 / D12; CIS 313).

A T2 or T9 release pins at most two entity-masked voice examples per channel. They are chosen when the release is
drafted (`select_for_release`), written in the draft's own transaction (`shared.writing_memory_release_exemplar`, the
CIS 313 trigger refuses any later change) and hashed into the release's `items_sha256` (`release_sha256`), so the
text approved is the text a writer sees. There is no retrieval at write time: no embeddings, no kNN, no ivfflat. A1
gets no example (E3).

The pool (`shared.writing_exemplar`):
  candidates   `refresh` turns every "use as example" signal (WM-07 `exemplar_pref = 'use'`) whose fact filter passed
               (`fact_status = 'pass'`) into one masked excerpt of the edited object's current text: a T9 piece that is
               `approved` (content_piece.content_text), or a T2 version (the edited field of rewritten_content, else
               the whole version). A source any signal marked "never use" gives no candidate, and its existing
               examples turn `never`. $0, no model call, idempotent (one row per masked text, CIS 313 unique index).
  masking      services/writing_memory/masking.mask: numbers, prices, times, links and names become [N], [PRICE],
               [TIME], [URL], [NAME]; the excerpt keeps whole sentences up to EXEMPLAR_MAX_WORDS.
  statuses     candidate (eligible) · pinned (an admin keeps it: pinned examples come first) · never (excluded for
               good). Every change carries the admin's reason and writes one audit row (ids and statuses only).

Selection (`select_for_release`, the one function the draft and the Exemplars tab preview call): per channel of the
writer, pinned first (newest pin first), then candidates (newest first), at most PER_CHANNEL; never a `never` one. A
tenant release takes only that tenant's examples (explicit `tenant_id` filter: cis_app's policy is USING (true)); a
platform release only platform ones. At write time a tenant with fewer than two examples of a channel is filled from
the platform release's examples of that channel (insertion.voice_examples), never from another tenant.

Isolation is checked three times: the selection filter, the CIS 313 trigger (owner, writer and channel of every link),
and `load_release_exemplars` (the resolver drops a release part whose links do not belong to it).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Optional
from uuid import UUID

import structlog

from services.writing_memory.masking import mask

logger = structlog.get_logger()

EXEMPLAR_WRITERS = ("t2", "t9_blog", "t9_social")
STATUSES = ("candidate", "pinned", "never")
PER_CHANNEL = 2
T2_CHANNEL = "tour_page"
EXEMPLAR_MAX_WORDS = 120         # the excerpt a prompt shows (whole sentences up to this)
EXEMPLAR_MIN_WORDS = 20          # shorter text shows no voice: no candidate
MAX_TEXT_CHARS = 2000            # CIS 313 writing_exemplar_text_check
REFRESH_MAX_SIGNALS = 500
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"   # the audit_log tenant of a platform row

_SENTENCE_RE = re.compile(r"[^.!?]+(?:[.!?]+[\"”’)]*|$)")


class ExemplarError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(status, code, detail)
        self.status, self.code, self.detail = status, code, detail


# ── pure helpers ───────────────────────────────────────────────────────────────────────────────────────────────────

def channel_for(writer: str, channel: Optional[str]) -> Optional[str]:
    """The pool channel of a writer: T2 = tour_page, T9 blog = blog, T9 social = its channel (never blog)."""
    if writer == "t2":
        return T2_CHANNEL
    if writer == "t9_blog":
        return "blog"
    ch = (channel or "").strip().lower()
    return ch if ch and ch != "blog" and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", ch) else None


def excerpt(text: Optional[str], max_words: int = EXEMPLAR_MAX_WORDS) -> str:
    """Whole sentences from the start of `text` up to `max_words` words (the first sentence cut at the limit when it
    is longer); whitespace folded to single spaces, paragraphs kept as one space."""
    flat = " ".join(str(text or "").split())
    if not flat:
        return ""
    out: list[str] = []
    n = 0
    for m in _SENTENCE_RE.finditer(flat):
        s = m.group(0).strip()
        if not s:
            continue
        w = len(s.split())
        if n + w > max_words:
            if not out:
                out.append(" ".join(s.split()[:max_words]))
            break
        out.append(s)
        n += w
    return " ".join(out)


def masked_excerpt(text: Optional[str]) -> Optional[dict]:
    """{masked_text, text_sha256, word_count} of the excerpt, or None when it is too short to show a voice."""
    m = mask(excerpt(text))
    m = " ".join(m.split())[:MAX_TEXT_CHARS]
    words = len(m.split())
    if words < EXEMPLAR_MIN_WORDS:
        return None
    return {"masked_text": m, "text_sha256": hashlib.sha256(m.encode("utf-8")).hexdigest(), "word_count": words}


def pick(rows: Iterable[dict], per_channel: int = PER_CHANNEL) -> list[dict]:
    """The selection rule over pool rows of ONE writer and owner: per channel, pinned first (newest pin first), then
    candidates (newest first), at most `per_channel`; `never` rows are skipped. Each picked row gets `position`."""
    def key(r):
        pinned = r.get("status") == "pinned"
        at = r.get("status_at") if pinned else r.get("created_at")
        return (0 if pinned else 1, -(at.timestamp() if hasattr(at, "timestamp") else 0), str(r.get("exemplar_id")))
    by_ch: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("status") == "never":
            continue
        by_ch.setdefault(r["channel"], []).append(r)
    out: list[dict] = []
    for ch in sorted(by_ch):
        for pos, r in enumerate(sorted(by_ch[ch], key=key)[:per_channel], 1):
            out.append({**r, "position": pos})
    return out


def release_sha256(rules: list[dict], exemplars: Optional[list[dict]] = None) -> str:
    """The release fingerprint. Without exemplars it is exactly releases.items_sha256 (every release before CIS 313
    keeps its hash); with them, sha256 of the sorted rule items and the sorted (exemplar_id, channel, position,
    masked_text) of the pinned examples."""
    from services.writing_memory.releases import items_sha256
    if not exemplars:
        return items_sha256(rules)
    r_items = sorted([str(r["rule_id"]), r.get("rule_text") or "", r.get("replacement")] for r in rules)
    e_items = sorted([str(e["exemplar_id"]), e["channel"], int(e["position"]), e.get("masked_text") or ""]
                     for e in exemplars)
    raw = json.dumps({"rules": r_items, "exemplars": e_items}, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _uuid(v: Any, what: str = "id") -> str:
    try:
        return str(UUID(str(v)))
    except (ValueError, TypeError):
        raise ExemplarError(404, "not_found", f"Unknown {what}")


# ── the pool ───────────────────────────────────────────────────────────────────────────────────────────────────────

_COLUMNS = ("exemplar_id::text AS exemplar_id, scope, tenant_id::text AS tenant_id, writer, channel, masked_text, "
            "text_sha256, word_count, source_table, source_id, source_signal_id::text AS source_signal_id, status, "
            "status_reason, status_by, status_at, created_at, updated_at")

_USE_SIGNALS_SQL = f"""
    SELECT signal_id::text AS signal_id, writer, channel, scope, tenant_id::text AS tenant_id, field, source_table,
           source_id
      FROM shared.writing_signal
     WHERE exemplar_pref = 'use' AND fact_status = 'pass' AND writer IN ('t2', 't9_blog', 't9_social')
       AND source_table IN ('content_piece', 'tenant_tour_version') AND scope = 'tenant'
     ORDER BY created_at DESC, signal_id
     LIMIT {REFRESH_MAX_SIGNALS}
"""
_NEVER_SOURCES_SQL = """
    SELECT DISTINCT source_table, source_id, tenant_id::text AS tenant_id
      FROM shared.writing_signal WHERE exemplar_pref = 'never'
"""
_PIECE_SQL = """
    SELECT content_text, status FROM acp_shared.content_piece WHERE piece_id = $1::uuid AND tenant_id = $2::uuid
"""
_VERSION_SQL = """
    SELECT rewritten_content FROM gold_aa_internal.tenant_tour_versions WHERE id = $1::uuid AND tenant_id = $2::uuid
"""
_INSERT_SQL = f"""
    INSERT INTO shared.writing_exemplar (scope, tenant_id, writer, channel, masked_text, text_sha256, word_count,
                                         source_table, source_id, source_signal_id)
    VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::uuid)
    ON CONFLICT DO NOTHING
    RETURNING {_COLUMNS}
"""
_MARK_NEVER_SQL = """
    UPDATE shared.writing_exemplar
       SET status = 'never', status_reason = 'marked never use on its source', status_by = $4, status_at = now(),
           updated_at = now()
     WHERE source_table = $1 AND source_id = $2 AND tenant_id IS NOT DISTINCT FROM $3::uuid AND status <> 'never'
     RETURNING exemplar_id::text AS exemplar_id
"""


def _version_text(rc: Any, field: Optional[str]) -> str:
    from services.writing_memory.eval_checks import flatten_text
    if isinstance(rc, str):
        try:
            rc = json.loads(rc)
        except ValueError:
            return rc
    if isinstance(rc, dict) and field and isinstance(rc.get(field), str):
        return rc[field]
    return flatten_text(rc or {})


async def source_text(conn, sig: dict) -> tuple[Optional[str], Optional[str]]:
    """(text, channel) of the object a signal edited, read with the owner filter; (None, why) when it gives none."""
    try:
        oid = str(UUID(str(sig["source_id"])))
    except (ValueError, TypeError):
        return None, "bad_source_id"
    if sig["source_table"] == "content_piece":
        row = await conn.fetchrow(_PIECE_SQL, oid, sig["tenant_id"])
        if row is None:
            return None, "source_missing"
        if row["status"] != "approved":
            return None, "not_approved"
        return row["content_text"], sig.get("channel")       # the signal carries the piece's channel
    row = await conn.fetchrow(_VERSION_SQL, oid, sig["tenant_id"])
    if row is None:
        return None, "source_missing"
    return _version_text(row["rewritten_content"], sig.get("field")), T2_CHANNEL


async def refresh(conn, *, dry_run: bool = True, actor: str = "system:exemplar_refresh") -> dict:
    """Candidates from the "use as example" signals and `never` from the "never use" ones ($0). DRY RUN by default:
    counts what an apply would add / mark, writes nothing. The caller holds the transaction."""
    never = {(r["source_table"], r["source_id"], r["tenant_id"]) for r in await conn.fetch(_NEVER_SOURCES_SQL)}
    sigs = [dict(r) for r in await conn.fetch(_USE_SIGNALS_SQL)]
    out: dict[str, Any] = {"dry_run": dry_run, "signals": len(sigs), "added": 0, "existing": 0, "marked_never": 0,
                           "skipped": {}}
    seen: set = set()

    def skip(why: str) -> None:
        out["skipped"][why] = out["skipped"].get(why, 0) + 1

    for s in sigs:
        src = (s["source_table"], s["source_id"], s["tenant_id"])
        if src in never:
            skip("never_use")
            continue
        text, channel = await source_text(conn, s)
        if text is None:
            skip(channel or "no_text")
            continue
        ch = channel_for(s["writer"], channel)
        if ch is None:
            skip("no_channel")
            continue
        m = masked_excerpt(text)
        if m is None:
            skip("too_short")
            continue
        key = (s["writer"], ch, s["tenant_id"], m["text_sha256"])
        if key in seen:
            skip("duplicate")
            continue
        seen.add(key)
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM shared.writing_exemplar WHERE writer = $1 AND channel = $2 AND scope = "
            "'tenant' AND tenant_id = $3::uuid AND text_sha256 = $4)", s["writer"], ch, s["tenant_id"],
            m["text_sha256"])
        if exists:
            out["existing"] += 1
            continue
        out["added"] += 1
        if not dry_run:
            await conn.fetchrow(_INSERT_SQL, "tenant", s["tenant_id"], s["writer"], ch, m["masked_text"],
                                m["text_sha256"], m["word_count"], s["source_table"], s["source_id"], s["signal_id"])
    for st, sid, tid in sorted(never, key=lambda x: (x[0], x[1], x[2] or "")):
        if dry_run:
            n = await conn.fetchval(
                "SELECT count(*) FROM shared.writing_exemplar WHERE source_table = $1 AND source_id = $2 "
                "AND tenant_id IS NOT DISTINCT FROM $3::uuid AND status <> 'never'", st, sid, tid)
        else:
            n = len(await conn.fetch(_MARK_NEVER_SQL, st, sid, tid, actor[:128]))
        out["marked_never"] += int(n or 0)
    return out


async def list_pool(conn, *, writer: Optional[str] = None, channel: Optional[str] = None,
                    scope: Optional[str] = None, tenant_id: Optional[str] = None, status: Optional[str] = None,
                    limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    where, args = ["true"], []
    for col, val in (("writer", writer), ("channel", channel), ("scope", scope), ("status", status)):
        if val:
            args.append(val)
            where.append(f"{col} = ${len(args)}")
    if tenant_id:
        args.append(_uuid(tenant_id, "tenant"))
        where.append(f"tenant_id = ${len(args)}::uuid")         # explicit owner filter (ADR 0010 decision 10)
    w = " AND ".join(where)
    total = await conn.fetchval(f"SELECT count(*) FROM shared.writing_exemplar WHERE {w}", *args)
    rows = await conn.fetch(f"SELECT {_COLUMNS} FROM shared.writing_exemplar WHERE {w} "
                            f"ORDER BY writer, channel, scope, tenant_id NULLS FIRST, "
                            f"(status = 'pinned') DESC, created_at DESC LIMIT {int(limit)} OFFSET {int(offset)}",
                            *args)
    return [dict(r) for r in rows], int(total or 0)


async def get(conn, exemplar_id: str, *, for_update: bool = False) -> Optional[dict]:
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM shared.writing_exemplar WHERE exemplar_id = $1::uuid"
                              + (" FOR UPDATE" if for_update else ""), _uuid(exemplar_id, "example"))
    return dict(row) if row else None


async def owner_pool(conn, writer: str, scope: str, tenant_id: Optional[str]) -> list[dict]:
    """Every non-never example of one writer and owner (explicit owner filter)."""
    rows = await conn.fetch(
        f"SELECT {_COLUMNS} FROM shared.writing_exemplar WHERE writer = $1 AND scope = $2 "
        "AND tenant_id IS NOT DISTINCT FROM $3::uuid AND status <> 'never'", writer, scope, tenant_id)
    return [dict(r) for r in rows]


async def select_for_release(conn, writer: str, scope: str, tenant_id: Optional[str]) -> list[dict]:
    """What a draft release of this writer and owner pins now ([] for A1 and for a writer without examples)."""
    if writer not in EXEMPLAR_WRITERS:
        return []
    return pick(await owner_pool(conn, writer, scope, tenant_id))


_TRANSITIONS = {"pin": ({"candidate"}, "pinned"), "unpin": ({"pinned", "never"}, "candidate"),
                "never": ({"candidate", "pinned"}, "never")}


async def set_status(conn, exemplar_id: str, action: str, *, reason: str, actor: str) -> dict:
    """pin (candidate -> pinned), unpin (pinned / never -> candidate), never (-> never), with the admin's reason, in the
    caller's transaction; one audit row (ids and statuses only)."""
    from services.acp_shared.audit_log import write_audit_log
    if action not in _TRANSITIONS:
        raise ExemplarError(422, "invalid", f"action must be one of {sorted(_TRANSITIONS)}")
    reason = " ".join((reason or "").split())
    if not reason:
        raise ExemplarError(422, "invalid", "a reason is required")
    ex = await get(conn, exemplar_id, for_update=True)
    if ex is None:
        raise ExemplarError(404, "not_found", "Unknown example")
    allowed, new = _TRANSITIONS[action]
    if ex["status"] not in allowed:
        raise ExemplarError(409, "bad_transition", f"a {ex['status']} example cannot be {action}ned"
                            if action != "never" else f"the example is already {ex['status']}")
    row = await conn.fetchrow(
        f"""UPDATE shared.writing_exemplar SET status = $2, status_reason = $3, status_by = $4, status_at = now(),
                   updated_at = now() WHERE exemplar_id = $1::uuid RETURNING {_COLUMNS}""",
        ex["exemplar_id"], new, reason[:500], actor[:128])
    out = dict(row)
    await write_audit_log(conn, tenant_id=out.get("tenant_id") or PLATFORM_TENANT_ID, actor=actor[:128],
                          action=f"writing_memory.exemplar_{action}", resource_type="writing_exemplar",
                          resource_id=out["exemplar_id"],
                          details={"writer": out["writer"], "channel": out["channel"], "scope": out["scope"],
                                   "from": ex["status"], "to": new})
    return out


# ── release links ──────────────────────────────────────────────────────────────────────────────────────────────────

async def pin_into_release(conn, release: dict, picked: list[dict]) -> int:
    """Write the links of a draft in the draft's transaction (CIS 313 trigger: same transaction, owner, writer,
    channel)."""
    for e in picked:
        await conn.execute(
            "INSERT INTO shared.writing_memory_release_exemplar (release_id, exemplar_id, writer, scope, tenant_id, "
            "channel, position) VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid, $6, $7)",
            release["release_id"], e["exemplar_id"], release["writer"], release["scope"], release.get("tenant_id"),
            e["channel"], int(e["position"]))
    return len(picked)


_RELEASE_EXEMPLARS_SQL = """
    SELECT l.exemplar_id::text AS exemplar_id, l.channel, l.position, l.writer AS link_writer, l.scope AS link_scope,
           l.tenant_id::text AS link_tenant_id, e.writer, e.channel AS ex_channel, e.scope, e.tenant_id::text AS
           tenant_id, e.masked_text, e.status, e.source_table, e.source_id
      FROM shared.writing_memory_release_exemplar l
      JOIN shared.writing_exemplar e ON e.exemplar_id = l.exemplar_id
     WHERE l.release_id = $1::uuid
     ORDER BY l.channel, l.position
"""


async def release_exemplars(db, release_id: str) -> list[dict]:
    """The links of one release with their example rows (no checks; [] before CIS 313 is applied)."""
    import asyncpg
    try:
        if hasattr(db, "transaction"):            # a connection: a savepoint, so a missing table aborts nothing
            async with db.transaction():
                rows = await db.fetch(_RELEASE_EXEMPLARS_SQL, str(release_id))
        else:                                     # a pool
            rows = await db.fetch(_RELEASE_EXEMPLARS_SQL, str(release_id))
    except asyncpg.UndefinedTableError:
        return []
    return [dict(r) for r in rows]


def exemplar_problem(row: dict, links: list[dict], writer: str) -> Optional[str]:
    """Why the links of a release row do not belong to it (writer, owner, channel), or None."""
    for e in links:
        if (e["writer"] != writer or e["link_writer"] != writer or e["channel"] != e["ex_channel"]
                or e["scope"] != row.get("scope") or e["link_scope"] != row.get("scope")
                or (e.get("tenant_id") or None) != (row.get("tenant_id") or None)
                or (e.get("link_tenant_id") or None) != (row.get("tenant_id") or None)):
            return "exemplar_isolation"
    return None


async def load_release_exemplars(db, row: dict, writer: str) -> Optional[list[dict]]:
    """The checked examples of a release row: [{exemplar_id, channel, position, masked_text}] ([] without any), or
    None when a link does not belong to the row (logged; the part then renders nothing)."""
    if not row.get("release_id") or writer not in EXEMPLAR_WRITERS:
        return []
    links = await release_exemplars(db, row["release_id"])
    problem = exemplar_problem(row, links, writer)
    if problem:
        logger.warning("wm_release_exemplars_refused", writer=writer, release_id=row.get("release_id"),
                       problem=problem)
        return None
    return [{"exemplar_id": e["exemplar_id"], "channel": e["channel"], "position": int(e["position"]),
             "masked_text": e["masked_text"]} for e in links]


async def never_in_release(conn, release_id: str) -> list[str]:
    """Ids of the release's examples an admin marked `never` since the draft (the API refuses a first activation)."""
    return [e["exemplar_id"] for e in await release_exemplars(conn, release_id) if e["status"] == "never"]


# ── reads for the admin page ───────────────────────────────────────────────────────────────────────────────────────

def serialise(row: dict) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(row).items()}


__all__ = ["EXEMPLAR_MAX_WORDS", "EXEMPLAR_WRITERS", "ExemplarError", "PER_CHANNEL", "STATUSES", "T2_CHANNEL",
           "channel_for", "excerpt", "exemplar_problem", "get", "list_pool", "load_release_exemplars",
           "masked_excerpt", "never_in_release", "owner_pool", "pick", "pin_into_release", "refresh",
           "release_exemplars", "release_sha256", "select_for_release", "serialise", "set_status"]
