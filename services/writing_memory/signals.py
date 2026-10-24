"""AA-916 WM-03 — record one writing signal in `shared.writing_signal` (CIS 277, app ADR 0010).

A signal is one piece of evidence about a writer's output: the before / after text of an admin or tenant edit, or
the reason code of an admin action (reject, dismiss, regenerate). `record_writer_feedback` is the only writer of the
table: one INSERT … RETURNING on the caller's connection (inside the caller's transaction), no model call. It
refuses what the table's CHECKs would refuse, so a bad call fails here with a clear message, not as a 23514.

Text lives only in the signal row, which the AA-839 tenant erasure deletes; `acp_shared.audit_log` rows of the same
action carry only `audit_details(ref)` (the signal id and the sha256 of each text, ADR 0010 decision 11).

Isolation: on the tenant pool (aa_app_user) RLS lets the insert through only for `scope = 'tenant'` and the
connection's own `app.tenant_id`. `cis_app` sees every row, so every `cis_app` read filters `tenant_id` explicitly
(`load_texts`).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from uuid import UUID

import structlog

from services.writing_memory.reason_codes import FACT_NOT_IN_SOURCE, REASON_CODES

logger = structlog.get_logger()

WRITERS = ("a1", "t2", "t9_blog", "t9_social")
SCOPES = ("platform", "tenant")
ACTIONS = ("field_edit", "sentence_cut", "sentence_rewrite", "sentence_manual", "tenant_edit", "reject", "dismiss",
           "regenerate")
ACTOR_TYPES = ("admin", "tenant", "system")
FACT_STATUSES = ("pending", "grey", "pass", "fail")
MAX_TEXT = 200_000                       # writing_signal_size_check
MAX_PROBLEM = 2_000                      # writing_signal_size_check (the apply bodies cap it at 1000)
MAX_ACTOR_ID = 128

# The AA master tenant: its rows (A1 Master content) are platform scope; any other tenant's text is tenant scope.
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"

_INSERT_SQL = """
    INSERT INTO shared.writing_signal
        (writer, channel, scope, tenant_id, field, locator, before_text, after_text, before_sha256, after_sha256,
         gate, reason_code, action, actor_type, actor_id, fact_status, audit_log_id, source_table, source_id, problem,
         created_at)
    VALUES ($1, $2, $3, $4::uuid, $5, $6::jsonb, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17::uuid, $18, $19,
            $20, coalesce($21::timestamptz, now()))
    RETURNING signal_id
