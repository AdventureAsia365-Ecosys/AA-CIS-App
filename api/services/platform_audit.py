"""AA-884 (S241 GĐ8) — read side of the staff audit trail: list rows and verify the hash chain (CIS 248).

- ``list_audit(pool, filters, cursor, limit)``: newest first, keyset on (created_at, id), staff scope by default
  (actor_type staff/machine or an ``admin:`` actor). Joins ``shared.admin_users`` for a display name / email when the
  actor is ``admin:<uuid>``; never selects password_hash or cognito_*.
- ``verify_chain(pool, from_seq=None)``: recomputes every chained row's ``row_hash`` in Python with EXACTLY the
  canonical text of ``acp_shared.audit_log_chain()`` (migration 248) and checks each ``prev_hash`` against the row
  before it. A missing ``chain_seq`` (a deleted row) or a changed column breaks it. The recompute is independent of
  the trigger function on purpose: replacing the function does not make old rows verify.

The canonical text (change it only together with a new migration that changes the trigger, never alone):
  prev_hash | id | chain_seq | tenant_id | actor | actor_type | action | resource_type | resource_id | details::text |
  created_at (UTC, YYYY-MM-DDTHH:MM:SS.ffffffZ) | http_method | route | status      (NULL -> '', sha256 hex, UTF-8)
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

MAX_LIMIT = 200
VERIFY_CACHE_KEY = "admin_platform_audit_verify:v1"
VERIFY_CACHE_TTL_S = 60
VERIFY_BATCH = 2000

_COLS = """a.id, a.tenant_id, a.actor, a.actor_type::text AS actor_type, a.action, a.resource_type, a.resource_id,
           a.details::text AS details_text, a.created_at, a.http_method, a.route, a.status, a.chain_seq,
           a.prev_hash, a.row_hash"""


def canonical_text(r: Any) -> str:
    created = r["created_at"]
    created_s = created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if created else ""
    parts = [r["prev_hash"] or "", str(r["id"]), str(r["chain_seq"]), r["tenant_id"] or "", r["actor"] or "",
             r["actor_type"] or "", r["action"] or "", r["resource_type"] or "", r["resource_id"] or "",
             r["details_text"] or "", created_s, r["http_method"] or "", r["route"] or "",
             "" if r["status"] is None else str(r["status"])]
    return "|".join(parts)


def row_hash(r: Any) -> str:
    return hashlib.sha256(canonical_text(r).encode("utf-8")).hexdigest()


# ── list ────────────────────────────────────────────────────────────────────────────────────────

def encode_cursor(created_at: datetime, row_id: Any) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        pad = "=" * (-len(cursor) % 4)
        created, row_id = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
        return datetime.fromisoformat(created), str(row_id)
    except Exception:
        raise ValueError("invalid cursor")


def build_list_query(*, scope: str = "staff", actor: Optional[str] = None, action: Optional[str] = None,
                     resource_type: Optional[str] = None, resource_id: Optional[str] = None,
                     route: Optional[str] = None, since: Optional[datetime] = None, until: Optional[datetime] = None,
                     cursor: Optional[str] = None, limit: int = 50) -> tuple[str, list]:
    """The SQL + args for one page. Every filter is a bind parameter; ``action`` is a prefix."""
    where: list[str] = []
    args: list[Any] = []

    def p(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    if scope == "staff":
        where.append("(a.actor_type::text IN ('staff', 'machine') OR a.actor LIKE 'admin:%')")
    elif scope != "all":
        raise ValueError("scope must be staff or all")
    if actor:
        where.append(f"a.actor = {p(actor)}")
    if action:
        pattern = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(f"a.action LIKE {p(pattern)}")
    if resource_type:
        where.append(f"a.resource_type = {p(resource_type)}")
    if resource_id:
        where.append(f"a.resource_id = {p(resource_id)}")
    if route:
        where.append(f"a.route = {p(route)}")
    if since:
        where.append(f"a.created_at >= {p(since)}")
    if until:
        where.append(f"a.created_at < {p(until)}")
    if cursor:
        c_at, c_id = decode_cursor(cursor)
        where.append(f"(a.created_at, a.id) < ({p(c_at)}, {p(c_id)}::uuid)")
    limit = max(1, min(int(limit), MAX_LIMIT))
    sql = f"""
        SELECT {_COLS},
               u.username AS actor_username, u.display_name AS actor_display_name, u.email AS actor_email
          FROM acp_shared.audit_log a
          LEFT JOIN shared.admin_users u
            ON a.actor ~ '^admin:[0-9a-fA-F-]{{36}}$' AND u.id = substr(a.actor, 7)::uuid
         {"WHERE " + " AND ".join(where) if where else ""}
         ORDER BY a.created_at DESC NULLS LAST, a.id DESC
         LIMIT {p(limit + 1)}
    """
    return sql, args


def _out(r: Any) -> dict:
    details: Any = r["details_text"]
    try:
        details = json.loads(details) if details is not None else None
    except ValueError:
        pass
    return {
        "id": str(r["id"]), "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "tenant_id": r["tenant_id"], "actor": r["actor"], "actor_type": r["actor_type"],
        "actor_name": r["actor_display_name"] or r["actor_username"], "actor_email": r["actor_email"],
        "action": r["action"], "resource_type": r["resource_type"], "resource_id": r["resource_id"],
        "http_method": r["http_method"], "route": r["route"], "status": r["status"], "details": details,
        "chain_seq": r["chain_seq"], "row_hash": r["row_hash"],
    }


async def list_audit(pool: Any, **filters: Any) -> dict:
    limit = max(1, min(int(filters.get("limit") or 50), MAX_LIMIT))
    sql, args = build_list_query(**{**filters, "limit": limit})
    rows = await pool.fetch(sql, *args)
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1]["created_at"], page[-1]["id"]) if len(rows) > limit and page else None
    return {"rows": [_out(r) for r in page], "next_cursor": next_cursor, "limit": limit}


# ── verify ──────────────────────────────────────────────────────────────────────────────────────

_VERIFY_SQL = f"""
    SELECT {_COLS} FROM acp_shared.audit_log a
     WHERE a.chain_seq IS NOT NULL AND a.chain_seq > $1
     ORDER BY a.chain_seq
     LIMIT $2
