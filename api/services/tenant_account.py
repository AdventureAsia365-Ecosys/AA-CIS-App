"""AA-761 — the tenant portal's Account pages: the read side of a tenant's own activity trail and API key facts.

- ``list_activity(pool, tenant_id, filters, page, page_size)``: the tenant's rows of ``acp_shared.audit_log``,
  newest first, offset-paged with a total (the portal's kit DataTable ``serverPagination``).
- ``export_activity_csv(pool, tenant_id, filters)``: the same filtered set as CSV (every page, not just one).
- ``api_key_facts(pool, tenant_id)``: masked key, when it was issued, and when it was last used.

Tenant scoping. ``acp_shared.audit_log`` has no RLS and no grant to the tenant role ``aa_app_user`` (schema
reference), and this lane adds no migration, so these reads run on the main pool (``cis_app``) with the tenant
id taken ONLY from the verified tenant JWT (``api.routers.v1_tours.get_tenant``) — the ``/v1/billing`` pattern
(AA-496). Every query binds that id; no route accepts a tenant id from the query string or body.

What a tenant may see. ``audit_log`` also holds rows tenants must never read: the AA admin silent corrections
(``*.admin_sentence_*``, ``AdminAuditAction`` — "tenants never read audit_log"), the staff HTTP request trail
(``staff.request``, AA-884), brand / config / membership edits by staff and the erasure record. So the read is
an ALLOWLIST of actions (``TENANT_VISIBLE_ACTIONS``), never "every row with my tenant id"; a new action is
invisible until it is added here on purpose. ``details`` is never returned raw (it holds staff before/after
values, job ids and internal plan ids): ``detail_text`` keeps a short, per-action whitelist of fields.
"""
from __future__ import annotations

import csv
import io
import json
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

# category id -> label (the Activity page's type filter, in this order)
CATEGORIES: dict[str, str] = {
    "content": "Content",
    "tours": "Tours",
    "account": "Account",
}

# action -> (category, label). The ONLY rows a tenant can read (module docstring). English: product UI.
TENANT_VISIBLE_ACTIONS: dict[str, tuple[str, str]] = {
    # the tenant's own actions (services.acp_shared.audit_log.TenantAuditAction)
    "tenant.login": ("account", "Signed in with your API key"),
    "slate.subject_picked": ("content", "Picked a topic"),
    "write.started": ("content", "Started writing"),
    "content_piece.created": ("content", "Draft created"),
    "content_piece.finished": ("content", "Draft finished"),
    "content_piece.edited": ("content", "Edited a draft"),
    "content_piece.exported": ("content", "Downloaded a post"),
    "content_piece.published": ("content", "Published a post"),
    "content_piece.unpublished": ("content", "Unpublished a post"),
    "tour.rewrite_triggered": ("tours", "Requested a tour rewrite"),
    "tenant_tour_version.exported": ("tours", "Downloaded a tour"),
    "integration.disconnected": ("account", "Disconnected a channel"),
    # what Adventure Asia changed on the account (AdminTenantAuditAction), facts the tenant is told anyway
    "agency.onboard": ("account", "Account opened"),
    "tenant.restored": ("account", "Account restored"),
    "tenant.plan_changed": ("account", "Plan changed"),
    "tenant.rate_limit_changed": ("account", "Rate limit changed"),
    "tenant.daily_publish_limit_changed": ("account", "Daily publish limit changed"),
    "tenant.status_changed": ("account", "Account status changed"),
    "tenant.key_rotated": ("account", "API key replaced"),
}

# The rows that (re)issue the key: its issue date is the newest of them.
KEY_ISSUE_ACTIONS = ("agency.onboard", "tenant.key_rotated", "tenant.restored")

MAX_PAGE_SIZE = 100
# One CSV holds at most this many rows (the newest). A tenant's whole trail is far below it on Dev (the table
# holds ~1.7k rows for every tenant together, schema reference 25/10/2026); the bound only keeps one request from
# loading an unbounded result into the api task's memory. A cut file says so in its last line.
EXPORT_MAX_ROWS = 50_000

_CHANNEL_LABELS = {"blog": "WordPress", "wordpress": "WordPress", "facebook": "Facebook", "instagram": "Instagram",
                   "tiktok": "TikTok", "email": "Email", "landing_page": "Landing page", "ads": "Ads"}
_MASK = "•" * 16
KEY_PREFIX = "cis_"


@dataclass(frozen=True)
class ActivityFilters:
    category: Optional[str] = None
    action: Optional[str] = None
    since: Optional[date] = None  # inclusive, UTC day
    until: Optional[date] = None  # inclusive, UTC day


def normalise_tenant_id(sub: Any) -> str:
    """The JWT ``sub`` as the canonical lower-case UUID text ``audit_log.tenant_id`` holds (writers store
    ``str(uuid)``). A sub that is not a UUID cannot own rows: ValueError (the route answers 403)."""
    return str(uuid.UUID(str(sub)))


# Date filters outside this window are refused (400). Without a bound, until=9999-12-31 overflowed in
# build_where (until + 1 day) and answered 500 (S251 review). No audit row predates the platform or is
# dated past 2100, so the window drops nothing real.
DATE_FILTER_MIN = date(2000, 1, 1)
DATE_FILTER_MAX = date(2100, 12, 31)