"""


@dataclass(frozen=True)
class SignalRef:
    signal_id: str
    before_sha256: Optional[str]
    after_sha256: Optional[str]


def text_sha256(text: Optional[str]) -> Optional[str]:
    """Lower-case hex sha256 of the text as UTF-8 (None for None); the value audit rows carry."""
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def scope_for_tenant(tenant_id) -> tuple[str, Optional[str]]:
    """(scope, tenant_id) for a row owned by `tenant_id`: the AA master tenant (or none) is platform scope."""
    if tenant_id is None or str(tenant_id) == PLATFORM_TENANT_ID:
        return "platform", None
    return "tenant", str(tenant_id)


def admin_actor_id(raw: Optional[str]) -> str:
    """The signal's actor_id for an admin action: the x-admin-user-id header as a canonical UUID (the shared.admin_users
    id the BFF forwards from the verified JWT), otherwise 'unknown'. The header is never stored as given: a long value
    would break writing_signal_size_check (and the admin's edit with it), and the column holds an id, never a name or
    an e-mail address."""
    try:
        return str(UUID(str(raw))) if raw else "unknown"
    except (ValueError, TypeError):
        return "unknown"


def writer_for_channel(channel: Optional[str]) -> str:
    """The T9 writer of a content piece channel: blog -> t9_blog, every social channel -> t9_social."""
    return "t9_blog" if (channel or "blog") == "blog" else "t9_social"


def _check(name: str, value, allowed: Iterable) -> None:
    if value not in allowed:
        raise ValueError(f"writing signal: {name} {value!r} is not one of {tuple(allowed)}")


async def record_writer_feedback(
    conn, *, writer: str, scope: str, tenant_id, action: str, actor_type: str, source_table: str, source_id,
    channel: Optional[str] = None, field: Optional[str] = None, locator: Optional[dict[str, Any]] = None,
    before_text: Optional[str] = None, after_text: Optional[str] = None, gate: Optional[str] = None,
    reason_code: Optional[str] = None, actor_id: Optional[str] = None, fact_status: Optional[str] = None,
    audit_log_id=None, problem: Optional[str] = None, created_at=None,
) -> SignalRef:
    """INSERT one signal on `conn` and return its id and the text hashes. Raises ValueError on an argument the
    table would refuse (a platform row naming a tenant, a tenant row without one, an unknown code …).

    `fact_status` defaults to 'fail' for reason `fact_not_in_source` (the admin says the fact is not in the source)
    and to 'pending' otherwise. `problem` is the gate's diagnostic of the edited sentence (it quotes the flagged
    text, so it is kept here and the audit row keeps only its sha256). `created_at` (AA-917 backfill only) keeps the
    time of the original edit; the live paths leave it to now()."""
    _check("writer", writer, WRITERS)
    _check("scope", scope, SCOPES)
    _check("action", action, ACTIONS)
    _check("actor_type", actor_type, ACTOR_TYPES)
    if reason_code is not None:
        _check("reason_code", reason_code, REASON_CODES)
    if fact_status is None:
        fact_status = "fail" if reason_code == FACT_NOT_IN_SOURCE else "pending"
    _check("fact_status", fact_status, FACT_STATUSES)
    if (scope == "platform") != (tenant_id is None):
        raise ValueError(f"writing signal: scope {scope!r} with tenant_id {tenant_id!r} (platform rows never name a "
                         "tenant, tenant rows always do)")
    if locator is not None and not isinstance(locator, dict):
        raise ValueError("writing signal: locator must be an object")
    for name, text, cap in (("before_text", before_text, MAX_TEXT), ("after_text", after_text, MAX_TEXT),
                            ("problem", problem, MAX_PROBLEM), ("actor_id", actor_id, MAX_ACTOR_ID)):
        if text is not None and len(text) > cap:
            raise ValueError(f"writing signal: {name} is longer than {cap} characters")
    before_h, after_h = text_sha256(before_text), text_sha256(after_text)
    signal_id = await conn.fetchval(
        _INSERT_SQL, writer, channel, scope, str(tenant_id) if tenant_id is not None else None, field,
        json.dumps(locator) if locator is not None else None, before_text, after_text, before_h, after_h, gate,
        reason_code, action, actor_type, actor_id, fact_status, str(audit_log_id) if audit_log_id else None,
        source_table, str(source_id), problem, created_at)
    return SignalRef(str(signal_id), before_h, after_h)


async def record_writer_feedback_safe(conn, *, event: str = "aa916_signal_failed", **kw) -> Optional[SignalRef]:
    """Best-effort `record_writer_feedback` for a path whose own action must not fail on a lost signal (the tenant
    edit, the reason-only admin signals): the insert runs in a nested transaction (a SAVEPOINT when `conn` is
    already in one, so the caller's transaction and its `app.tenant_id` GUC survive a failure), and any error is
    logged as `event` and returns None."""
    try:
        async with conn.transaction():
            return await record_writer_feedback(conn, **kw)
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.warning(event, writer=kw.get("writer"), action=kw.get("action"),
                       source_table=kw.get("source_table"), source_id=str(kw.get("source_id")),
                       error_type=type(exc).__name__, error=str(exc)[:200])
        return None


def audit_details(ref: Optional[SignalRef], reason_code: Optional[str] = None) -> dict[str, Any]:
    """What an audit_log row of the same action keeps instead of the text: ids, hashes, the reason chip."""
    out: dict[str, Any] = {}
    if ref is not None:
        out.update({"signal_id": ref.signal_id, "before_sha256": ref.before_sha256,
                    "after_sha256": ref.after_sha256})
    if reason_code is not None:
        out["reason_code"] = reason_code
    return out


_TEXTS_SQL = """
    SELECT signal_id::text AS signal_id, before_text, after_text, problem, reason_code, exemplar_pref, fact_status
      FROM shared.writing_signal
     WHERE signal_id = ANY($1::uuid[]) AND tenant_id IS NOT DISTINCT FROM $2::uuid
"""


async def load_texts(conn, signal_ids: Iterable[str], tenant_id) -> dict[str, dict]:
    """signal_id -> {before_text, after_text, problem, reason_code, exemplar_pref, fact_status} for the ids of ONE
    owner (`tenant_id`; None = platform rows). cis_app sees every row, so the tenant filter is explicit here (ADR 0010
    decision 10): an id of another tenant never resolves."""
    ids = []
    for s in signal_ids:
        try:
            ids.append(str(UUID(str(s))))
        except (ValueError, TypeError):
            continue
    if not ids:
        return {}
    _, owner = scope_for_tenant(tenant_id)
    rows = await conn.fetch(_TEXTS_SQL, ids, owner)
    return {r["signal_id"]: dict(r) for r in rows}


async def with_signal_texts(conn, edits: list[dict], tenant_id) -> list[dict]:
    """Admin edit history rows (audit_log, details parsed): a row written since AA-916 names its signal
    (details.signal_id) and holds no text; fill details.before / details.after / details.problem (and reason_code)
    from the signal so the drawer reads every row the same way. A legacy row keeps its own text (D16)."""
    ids = [e["details"].get("signal_id") for e in edits if isinstance(e.get("details"), dict)]
    texts = await load_texts(conn, [i for i in ids if i], tenant_id)
    out = []
    for e in edits:
        d = e.get("details") if isinstance(e.get("details"), dict) else {}
        sig = texts.get(str(d.get("signal_id"))) if d.get("signal_id") else None
        if sig is not None:
            d = {**d, "before": sig["before_text"], "after": sig["after_text"] or "",
                 **({"problem": sig["problem"]} if sig["problem"] is not None else {}),
                 **({"reason_code": sig["reason_code"]} if sig["reason_code"] else {}),
                 "exemplar_pref": sig["exemplar_pref"]}
        out.append({**e, "details": d})
    return out


__all__ = ["ACTIONS", "PLATFORM_TENANT_ID", "SignalRef", "WRITERS", "admin_actor_id", "audit_details", "load_texts",
           "record_writer_feedback", "record_writer_feedback_safe", "scope_for_tenant", "text_sha256",
           "with_signal_texts", "writer_for_channel"]
