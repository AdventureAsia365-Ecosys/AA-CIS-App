"""shared/aws_client/cost_explorer.py — AA-623 satellite Cost Explorer client + snapshot storage.

Two concerns in one module, mirroring shared/dfs_client/balance.py's split (AA-627):
  - fetch_all_accounts_cost(): live AWS calls (ce:GetCostAndUsage), grouped by SERVICE, one call
    per AWS account. Costs ~$0.01/request per AWS's own CE pricing docs -- callers should cache,
    not call this on every page view.
  - record_snapshot() / read_latest_snapshot(): DB persistence, same fetch-then-read split as
    balance.py (POST /admin/cost-explorer/check writes, GET /admin/cost-explorer reads latest).

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
from typing import Any, Optional

import asyncpg
import boto3
import structlog
from botocore.exceptions import ClientError

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
    try:
        client = _get_ce_client(account)
        resp = client.get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
    except ClientError as e:
        raise CostExplorerUnavailable(f"GetCostAndUsage failed for {account} ({account_id}): {e}") from e

    rows: list[dict[str, Any]] = []
    for result_by_time in resp.get("ResultsByTime", []):
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

_LATEST_BATCH_FETCHED_AT_SQL = """
    SELECT fetched_at FROM shared.cost_explorer_snapshot
     ORDER BY fetched_at DESC
     LIMIT 1
"""

_LATEST_BATCH_ROWS_SQL = """
    SELECT account_id, service, period_start, period_end, amount_usd, unit, fetched_at
      FROM shared.cost_explorer_snapshot
     WHERE fetched_at = $1
     ORDER BY account_id, service
"""


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    if d.get("amount_usd") is not None:
        d["amount_usd"] = float(d["amount_usd"])
    if d.get("period_start") is not None:
        d["period_start"] = d["period_start"].isoformat()
    if d.get("period_end") is not None:
        d["period_end"] = d["period_end"].isoformat()
    if d.get("fetched_at") is not None:
        d["fetched_at"] = d["fetched_at"].isoformat()
    return d


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
                    row["account_id"], row["service"], row["period_start"], row["period_end"],
                    row["amount_usd"], row["unit"], json.dumps(row.get("raw")),
                )
    logger.info("cost_explorer_snapshot_written", row_count=len(rows))
    return len(rows)


async def read_latest_snapshot(pool: asyncpg.Pool) -> Optional[dict[str, Any]]:
    """Return the most recent fetch batch (all rows sharing the latest fetched_at), or None if
    no snapshot has been recorded yet."""
    async with pool.acquire() as conn:
        latest_fetched_at = await conn.fetchval(_LATEST_BATCH_FETCHED_AT_SQL)
        if latest_fetched_at is None:
            return None
        db_rows = await conn.fetch(_LATEST_BATCH_ROWS_SQL, latest_fetched_at)
    rows = [_row_to_dict(r) for r in db_rows]
    total_usd = round(sum(r["amount_usd"] for r in rows), 4)
    return {"rows": rows, "total_usd": total_usd, "fetched_at": rows[0]["fetched_at"] if rows else None}
