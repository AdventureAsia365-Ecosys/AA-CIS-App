"""AA-917 WM-09 — reads and writes of shared.writing_rule (CIS 280, app ADR 0010).

Isolation (decision 10): `cis_app` sees every row (parity policy), so every read that serves one owner filters
`scope` and `tenant_id` explicitly — `rules_for(conn, writer, tenant_id)` returns the platform rules plus that tenant's
own, never another tenant's. The tenant pool has no privilege on the table.

Writes: `insert_candidate` (the miner; one open rule per cluster, a re-run is a no-op), `review` (the admin: edit /
reject; approve needs a human id and never comes from a job — nothing auto-activates), `expire_stale` (candidates and
approved rules past `expires_at`; an active rule only leaves through a release, AA-919, or an L0 rollback).
AA-918: `store_r0` (the R0 replay result in `lint.r0`), `activate_l0` / `rollback` (the L0 lexicon fast path: one admin
click each, no release).
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional
from uuid import UUID

from services.writing_memory import config as cfg
from services.writing_memory.signals import PLATFORM_TENANT_ID, WRITERS

KINDS = ("lexicon", "writer_instruction", "repair_hint", "preference_summary")
STATUSES = ("candidate", "rejected", "approved", "active", "expired")
OPEN_STATUSES = ("candidate", "approved", "active")
MAX_SIGNAL_IDS = 500             # cardinality(signal_ids) <= 500 (CIS 280)

_COLUMNS = """
    rule_id::text AS rule_id, kind, scope, tenant_id::text AS tenant_id, writer, applies_to, reason_code, gate_class,
    cluster_key, rule_text, replacement, match_phrases, token_estimate, status, signal_ids::text[] AS signal_ids,
    support_signals, support_pieces, support_tenants, support_reviewers, lint, drafted_by, mine_job_id::text AS
    mine_job_id, expires_at, approved_by, approved_at, rejected_by, rejected_reason, created_at, updated_at
"""

_INSERT_SQL = """
    INSERT INTO shared.writing_rule
        (kind, scope, tenant_id, writer, applies_to, reason_code, gate_class, cluster_key, rule_text, replacement,
         match_phrases, status, signal_ids, support_signals, support_pieces, support_tenants, support_reviewers, lint,
         drafted_by, mine_job_id, rejected_by, rejected_reason, expires_at)
    VALUES ($1, $2, $3::uuid, $4, $5::text[], $6, $7, $8, $9, $10, $11::text[], $12, $13::uuid[], $14, $15, $16, $17,
            $18::jsonb, $19, $20::uuid, $21, $22, now() + make_interval(days => $23))
    ON CONFLICT (writer, scope, coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), cluster_key)
        WHERE status IN ('candidate', 'approved', 'active')
    DO NOTHING
    RETURNING rule_id::text
