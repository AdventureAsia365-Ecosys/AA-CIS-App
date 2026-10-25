"""AA-917 WM-10 — backfill shared.writing_signal from the learning material that already exists (app ADR 0010).

Four sources, each row keyed so a re-run inserts nothing (`locator.backfill`, unique per source row):

  audit    admin sentence cut / rewrite / edit of a T9 piece or a T2 version written BEFORE AA-916
           (acp_shared.audit_log `content_piece.admin_sentence_*` / `tenant_tour_version.admin_sentence_*` whose details
           still hold `before`; rows since AA-916 name their signal instead). The text is copied; the legacy audit copy
           stays (D16 retention exception, AI Use Register note). audit_log.tenant_id (varchar(50)) is cast to uuid.
  t2_edit  a tenant's T2 edit: a `tenant_edit` version against its parent, one signal per changed top-level field
           of `rewritten_content`.
  t9_attempt  every T9 attempt that failed a gate and was rewritten (content_piece.repair_log entries with a registered
           gate), with the discarded attempt's text when content_piece.discarded_attempts kept it: a system signal
           (action regenerate, actor system).
  a1_lesson   the A1 brand audit's lessons (silver_aa_internal.quality_scores.lessons_extracted: pattern, failure
           code, field, example): a system signal on the generated_content row (action reject).

Scope follows `signals.scope_for_tenant` like the live capture: the AA master tenant's rows are platform, any other
tenant's are tenant scope (ADR 0010 decision 10: platform rows never hold tenant text). Signals keep the time of the
original row (created_at). Every new signal starts `pending` (the fact filter runs next); an admin `fact_not_in_source`
does not exist in legacy rows.

Idempotent, insert-only, $0. Runs as the `writing_signal_backfill` job (services/jobs/writing_memory_job.py); it also
works as a sweep: new T2 tenant edits, T9 attempts and A1 lessons since the last run are picked up the same way.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Optional
from uuid import UUID

import asyncpg
import structlog

from services.quality import registry as qr
from services.writing_memory import signals as wm

logger = structlog.get_logger()

SOURCES = ("audit", "t2_edit", "t9_attempt", "a1_lesson")
_MODE_BY_SUFFIX = {"cut": "sentence_cut", "rewritten": "sentence_rewrite", "edited": "sentence_manual"}
_AUDIT_PREFIXES = ("content_piece.admin_sentence_", "tenant_tour_version.admin_sentence_")

_EXISTING_SQL = "SELECT locator ->> 'backfill' FROM shared.writing_signal WHERE locator ? 'backfill'"

_AUDIT_SQL = """
    SELECT al.id::text AS audit_id, al.tenant_id, al.actor, al.action, al.resource_type, al.resource_id, al.details,
           al.created_at
      FROM acp_shared.audit_log al
     WHERE (al.action LIKE 'content\\_piece.admin\\_sentence\\_%'
            OR al.action LIKE 'tenant\\_tour\\_version.admin\\_sentence\\_%')
       AND al.details ? 'before' AND NOT al.details ? 'signal_id'
     ORDER BY al.created_at, al.id
"""
_CHANNELS_SQL = ("SELECT piece_id::text AS piece_id, channel FROM acp_shared.content_piece "
                 "WHERE piece_id = ANY($1::uuid[])")
_T2_SQL = """
    SELECT c.id::text AS id, c.tenant_id::text AS tenant_id, c.rewritten_content AS after_rc,
           p.rewritten_content AS before_rc, coalesce(c.edited_at, c.created_at) AS at
      FROM gold_aa_internal.tenant_tour_versions c
      JOIN gold_aa_internal.tenant_tour_versions p ON p.id = c.parent_version_id AND p.tenant_id = c.tenant_id
     WHERE c.edit_source = 'tenant_edit'
     ORDER BY c.created_at, c.id
"""
_T9_SQL = """
    SELECT piece_id::text AS piece_id, tenant_id::text AS tenant_id, channel, repair_log, discarded_attempts,
           created_at
      FROM acp_shared.content_piece
     WHERE jsonb_typeof(repair_log) = 'array' AND jsonb_array_length(repair_log) > 0
     ORDER BY created_at, piece_id
"""
_LESSON_SQL = """
    SELECT qs.id::text AS id, qs.generated_content_id::text AS content_id, qs.tenant_id::text AS tenant_id,
           qs.lessons_extracted, qs.evaluated_at
      FROM silver_aa_internal.quality_scores qs
     WHERE jsonb_typeof(qs.lessons_extracted) = 'array' AND jsonb_array_length(qs.lessons_extracted) > 0
     ORDER BY qs.evaluated_at, qs.id
