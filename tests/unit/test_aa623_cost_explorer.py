"""AA-623 — AWS Cost Explorer actual-spend reconcile (satellite AssumeRole + snapshot storage).

Covers: shared/aws_client/cost_explorer.py's per-account fetch (boto3 CE client mocked, no live
AWS), the AssumeRole satellite session cache, and record_snapshot()/read_latest_snapshot()
(asyncpg mocked, no live DB). Mirrors tests/unit/test_aa627_dfs_balance.py's mocking style.
No live AWS / no live DB.
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError


def _reset_session_cache():
    from shared.aws_client import cost_explorer as ce_mod
    ce_mod._cached_sessions.clear()
    ce_mod._cached_session_expiry.clear()


# ── _fetch_one_account_cost ──────────────────────────────────────────────────

def test_fetch_one_account_cost_acc2_uses_direct_client_no_assume_role():
    """acc2 must call boto3.client('ce', ...) directly -- no STS AssumeRole."""
    from shared.aws_client import cost_explorer as ce_mod

    fake_ce_client = MagicMock()
    fake_ce_client.get_cost_and_usage.return_value = {
        "ResultsByTime": [{
            "TimePeriod": {"Start": "2026-09-16", "End": "2026-09-17"},
            "Groups": [
                {"Keys": ["Amazon Elastic Compute Cloud"],
                 "Metrics": {"UnblendedCost": {"Amount": "1.2300", "Unit": "USD"}}},
            ],
        }],
    }
    with patch("shared.aws_client.cost_explorer.boto3.client", return_value=fake_ce_client) as mock_client:
        rows = ce_mod._fetch_one_account_cost("acc2", "2026-09-16", "2026-09-17")

    mock_client.assert_called_once_with("ce", region_name="us-east-1")
    assert len(rows) == 1
    assert rows[0]["account_id"] == ce_mod.ACC2_ACCOUNT_ID
    assert rows[0]["service"] == "Amazon Elastic Compute Cloud"
    assert rows[0]["amount_usd"] == 1.23
    assert rows[0]["period_start"] == "2026-09-16"


def test_fetch_one_account_cost_acc1_assumes_role_first():
    """acc1/acc3 must go through STS AssumeRole (satellite session) before calling CE."""
    from shared.aws_client import cost_explorer as ce_mod
    _reset_session_cache()

    fake_sts = MagicMock()
    fake_sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "AKIA...", "SecretAccessKey": "secret", "SessionToken": "token",
            "Expiration": MagicMock(timestamp=lambda: time.time() + 3600),
        }
    }
    fake_ce_client = MagicMock()
    fake_ce_client.get_cost_and_usage.return_value = {"ResultsByTime": []}
    fake_session = MagicMock()
    fake_session.client.return_value = fake_ce_client

    with patch("shared.aws_client.cost_explorer.boto3.client", return_value=fake_sts), \
         patch("shared.aws_client.cost_explorer.boto3.Session", return_value=fake_session):
        rows = ce_mod._fetch_one_account_cost("acc1", "2026-09-16", "2026-09-17")

    fake_sts.assume_role.assert_called_once()
    call_kwargs = fake_sts.assume_role.call_args.kwargs
    assert call_kwargs["RoleArn"] == ce_mod.ACC1_ROLE_ARN
    assert call_kwargs["ExternalId"] == ce_mod.ACC1_EXTERNAL_ID
    assert rows == []
    _reset_session_cache()


def test_fetch_one_account_cost_raises_unavailable_on_client_error():
    from shared.aws_client import cost_explorer as ce_mod

    fake_ce_client = MagicMock()
    fake_ce_client.get_cost_and_usage.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "GetCostAndUsage"
    )
    with patch("shared.aws_client.cost_explorer.boto3.client", return_value=fake_ce_client):
        with pytest.raises(ce_mod.CostExplorerUnavailable):
            ce_mod._fetch_one_account_cost("acc2", "2026-09-16", "2026-09-17")


# ── fetch_all_accounts_cost — partial-failure tolerance ─────────────────────

def test_fetch_all_accounts_cost_partial_failure_keeps_other_accounts():
    """If acc1 fails (e.g. AssumeRole not applied yet) but acc2/acc3 succeed, the result must
    still contain acc2/acc3's rows plus an entry under errors for acc1 -- not raise."""
    from shared.aws_client import cost_explorer as ce_mod

    def fake_fetch_one(account, start, end):
        if account == "acc1":
            raise ce_mod.CostExplorerUnavailable("AssumeRole not applied yet")
        return [{"account_id": account, "service": "X", "period_start": start,
                  "period_end": end, "amount_usd": 1.0, "unit": "USD", "raw": {}}]

    with patch("shared.aws_client.cost_explorer._fetch_one_account_cost", side_effect=fake_fetch_one):
        result = ce_mod.fetch_all_accounts_cost("2026-09-16", "2026-09-17")

    assert "acc1" in result["errors"]
    assert len(result["rows"]) == 2  # acc2 + acc3
    assert {r["account_id"] for r in result["rows"]} == {"acc2", "acc3"}


def test_fetch_all_accounts_cost_raises_when_all_fail():
    from shared.aws_client import cost_explorer as ce_mod

    def fake_fetch_one(account, start, end):
        raise ce_mod.CostExplorerUnavailable(f"{account} down")

    with patch("shared.aws_client.cost_explorer._fetch_one_account_cost", side_effect=fake_fetch_one):
        with pytest.raises(ce_mod.CostExplorerUnavailable):
            ce_mod.fetch_all_accounts_cost("2026-09-16", "2026-09-17")


# ── record_snapshot / read_latest_snapshot ──────────────────────────────────

