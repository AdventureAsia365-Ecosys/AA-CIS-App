"""shared/aws_client/cost_explorer.py — AA-623 satellite Cost Explorer client + snapshot storage.

Two concerns in one module, mirroring shared/dfs_client/balance.py's split (AA-627):
  - fetch_all_accounts_cost(): live AWS calls (ce:GetCostAndUsage), grouped by SERVICE, one call
    per AWS account. Costs ~$0.01/request per AWS's own CE pricing docs -- callers should cache,
    not call this on every page view.
  - record_snapshot() / read_cost_range(): DB persistence, same fetch-then-read split as
    balance.py (POST /admin/cost-explorer/check writes, GET /admin/cost-explorer reads a date
    range, Bedrock vs infra split per account).

Cross-account AssumeRole reuses the exact pattern shared/llm_client/bedrock_satellite.py already
verified in production (AA-296/AA-397): acc2's own ECS task role can call ce:GetCostAndUsage
directly (modules/ecs/main.tf's ecs_task_infra_status_readonly, AA-623 PR #68); acc1/acc3 need
STS AssumeRole into their own AA-CostExplorer-Reader / AA3-CostExplorer-Reader roles (AA-CIS-Infra
PR #69) because acc2 is a member account, not the AWS Organizations payer -- confirmed via
`aws organizations describe-organization` (payer is 033086823579, unrelated to acc1/acc2/acc3),
so there is no single consolidated CE call across all three.

Distinct session cache from bedrock_satellite.py's (different role, different ExternalId) --
does NOT reuse or modify that module's cache dicts.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Any, Optional

import asyncpg
import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError

logger = structlog.get_logger()

# ---------------------------------------------------------------- config
ACC1_ACCOUNT_ID = "867490540162"
ACC1_ROLE_ARN = f"arn:aws:iam::{ACC1_ACCOUNT_ID}:role/AA-CostExplorer-Reader"
ACC1_EXTERNAL_ID = "aa623-satellite-cost-explorer"

ACC2_ACCOUNT_ID = "005097885195"  # direct call, own ECS task role -- no AssumeRole needed

ACC3_ACCOUNT_ID = "786888028788"
ACC3_ROLE_ARN = f"arn:aws:iam::{ACC3_ACCOUNT_ID}:role/AA3-CostExplorer-Reader"
ACC3_EXTERNAL_ID = "aa623-satellite-cost-explorer-acc3"

# Cost Explorer is a global (non-regional) API endpoint, but the SDK client still needs a
# region -- us-east-1 is AWS's documented convention for CE, independent of where the app's
# other resources (us-west-1) live.
_CE_REGION = "us-east-1"

_SATELLITE_ACCOUNTS = {
    "acc1": {"role_arn": ACC1_ROLE_ARN, "external_id": ACC1_EXTERNAL_ID, "account_id": ACC1_ACCOUNT_ID},
    "acc3": {"role_arn": ACC3_ROLE_ARN, "external_id": ACC3_EXTERNAL_ID, "account_id": ACC3_ACCOUNT_ID},
}

# session cache -- avoid AssumeRole on every call (STS default session 1h).
_cached_sessions: dict[str, boto3.Session] = {}
_cached_session_expiry: dict[str, float] = {}
_SESSION_REFRESH_MARGIN_SECONDS = 300  # refresh 5 min before real expiry


class CostExplorerUnavailable(Exception):
    """Raised on AssumeRole or GetCostAndUsage failure for one account. Caller decides whether
    to skip that account (partial result) or fail the whole check -- see fetch_all_accounts_cost."""
    pass


def _get_satellite_session(account: str) -> boto3.Session:
    if account not in _SATELLITE_ACCOUNTS:
        raise ValueError(f"Unknown satellite account: {account!r} (valid: {list(_SATELLITE_ACCOUNTS)})")
    cfg = _SATELLITE_ACCOUNTS[account]

    now = time.time()
    cached = _cached_sessions.get(account)
    if cached is not None and now < _cached_session_expiry.get(account, 0.0):
        return cached

    sts = boto3.client("sts")  # uses the current ECS task role identity (acc2)
    try:
        resp = sts.assume_role(
            RoleArn=cfg["role_arn"],
            RoleSessionName=f"aa-cis-ecs-ce-{account}-{int(now)}",
            ExternalId=cfg["external_id"],
            DurationSeconds=3600,
        )
    except ClientError as e:
        raise CostExplorerUnavailable(f"AssumeRole to CE satellite ({account}) failed: {e}") from e

    creds = resp["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    _cached_sessions[account] = session
    _cached_session_expiry[account] = creds["Expiration"].timestamp() - _SESSION_REFRESH_MARGIN_SECONDS
    return session


def _get_ce_client(account: str):
    """account: "acc1" | "acc2" | "acc3". acc2 uses the ECS task's own credentials directly
    (no AssumeRole -- it already has ce:GetCostAndUsage on its own task role, AA-623 PR #68)."""
    if account == "acc2":
        return boto3.client("ce", region_name=_CE_REGION)
    session = _get_satellite_session(account)
    return session.client("ce", region_name=_CE_REGION)


def _fetch_one_account_cost(account: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """One ce:GetCostAndUsage call for one account, grouped by SERVICE. Returns a list of row
    dicts ready for record_snapshot(). Raises CostExplorerUnavailable on failure -- caller in
    fetch_all_accounts_cost() decides whether to skip this account or propagate."""
    account_id = {"acc1": ACC1_ACCOUNT_ID, "acc2": ACC2_ACCOUNT_ID, "acc3": ACC3_ACCOUNT_ID}[account]
    # DAILY x GroupBy SERVICE paginates on longer ranges (NextPageToken) -- without the loop a
    # 30/90-day fetch silently drops every day after the first page.
    results_by_time: list[dict[str, Any]] = []
    try:
        client = _get_ce_client(account)
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start_date, "End": end_date},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        while True:
            resp = client.get_cost_and_usage(**kwargs)
            results_by_time.extend(resp.get("ResultsByTime", []))
            token = resp.get("NextPageToken")
            if not token:
                break
            kwargs["NextPageToken"] = token
    except ClientError as e:
        raise CostExplorerUnavailable(f"GetCostAndUsage failed for {account} ({account_id}): {e}") from e

    rows: list[dict[str, Any]] = []
    for result_by_time in results_by_time:
        period_start = result_by_time["TimePeriod"]["Start"]
        period_end = result_by_time["TimePeriod"]["End"]
        for group in result_by_time.get("Groups", []):
            service = group["Keys"][0] if group.get("Keys") else "Unknown"
            metric = group.get("Metrics", {}).get("UnblendedCost", {})
            amount = metric.get("Amount")
            unit = metric.get("Unit", "USD")
            if amount is None:
                continue
            rows.append({
                "account_id": account_id,
                "service": service,
                "period_start": period_start,
                "period_end": period_end,
                "amount_usd": float(amount),
                "unit": unit,
                "raw": group,
            })
    return rows


def fetch_all_accounts_cost(start_date: str, end_date: str) -> dict[str, Any]:
    """Fetch Cost Explorer data for acc1/acc2/acc3 independently (no single consolidated call --
    acc2 is not the Organizations payer, see module docstring). A failure on one satellite
    account (e.g. AssumeRole not yet applied) does NOT fail the whole check -- that account's
    rows are simply omitted and listed under "errors", same partial-result philosophy as
    admin_llm_ops.py's per-stage config reads. Raises only if ALL accounts fail.

    start_date/end_date: 'YYYY-MM-DD' strings, CE's TimePeriod format (End is exclusive).
    """
    rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for account in ("acc1", "acc2", "acc3"):
        try:
            rows.extend(_fetch_one_account_cost(account, start_date, end_date))
        except CostExplorerUnavailable as e:
            logger.warning("cost_explorer_account_fetch_failed", account=account, error=str(e))
            errors[account] = str(e)

    if len(errors) == 3:
        raise CostExplorerUnavailable(f"Cost Explorer fetch failed for all accounts: {errors}")

    return {"rows": rows, "errors": errors}


# ---------------------------------------------------------------- DB persistence
_INSERT_SQL = """
    INSERT INTO shared.cost_explorer_snapshot
        (account_id, service, period_start, period_end, amount_usd, unit, raw)
    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
"""

# Snapshots accumulate (every check inserts a fresh batch), and overlapping checks cover the same
# days -- the most recent fetch of a given (account, service, day) wins, since CE revises the
# latest ~24-48h upward as usage finalizes.
_RANGE_ROWS_SQL = """
    SELECT DISTINCT ON (account_id, service, period_start)
           account_id, service, period_start, amount_usd, fetched_at
      FROM shared.cost_explorer_snapshot
     WHERE period_start >= $1 AND period_start < $2
     ORDER BY account_id, service, period_start, fetched_at DESC
"""


def is_bedrock_service(service: str) -> bool:
    """CE splits Bedrock spend across several SERVICE line items: 'Amazon Bedrock' plus one per
    Marketplace model ('Claude Sonnet 4.6 (Amazon Bedrock Edition)', 'Cohere Embed 4 Model
    (Amazon Bedrock Edition)', 'Palmyra X5 (Amazon Bedrock Edition)', ...) -- all contain
    'Bedrock'. Everything else (ECS, RDS, ELB, ...) is infra, not LLM spend."""
    return "bedrock" in service.lower()


def _to_date(value: Any) -> date:
    """CE's ResultsByTime gives period start/end as 'YYYY-MM-DD' strings -- asyncpg's date
    codec requires an actual date object (calls .toordinal() on it), it does not parse
    strings itself. record_snapshot() rows may already carry a date (e.g. re-inserted from a
    prior read) or a string (fresh from _fetch_one_account_cost) -- accept both."""
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


async def record_snapshot(pool: asyncpg.Pool, rows: list[dict[str, Any]]) -> int:
    """Insert one batch of Cost Explorer rows (same fetched_at for the whole batch, via
    now() default -- inserted in one transaction so a partial-write batch cannot happen).
    Returns the number of rows written. Empty `rows` is a no-op (0 written), not an error --
    e.g. all 3 accounts failed to fetch but the caller chose to record errors elsewhere."""
    if not rows:
        return 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for row in rows:
                await conn.execute(
                    _INSERT_SQL,
                    row["account_id"], row["service"],
                    _to_date(row["period_start"]), _to_date(row["period_end"]),
                    row["amount_usd"], row["unit"], json.dumps(row.get("raw")),
                )
    logger.info("cost_explorer_snapshot_written", row_count=len(rows))
    return len(rows)


async def read_cost_range(pool: asyncpg.Pool, start: date, end_exclusive: date) -> Optional[dict[str, Any]]:
    """Stored CE cost for days in [start, end_exclusive), newest fetch per (account, service, day),
    split into Bedrock (LLM) vs other (infra) per account. None if no stored day falls in range.

    `covered_from`/`covered_to` are the first/last day actually present -- when they don't span
    the requested range, the caller should fetch that range from AWS first (a check only stores
    the window it was asked for)."""
    async with pool.acquire() as conn:
        db_rows = await conn.fetch(_RANGE_ROWS_SQL, start, end_exclusive)
    if not db_rows:
        return None

    accounts: dict[str, dict[str, Any]] = {}
    services: dict[tuple[str, str], float] = {}
    for r in db_rows:
        amount = float(r["amount_usd"])
        acc = accounts.setdefault(r["account_id"], {"account_id": r["account_id"], "total_usd": 0.0,
                                                      "bedrock_usd": 0.0, "other_usd": 0.0})
        acc["total_usd"] += amount
        acc["bedrock_usd" if is_bedrock_service(r["service"]) else "other_usd"] += amount
        key = (r["account_id"], r["service"])
        services[key] = services.get(key, 0.0) + amount

    for acc in accounts.values():
        for k in ("total_usd", "bedrock_usd", "other_usd"):
            acc[k] = round(acc[k], 4)
    service_rows = sorted(
        ({"account_id": a, "service": s, "amount_usd": round(v, 4), "is_bedrock": is_bedrock_service(s)}
         for (a, s), v in services.items()),
        key=lambda x: -x["amount_usd"],
    )
    return {
        "accounts": sorted(accounts.values(), key=lambda x: -x["total_usd"]),
        "services": service_rows,
        "total_usd": round(sum(a["total_usd"] for a in accounts.values()), 4),
        "bedrock_usd": round(sum(a["bedrock_usd"] for a in accounts.values()), 4),
        "covered_from": min(r["period_start"] for r in db_rows).isoformat(),
        "covered_to": max(r["period_start"] for r in db_rows).isoformat(),
        "fetched_at": max(r["fetched_at"] for r in db_rows).isoformat(),
    }


# ---------------------------------------------------------------- AA-884: acc2 cost by App tag
# One GetCostAndUsage call on acc2 (the only account whose resources carry the App cost-allocation tag; acc1/acc3
# are LLM satellites, attributed from shared.llm_call_log instead), DAILY, grouped by TAG App and SERVICE. CE returns
# the tag group key as "App$<value>"; "App$" (empty value) is untagged spend. Same ~$0.01 per request as above
# (pagination pages are further requests). Stored in shared.cost_explorer_app_snapshot (CIS 248).
APP_TAG_KEY = "App"
CE_REQUEST_COST_USD = 0.01


def strip_app_tag(key: str) -> str:
    """'App$cis' -> 'cis'; 'App$' -> '' (untagged). A key without the prefix is returned unchanged."""
    prefix = f"{APP_TAG_KEY}$"
    return key[len(prefix):] if key.startswith(prefix) else key


def fetch_acc2_cost_by_app(start_date: str, end_date: str) -> dict[str, Any]:
    """acc2 UnblendedCost per (day, App tag, service) for [start_date, end_date) ('YYYY-MM-DD', End exclusive).

    Returns {"rows": [...], "requests": n}; rows are ready for record_app_snapshot(). Raises CostExplorerUnavailable
    on any AWS error (the caller keeps its stored data)."""
    results_by_time: list[dict[str, Any]] = []
    requests = 0
    try:
        client = _get_ce_client("acc2")
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start_date, "End": end_date},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "TAG", "Key": APP_TAG_KEY}, {"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        while True:
            resp = client.get_cost_and_usage(**kwargs)
            requests += 1
            results_by_time.extend(resp.get("ResultsByTime", []))
            token = resp.get("NextPageToken")
            if not token:
                break
            kwargs["NextPageToken"] = token
    except (ClientError, BotoCoreError, CostExplorerUnavailable) as e:
        raise CostExplorerUnavailable(f"GetCostAndUsage by App tag failed for acc2 ({ACC2_ACCOUNT_ID}): {e}") from e

    rows: list[dict[str, Any]] = []
    for rbt in results_by_time:
        period_start, period_end = rbt["TimePeriod"]["Start"], rbt["TimePeriod"]["End"]
        for group in rbt.get("Groups", []):
            keys = group.get("Keys") or []
            tag = strip_app_tag(keys[0]) if keys else ""
            service = keys[1] if len(keys) > 1 else "Unknown"
            metric = group.get("Metrics", {}).get("UnblendedCost", {})
            amount = metric.get("Amount")
            if amount is None:
                continue
            rows.append({"account_id": ACC2_ACCOUNT_ID, "app_tag": tag, "service": service,
                         "period_start": period_start, "period_end": period_end,
                         "amount_usd": float(amount), "unit": metric.get("Unit", "USD")})
    return {"rows": rows, "requests": requests}


_APP_INSERT_SQL = """
    INSERT INTO shared.cost_explorer_app_snapshot
        (account_id, app_tag, service, period_start, period_end, amount_usd, unit)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

# Newest fetch per (account, tag, service, day): CE revises the last ~24-48 h upward, like the SERVICE snapshot.
_APP_RANGE_ROWS_SQL = """
    SELECT DISTINCT ON (account_id, app_tag, service, period_start)
           account_id, app_tag, service, period_start, amount_usd, fetched_at
      FROM shared.cost_explorer_app_snapshot
     WHERE period_start >= $1 AND period_start < $2
     ORDER BY account_id, app_tag, service, period_start, fetched_at DESC
"""


async def record_app_snapshot(pool: asyncpg.Pool, rows: list[dict[str, Any]]) -> int:
    """Insert one batch (one transaction, one fetched_at via now()). Empty rows: 0, no-op."""
    if not rows:
        return 0
    args = [(r["account_id"], r["app_tag"], r["service"], _to_date(r["period_start"]), _to_date(r["period_end"]),
             r["amount_usd"], r["unit"]) for r in rows]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(_APP_INSERT_SQL, args)
    logger.info("cost_explorer_app_snapshot_written", row_count=len(rows))
    return len(rows)


async def read_app_cost_range(pool: asyncpg.Pool, start: date, end_exclusive: date) -> Optional[dict[str, Any]]:
    """Stored acc2 cost per App tag for days in [start, end_exclusive): {"tags": {tag: {"total_usd", "bedrock_usd",
    "services": [{"service", "amount_usd", "is_bedrock"}]}}, "total_usd", "covered_from", "covered_to",
    "fetched_at" (newest), "oldest_fetched_at", "day_fetched_at": {day ISO: newest fetched_at ISO of that day}}.
    None when no stored day falls in the range."""
    async with pool.acquire() as conn:
        db_rows = await conn.fetch(_APP_RANGE_ROWS_SQL, start, end_exclusive)
    if not db_rows:
        return None
    tags: dict[str, dict[str, Any]] = {}
    per_service: dict[tuple[str, str], float] = {}
    day_fetched: dict[date, datetime] = {}
    for r in db_rows:
        d = r["period_start"]
        if d not in day_fetched or r["fetched_at"] > day_fetched[d]:
            day_fetched[d] = r["fetched_at"]
        amount = float(r["amount_usd"])
        t = tags.setdefault(r["app_tag"], {"total_usd": 0.0, "bedrock_usd": 0.0})
        t["total_usd"] += amount
        if is_bedrock_service(r["service"]):
            t["bedrock_usd"] += amount
        per_service[(r["app_tag"], r["service"])] = per_service.get((r["app_tag"], r["service"]), 0.0) + amount
    for tag, t in tags.items():
        t["total_usd"] = round(t["total_usd"], 4)
        t["bedrock_usd"] = round(t["bedrock_usd"], 4)
        t["services"] = sorted(
            ({"service": s, "amount_usd": round(v, 4), "is_bedrock": is_bedrock_service(s)}
             for (tg, s), v in per_service.items() if tg == tag),
            key=lambda x: -x["amount_usd"])
    return {
        "tags": tags,
        "total_usd": round(sum(t["total_usd"] for t in tags.values()), 4),
        "covered_from": min(r["period_start"] for r in db_rows).isoformat(),
        "covered_to": max(r["period_start"] for r in db_rows).isoformat(),
        "fetched_at": max(r["fetched_at"] for r in db_rows).isoformat(),
        "oldest_fetched_at": min(r["fetched_at"] for r in db_rows).isoformat(),
        "day_fetched_at": {d.isoformat(): f.isoformat() for d, f in sorted(day_fetched.items())},
    }
