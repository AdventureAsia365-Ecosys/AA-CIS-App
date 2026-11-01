"""AA-1002 (gateway slice S10) — nightly reconciliation of the gateway's cost ledger with each provider's own figure.

For every connection of ``shared.llm_provider_connection`` (mock rows excepted) and every UTC day of the window, one
``shared.llm_cost_reconcile`` row (migration 317, upsert): what ``shared.llm_call_log`` logged on that connection
(``account = connection_key``) next to what the provider says it billed, and the difference.

Sources, one per connection kind (``SOURCES``):

- ``bedrock`` → ``aws_cost_explorer``: the STORED Cost Explorer snapshot (``shared.cost_explorer_snapshot``, newest
  fetch per account / service / day, Bedrock services only, ``cost_explorer.is_bedrock_service``) of the AWS account
  the connection calls (assume-role: its account; task role: this task's own account). No live Cost Explorer call
  here (they cost ~$0.01 each): the snapshot is written by the admin's Cost Explorer check. A day without a stored
  row is ``no_data``; a day whose newest fetch was taken less than ``SETTLE_HOURS`` after it ended is ``provisional``
  (Cost Explorer revises the last 24-48 h; never alerted). Cost Explorer bills per AWS account, so every Bedrock
  connection on the same account is compared together (``details.account_connections``). The figure compared is the
  GROSS Bedrock usage (record types Usage / DiscountedUsage / SavingsPlanCoveredUsage, kept per row since the snapshot
  is split by RECORD_TYPE, ``cost_explorer.usage_split``), never the stored net amount: acc3 is billed against a
  promotional credit, so its net is about $0 (or below zero after a refund) on a day of real use. Credits and refunds
  are shown in ``details.ce_adjustments_usd``. A day stored before the split is ``no_data`` (its net may hide credits).
- ``anthropic`` → ``anthropic_cost_report``: ``GET /v1/organizations/cost_report`` of the Admin API (raw HTTP: not in
  the SDK), daily buckets, amounts in cents as decimal strings, filtered to the policy's ``usage_scope_id`` workspace
  when set.
- ``openai`` → ``openai_costs``: ``GET /v1/organization/costs`` (admin key), daily buckets, ``amount.value`` in USD,
  ``project_ids`` = the policy's ``usage_scope_id`` when set.
- ``azure_openai`` → ``unsupported`` (Azure Cost Management is not built).

The two vendor sources are OFF until a credential exists: the read-only usage / admin key is the secret NAMED by
``shared.llm_connection_cost_policy.usage_secret_name`` (``connections.resolve_usage_key``: ``aa-llm/`` prefix checked
before anything is read, fails closed). No name = ``not_configured``, and no request is sent. The host of each source
is a constant here, never a column, so an edited row cannot send a usage key elsewhere. The usage endpoints are not
billed; nothing in this module calls a model.

Status of a row (``compare``): ``ok`` when |logged - provider| <= max(``TOLERANCE_USD``, ``TOLERANCE_PCT`` % of the
provider figure), else ``mismatch``; ``not_configured`` / ``unsupported`` / ``no_data`` / ``error`` when there is no
provider figure; a provider figure below zero (credits netted in) is ``no_data`` too, never written or alerted. A
non-provisional mismatch on a configured source raises one admin bell per 24 h (``MISMATCH_EVENT``). Each connection
runs on its own: one that fails (a read, an upsert) is named in the summary's ``errors`` and the others are still
written; only a run where every connection failed fails the job. Rows older than ``RETENTION_DAYS`` are deleted by
every run.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

import structlog

from shared import cost_guard
from shared.llm_client import connections as conn_registry

logger = structlog.get_logger()

DEFAULT_DAYS = 3
MAX_DAYS = 31
RETENTION_DAYS = 400
TOLERANCE_USD = 0.05
TOLERANCE_PCT = 5.0
SETTLE_HOURS = 24
HTTP_TIMEOUT_S = 30.0
MAX_PAGES = 20

ANTHROPIC_COST_URL = "https://api.anthropic.com/v1/organizations/cost_report"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"

SOURCE_ANTHROPIC = "anthropic_cost_report"
SOURCE_OPENAI = "openai_costs"
SOURCE_AWS = "aws_cost_explorer"
SOURCE_NONE = "none"
SOURCES = {"bedrock": SOURCE_AWS, "anthropic": SOURCE_ANTHROPIC, "openai": SOURCE_OPENAI}
SKIPPED_KINDS = ("mock",)

STATUSES = ("ok", "mismatch", "not_configured", "unsupported", "no_data", "error")
MISMATCH_EVENT = "platform.llm_cost_reconcile.mismatch"
_PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"


class ReconcileUnavailable(RuntimeError):
    """The reconcile tables are missing (migration 317 not applied)."""


class SourceError(RuntimeError):
    """A provider source failed (HTTP error, unexpected body). The message never holds a key."""


@dataclass
class DayFigure:
    status: str                        # ok-able ("figure") or a no-figure status
    provider_usd: Optional[float] = None
    details: dict = field(default_factory=dict)


def window(days: int, end_day: Optional[date] = None, today: Optional[date] = None) -> tuple[date, date]:
    """[start, end_exclusive) of ``days`` complete UTC days ending with ``end_day`` (default: yesterday, UTC)."""
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be an integer from 1 to {MAX_DAYS}")
    today = today or datetime.now(timezone.utc).date()
    end_day = end_day or (today - timedelta(days=1))
    if end_day >= today:
        raise ValueError("end_day must be a complete UTC day (before today)")
    return end_day - timedelta(days=days - 1), end_day + timedelta(days=1)


def days_of(start: date, end_exclusive: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end_exclusive - start).days)]


def compare(logged_usd: float, provider_usd: Optional[float]) -> tuple[str, Optional[float], Optional[float]]:
    """(status, diff_usd = logged - provider, diff_pct of the provider figure) for a day with a provider figure;
    ("no_data", None, None) without one. Pure."""
    if provider_usd is None:
        return "no_data", None, None
    diff = round(float(logged_usd) - float(provider_usd), 6)
    pct = round(100.0 * diff / provider_usd, 2) if provider_usd > 0 else None
    tolerance = max(TOLERANCE_USD, TOLERANCE_PCT / 100.0 * float(provider_usd))
    return ("ok" if abs(diff) <= tolerance else "mismatch"), diff, pct


# ── provider sources (sync; run in a thread) ─────────────────────────────────────────────────────────────────────

def _http_get(url: str, *, params: list, headers: dict, http: Any = None) -> dict:
    import httpx

    client = http or httpx.Client(timeout=HTTP_TIMEOUT_S)
    try:
        resp = client.get(url, params=params, headers=headers)
    except httpx.HTTPError as e:
        raise SourceError(f"{type(e).__name__} reaching {url}") from e
    finally:
        if http is None:
            client.close()
    if resp.status_code != 200:
        raise SourceError(f"{url} answered HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as e:
        raise SourceError(f"{url} answered a body that is not JSON") from e
    if not isinstance(body, dict):
        raise SourceError(f"{url} answered an unexpected body")
    return body


def _bucket_day(value) -> Optional[date]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise SourceError(f"unexpected amount {value!r}") from e


def fetch_anthropic_costs(admin_key: str, start: date, end_exclusive: date, scope_id: Optional[str] = None,
                          http: Any = None) -> dict[date, float]:
    """USD per UTC day from the Admin API cost report (amounts are cents, decimal strings). ``scope_id`` keeps one
    workspace (results grouped by workspace_id; the default workspace is null there)."""
    params: list = [("starting_at", f"{start.isoformat()}T00:00:00Z"),
                    ("ending_at", f"{end_exclusive.isoformat()}T00:00:00Z"),
                    ("bucket_width", "1d"), ("limit", str(MAX_DAYS))]
    if scope_id:
        params.append(("group_by[]", "workspace_id"))
    headers = {"x-api-key": admin_key, "anthropic-version": ANTHROPIC_VERSION}
    cents: dict[date, Decimal] = {}
    page = None
    for _ in range(MAX_PAGES):
        body = _http_get(ANTHROPIC_COST_URL, params=params + ([("page", page)] if page else []), headers=headers,
                         http=http)
        for bucket in body.get("data") or []:
            day = _bucket_day(bucket.get("starting_at"))
            if day is None or not start <= day < end_exclusive:
                continue
            total = cents.setdefault(day, Decimal(0))
            for r in bucket.get("results") or []:
                if scope_id and r.get("workspace_id") != scope_id:
                    continue
                if (r.get("currency") or "USD").upper() != "USD":
                    raise SourceError(f"cost report currency {r.get('currency')!r} is not USD")
                total += _decimal(r.get("amount") or 0)
                cents[day] = total
        if not body.get("has_more") or not body.get("next_page"):
            break
        page = body["next_page"]
    else:
        raise SourceError(f"cost report still had more pages after {MAX_PAGES}")
    return {d: float(v / 100) for d, v in cents.items()}


def fetch_openai_costs(admin_key: str, start: date, end_exclusive: date, scope_id: Optional[str] = None,
                       http: Any = None) -> dict[date, float]:
    """USD per UTC day from the OpenAI organization costs API (``amount.value`` in USD)."""
    start_ts = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime(end_exclusive.year, end_exclusive.month, end_exclusive.day, tzinfo=timezone.utc).timestamp())
    params: list = [("start_time", str(start_ts)), ("end_time", str(end_ts)), ("bucket_width", "1d"),
                    ("limit", str(MAX_DAYS))]
    if scope_id:
        params.append(("project_ids", scope_id))
    headers = {"Authorization": f"Bearer {admin_key}"}
    usd: dict[date, Decimal] = {}
    page = None
    for _ in range(MAX_PAGES):
        body = _http_get(OPENAI_COSTS_URL, params=params + ([("page", page)] if page else []), headers=headers,
                         http=http)
        for bucket in body.get("data") or []:
            day = _bucket_day(bucket.get("start_time"))
            if day is None or not start <= day < end_exclusive:
                continue
            total = usd.setdefault(day, Decimal(0))
            for r in bucket.get("results") or []:
                amount = r.get("amount") or {}
                if (amount.get("currency") or "usd").lower() != "usd":
                    raise SourceError(f"costs currency {amount.get('currency')!r} is not USD")
                total += _decimal(amount.get("value") or 0)
                usd[day] = total
        if not body.get("has_more") or not body.get("next_page"):
            break
        page = body["next_page"]
    else:
        raise SourceError(f"costs API still had more pages after {MAX_PAGES}")
    return {d: float(v) for d, v in usd.items()}


VENDOR_FETCHERS: dict[str, Callable[..., dict]] = {"anthropic": fetch_anthropic_costs, "openai": fetch_openai_costs}


# ── database reads ───────────────────────────────────────────────────────────────────────────────────────────────

_LOGGED_SQL = """
    SELECT account, (created_at AT TIME ZONE 'utc')::date AS day,
           coalesce(sum(cost_usd), 0) AS usd, count(*)::int AS calls
      FROM shared.llm_call_log
     WHERE account = ANY($1::text[]) AND created_at >= $2 AND created_at < $3
     GROUP BY 1, 2