"""


def _uuid(value) -> Optional[str]:
    try:
        return str(UUID(str(value))) if value is not None else None
    except (ValueError, TypeError):
        return None


def owner_filter(tenant_id) -> tuple[str, Optional[str]]:
    """(scope, tenant) of one owner: None or the AA master tenant = the platform."""
    t = _uuid(tenant_id)
    return ("platform", None) if t is None or t == PLATFORM_TENANT_ID else ("tenant", t)


REDACTED_TEXT = "[redacted: the draft shared text with a tenant's text or named a tenant brand]"
# lint details that quote the rule's own text (a redacted row keeps only their ok flag)
_QUOTING_CHECKS = ("constraint_only", "lexicon_specifics")


def _clamp_words(text: str, max_words: int) -> str:
    words = text.split()
    return " ".join(words[:max_words]) if len(words) > max_words else text


def _clamp_sentences(text: str, max_sentences: int) -> str:
    """Drop the sentence marks after the max-th run of [.!?] (the CIS 280 count), keeping the words."""
    seen = 0

    def keep(m):
        nonlocal seen
        seen += 1
        return m.group(0) if seen <= max_sentences else ""
    return re.sub(r"[.!?]+", keep, text)


def fit_to_store(*, kind: str, rule_text: str, replacement: Optional[str], match_phrases: Iterable[str],
                 lint: dict) -> tuple[str, Optional[str], list[str], dict]:
    """(rule_text, replacement, match_phrases, lint) as CIS 280 accepts them for any status.

    A draft whose overlap lint found tenant text (`leak`) is stored redacted: no text, no phrases, and the quoting
    lint details dropped, so a platform row never holds tenant text (ADR 0010 decision 10). Any other draft is
    clamped to the store limits (25 words / 1000 characters; a summary 125 words and 5 sentences; replacement 200
    characters and lexicon only; 5 phrases, 400 characters together); a passing lint already keeps them, so only a
    lint-rejected draft is ever cut (`lint.clamped`)."""
    lint = dict(lint or {})
    overlap = lint.get("five_gram_overlap")
    if isinstance(overlap, dict) and overlap.get("leak"):
        for k in _QUOTING_CHECKS:
            if isinstance(lint.get(k), dict):
                lint[k] = {"ok": bool(lint[k].get("ok")), "detail": "redacted"}
        lint["redacted"] = True
        return REDACTED_TEXT, None, [], lint
    text = (rule_text or "").strip() or "[empty draft]"
    if kind == "preference_summary":
        text = _clamp_sentences(_clamp_words(text, cfg.SUMMARY_MAX_WORDS), cfg.SUMMARY_MAX_SENTENCES)
    else:
        text = _clamp_words(text, cfg.RULE_MAX_WORDS)
    text = text[: cfg.RULE_MAX_CHARS].strip() or "[empty draft]"
    rep = (replacement or None) if kind == "lexicon" else None
    rep = rep[: cfg.REPLACEMENT_MAX_CHARS] if rep else rep
    phrases, used = [], 0
    for p in list(match_phrases or [])[: cfg.MAX_MATCH_PHRASES]:
        p = (p or "")[: cfg.MATCH_PHRASES_MAX_CHARS - used]
        if not p:
            break
        phrases.append(p)
        used += len(p)
    if (text, rep, phrases) != ((rule_text or "").strip(), replacement or None, list(match_phrases or [])):
        lint["clamped"] = True
    return text, rep, phrases, lint


async def insert_candidate(conn, *, kind: str, scope: str, tenant_id, writer: str, cluster_key: str, rule_text: str,
                           replacement: Optional[str] = None, match_phrases: Iterable[str] = (),
                           applies_to: Iterable[str] = (), reason_code: Optional[str] = None,
                           gate_class: Optional[str] = None, signal_ids: Iterable[str] = (),
                           support: Optional[dict] = None, lint: Optional[dict] = None,
                           drafted_by: Optional[str] = None, mine_job_id=None) -> Optional[str]:
    """INSERT one drafted rule. A rule whose lint failed is stored `rejected` (rejected_by 'lint'), so the admin sees
    it and the cluster is drafted again only when new signals join it; a passing one is a `candidate`. The text goes
    through `fit_to_store` (redacted on a tenant-text leak, else clamped to the CIS 280 limits). Returns the id, or None
    when the cluster already has an open rule."""
    if kind not in KINDS or writer not in WRITERS:
        raise ValueError(f"writing rule: kind {kind!r} / writer {writer!r}")
    if (scope == "platform") != (tenant_id is None):
        raise ValueError(f"writing rule: scope {scope!r} with tenant_id {tenant_id!r}")
    lint = lint or {}
    ok = bool(lint.get("ok"))
    failed = [k for k, v in lint.items() if isinstance(v, dict) and not v.get("ok")]
    rule_text, replacement, phrases, lint = fit_to_store(kind=kind, rule_text=rule_text, replacement=replacement,
                                                         match_phrases=match_phrases, lint=lint)
    s = support or {}
    return await conn.fetchval(
        _INSERT_SQL, kind, scope, _uuid(tenant_id), writer, list(applies_to)[:8], reason_code,
        (gate_class or None) and gate_class[:128], cluster_key[:200], rule_text, replacement,
        phrases, "candidate" if ok else "rejected", list(signal_ids)[:MAX_SIGNAL_IDS],
        int(s.get("signals", 0)), int(s.get("pieces", 0)), int(s.get("tenants", 0)), int(s.get("reviewers", 0)),
        json.dumps(lint, default=str), drafted_by, _uuid(mine_job_id), None if ok else "lint",
        None if ok else ("lint: " + ", ".join(failed))[:500], cfg.RULE_TTL_DAYS)


async def open_cluster_keys(conn) -> set[tuple[str, str, Optional[str], str]]:
    """(writer, scope, tenant_id, cluster_key) of every open rule: the miner skips those clusters."""
    rows = await conn.fetch("SELECT writer, scope, tenant_id::text AS tenant_id, cluster_key FROM shared.writing_rule "
                            "WHERE status = ANY($1::text[])", list(OPEN_STATUSES))
    return {(r["writer"], r["scope"], r["tenant_id"], r["cluster_key"]) for r in rows}


async def lint_rejected_signal_sets(conn) -> dict[tuple[str, str, Optional[str], str], frozenset]:
    """(writer, scope, tenant_id, cluster_key) -> the signal ids of the newest lint-rejected rule of that cluster: the
    miner does not pay to redraft a cluster whose evidence has not changed since its draft failed the lint."""
    rows = await conn.fetch(
        "SELECT DISTINCT ON (writer, scope, tenant_id, cluster_key) writer, scope, tenant_id::text AS tenant_id, "
        "cluster_key, signal_ids::text[] AS signal_ids FROM shared.writing_rule "
        "WHERE status = 'rejected' AND rejected_by = 'lint' "
        "ORDER BY writer, scope, tenant_id, cluster_key, created_at DESC")
    return {(r["writer"], r["scope"], r["tenant_id"], r["cluster_key"]): frozenset(r["signal_ids"] or [])
            for r in rows}


async def rules_for(conn, writer: str, tenant_id, statuses: Iterable[str] = ("active",)) -> list[dict]:
    """The rules one owner's writer may see: platform rules + the tenant's own (explicit tenant filter, decision 10).
    Platform owner (None / the AA master): platform rules only."""
    scope, tenant = owner_filter(tenant_id)
    rows = await conn.fetch(
        f"SELECT {_COLUMNS} FROM shared.writing_rule WHERE writer = $1 AND status = ANY($2::text[]) "
        "AND (scope = 'platform' OR (scope = 'tenant' AND $3::uuid IS NOT NULL AND tenant_id = $3::uuid)) "
        "ORDER BY scope DESC, created_at", writer, list(statuses), tenant)
    return [dict(r) for r in rows]


async def list_rules(conn, *, status: Optional[str] = None, writer: Optional[str] = None, scope: Optional[str] = None,
                     tenant_id=None, limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    """The admin Candidates tab: every owner, filtered (admin only, behind TENANT_READ)."""
    where, args = ["true"], []
    for col, val in (("status", status), ("writer", writer), ("scope", scope)):
        if val:
            args.append(val)
            where.append(f"{col} = ${len(args)}")
    if tenant_id:
        args.append(_uuid(tenant_id))
        where.append(f"tenant_id = ${len(args)}::uuid")
    w = " AND ".join(where)
    total = await conn.fetchval(f"SELECT count(*) FROM shared.writing_rule WHERE {w}", *args)
    rows = await conn.fetch(f"SELECT {_COLUMNS} FROM shared.writing_rule WHERE {w} ORDER BY created_at DESC, rule_id "
                            f"LIMIT {int(limit)} OFFSET {int(offset)}", *args)
    return [dict(r) for r in rows], int(total or 0)


async def get_rule(conn, rule_id: str, *, for_update: bool = False) -> Optional[dict]:
    """One rule. `for_update` (inside the caller's transaction) locks the row, so a check made on what was read holds
    until the write that depends on it (AA-918 activation: an edit cannot slip between the R0 check and the UPDATE)."""
    if _uuid(rule_id) is None:
        return None
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM shared.writing_rule WHERE rule_id = $1::uuid"
                              + (" FOR UPDATE" if for_update else ""), _uuid(rule_id))
    return dict(row) if row else None


async def edit_candidate(conn, rule_id: str, *, rule_text: str, replacement: Optional[str],
                         match_phrases: list[str], lint: dict, actor: str,
                         kind: str) -> Optional[dict]:
    """Replace the text of a candidate (the admin's edit) with its new lint result; a failing lint rejects it. The
    router refuses a leaking or oversized edit before this; `fit_to_store` is the backstop. `kind` is the row's."""
    ok = bool(lint.get("ok"))
    rule_text, replacement, match_phrases, lint = fit_to_store(kind=kind, rule_text=rule_text,
                                                               replacement=replacement, match_phrases=match_phrases,
                                                               lint=lint)
    row = await conn.fetchrow(
        f"""UPDATE shared.writing_rule
               SET rule_text = $2, replacement = $3, match_phrases = $4::text[], lint = $5::jsonb,
                   status = CASE WHEN $6 THEN 'candidate' ELSE 'rejected' END,
                   rejected_by = CASE WHEN $6 THEN NULL ELSE $7 END,
                   rejected_reason = CASE WHEN $6 THEN NULL ELSE 'lint after the admin edit' END,
                   updated_at = now()
             WHERE rule_id = $1::uuid AND status = 'candidate'
         RETURNING {_COLUMNS}""",
        _uuid(rule_id), rule_text, replacement, match_phrases, json.dumps(lint, default=str), ok, actor[:128])
    return dict(row) if row else None


async def reject(conn, rule_id: str, *, reason: str, actor: str) -> Optional[dict]:
    """A candidate or approved rule -> rejected (with the admin's reason). An active rule leaves through a release."""
    row = await conn.fetchrow(
        f"""UPDATE shared.writing_rule
               SET status = 'rejected', rejected_by = $2, rejected_reason = $3, updated_at = now()
             WHERE rule_id = $1::uuid AND status IN ('candidate', 'approved')
         RETURNING {_COLUMNS}""", _uuid(rule_id), actor[:128], reason[:500])
    return dict(row) if row else None


async def store_r0(conn, rule_id: str, r0: dict) -> bool:
    """AA-918 — keep an R0 result on the rule (`lint.r0`). updated_at is left alone: the result names the text it
    replayed (`rule_sha`), so an edit makes it stale whatever the clock says."""
    res = await conn.execute(
        "UPDATE shared.writing_rule SET lint = lint || jsonb_build_object('r0', $2::jsonb) WHERE rule_id = $1::uuid",
        _uuid(rule_id), json.dumps(r0, default=str))
    return str(res).endswith(" 1")


async def activate_l0(conn, rule_id: str, *, approved_by: str) -> Optional[dict]:
    """AA-918 (WM-16) — the one human click of an L0 rule: candidate / approved -> active, with the approver's id
    (CIS 280 writing_rule_approval_check; the token-cap trigger still applies). The caller checked the rule (lexicon,
    T9 writer, fresh passing R0) on a row it read with `get_rule(..., for_update=True)` in the same transaction, so
    the text it checked is the text that goes active. None when the row is no longer activatable."""
    row = await conn.fetchrow(
        f"""UPDATE shared.writing_rule
               SET status = 'active', approved_by = $2, approved_at = now(), updated_at = now()
             WHERE rule_id = $1::uuid AND status IN ('candidate', 'approved') AND kind = 'lexicon'
         RETURNING {_COLUMNS}""", _uuid(rule_id), approved_by[:128])
    return dict(row) if row else None


async def rollback(conn, rule_id: str, *, actor: str, reason: str) -> Optional[dict]:
    """AA-918 — the one-click rollback of an active rule: active -> rejected (`rollback: <reason>`), so the next write
    no longer gets it and the cluster may be drafted again. The approval stays on the row (who / when)."""
    row = await conn.fetchrow(
        f"""UPDATE shared.writing_rule
               SET status = 'rejected', rejected_by = $2, rejected_reason = $3, updated_at = now()
             WHERE rule_id = $1::uuid AND status = 'active'
         RETURNING {_COLUMNS}""", _uuid(rule_id), actor[:128], f"rollback: {reason}"[:500])
    return dict(row) if row else None


async def expire_stale(conn) -> int:
    """Candidates and approved rules past expires_at -> expired. Never an active rule (a release change)."""
    res = await conn.execute("UPDATE shared.writing_rule SET status = 'expired', updated_at = now() "
                             "WHERE status IN ('candidate', 'approved') AND expires_at < now()")
    return int(str(res).split()[-1] or 0)


def serialise(rule: dict) -> dict[str, Any]:
    out = {}
    for k, v in rule.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif k == "lint" and isinstance(v, str):
            v = json.loads(v)
        out[k] = v
    return out


__all__ = ["KINDS", "MAX_SIGNAL_IDS", "OPEN_STATUSES", "REDACTED_TEXT", "STATUSES", "activate_l0", "edit_candidate",
           "expire_stale", "fit_to_store", "get_rule", "insert_candidate", "lint_rejected_signal_sets", "list_rules",
           "open_cluster_keys", "owner_filter", "reject", "rollback", "rules_for", "serialise", "store_r0"]