"""


async def verify_chain(pool: Any, from_seq: Optional[int] = None) -> dict:
    """{checked, ok, first_bad_seq, problem, last_seq, from_seq, pre_chain_rows}. Reads in batches of
    VERIFY_BATCH by chain_seq. ``from_seq`` starts at that row: its own prev_hash is taken as given."""
    checked, last_seq, prev_hash = 0, None, None
    start = (from_seq - 1) if from_seq else 0
    cursor = start
    first = True
    while True:
        rows = await pool.fetch(_VERIFY_SQL, cursor, VERIFY_BATCH)
        if not rows:
            break
        for r in rows:
            seq = r["chain_seq"]
            if first:
                expected_seq = from_seq if from_seq else 1
                if seq != expected_seq:
                    return _verdict(checked, False, seq, f"chain starts at {seq}, expected {expected_seq}",
                                    last_seq, from_seq)
                if from_seq is None and r["prev_hash"] is not None:
                    return _verdict(checked, False, seq, "genesis row has a prev_hash", last_seq, from_seq)
            else:
                if seq != last_seq + 1:
                    return _verdict(checked, False, last_seq + 1, f"rows {last_seq + 1}..{seq - 1} are missing",
                                    last_seq, from_seq)
                if r["prev_hash"] != prev_hash:
                    return _verdict(checked, False, seq, "prev_hash does not match the previous row_hash",
                                    last_seq, from_seq)
            if row_hash(r) != r["row_hash"]:
                return _verdict(checked, False, seq, "row_hash does not match the row's content",
                                last_seq, from_seq)
            checked += 1
            last_seq, prev_hash, first = seq, r["row_hash"], False
        cursor = last_seq
        if len(rows) < VERIFY_BATCH:
            break
    return _verdict(checked, True, None, None, last_seq, from_seq)


def _verdict(checked: int, ok: bool, bad: Optional[int], problem: Optional[str], last_seq: Optional[int],
             from_seq: Optional[int]) -> dict:
    return {"checked": checked, "ok": ok, "first_bad_seq": bad, "problem": problem, "last_seq": last_seq,
            "from_seq": from_seq}


async def verify_chain_cached(pool: Any, redis: Any = None, from_seq: Optional[int] = None) -> dict:
    """verify_chain + the count of pre-248 rows, cached VERIFY_CACHE_TTL_S in Redis (full-chain checks only)."""
    key = VERIFY_CACHE_KEY if from_seq is None else None
    if redis is not None and key:
        try:
            cached = await redis.get(key)
            if cached:
                return {**json.loads(cached), "_cached": True}
        except Exception as e:
            logger.warning("platform_audit_verify_cache_read_failed", error=repr(e)[:200])
    result = await verify_chain(pool, from_seq)
    result["pre_chain_rows"] = int(await pool.fetchval(
        "SELECT count(*) FROM acp_shared.audit_log WHERE chain_seq IS NULL") or 0)
    result["verified_at"] = datetime.now(timezone.utc).isoformat()
    if not result["ok"]:
        logger.warning("platform_audit_chain_broken", first_bad_seq=result["first_bad_seq"],
                       problem=result["problem"])
    if redis is not None and key:
        try:
            await redis.set(key, json.dumps(result, default=str), ex=VERIFY_CACHE_TTL_S)
        except Exception as e:
            logger.warning("platform_audit_verify_cache_write_failed", error=repr(e)[:200])
    return {**result, "_cached": False}