"""

# Newest fetch per (service, day) of one account (cost_explorer.read_cost_range's rule), with when it was fetched.
_CE_DAYS_SQL = """
    SELECT service, period_start, amount_usd, raw, fetched_at FROM (
        SELECT DISTINCT ON (service, period_start) service, period_start, amount_usd, raw, fetched_at
          FROM shared.cost_explorer_snapshot
         WHERE account_id = $1 AND period_start >= $2 AND period_start < $3
         ORDER BY service, period_start, fetched_at DESC) s
"""

_UPSERT_SQL = """
    INSERT INTO shared.llm_cost_reconcile
        (connection_key, day, source, status, logged_usd, logged_calls, provider_usd, diff_usd, diff_pct, details,
         job_id, checked_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::uuid, now())
    ON CONFLICT (connection_key, day) DO UPDATE SET
        source = EXCLUDED.source, status = EXCLUDED.status, logged_usd = EXCLUDED.logged_usd,
        logged_calls = EXCLUDED.logged_calls, provider_usd = EXCLUDED.provider_usd, diff_usd = EXCLUDED.diff_usd,
        diff_pct = EXCLUDED.diff_pct, details = EXCLUDED.details, job_id = EXCLUDED.job_id, checked_at = now()
"""
_PRUNE_SQL = "DELETE FROM shared.llm_cost_reconcile WHERE day < $1"

_RECENT_BELL_SQL = """
    SELECT 1 FROM shared.notifications
     WHERE event_type = $1 AND entity_id = $2 AND is_read = FALSE AND created_at >= now() - interval '24 hours'
     LIMIT 1
