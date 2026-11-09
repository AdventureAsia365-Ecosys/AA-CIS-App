"""shared/provider_credit.py — AA-1025 B5 (S263 gateway plan): the estimated credit of the provider connections that
have no balance API, and the low-credit alert.

Which connections. Every row of ``shared.llm_provider_connection`` whose ``balance_source`` is ``topups_minus_spend``
(migration 343: the direct vendor APIs, kinds anthropic and openai), except Jev (``typesafe``), which keeps its own
ledger and alert (shared.jev_credit, shared.jev_credit_topup).

The ledger. ``shared.provider_credit_topup`` (migration 342): an admin records each top-up (``kind = 'topup'``, money
added) and, when they read the balance on the provider's console, a balance check (``kind = 'opening_balance'``: the
balance at ``effective_at``). The estimate

    anchor        = the newest opening balance, else the first top-up
    recorded      = anchor.amount + the top-ups after it (effective_at, then id)
    spent         = the connection's spend in shared.llm_call_log (connection_key, or account for rows before the S6
                    keys) since anchor.effective_at
    estimated     = recorded - spent

so recording a fresh balance read re-anchors the estimate and drops any drift of the older entries.

The alert. ``check_low_credit`` compares each estimate with the row's ``low_balance_usd`` (admin-editable through
PATCH /admin/llm-connections/{key}; NULL = no alert). Below it: one admin bell (``platform.provider_credit.low``,
entity ``llm_connection:<key>``, at most one real bell per connection per 24 h, read or not: the estimate does not
recover on its own) and, when the bell was written, one ``ops_alert_email`` job (at most one per connection and UTC
day). Called by the api's ops sampler every CHECK_EVERY_S, after a ledger entry is added, and by
POST /admin/ai-gateway/credit/check. No entry = no estimate = no alert.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

LOW_EVENT = "platform.provider_credit.low"
ENTITY_TYPE = "llm_connection"
BALANCE_SOURCE = "topups_minus_spend"
EXCLUDED_KINDS = ("typesafe",)          # Jev: shared/jev_credit.py
KINDS = ("topup", "opening_balance")
CHECK_EVERY_S = 1800.0                  # the api sampler's schedule (same as the Jev credit check)
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"
ALERT_ROLES = ["admin"]
_ALERT_LOCK_KEY = 1_025_000_343        # serialises the check-then-insert across tasks (the AA-756 race)
MAX_ENTRIES = 200                       # entries returned per connection (newest first)

ENTRIES_SQL = """
    SELECT id, connection_key, kind, effective_at, amount_usd::float AS amount_usd, note, created_by, created_at
      FROM shared.provider_credit_topup
     WHERE connection_key = $1
     ORDER BY effective_at, id
"""
# The connection's spend since the anchor: rows carry connection_key since the S6 keys, account before that.
SPEND_SQL = """
    SELECT coalesce(sum(cost_usd), 0)::float AS spent
      FROM shared.llm_call_log
     WHERE created_at >= $2 AND coalesce(connection_key, account) = $1
"""
INSERT_SQL = """
    INSERT INTO shared.provider_credit_topup (connection_key, kind, effective_at, amount_usd, note, created_by)
    VALUES ($1, $2, coalesce($3, now()), $4, $5, $6)
    RETURNING id, connection_key, kind, effective_at, amount_usd::float AS amount_usd, note, created_by, created_at
"""
DELETE_SQL = """
    DELETE FROM shared.provider_credit_topup WHERE id = $1
    RETURNING id, connection_key, kind, effective_at, amount_usd::float AS amount_usd
"""
LOW_ALERT_RECENT_SQL = """
    SELECT 1 FROM shared.notifications
     WHERE event_type = $1 AND entity_id = $2 AND created_at >= now() - interval '24 hours'
     LIMIT 1
"""
LOW_ALERT_INSERT_SQL = """
    INSERT INTO shared.notifications
        (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, $3, $4, $5::jsonb, $6::text[])
"""
LAST_LOW_ALERT_SQL = """
    SELECT entity_id, max(created_at) AS last_at FROM shared.notifications
     WHERE event_type = $1 GROUP BY entity_id