"""


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _text(value) -> Optional[str]:
    """A field value as signal text: a string as is, anything else as canonical JSON (the A1 PATCH's rule)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _uuid(value) -> Optional[str]:
    try:
        return str(UUID(str(value))) if value is not None else None
    except (ValueError, TypeError):
        return None


def _cap(text: Optional[str], n: int) -> Optional[str]:
    return text if text is None or len(text) <= n else text[: n - 1] + "…"


def audit_signals(row: dict) -> list[dict]:
    """One signal (record_writer_feedback kwargs) for a legacy admin sentence audit row, [] when unusable."""
    d = _json(row["details"]) or {}
    if not isinstance(d, dict) or "before" not in d:
        return []
    tenant = _uuid(row["tenant_id"])
    if tenant is None:
        return []                                         # audit_log.tenant_id that is not a uuid: cannot scope it
    mode = _MODE_BY_SUFFIX.get(str(row["action"]).rsplit("_", 1)[-1])
    if mode is None:
        return []
    scope, owner = wm.scope_for_tenant(tenant)
    piece = row["resource_type"] == "content_piece"
    locator: dict[str, Any] = {k: d[k] for k in ("start", "end", "field", "index") if d.get(k) is not None}
    locator["backfill"] = f"audit:{row['audit_id']}"
    actor = str(row["actor"] or "")
    return [dict(
        writer=wm.writer_for_channel(row.get("channel")) if piece else "t2",
        channel=(row.get("channel") or "blog") if piece else "tour",
        scope=scope, tenant_id=owner, field="content_text" if piece else _cap(d.get("field"), 200),
        locator=locator, before_text=_text(d.get("before")) or "", after_text=_text(d.get("after")) or "",
        gate=_cap(d.get("gate"), 128), problem=_cap(_text(d.get("problem")), wm.MAX_PROBLEM), action=mode,
        actor_type="admin", actor_id=wm.admin_actor_id(actor.split(":", 1)[1] if ":" in actor else None),
        audit_log_id=row["audit_id"], source_table="content_piece" if piece else "tenant_tour_version",
        source_id=row["resource_id"], created_at=row["created_at"])]


def t2_edit_signals(row: dict) -> list[dict]:
    """One signal per top-level field of rewritten_content the tenant changed against the parent version."""
    before, after = _json(row["before_rc"]) or {}, _json(row["after_rc"]) or {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    scope, owner = wm.scope_for_tenant(row["tenant_id"])
    out = []
    for key in sorted(set(before) | set(after)):
        b, a = _text(before.get(key)), _text(after.get(key))
        if b == a or len(str(key)) > 200:
            continue
        if (b and len(b) > wm.MAX_TEXT) or (a and len(a) > wm.MAX_TEXT):
            continue
        out.append(dict(
            writer="t2", channel="tour", scope=scope, tenant_id=owner, field=str(key),
            locator={"field": str(key), "backfill": f"t2:{row['id']}:{key}"}, before_text=b, after_text=a,
            action="tenant_edit", actor_type="tenant", actor_id=f"tenant:{row['tenant_id']}",
            source_table="tenant_tour_version", source_id=row["id"], created_at=row["at"]))
    return out


def t9_attempt_signals(row: dict) -> list[dict]:
    """One system signal per repair_log entry that names a registered gate (a failed attempt the job rewrote)."""
    log = _json(row["repair_log"]) or []
    discarded = _json(row["discarded_attempts"]) or []
    texts = {d.get("attempt_number"): d.get("content_text") for d in discarded if isinstance(d, dict)}
    scope, owner = wm.scope_for_tenant(row["tenant_id"])
    out = []
    for idx, entry in enumerate(log if isinstance(log, list) else []):
        if not isinstance(entry, dict):
            continue
        gate = entry.get("gate_targeted")
        if not gate or qr.gate(gate) is None:
            continue                                      # the regression guard, a cut summary: no gate failure
        violations = entry.get("violations") or []
        problem = "; ".join(str(v) for v in violations) if isinstance(violations, list) else str(violations)
        before = texts.get(entry.get("attempt"))
        if before is not None and len(before) > wm.MAX_TEXT:
            before = None
        out.append(dict(
            writer=wm.writer_for_channel(row["channel"]), channel=row["channel"] or "blog", scope=scope,
            tenant_id=owner, field="content_text",
            locator={"attempt": entry.get("attempt"), "backfill": f"t9:{row['piece_id']}:{idx}"},
            before_text=before, gate=_cap(gate, 128), problem=_cap(problem, wm.MAX_PROBLEM) or None,
            action="regenerate", actor_type="system", actor_id="system:t9_write", source_table="content_piece",
            source_id=row["piece_id"], created_at=row["created_at"]))
    return out


def a1_lesson_signals(row: dict) -> list[dict]:
    """One system signal per brand-audit lesson ({pattern, failure_code, field, example_before, severity})."""
    lessons = _json(row["lessons_extracted"]) or []
    scope, owner = wm.scope_for_tenant(row["tenant_id"])
    out = []
    for idx, lesson in enumerate(lessons if isinstance(lessons, list) else []):
        if not isinstance(lesson, dict) or not (lesson.get("pattern") or lesson.get("example_before")):
            continue
        out.append(dict(
            writer="a1", channel="master", scope=scope, tenant_id=owner, field=_cap(lesson.get("field"), 200),
            locator={"index": idx, "severity": lesson.get("severity"), "backfill": f"lesson:{row['id']}:{idx}"},
            before_text=_cap(_text(lesson.get("example_before")), wm.MAX_TEXT),
            gate=_cap(lesson.get("failure_code"), 128), problem=_cap(_text(lesson.get("pattern")), wm.MAX_PROBLEM),
            action="reject", actor_type="system", actor_id="system:a1_brand_audit",
            source_table="generated_content", source_id=row["content_id"], created_at=row["evaluated_at"]))
    return out


_SOURCES = {
    "audit": (_AUDIT_SQL, audit_signals),
    "t2_edit": (_T2_SQL, t2_edit_signals),
    "t9_attempt": (_T9_SQL, t9_attempt_signals),
    "a1_lesson": (_LESSON_SQL, a1_lesson_signals),
}


async def _add_channels(pool, rows: list[dict]) -> None:
    """The T9 channel of each content_piece audit row (writer t9_blog / t9_social); unknown -> None (blog)."""
    ids = sorted({_uuid(r["resource_id"]) for r in rows if r["resource_type"] == "content_piece"} - {None})
    channels: dict[str, Optional[str]] = {}
    if ids:
        try:
            async with pool.acquire() as conn, conn.transaction():
                channels = {r["piece_id"]: r["channel"] for r in await conn.fetch(_CHANNELS_SQL, ids)}
        except asyncpg.PostgresError as e:     # no channel column in this DB: every piece counts as a blog
            logger.warning("aa917_backfill_channels_missing", error=str(e)[:200])
    for r in rows:
        r["channel"] = channels.get(_uuid(r["resource_id"]) or "")


async def run_backfill(pool, *, sources=SOURCES, dry_run: bool = False) -> dict:
    """Insert every signal of `sources` not inserted before. Returns counts per source (inserted, existing, skipped
    rows) and per tenant ('platform' for platform rows). `dry_run` counts without inserting."""
    async with pool.acquire() as conn:
        existing = {r[0] for r in await conn.fetch(_EXISTING_SQL)}
    per_source: dict[str, Counter] = {}
    per_tenant: dict[str, Counter] = defaultdict(Counter)
    for name in sources:
        sql, build = _SOURCES[name]
        counts: Counter = Counter()
        try:
            async with pool.acquire() as conn, conn.transaction():   # a savepoint when the caller holds one
                rows = [dict(r) for r in await conn.fetch(sql)]
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as e:   # a source this DB does not have
            per_source[name] = Counter({"missing_table": 1})
            logger.warning("aa917_backfill_source_missing", source=name, error=str(e)[:200])
            continue
        if name == "audit":
            await _add_channels(pool, rows)
        for row in rows:
            kws = build(row)
            if not kws:
                counts["rows_skipped"] += 1
                continue
            for kw in kws:
                key = kw["locator"]["backfill"]
                if key in existing:
                    counts["existing"] += 1
                    continue
                if not dry_run:
                    async with pool.acquire() as conn:
                        await wm.record_writer_feedback(conn, **kw)
                existing.add(key)
                counts["inserted"] += 1
                per_tenant[kw["tenant_id"] or "platform"][name] += 1
        per_source[name] = counts
        logger.info("aa917_backfill_source_done", source=name, dry_run=dry_run, **dict(counts))
    return {"dry_run": dry_run,
            "per_source": {k: dict(v) for k, v in per_source.items()},
            "per_tenant": {k: dict(v) for k, v in sorted(per_tenant.items())},
            "inserted": sum(c.get("inserted", 0) for c in per_source.values())}


__all__ = ["SOURCES", "a1_lesson_signals", "audit_signals", "run_backfill", "t2_edit_signals", "t9_attempt_signals"]