"""
_INSERT_BELL_SQL = """
    INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, 'llm_connection', $3, $4::jsonb, ARRAY['admin'])
"""


def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


async def logged_costs(conn, keys: list[str], start: date, end_exclusive: date) -> dict[tuple[str, date], tuple]:
    rows = await conn.fetch(_LOGGED_SQL, list(keys), _utc(start), _utc(end_exclusive))
    return {(r["account"], r["day"]): (float(r["usd"] or 0.0), int(r["calls"] or 0)) for r in rows}


@dataclass
class StoredDay:
    """The stored Cost Explorer figure of one account and day, Bedrock services only."""
    usage_usd: Optional[float]         # gross Bedrock usage; None when a row predates the RECORD_TYPE split
    adjustments_usd: float             # credits, refunds, tax... of those services (<= 0 for a credit)
    net_usd: float                     # what the Cost page shows (usage + adjustments)
    fetched_at: datetime
    services: int


async def stored_bedrock_costs(conn, account_id: str, start: date, end_exclusive: date) -> dict[date, StoredDay]:
    """{day: StoredDay} from the stored Cost Explorer snapshot of one account. The gross usage comes from each row's
    record-type split (``cost_explorer.usage_split``): the stored ``amount_usd`` nets credits in, so an account billed
    against a promotional credit (acc3 since 29/10/2026) shows about $0, or less after a refund, for a day of real
    use."""
    from shared.aws_client.cost_explorer import is_bedrock_service, usage_split

    out: dict[date, dict] = {}
    for r in await conn.fetch(_CE_DAYS_SQL, account_id, start, end_exclusive):
        day = r["period_start"]
        entry = out.setdefault(day, {"usage": Decimal(0), "adj": Decimal(0), "net": Decimal(0), "split": True,
                                     "fetched_at": r["fetched_at"], "services": 0})
        entry["fetched_at"] = max(entry["fetched_at"], r["fetched_at"])
        if not is_bedrock_service(r["service"]):
            continue
        entry["net"] += Decimal(str(r["amount_usd"]))
        entry["services"] += 1
        split = usage_split(r.get("raw"))
        if split is None:
            entry["split"] = False
        else:
            entry["usage"] += Decimal(str(split["usage_usd"]))
            entry["adj"] += Decimal(str(split["adjustments_usd"]))
    return {d: StoredDay(usage_usd=float(v["usage"]) if v["split"] else None, adjustments_usd=float(v["adj"]),
                         net_usd=float(v["net"]), fetched_at=v["fetched_at"], services=v["services"])
            for d, v in out.items()}


def _bedrock_account(c: conn_registry.ProviderConnection, own_account: Callable[[], str]) -> Optional[str]:
    if c.auth_mode == "task_role":
        return own_account()
    return c.resolved_account_id()


def _own_account() -> str:
    from shared.aws_client import bronze
    return bronze.caller_account_id()


# ── one run ──────────────────────────────────────────────────────────────────────────────────────────────────────

async def _vendor_figures(c: conn_registry.ProviderConnection, policy: cost_guard.ConnectionCostPolicy,
                          days: list[date], start: date, end_exclusive: date, fetchers: dict) -> dict[date, DayFigure]:
    if not policy.usage_secret_name:
        note = {"reason": "no usage key configured (llm_connection_cost_policy.usage_secret_name)"}
        return {d: DayFigure("not_configured", details=dict(note)) for d in days}
    try:
        key = await asyncio.to_thread(conn_registry.resolve_usage_key, c.connection_key, policy.usage_secret_name)
        figures = await asyncio.to_thread(fetchers[c.kind], key, start, end_exclusive, policy.usage_scope_id)
    except conn_registry.ConnectionCredentialError as e:
        return {d: DayFigure("error", details={"error": str(e)[:300]}) for d in days}
    except Exception as e:  # noqa: BLE001 — a source failure is this connection's result, the run goes on
        msg = str(e) if isinstance(e, SourceError) else type(e).__name__
        return {d: DayFigure("error", details={"error": msg[:300]}) for d in days}
    scope = {"usage_scope_id": policy.usage_scope_id} if policy.usage_scope_id else {}
    # A day the report has no bucket for: the provider billed nothing that day (its buckets cover the whole window).
    return {d: DayFigure("figure", round(figures.get(d, 0.0), 6), dict(scope)) for d in days}


async def _bedrock_figures(conn, c: conn_registry.ProviderConnection, peers: list[str], days: list[date],
                           start: date, end_exclusive: date, own_account: Callable[[], str]) -> dict[date, DayFigure]:
    try:
        account_id = await asyncio.to_thread(_bedrock_account, c, own_account)
    except Exception as e:  # noqa: BLE001
        return {d: DayFigure("error", details={"error": f"account unresolved ({type(e).__name__})"}) for d in days}
    if not account_id:
        return {d: DayFigure("error", details={"error": "the connection names no AWS account"}) for d in days}
    stored = await stored_bedrock_costs(conn, account_id, start, end_exclusive)
    out: dict[date, DayFigure] = {}
    for d in days:
        details = {"account_id": account_id, "account_connections": peers}
        hit = stored.get(d)
        if hit is None:
            details["reason"] = "no stored Cost Explorer snapshot for the day (run a check on the Cost page)"
            out[d] = DayFigure("no_data", details=details)
            continue
        settled = hit.fetched_at >= _utc(d + timedelta(days=1)) + timedelta(hours=SETTLE_HOURS)
        details.update({"ce_fetched_at": hit.fetched_at.isoformat(), "bedrock_services": hit.services,
                        "provisional": not settled, "ce_net_usd": round(hit.net_usd, 6)})
        if hit.usage_usd is None:
            # Fetched before the RECORD_TYPE split: its amount may have credits netted in, so it is not compared.
            details["reason"] = ("the stored Cost Explorer snapshot has no usage/credit split (credits may be netted "
                                 "in); run a new check on the Cost page")
            out[d] = DayFigure("no_data", details=details)
            continue
        details["ce_adjustments_usd"] = round(hit.adjustments_usd, 6)     # credits / refunds, shown, not compared
        out[d] = DayFigure("figure", round(hit.usage_usd, 6), details)
    return out


def _usable_figure(f: DayFigure) -> DayFigure:
    """A provider figure below zero (credits or refunds netted in) is never compared or written (the table refuses a
    negative provider_usd): it becomes no_data with the reason, and rings no bell."""
    if f.status == "figure" and (f.provider_usd is None or f.provider_usd < 0):
        details = {**f.details, "reason": "the provider figure is below zero (credits or refunds netted in)",
                   "provider_figure_usd": f.provider_usd}
        return DayFigure("no_data", details=details)
    return f


async def _reconcile_connection(conn, key: str, c: conn_registry.ProviderConnection, account_of: dict,
                                policies: dict, logged: dict, day_list: list[date], start: date, end_exclusive: date,
                                fetchers: dict, own_account: Callable[[], str], job_id: Optional[str],
                                summary: dict) -> int:
    """The rows of one connection over the window (upserted); its totals and settled mismatches go into ``summary``.
    Returns the number of rows written."""
    source = SOURCES.get(c.kind, SOURCE_NONE)
    peers = sorted(k for k, a in account_of.items() if a and a == account_of.get(key)) or [key]
    if c.kind == "bedrock":
        figures = await _bedrock_figures(conn, c, peers, day_list, start, end_exclusive, own_account)
    elif c.kind in fetchers:
        figures = await _vendor_figures(c, cost_guard.policy_of(policies, key), day_list, start,
                                        end_exclusive, fetchers)
    else:
        figures = {d: DayFigure("unsupported", details={"reason": f"no cost source for kind {c.kind}"})
                   for d in day_list}
    counts: dict[str, int] = {}
    logged_total = provider_total = 0.0
    mismatches: list[dict] = []
    rows = 0
    for d in day_list:
        f = _usable_figure(figures[d])
        group = peers if c.kind == "bedrock" else [key]
        usd = sum(logged.get((k, d), (0.0, 0))[0] for k in group)
        calls = sum(logged.get((k, d), (0.0, 0))[1] for k in group)
        if f.status == "figure":
            status, diff, pct = compare(usd, f.provider_usd)
        else:
            status, diff, pct = f.status, None, None
        counts[status] = counts.get(status, 0) + 1
        logged_total += usd
        provider_total += f.provider_usd or 0.0
        if status == "mismatch" and not f.details.get("provisional"):
            mismatches.append({"connection_key": key, "day": d.isoformat(), "logged_usd": round(usd, 4),
                               "provider_usd": f.provider_usd, "diff_usd": diff})
        await conn.execute(_UPSERT_SQL, key, d, source if status != "unsupported" else SOURCE_NONE, status,
                           round(usd, 6), calls, f.provider_usd, diff, pct,
                           json.dumps(f.details, default=str), job_id)
        rows += 1
    summary["mismatches"].extend(mismatches)
    summary["connections"][key] = {"kind": c.kind, "source": source, "statuses": counts,
                                   "logged_usd": round(logged_total, 4), "provider_usd": round(provider_total, 4)}
    return rows


async def reconcile(pool, *, days: int = DEFAULT_DAYS, end_day: Optional[date] = None, job_id: Optional[str] = None,
                    today: Optional[date] = None, fetchers: Optional[dict] = None,
                    own_account: Callable[[], str] = _own_account) -> dict:
    """One reconcile run over ``days`` UTC days ending with ``end_day`` (default yesterday). Writes one row per
    connection and day, prunes rows older than RETENTION_DAYS, raises the mismatch bell. Returns the run summary.
    Raises ReconcileUnavailable before migration 317, ValueError for a bad window."""
    import asyncpg

    start, end_exclusive = window(days, end_day, today)
    day_list = days_of(start, end_exclusive)
    fetchers = VENDOR_FETCHERS if fetchers is None else fetchers
    async with pool.acquire() as conn:
        registry = await conn_registry.fetch_connections(conn)
        try:
            policies = await cost_guard.read_cost_policies(conn)
            await conn.fetchval("SELECT 1 FROM shared.llm_cost_reconcile LIMIT 1")
        except asyncpg.exceptions.UndefinedTableError as e:
            raise ReconcileUnavailable("shared.llm_cost_reconcile is missing (migration 317 not applied)") from e
        targets = {k: c for k, c in registry.items() if c.kind not in SKIPPED_KINDS}
        logged = await logged_costs(conn, sorted(targets), start, end_exclusive)

        # Cost Explorer bills per AWS account: Bedrock connections on the same account are compared together.
        account_of: dict[str, Optional[str]] = {}
        for k, c in targets.items():
            if c.kind == "bedrock":
                try:
                    account_of[k] = await asyncio.to_thread(_bedrock_account, c, own_account)
                except Exception:  # noqa: BLE001 — reported per connection by _bedrock_figures
                    account_of[k] = None

        summary: dict[str, Any] = {"window": {"start": start.isoformat(), "end": (end_exclusive - timedelta(days=1))
                                              .isoformat(), "days": days}, "connections": {}, "mismatches": []}
        rows = 0
        errors: dict[str, str] = {}
        for key in sorted(targets):
            try:
                rows += await _reconcile_connection(conn, key, targets[key], account_of, policies, logged, day_list,
                                                    start, end_exclusive, fetchers, own_account, job_id, summary)
            except Exception as e:  # noqa: BLE001 — one connection's failure never costs the other connections' rows
                errors[key] = f"{type(e).__name__}: {str(e)[:300]}"
                summary["connections"][key] = {"kind": targets[key].kind, "error": errors[key]}
                logger.warning("llm_cost_reconcile_connection_failed", connection=key, error=errors[key])
        if errors:
            summary["errors"] = errors
        if targets and len(errors) == len(targets):
            raise RuntimeError("llm_cost_reconcile: every connection failed: "
                               + "; ".join(f"{k}: {v}" for k, v in sorted(errors.items())))
        pruned = await conn.execute(_PRUNE_SQL, (today or datetime.now(timezone.utc).date())
                                    - timedelta(days=RETENTION_DAYS))
        summary["rows_written"] = rows
        summary["pruned"] = int(str(pruned).rsplit(" ", 1)[-1]) if pruned else 0
        if summary["mismatches"]:
            summary["bell"] = await _mismatch_bell(conn, summary["mismatches"], summary["window"])
    logger.info("llm_cost_reconciled", window=summary["window"], rows=rows, mismatches=len(summary["mismatches"]))
    return summary


async def _mismatch_bell(conn, mismatches: list[dict], window: Optional[dict] = None) -> bool:
    """One admin bell per 24 h for the run's settled mismatches, and (AA-642 S263 L2) its e-mail. Best effort."""
    try:
        if await conn.fetchval(_RECENT_BELL_SQL, MISMATCH_EVENT, "llm_cost_reconcile"):
            return False
        keys = sorted({m["connection_key"] for m in mismatches})
        message = (f"LLM cost reconciliation: the logged cost differs from the provider's figure on "
                   f"{len(mismatches)} connection-day(s) ({', '.join(keys)}). Settings › LLM Models › Connections.")
        await conn.execute(_INSERT_BELL_SQL, _PLATFORM_TENANT_ID, MISMATCH_EVENT, "llm_cost_reconcile",
                           json.dumps({"message": message, "mismatches": mismatches[:20]}, default=str))
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_cost_reconcile_bell_failed", error=str(e)[:300])
        return False
    await _mismatch_email(conn, mismatches, window)
    return True