"""


class LedgerUnavailable(RuntimeError):
    """shared.provider_credit_topup does not exist yet (migration 342 not applied)."""


def entity_id(connection_key: str) -> str:
    return f"{ENTITY_TYPE}:{connection_key}"


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


# ── the estimate (pure) ──────────────────────────────────────────────────────────────────────────────────────────

def anchor_of(entries: list[dict]) -> tuple[Optional[dict], list[dict]]:
    """(the anchor entry, the top-ups after it) of entries in (effective_at, id) order. The anchor is the newest
    opening balance, else the first top-up; no entry = (None, [])."""
    if not entries:
        return None, []
    idx = max((i for i, e in enumerate(entries) if e.get("kind") == "opening_balance"), default=0)
    return entries[idx], [e for e in entries[idx + 1:] if e.get("kind") == "topup"]


def estimate_from(entries: list[dict], spent: Optional[float]) -> dict:
    """{recorded_usd, since, spent_usd, estimated_left_usd} (all None without an entry). Pure."""
    anchor, after = anchor_of(entries)
    if anchor is None:
        return {"recorded_usd": None, "since": None, "spent_usd": None, "estimated_left_usd": None,
                "anchor_kind": None}
    recorded = round(float(anchor["amount_usd"]) + sum(float(e["amount_usd"]) for e in after), 4)
    spent_f = round(float(spent or 0.0), 6)
    return {"recorded_usd": recorded, "since": _iso(anchor["effective_at"]), "spent_usd": spent_f,
            "estimated_left_usd": round(recorded - spent_f, 4), "anchor_kind": anchor.get("kind")}


def is_below(estimated_left: Optional[float], threshold_usd: Optional[float]) -> bool:
    """True only for a known estimate strictly below a set threshold."""
    return estimated_left is not None and threshold_usd is not None and estimated_left < float(threshold_usd)


def low_message(label: str, estimated_left: float, threshold_usd: float) -> str:
    return (f"{label} credit is running low: an estimated ${estimated_left:.2f} is left, below the "
            f"${threshold_usd:.2f} alert threshold. Top up the account with the provider and record it in AI "
            "providers › Credit. The estimate is the recorded balance minus the spend logged since then (the "
            "provider has no balance API the gateway may call).")


def ledger_connection(c) -> bool:
    """Whether a registry row (connections.ProviderConnection) keeps its balance in this ledger."""
    return getattr(c, "balance_source", None) == BALANCE_SOURCE and getattr(c, "kind", None) not in EXCLUDED_KINDS


# ── reads ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def ledger_connections(conn) -> list:
    """The registry rows that keep their balance in this ledger, in key order (``conn``: asyncpg connection)."""
    from shared.llm_client import connections as conn_registry

    rows = await conn_registry.fetch_connections(conn, llm_only=False)
    return [rows[k] for k in sorted(rows) if ledger_connection(rows[k])]


def _entry(r) -> dict:
    d = dict(r)
    d["effective_at"] = _iso(d.get("effective_at"))
    d["created_at"] = _iso(d.get("created_at"))
    d["id"] = int(d["id"])
    return d


async def entries_of(conn, connection_key: str) -> list[dict]:
    try:
        return [dict(r) for r in await conn.fetch(ENTRIES_SQL, connection_key)]
    except Exception as e:
        if type(e).__name__ == "UndefinedTableError":
            raise LedgerUnavailable("the credit ledger needs migration 342") from None
        raise


async def estimate(conn, connection_key: str, entries: Optional[list[dict]] = None) -> dict:
    """The estimate of one connection (reads its entries unless given, and its spend since the anchor)."""
    entries = await entries_of(conn, connection_key) if entries is None else entries
    anchor, _ = anchor_of(entries)
    spent = None
    if anchor is not None:
        row = await conn.fetchrow(SPEND_SQL, connection_key, anchor["effective_at"])
        spent = float(row["spent"]) if row else 0.0
    return estimate_from(entries, spent)


async def read_all(conn) -> list[dict]:
    """GET /admin/ai-gateway/credit: every ledger connection with its entries (newest first), estimate, threshold
    and last low-credit bell. Raises LedgerUnavailable before migration 342."""
    last = {r["entity_id"]: r["last_at"] for r in await conn.fetch(LAST_LOW_ALERT_SQL, LOW_EVENT)}
    out = []
    for c in await ledger_connections(conn):
        entries = await entries_of(conn, c.connection_key)
        est = await estimate(conn, c.connection_key, entries)
        threshold = float(c.low_balance_usd) if c.low_balance_usd is not None else None
        out.append({"connection_key": c.connection_key, "label": c.label, "kind": c.kind, "enabled": c.enabled,
                    "low_balance_usd": threshold, **est,
                    "below_threshold": is_below(est["estimated_left_usd"], threshold),
                    "last_low_alert_at": _iso(last.get(entity_id(c.connection_key))),
                    "entries": [_entry(e) for e in reversed(entries)][:MAX_ENTRIES]})
    return out


async def balances(db) -> dict[str, dict]:
    """{connection_key: the Providers page ``balance`` block} for every ledger connection (``db``: pool or
    connection). Raises on a failed read (the page guards it); before migration 342 every ledger connection has an
    empty estimate."""
    async def run(conn):
        out = {}
        for c in await ledger_connections(conn):
            threshold = float(c.low_balance_usd) if c.low_balance_usd is not None else None
            try:
                est = await estimate(conn, c.connection_key)
            except LedgerUnavailable:
                est = estimate_from([], None)
            out[c.connection_key] = balance_block(est, threshold)
        return out
    if hasattr(db, "acquire"):
        async with db.acquire() as conn:
            return await run(conn)
    return await run(db)


def balance_block(est: dict, threshold: Optional[float]) -> dict:
    """The ``balance`` of a provider row (the Jev row's shape, lib/aiGateway.ts ProviderBalance). Pure."""
    left = est.get("estimated_left_usd")
    return {"usd": left, "estimated": True, "threshold_usd": threshold,
            "below_threshold": is_below(left, threshold), "as_of": None, "source": "topups_minus_logged_spend",
            "topped_up_usd": est.get("recorded_usd"), "since": est.get("since")}


# ── writes ───────────────────────────────────────────────────────────────────────────────────────────────────────

async def add_entry(conn, connection_key: str, *, kind: str, amount_usd: float, effective_at: Optional[datetime],
                    note: Optional[str], actor: str) -> dict:
    """Insert one entry (the caller audits it in the same transaction). Raises LookupError when the connection does
    not keep its balance here, ValueError for a bad kind / amount, LedgerUnavailable before migration 342."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if amount_usd < 0 or (kind == "topup" and amount_usd <= 0):
        raise ValueError("a top-up must be above 0 and a balance check at least 0")
    if connection_key not in {c.connection_key for c in await ledger_connections(conn)}:
        raise LookupError(connection_key)
    if effective_at is not None and effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=timezone.utc)
    try:
        row = await conn.fetchrow(INSERT_SQL, connection_key, kind, effective_at, round(float(amount_usd), 2),
                                  note, actor)
    except Exception as e:
        if type(e).__name__ == "UndefinedTableError":
            raise LedgerUnavailable("the credit ledger needs migration 342") from None
        raise
    return _entry(row)


async def delete_entry(conn, entry_id: int) -> Optional[dict]:
    """Delete one entry; None when there is no such id. Raises LedgerUnavailable before migration 342."""
    try:
        row = await conn.fetchrow(DELETE_SQL, entry_id)
    except Exception as e:
        if type(e).__name__ == "UndefinedTableError":
            raise LedgerUnavailable("the credit ledger needs migration 342") from None
        raise
    return _entry(row) if row is not None else None


# ── the low-credit alert ─────────────────────────────────────────────────────────────────────────────────────────

async def _maybe_insert_low_bell(conn, connection_key: str, payload: dict) -> bool:
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _ALERT_LOCK_KEY)
        if await conn.fetchval(LOW_ALERT_RECENT_SQL, LOW_EVENT, entity_id(connection_key)):
            return False
        await conn.execute(LOW_ALERT_INSERT_SQL, PLATFORM_TENANT_ID, LOW_EVENT, ENTITY_TYPE, entity_id(connection_key),
                           json.dumps(payload), ALERT_ROLES)
    return True


async def check_connection(conn, c, *, source: str, now: Optional[datetime] = None) -> dict:
    """One connection: estimate, compare, and below the threshold the throttled bell + e-mail. A failed bell or
    e-mail is logged and reported as alerted False / email_job_id None."""
    from shared import ops_alerts   # lazy: ops_alerts -> ses_mailer

    est = await estimate(conn, c.connection_key)
    threshold = float(c.low_balance_usd) if c.low_balance_usd is not None else None
    left = est["estimated_left_usd"]
    below = is_below(left, threshold)
    alerted, email_job_id = False, None
    if below:
        details = {"connection_key": c.connection_key, "label": c.label, "estimated_left_usd": round(left, 2),
                   "threshold_usd": threshold, "recorded_usd": est["recorded_usd"], "spent_usd": est["spent_usd"],
                   "since": est["since"], "source": source,
                   "message": low_message(c.label, left, threshold)}
        try:
            alerted = await _maybe_insert_low_bell(conn, c.connection_key, details)
        except Exception as e:  # noqa: BLE001 — the bell is best effort, the result still says below
            logger.warning("provider_credit_low_alert_insert_failed", connection=c.connection_key,
                           error=f"{type(e).__name__}: {e}"[:200])
        if alerted:
            try:
                email_job_id = await ops_alerts.enqueue_provider_credit_email(conn, details=details, now=now)
            except Exception as e:  # noqa: BLE001 — the bell is written; the mail is a second channel
                logger.warning("provider_credit_low_email_enqueue_failed", connection=c.connection_key,
                               error=f"{type(e).__name__}: {e}"[:200])
    (logger.warning if alerted else logger.info)(
        "provider_credit_low_alert" if alerted else "provider_credit_checked", connection=c.connection_key,
        source=source, estimated_left_usd=left, threshold_usd=threshold, below=below, email_job_id=email_job_id)
    return {"connection_key": c.connection_key, **est, "threshold_usd": threshold, "below_threshold": below,
            "alerted": alerted, "email_job_id": email_job_id}


async def check_low_credit(pool, *, source: str, connection_key: Optional[str] = None,
                           now: Optional[datetime] = None) -> list[dict]:
    """Every ledger connection (or only ``connection_key``): ``check_connection``. Raises LedgerUnavailable before
    migration 342 and on a failed registry read; one connection's failed estimate is logged and skipped."""
    out = []
    async with pool.acquire() as conn:
        for c in await ledger_connections(conn):
            if connection_key is not None and c.connection_key != connection_key:
                continue
            try:
                out.append(await check_connection(conn, c, source=source, now=now))
            except LedgerUnavailable:
                raise
            except Exception as e:  # noqa: BLE001 — the other connections are still checked
                logger.warning("provider_credit_check_error", connection=c.connection_key,
                               error=f"{type(e).__name__}: {e}"[:200])
    return out