def test_to_date_converts_ce_date_strings():
    """Regression for a live bug (AA-623): CE's ResultsByTime gives 'YYYY-MM-DD' strings for
    period start/end, but asyncpg's date codec requires an actual date object -- passing the
    string through raised asyncpg.exceptions.DataError at INSERT time in production. Caught
    only because mocked tests below don't exercise asyncpg's real type checking."""
    from datetime import date as date_cls

    from shared.aws_client import cost_explorer as ce_mod

    assert ce_mod._to_date("2026-09-16") == date_cls(2026, 9, 16)
    assert ce_mod._to_date(date_cls(2026, 9, 16)) == date_cls(2026, 9, 16)


@pytest.mark.asyncio
async def test_record_snapshot_writes_all_rows_in_one_transaction():
    from shared.aws_client import cost_explorer as ce_mod

    rows = [
        {"account_id": "005097885195", "service": "ECS", "period_start": "2026-09-16",
         "period_end": "2026-09-17", "amount_usd": 1.0, "unit": "USD", "raw": {}},
        {"account_id": "005097885195", "service": "S3", "period_start": "2026-09-16",
         "period_end": "2026-09-17", "amount_usd": 2.0, "unit": "USD", "raw": {}},
    ]
    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock()
    fake_tx = MagicMock()
    fake_tx.__aenter__ = AsyncMock(return_value=None)
    fake_tx.__aexit__ = AsyncMock(return_value=False)
    fake_conn.transaction = MagicMock(return_value=fake_tx)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    written = await ce_mod.record_snapshot(pool, rows)
    assert written == 2
    assert fake_conn.execute.await_count == 2
    # regression guard: period_start/end must be real date objects by the time they reach
    # conn.execute, not the raw 'YYYY-MM-DD' strings CE returns (see test_to_date_* above).
    from datetime import date as date_cls
    first_call_args = fake_conn.execute.call_args_list[0].args
    assert isinstance(first_call_args[3], date_cls)
    assert isinstance(first_call_args[4], date_cls)


@pytest.mark.asyncio
async def test_record_snapshot_empty_rows_is_noop():
    from shared.aws_client import cost_explorer as ce_mod

    pool = MagicMock()
    written = await ce_mod.record_snapshot(pool, [])
    assert written == 0
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_read_latest_snapshot_none_when_empty():
    from shared.aws_client import cost_explorer as ce_mod

    fake_conn = AsyncMock()
    fake_conn.fetchval = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    assert await ce_mod.read_latest_snapshot(pool) is None


@pytest.mark.asyncio
async def test_read_latest_snapshot_sums_total_usd():
    from shared.aws_client import cost_explorer as ce_mod

    fetched_at = MagicMock(isoformat=lambda: "2026-09-23T00:00:00+00:00")
    db_rows = [
        {"account_id": "005097885195", "service": "ECS",
         "period_start": MagicMock(isoformat=lambda: "2026-09-16"),
         "period_end": MagicMock(isoformat=lambda: "2026-09-17"),
         "amount_usd": 1.5, "unit": "USD", "fetched_at": fetched_at},
        {"account_id": "867490540162", "service": "Bedrock",
         "period_start": MagicMock(isoformat=lambda: "2026-09-16"),
         "period_end": MagicMock(isoformat=lambda: "2026-09-17"),
         "amount_usd": 2.5, "unit": "USD", "fetched_at": fetched_at},
    ]
    fake_conn = AsyncMock()
    fake_conn.fetchval = AsyncMock(return_value=fetched_at)
    fake_conn.fetch = AsyncMock(return_value=db_rows)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await ce_mod.read_latest_snapshot(pool)
    assert result["total_usd"] == 4.0
    assert len(result["rows"]) == 2


# ── endpoints (admin_llm_ops.py) ─────────────────────────────────────────────

def _make_pool(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def _make_request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


@pytest.mark.asyncio
async def test_check_cost_explorer_endpoint_verifies_secret_and_persists():
    from api.routers import admin_llm_ops

    fake_result = {"rows": [{"account_id": "005097885195", "service": "ECS",
                              "period_start": "2026-09-16", "period_end": "2026-09-17",
                              "amount_usd": 1.0, "unit": "USD", "raw": {}}], "errors": {}}

    with patch.object(admin_llm_ops, "verify_admin_secret") as mock_verify, \
         patch.object(admin_llm_ops, "fetch_all_accounts_cost", return_value=fake_result), \
         patch.object(admin_llm_ops, "record_snapshot", AsyncMock(return_value=1)) as mock_record:
        result = await admin_llm_ops.check_cost_explorer(
            _make_request(_make_pool(AsyncMock())), x_admin_secret="s3cr3t",
        )

    mock_verify.assert_called_once_with("s3cr3t")
    mock_record.assert_awaited_once()
    assert result["row_count"] == 1
    assert result["errors"] == {}


@pytest.mark.asyncio
async def test_get_cost_explorer_endpoint_no_data():
    from api.routers import admin_llm_ops

    with patch.object(admin_llm_ops, "read_latest_snapshot", AsyncMock(return_value=None)):
        result = await admin_llm_ops.get_cost_explorer(_make_request(_make_pool(AsyncMock())))

    assert result["has_data"] is False
    assert result["rows"] == []


@pytest.mark.asyncio
async def test_get_cost_explorer_endpoint_with_data():
    from api.routers import admin_llm_ops

    fake_latest = {"rows": [{"account_id": "005097885195"}], "total_usd": 1.0, "fetched_at": "x"}
    with patch.object(admin_llm_ops, "read_latest_snapshot", AsyncMock(return_value=fake_latest)):
        result = await admin_llm_ops.get_cost_explorer(_make_request(_make_pool(AsyncMock())))

    assert result["has_data"] is True
    assert result["total_usd"] == 1.0
