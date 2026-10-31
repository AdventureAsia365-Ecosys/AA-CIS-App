"""AA-804 (UI part 3, root ADR 0009) — the Calls tab of the admin "AI providers" page: one list of every logged paid
call, newest first, across the ledgers.

``GET /admin/ai-gateway/calls`` (``list_calls``) reads, all $0 (database reads plus one CRM read, no provider call):

  source     ledger                                       what one row is
  llm        shared.llm_call_log (Jev's ``typesafe`` rows   one LLM attempt (ledger v2: status, error_class, latency,
             left out: Jev is the ``jev`` source)           attempt number; refused attempts have a 0 USD row)
  jev        shared.decision_log                            one Jev verdict (its share of the call's cost)
  dfs        shared.dfs_call_log                            one DataForSEO call or cache hit
  geocoder   geo.geocode_call                               one geocoder request batch
  crm        crm.ops.v1 GET /llm/calls (AA-Partner)         one CRM LLM attempt (scrubbed fields only, no text)

Filters (all optional, combined with AND): ``job_id``, ``tour_id`` (the tour's jobs, as the tour trace reads them, plus
the ``tour_id`` columns), ``app`` (cis | tripplanner | crm | …), ``provider`` (a spend-budget provider:
``shared.cost_guard.PROVIDERS``), ``status`` (ok | refused | error | billed_unusable | truncated), ``stage``,
``source`` and ``window`` (1h | 24h | 7d | 30d, default 24h). A filter a ledger cannot answer leaves that ledger out
(for example ``stage`` leaves the geocoder out, ``status=truncated`` leaves everything but the LLM log out), so a
filtered list never shows rows that only look like matches.

Paging is keyset on (``at``, ``id``) descending, where ``id`` is ``<source>:<ledger row id>`` compared byte-wise
(``COLLATE "C"`` in SQL, plain ``str`` order in Python), so rows with the same timestamp (one transaction's batch)
are never skipped or repeated. ``cursor`` is the ``next_cursor`` of the previous page, ``<at ISO>~<id>``. Each ledger
is read with the created_at index from the cursor down, at most ``limit + 1`` rows; the pages are merged here.

CRM rows come from the CRM's own ledger (``crm.llm_call``, schema owned by AA-Partner; CIS cannot read it) through
``crm.ops.v1`` 1.6 ``GET /llm/calls`` with the signed-in staff member's own WORKFORCE token (the same pattern as the
CRM job summary, ``api.services.platform_apps._crm_ops_get``). Its keyset is the same order, so the CIS cursor maps
onto it (``crm:`` sorts before every other source, so a cursor on another source at time T asks the CRM for its rows
at or before T). The CRM filters by whole UTC days only, so the first page also sends a cursor at the window's end and
every CRM row is checked against the window here; a short CRM page (the provider filter, applied here) asks the CRM
for its next page, at most ``CRM_MAX_PAGES`` reads; when that cap is hit the merged page stops at the last CRM row
read (the "floor") and ``next_cursor`` resumes from it. The CRM is asked only when a CRM row could match (no job or
tour filter, app unset or ``crm``, a stage named ``crm_*`` or none, a status it knows). When it cannot be asked or
answers an error, the list carries no CRM rows and ``crm`` says why, with the CRM's aggregate (``GET /llm/usage``,
whole UTC days ``from`` .. ``to``, so it can cover more than the window) when that still answers.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from api.services import ai_gateway as gw
from shared.aws_client.accounts import VENDOR_CONNECTION_LABELS

SOURCES = ("llm", "jev", "dfs", "geocoder", "crm")
CIS_SOURCES = ("llm", "jev", "dfs", "geocoder")
STATUSES = ("ok", "refused", "error", "billed_unusable", "truncated")
WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
DEFAULT_WINDOW = "24h"
DEFAULT_LIMIT = 100
MAX_LIMIT = 200
# The statuses each ledger can hold (decision_log and geocode_call have none: derived, ok | error).
SOURCE_STATUSES = {
    "llm": set(STATUSES), "dfs": {"ok", "refused", "error"}, "jev": {"ok", "error"},
    "geocoder": {"ok", "error"}, "crm": {"ok", "refused", "error"},
}
# The spend-budget provider (shared/cost_guard.py PROVIDERS) a source answers for; llm and crm by their provider.
SOURCE_OF_PROVIDER = {"jev": "jev", "dfs": "dfs", "geocoder": "geocoder"}
LLM_PROVIDERS = ("bedrock", "openai", "anthropic", "azure_openai")
MAX_TOUR_JOBS = gw.TOUR_JOBS_LIMIT
# The highest uuid: a CRM keyset cursor that keeps every CRM row of time T ("crm:" sorts first at equal T).
_MAX_UUID = "ffffffff-ffff-ffff-ffff-ffffffffffff"
# At most this many crm.ops.v1 /llm/calls reads per Calls page (only a provider filter makes a CRM page short).
CRM_MAX_PAGES = 5

_APP_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_STAGE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_KEY_RE = re.compile(r"^(llm|jev|dfs|geocoder|crm):[0-9A-Za-z-]{1,64}$")


@dataclass(frozen=True)
class CallFilters:
    job_id: Optional[str] = None
    tour_id: Optional[str] = None
    app: Optional[str] = None
    provider: Optional[str] = None
    status: Optional[str] = None
    stage: Optional[str] = None
    source: Optional[str] = None
    window: str = DEFAULT_WINDOW


def _uuid(value: Optional[str], name: str) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except ValueError:
        raise ValueError(f"{name} must be a UUID") from None


def parse_filters(*, job_id: Optional[str] = None, tour_id: Optional[str] = None, app: Optional[str] = None,
                  provider: Optional[str] = None, status: Optional[str] = None, stage: Optional[str] = None,
                  source: Optional[str] = None, window: Optional[str] = None) -> CallFilters:
    """The checked filters, or ValueError naming the first bad one (the router answers 422). Pure."""
    from shared.cost_guard import PROVIDERS

    def blank(v: Optional[str]) -> Optional[str]:
        return None if v is None or v.strip() == "" else v.strip()

    app, provider, status, stage, source, window = map(blank, (app, provider, status, stage, source, window))
    if app is not None and not _APP_RE.match(app):
        raise ValueError("app must be a lower-case app name")
    if provider is not None and provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if stage is not None and not _STAGE_RE.match(stage):
        raise ValueError("stage must be a stage key (lower case, digits, underscores)")
    if source is not None and source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    if window is not None and window not in WINDOWS:
        raise ValueError(f"window must be one of {', '.join(WINDOWS)}")
    return CallFilters(job_id=_uuid(job_id, "job_id"), tour_id=_uuid(tour_id, "tour_id"), app=app,
                       provider=provider, status=status, stage=stage, source=source,
                       window=window or DEFAULT_WINDOW)


def parse_cursor(value: Optional[str]) -> Optional[tuple[datetime, str]]:
    """``<at ISO>~<source>:<id>`` → (at, id), or ValueError. Pure."""
    if value is None or value == "":
        return None
    at_text, sep, key = value.partition("~")
    try:
        at = datetime.fromisoformat(at_text)
    except ValueError:
        raise ValueError("cursor must be the next_cursor of a previous page") from None
    if not sep or at.tzinfo is None or not _KEY_RE.match(key):
        raise ValueError("cursor must be the next_cursor of a previous page")
    return at, key


def make_cursor(at: datetime, key: str) -> str:
    return f"{at.isoformat()}~{key}"


def sources_for(f: CallFilters) -> tuple[str, ...]:
    """The ledgers that can hold a row matching every filter. Pure."""
    out = []
    for s in SOURCES:
        if f.source is not None and s != f.source:
            continue
        if f.status is not None and f.status not in SOURCE_STATUSES[s]:
            continue
        if f.provider is not None:
            if s in ("llm", "crm"):
                if f.provider not in LLM_PROVIDERS:
                    continue
            elif SOURCE_OF_PROVIDER.get(f.provider) != s:
                continue
        if f.app is not None:
            if s == "crm" and f.app != "crm":
                continue
            if s in ("jev", "geocoder") and f.app != "cis":
                continue
            if s in ("llm", "dfs") and f.app == "crm":
                continue  # the CRM's rows are in its own ledger
        if f.stage is not None:
            if s == "geocoder":
                continue
            if s == "crm" and not f.stage.startswith("crm_"):
                continue
        if s == "crm" and (f.job_id is not None or f.tour_id is not None):
            continue  # CRM rows carry crm.job ids and no tour
        out.append(s)
    return tuple(out)


# ── SQL ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

# Per ledger: the row columns (shared with the trace), its time column, its key expression and the filters it
# answers. Every key expression is COLLATE "C", so SQL and Python order the keys the same way.
_KEY = {
    "llm": "('llm:' || id::text) COLLATE \"C\"",
    "jev": "('jev:' || id::text) COLLATE \"C\"",
    "dfs": "('dfs:' || id::text) COLLATE \"C\"",
    "geocoder": "('geocoder:' || id::text) COLLATE \"C\"",
}
_TABLE = {"llm": "shared.llm_call_log", "jev": "shared.decision_log", "dfs": "shared.dfs_call_log",
          "geocoder": "geo.geocode_call"}
_TIME = {"llm": "created_at", "jev": "created_at", "dfs": "created_at", "geocoder": "called_at"}
_COLS = {"llm": gw._LLM_COLS, "jev": gw._DECISION_COLS, "dfs": gw._DFS_COLS, "geocoder": gw._GEO_COLS}
_STATUS_EXPR = {
    "llm": "coalesce(status, 'ok')",
    "dfs": "coalesce(status, 'ok')",
    "jev": "CASE WHEN error IS NOT NULL OR zone = 'error' THEN 'error' ELSE 'ok' END",
    "geocoder": ("CASE WHEN coalesce(failed_requests, 0) > 0 AND failed_requests >= requests "
                 "THEN 'error' ELSE 'ok' END"),
}
_APP_EXPR = {"llm": "coalesce(app, quality_signal ->> 'app', 'cis')", "dfs": "coalesce(app, 'cis')"}
_TENANT = {"llm": "tenant_id::text", "jev": "tenant_id::text", "dfs": "tenant_id::text",
           "geocoder": "tenant_id::text"}


def source_sql(source: str, f: CallFilters, *, since: datetime, until: datetime,
               cursor: Optional[tuple[datetime, str]], limit: int,
               tour_jobs: Optional[list[str]] = None) -> tuple[str, list[Any]]:
    """(SQL, args) of one ledger's page: rows in [since, until], below the cursor, matching the filters, newest
    first, at most ``limit`` rows. Pure (unit-tested; the SQL runs in tests/integration)."""
    args: list[Any] = []

    def arg(v: Any) -> str:
        args.append(v)
        return f"${len(args)}"

    t = _TIME[source]
    key = _KEY[source]
    where = [f"{t} >= {arg(since)}", f"{t} <= {arg(until)}"]
    if source == "llm":
        where.append("coalesce(provider, '') <> 'typesafe'")
    if cursor is not None:
        c_at, c_key = arg(cursor[0]), arg(cursor[1])
        where.append(f"{t} <= {c_at} AND ({t} < {c_at} OR {key} < {c_key})")
    if f.job_id is not None:
        where.append(f"job_id = {arg(f.job_id)}::uuid")
    if f.tour_id is not None:
        jobs = arg(list(tour_jobs or []))
        if source in ("llm", "dfs"):
            where.append(f"(tour_id = {arg(f.tour_id)}::uuid OR job_id = ANY({jobs}::uuid[]))")
        else:
            where.append(f"job_id = ANY({jobs}::uuid[])")
    if f.app is not None and source in _APP_EXPR:
        where.append(f"{_APP_EXPR[source]} = {arg(f.app)}")
    if f.status is not None:
        where.append(f"{_STATUS_EXPR[source]} = {arg(f.status)}")
    if f.stage is not None and source in ("llm", "jev", "dfs"):
        where.append(f"stage = {arg(f.stage)}")
    if f.provider is not None and source == "llm":
        if f.provider == "bedrock":
            where.append("provider LIKE 'bedrock%'")
        else:
            where.append(f"provider = {arg(f.provider)}")
    sql = (f"SELECT {key} AS key, {_TENANT[source]} AS tenant_id, {_COLS[source]}\n"
           f"  FROM {_TABLE[source]}\n"
           f" WHERE " + "\n   AND ".join(where) + "\n"
           f" ORDER BY {t} DESC, key DESC\n"
           f" LIMIT {arg(limit)}")
    return sql, args


# ── rows ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _at(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(v))


def cis_row(source: str, r: dict) -> dict:
    """One Calls row from a CIS ledger row (the trace event plus its key, tenant and time). Pure."""
    e = gw.EVENT_OF[source](r)
    e["id"] = r["key"]
    e["tenant_id"] = r.get("tenant_id")
    e["_at"] = _at(r["at"])
    return e


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else round(float(v), 6)
    except (TypeError, ValueError):
        return None


def crm_row(r: dict) -> Optional[dict]:
    """One Calls row from a crm.ops.v1 LlmCallRow (scrubbed fields only); None for a row that does not parse. Pure."""
    try:
        call_id = str(uuid.UUID(str(r["call_id"])))
        at = _at(r["at"])
    except (KeyError, TypeError, ValueError):
        return None
    status = r.get("status") if r.get("status") in SOURCE_STATUSES["crm"] else "error"
    shadow = bool(r.get("is_shadow"))
    return {
        "id": f"crm:{call_id}", "_at": at, "at": at.isoformat(), "source": "crm", "stage": r.get("stage"),
        "what": r.get("role"), "model": r.get("model"), "provider": r.get("provider"),
        # The CRM's connection keys ("bedrock", "bedrock_role", "stub", …) are not CIS account keys: only a vendor
        # API connection is named; the key itself stays in detail.connection_key.
        "account": r.get("connection_key") if r.get("connection_key") in VENDOR_CONNECTION_LABELS else None,
        "outcome": status if status != "ok" else ("shadow" if shadow else "ok"), "status": status,
        "error_class": r.get("error_class") or r.get("refusal_code"), "cost_usd": _num(r.get("cost_usd")),
        "latency_ms": r.get("latency_ms"), "tokens_in": r.get("input_tokens"), "tokens_out": r.get("output_tokens"),
        "job_id": None, "app": "crm", "tenant_id": r.get("tenant_id"),
        "detail": {"attempt_no": r.get("attempt_no"), "refusal_code": r.get("refusal_code"),
                   "pricing_mode": r.get("pricing_mode"), "units": r.get("units"), "unit_kind": r.get("unit_kind"),
                   "connection_key": r.get("connection_key"), "crm_job_id": r.get("job_id"),
                   "is_experiment": bool(r.get("is_experiment")), "scrub_entities": r.get("scrub_entities")},
    }


def crm_query(f: CallFilters, *, since: datetime, until: datetime, cursor: Optional[tuple[datetime, str]],
              limit: int) -> dict:
    """The crm.ops.v1 /llm/calls query of this page (UTC days, CRM keyset cursor). Pure.

    The CRM's ``from`` / ``to`` are whole UTC days, so the cursor is always sent: the page's cursor, or the window's
    end with the max uuid (every CRM row at or before ``until``). Rows older than ``since`` are dropped by the caller.
    """
    q: dict[str, Any] = {"from": since.astimezone(timezone.utc).date().isoformat(),
                         "to": until.astimezone(timezone.utc).date().isoformat(), "limit": limit}
    if f.stage is not None:
        q["stage"] = f.stage
    if f.status is not None:
        q["status"] = f.status
    if cursor is None or cursor[0] > until:
        at, call = until, _MAX_UUID
    else:
        at, key = cursor
        call = key.split(":", 1)[1] if key.startswith("crm:") else _MAX_UUID
    q["cursor"] = f"{at.isoformat()}~{call}"
    return q


def merge_page(pages: dict[str, list[dict]], limit: int,
               floor: Optional[tuple[datetime, str]] = None) -> tuple[list[dict], Optional[str]]:
    """Merge each source's newest-first page into one page of ``limit`` rows and the next cursor (None when every
    source is exhausted). ``floor``: the last CRM row read when the CRM still had rows below it; rows below it are
    left for the next page, which starts right after it. Pure."""
    rows = [r for page in pages.values() for r in page]
    if floor is not None:
        rows = [r for r in rows if (r["_at"], r["id"]) >= floor]
    rows.sort(key=lambda r: (r["_at"], r["id"]), reverse=True)
    more = len(rows) > limit
    rows = rows[:limit]
    if more and rows:
        nxt: Optional[str] = make_cursor(rows[-1]["_at"], rows[-1]["id"])
    elif floor is not None:
        nxt = make_cursor(*floor)
    else:
        nxt = None
    return [{k: v for k, v in r.items() if k != "_at"} for r in rows], nxt


# ── read ────────────────────────────────────────────────────────────────────────────────────────────────────────────

CrmCalls = Callable[[dict], Awaitable[tuple[Optional[dict], dict]]]
CrmUsage = Callable[[date, date], Awaitable[dict]]


def _crm_floor(next_cursor: Any) -> Optional[tuple[datetime, str]]:
    """The CRM's ``next_cursor`` (``<at ISO>~<call id>``, its page's last row) as a CIS keyset; None if malformed."""
    if not isinstance(next_cursor, str):
        return None
    try:
        return parse_cursor(next_cursor.replace("~", "~crm:", 1))
    except ValueError:
        return None


async def _crm_page(crm_calls: CrmCalls, crm_usage: Optional[CrmUsage], f: CallFilters, *, since: datetime,
                    until: datetime, cursor: Optional[tuple[datetime, str]],
                    limit: int) -> tuple[list[dict], dict, Optional[tuple[datetime, str]]]:
    """(rows, crm status, floor) of the CRM ledger; never raises. Rows are those in [since, until] that match the
    provider filter (at least ``limit`` when the window holds that many); ``floor`` is the last CRM row read when the
    CRM still has unread rows inside the window after ``CRM_MAX_PAGES`` reads (or a later read failed), so the merge
    stops there."""
    q = crm_query(f, since=since, until=until, cursor=cursor, limit=limit)
    body, problem = await crm_calls(q)
    if body is not None and isinstance(body.get("rows"), list):
        rows: list[dict] = []
        floor: Optional[tuple[datetime, str]] = None
        for n in range(1, CRM_MAX_PAGES + 1):
            for r in (crm_row(x) for x in body["rows"] if isinstance(x, dict)):
                if r is not None and since <= r["_at"] <= until and (
                        f.provider is None or gw.budget_provider(r["provider"]) == f.provider):
                    rows.append(r)
            last = _crm_floor(body.get("next_cursor"))  # this page's last row, when the CRM has more
            if last is None or last[0] < since or len(rows) >= limit:
                break  # no more CRM rows in the window, or the page is full
            if n == CRM_MAX_PAGES:
                floor = last
                break
            body, _ = await crm_calls(dict(q, cursor=body["next_cursor"]))
            if body is None or not isinstance(body.get("rows"), list):
                floor = last  # a later read failed: the next page resumes after the last row read
                break
        return rows, {"status": "ok", "rows": len(rows)}, floor
    status = {"status": problem.get("status") or "error", "reason": problem.get("reason"),
              "http_status": problem.get("http_status"), "aggregate": None}
    if body is not None:
        status.update(status="error", reason="unexpected_shape")
    if crm_usage is not None:
        start = since.astimezone(timezone.utc).date()
        end = until.astimezone(timezone.utc).date() + timedelta(days=1)
        try:
            agg = await crm_usage(start, end)
        except Exception:  # noqa: BLE001 — the aggregate is a courtesy; the page still answers
            agg = None
        if isinstance(agg, dict) and agg.get("status") == "ok":
            status["aggregate"] = {"calls": agg.get("calls", 0), "cost_usd": agg.get("cost_usd", 0.0),
                                   "by_model": agg.get("by_model", []), "from": start.isoformat(),
                                   "to": (end - timedelta(days=1)).isoformat()}
    return [], status, None


async def list_calls(pool, f: CallFilters, *, cursor: Optional[str] = None, limit: int = DEFAULT_LIMIT,
                     crm_calls: Optional[CrmCalls] = None, crm_usage: Optional[CrmUsage] = None,
                     now: Optional[datetime] = None) -> dict:
    """``GET /admin/ai-gateway/calls`` (see the module docstring). ``crm_calls`` / ``crm_usage``: the crm.ops.v1
    reads bound to the caller's token (None: the CRM is not asked)."""
    now = now or datetime.now(timezone.utc)
    limit = max(1, min(int(limit), MAX_LIMIT))
    keyset = parse_cursor(cursor)
    since, until = now - WINDOWS[f.window], now
    sources = sources_for(f)
    errors: list[dict] = []

    tour_jobs: list[str] = []
    if f.tour_id is not None:
        jobs, err = await gw._guard("Jobs of the tour", lambda: gw.tour_job_ids(pool, f.tour_id, MAX_TOUR_JOBS))
        tour_jobs = [j["id"] for j in jobs or []]
        if err:
            errors.append({"source": "jobs", "error": err})

    async def read(source: str) -> list[dict]:
        sql, args = source_sql(source, f, since=since, until=until, cursor=keyset, limit=limit + 1,
                               tour_jobs=tour_jobs)
        return [cis_row(source, r) for r in await gw._fetch(pool, sql, *args)]

    cis = [s for s in sources if s in CIS_SOURCES]
    results = await asyncio.gather(*(gw._guard(gw.SOURCE_LABELS[s], lambda s=s: read(s)) for s in cis))
    pages: dict[str, list[dict]] = {}
    for s, (rows, err) in zip(cis, results):
        pages[s] = rows or []
        if err:
            errors.append({"source": s, "error": err})

    crm: dict[str, Any]
    floor: Optional[tuple[datetime, str]] = None
    if "crm" not in sources:
        crm = {"status": "not_asked", "reason": "filtered_out"}
    elif crm_calls is None:
        crm = {"status": "not_asked", "reason": "no_reader"}
    else:
        pages["crm"], crm, floor = await _crm_page(crm_calls, crm_usage, f, since=since, until=until,
                                                   cursor=keyset, limit=limit + 1)
    rows, nxt = merge_page(pages, limit, floor)
    if crm.get("status") == "ok":
        crm["rows"] = sum(1 for r in rows if r["source"] == "crm")  # the CRM rows on this page, after the merge
    return {
        "generated_at": now.isoformat(),
        "window": {"key": f.window, "from": since.isoformat(), "to": until.isoformat()},
        "filters": {k: v for k, v in asdict(f).items() if v is not None},
        "sources": list(sources),
        "rows": rows,
        "next_cursor": nxt,
        "limit": limit,
        "crm": crm,
        "errors": errors,
    }