def validate_filters(f: ActivityFilters) -> None:
    if f.category is not None and f.category not in CATEGORIES:
        raise ValueError(f"unknown category '{f.category}'")
    if f.action is not None and f.action not in TENANT_VISIBLE_ACTIONS:
        raise ValueError(f"unknown action '{f.action}'")
    for name, d in (("since", f.since), ("until", f.until)):
        if d is not None and not (DATE_FILTER_MIN <= d <= DATE_FILTER_MAX):
            raise ValueError(f"'{name}' must be between {DATE_FILTER_MIN.isoformat()} and "
                             f"{DATE_FILTER_MAX.isoformat()}")
    if f.since and f.until and f.since > f.until:
        raise ValueError("'since' is after 'until'")


def visible_actions(f: ActivityFilters) -> list[str]:
    """The allowlist narrowed by the filters (always a subset of TENANT_VISIBLE_ACTIONS, possibly empty)."""
    actions = [a for a, (cat, _) in TENANT_VISIBLE_ACTIONS.items() if f.category in (None, cat)]
    if f.action is not None:
        actions = [a for a in actions if a == f.action]
    return actions


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def build_where(tenant_id: str, f: ActivityFilters) -> tuple[str, list[Any]]:
    """WHERE clause + args: own tenant AND an allowed action AND the date window. Every value is a bind."""
    where = ["a.tenant_id = $1", "a.action = ANY($2::text[])"]
    args: list[Any] = [tenant_id, visible_actions(f)]
    if f.since is not None:
        args.append(_day_start(f.since))
        where.append(f"a.created_at >= ${len(args)}")
    if f.until is not None:
        args.append(_day_start(f.until + timedelta(days=1)))
        where.append(f"a.created_at < ${len(args)}")
    return " AND ".join(where), args


def build_page_queries(tenant_id: str, f: ActivityFilters, page: int, page_size: int) -> tuple[str, str, list[Any]]:
    where, args = build_where(tenant_id, f)
    count_sql = f"SELECT count(*) FROM acp_shared.audit_log a WHERE {where}"
    args_page = [*args, page_size, (page - 1) * page_size]
    rows_sql = (
        "SELECT a.id, a.created_at, a.actor, a.action, a.resource_type, a.resource_id, a.details "
        f"FROM acp_shared.audit_log a WHERE {where} "
        f"ORDER BY a.created_at DESC, a.id DESC LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}"
    )
    return count_sql, rows_sql, args_page