async def _mismatch_email(conn, mismatches: list[dict], window: Optional[dict]) -> None:
    """AA-642 (S263 L2, B4): the operator e-mail of a new mismatch bell (at most one per UTC day). Best effort."""
    try:
        from shared import ops_alerts
        await ops_alerts.enqueue_llm_cost_mismatch_email(conn, details={
            "window": window or {}, "mismatches": json.loads(json.dumps(mismatches, default=str))})
    except Exception as e:  # noqa: BLE001 — the bell is written; a failed e-mail enqueue is logged only
        logger.warning("llm_cost_reconcile_email_failed", error=str(e)[:300])


# ── the admin read ───────────────────────────────────────────────────────────────────────────────────────────────

_LATEST_SQL = """
    SELECT connection_key, day, source, status, logged_usd, logged_calls, provider_usd, diff_usd, diff_pct, details,
           checked_at
      FROM shared.llm_cost_reconcile
     WHERE day >= $1
     ORDER BY connection_key, day DESC
"""


def _num(v) -> Optional[float]:
    return float(v) if v is not None else None


async def read_recent(conn, days: int = 14, today: Optional[date] = None) -> Optional[dict[str, list[dict]]]:
    """{connection_key: [row, ...] newest day first} for the last ``days`` days; None before migration 317."""
    import asyncpg

    since = (today or datetime.now(timezone.utc).date()) - timedelta(days=days)
    try:
        rows = await conn.fetch(_LATEST_SQL, since)
    except asyncpg.exceptions.UndefinedTableError:
        return None
    out: dict[str, list[dict]] = {}
    for r in rows:
        details = r["details"]
        out.setdefault(r["connection_key"], []).append({
            "day": r["day"].isoformat(), "source": r["source"], "status": r["status"],
            "logged_usd": _num(r["logged_usd"]), "logged_calls": r["logged_calls"],
            "provider_usd": _num(r["provider_usd"]), "diff_usd": _num(r["diff_usd"]), "diff_pct": _num(r["diff_pct"]),
            "details": json.loads(details) if isinstance(details, str) else (details or {}),
            "checked_at": r["checked_at"].isoformat() if r["checked_at"] else None,
        })
    return out