def _details(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _titled(value: Any) -> str:
    s = str(value or "").replace("_", " ").strip()
    return s[:1].upper() + s[1:] if s else ""


def detail_text(action: str, raw_details: Any) -> str:
    """A short description from a per-action whitelist of ``details`` fields. Never staff ids, job ids, plan ids,
    request ids, hashes or free text written by staff."""
    d = _details(raw_details)
    channel = _CHANNEL_LABELS.get(str(d.get("channel") or d.get("integration_type") or "").lower(), "")
    if action in ("content_piece.created", "content_piece.unpublished", "content_piece.finished"):
        return channel
    if action == "content_piece.published":
        if d.get("success") is False:
            return f"{channel} · failed".strip(" ·")
        return channel
    if action == "content_piece.exported":
        fmt = str(d.get("format") or "").upper()
        return " · ".join(p for p in (channel, fmt) if p)
    if action == "integration.disconnected":
        return channel
    if action == "tenant_tour_version.exported":
        parts = [str(d.get("format") or "").upper()]
        if isinstance(d.get("version_number"), int):
            parts.append(f"v{d['version_number']}")
        return " · ".join(p for p in parts if p)
    if action == "tour.rewrite_triggered":
        return "Retry" if d.get("retry") is True else ""
    if action == "tenant.login":
        return "Content studio" if d.get("client") == "portal" else "API"
    if action == "tenant.plan_changed":
        before = (d.get("before") or {}).get("plan_tier") if isinstance(d.get("before"), dict) else None
        after = (d.get("after") or {}).get("plan_tier") if isinstance(d.get("after"), dict) else None
        if before or after:
            return f"{_titled(before) or '—'} → {_titled(after) or '—'}"
        return ""
    if action == "tenant.rate_limit_changed":
        after = d.get("after") if isinstance(d.get("after"), dict) else {}
        rpm = after.get("rate_limit_rpm")
        return f"{rpm} requests / minute" if isinstance(rpm, int) else ""
    if action == "tenant.daily_publish_limit_changed":
        after = d.get("after") if isinstance(d.get("after"), dict) else {}
        n = after.get("daily_publish_limit")
        return f"{n} posts / day" if isinstance(n, int) else "Default"
    if action == "tenant.status_changed":
        after = d.get("after")
        return _titled(after) if isinstance(after, str) else ""
    return ""


def actor_kind(actor: Optional[str]) -> str:
    """'you' (the tenant: ``tenant:<id>`` or a tenant user), 'adventure_asia' (``admin:`` staff), 'system'."""
    a = (actor or "").lower()
    if a.startswith("admin"):
        return "adventure_asia"
    if a.startswith("tenant"):
        return "you"
    return "system"


ACTOR_LABELS = {"you": "You", "adventure_asia": "Adventure Asia", "system": "Automatic"}


def row_to_item(r: Any) -> dict:
    category, label = TENANT_VISIBLE_ACTIONS[r["action"]]
    created = r["created_at"]
    return {
        "id": str(r["id"]),
        "at": created.astimezone(timezone.utc).isoformat() if created else None,
        "action": r["action"],
        "category": category,
        "label": label,
        "actor": actor_kind(r["actor"]),
        "resource_type": r["resource_type"],
        "resource_id": r["resource_id"],
        "detail": detail_text(r["action"], r["details"]),
    }


async def list_activity(pool, tenant_id: str, f: ActivityFilters, page: int = 1, page_size: int = 25) -> dict:
    validate_filters(f)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    count_sql, rows_sql, args = build_page_queries(tenant_id, f, page, page_size)
    async with pool.acquire() as conn:
        total = int(await conn.fetchval(count_sql, *args[:-2]) or 0)
        rows = await conn.fetch(rows_sql, *args) if total else []
    return {
        "data": [row_to_item(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "pages": math.ceil(total / page_size) if total else 0},
        "categories": [{"id": k, "label": v} for k, v in CATEGORIES.items()],
        "actions": [{"id": a, "label": label, "category": cat} for a, (cat, label) in TENANT_VISIBLE_ACTIONS.items()],
    }


CSV_HEADER = ["Date (UTC)", "Type", "Activity", "By", "Detail", "Resource type", "Resource id"]


def _csv_safe(value: Any) -> str:
    """A cell a spreadsheet will not run as a formula (CSV injection: = + - @ tab CR at the start)."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def items_to_csv(items: list[dict], truncated: bool = False) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for it in items:
        w.writerow([_csv_safe(v) for v in (
            it["at"], CATEGORIES.get(it["category"], it["category"]), it["label"], ACTOR_LABELS[it["actor"]],
            it["detail"], it["resource_type"], it["resource_id"])])
    if truncated:
        w.writerow([f"Only the newest {EXPORT_MAX_ROWS} rows are included. Narrow the dates to export the rest."])
    return buf.getvalue()


async def export_activity_csv(pool, tenant_id: str, f: ActivityFilters) -> str:
    validate_filters(f)
    where, args = build_where(tenant_id, f)
    sql = (
        "SELECT a.id, a.created_at, a.actor, a.action, a.resource_type, a.resource_id, a.details "
        f"FROM acp_shared.audit_log a WHERE {where} "
        f"ORDER BY a.created_at DESC, a.id DESC LIMIT ${len(args) + 1}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args, EXPORT_MAX_ROWS + 1)
    truncated = len(rows) > EXPORT_MAX_ROWS
    return items_to_csv([row_to_item(r) for r in rows[:EXPORT_MAX_ROWS]], truncated=truncated)


# ── API key facts ───────────────────────────────────────────────────────────────────────────────

API_KEY_FACTS_SQL = """
    SELECT
        t.api_key_hash IS NOT NULL AND t.api_key_hash NOT LIKE 'REVOKED\\_%' AS has_key,
        (SELECT max(a.created_at) FROM acp_shared.audit_log a
          WHERE a.tenant_id = $2 AND a.action = ANY($3::text[])) AS issued_at,
        (SELECT max(a.created_at) FROM acp_shared.audit_log a
          WHERE a.tenant_id = $2 AND a.action = 'tenant.login') AS last_sign_in_at,
        (SELECT max(u.called_at) FROM shared.tenant_api_usage u
          WHERE u.tenant_id = $1 AND u.actor_type = 'tenant') AS last_api_call_at
    FROM shared.tenants t
    WHERE t.tenant_id = $1
"""


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.astimezone(timezone.utc).isoformat() if v else None


async def api_key_facts(pool, tenant_id: str) -> dict:
    """The key itself is never readable (only its sha256 is stored, ``shared.tenants.api_key_hash``), so the
    masked view is the fixed prefix and a mask. ``last_used_at`` = the newer of a sign-in with the key (portal or
    ``/auth/tenant-login``) and a metered API call made with a token from it."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(API_KEY_FACTS_SQL, uuid.UUID(tenant_id), tenant_id, list(KEY_ISSUE_ACTIONS))
    if row is None:
        return {"has_key": False, "masked": None, "issued_at": None, "last_sign_in_at": None,
                "last_api_call_at": None, "last_used_at": None}
    used = [v for v in (row["last_sign_in_at"], row["last_api_call_at"]) if v]
    has_key = bool(row["has_key"])
    return {
        "has_key": has_key,
        "masked": f"{KEY_PREFIX}{_MASK}" if has_key else None,
        "issued_at": _iso(row["issued_at"]),
        "last_sign_in_at": _iso(row["last_sign_in_at"]),
        "last_api_call_at": _iso(row["last_api_call_at"]),
        "last_used_at": _iso(max(used)) if used else None,
    }
