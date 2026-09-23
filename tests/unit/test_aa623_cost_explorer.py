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


def test_fetch_one_account_cost_follows_next_page_token():
    """DAILY x GroupBy SERVICE paginates on longer windows -- every page must be read, not just
    the first (the original fetch silently dropped later days)."""
    from shared.aws_client import cost_explorer as ce_mod

    def page(day, token=None):
        resp = {"ResultsByTime": [{
            "TimePeriod": {"Start": day, "End": day},
            "Groups": [{"Keys": ["AWS WAF"], "Metrics": {"UnblendedCost": {"Amount": "0.31", "Unit": "USD"}}}],
        }]}
        if token:
            resp["NextPageToken"] = token
        return resp

    fake_ce_client = MagicMock()
    fake_ce_client.get_cost_and_usage.side_effect = [page("2026-09-01", "t1"), page("2026-09-02")]
    with patch("shared.aws_client.cost_explorer.boto3.client", return_value=fake_ce_client):
        rows = ce_mod._fetch_one_account_cost("acc2", "2026-09-01", "2026-09-03")

    assert [r["period_start"] for r in rows] == ["2026-09-01", "2026-09-02"]
    assert fake_ce_client.get_cost_and_usage.call_args_list[1].kwargs["NextPageToken"] == "t1"


def test_is_bedrock_service_matches_ce_service_names():
    """Real SERVICE names seen in acc1/acc2/acc3 Cost Explorer (Sept 2026 CSV exports)."""
    from shared.aws_client.cost_explorer import is_bedrock_service

    for s in ("Amazon Bedrock", "Claude Sonnet 4.6 (Amazon Bedrock Edition)",
              "Claude Haiku 4.5 (Amazon Bedrock Edition)", "Cohere Embed 4 Model (Amazon Bedrock Edition)",
              "Palmyra X5 (Amazon Bedrock Edition)"):
        assert is_bedrock_service(s), s
    for s in ("Amazon Elastic Container Service", "Amazon Relational Database Service",
              "Amazon ElastiCache", "Amazon Elastic Load Balancing", "AWS WAF", "Tax"):
        assert not is_bedrock_service(s), s


def _pool_with_fetch(rows):
    fake_conn = AsyncMock()
    fake_conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, fake_conn


@pytest.mark.asyncio
async def test_read_cost_range_none_when_empty():
    from datetime import date as date_cls

    from shared.aws_client import cost_explorer as ce_mod

    pool, _ = _pool_with_fetch([])
    assert await ce_mod.read_cost_range(pool, date_cls(2026, 9, 16), date_cls(2026, 9, 23)) is None


@pytest.mark.asyncio
async def test_read_cost_range_splits_bedrock_vs_infra_per_account():
    """The regression this follow-up exists for: acc2's whole-account total (ELB/ECS/RDS/...)
    was shown as '(Bedrock)' spend. bedrock_usd must count only Bedrock SERVICE lines."""
    from datetime import date as date_cls
    from datetime import datetime as dt_cls
    from datetime import timezone as tz

    from shared.aws_client import cost_explorer as ce_mod

    f1 = dt_cls(2026, 9, 23, 3, 0, tzinfo=tz.utc)
    f2 = dt_cls(2026, 9, 23, 9, 0, tzinfo=tz.utc)
    d16, d17 = date_cls(2026, 9, 16), date_cls(2026, 9, 17)

    def row(acct, service, day, amount, fetched):
        return {"account_id": acct, "service": service, "period_start": day,
                "amount_usd": amount, "fetched_at": fetched}

    db_rows = [
        row("005097885195", "Amazon Elastic Load Balancing", d16, 1.21, f1),
        row("005097885195", "Amazon Relational Database Service", d17, 0.68, f2),
        row("005097885195", "Cohere Embed 4 Model (Amazon Bedrock Edition)", d17, 0.01, f2),
        row("786888028788", "Claude Sonnet 4.6 (Amazon Bedrock Edition)", d16, 3.0, f1),
    ]
    pool, conn = _pool_with_fetch(db_rows)
    result = await ce_mod.read_cost_range(pool, d16, date_cls(2026, 9, 23))

    conn.fetch.assert_awaited_once()
    assert conn.fetch.call_args.args[1:] == (d16, date_cls(2026, 9, 23))  # real date objects, not strings
    acc = {a["account_id"]: a for a in result["accounts"]}
    assert acc["005097885195"]["total_usd"] == 1.9
    assert acc["005097885195"]["bedrock_usd"] == 0.01
    assert acc["005097885195"]["other_usd"] == 1.89
    assert acc["786888028788"]["bedrock_usd"] == 3.0
    assert result["total_usd"] == 4.9
    assert result["bedrock_usd"] == 3.01
    assert result["covered_from"] == "2026-09-16"
    assert result["covered_to"] == "2026-09-17"
    assert result["fetched_at"] == f2.isoformat()
    assert result["services"][0]["service"] == "Claude Sonnet 4.6 (Amazon Bedrock Edition)"


# ── _resolve_window (shared by every External Spend endpoint) ────────────────

def test_resolve_window_rolling_days_when_no_dates():
    from api.routers.admin_llm_ops import _resolve_window

    since, until = _resolve_window(7, None, None)
    assert round((until - since).total_seconds()) == 7 * 86400


def test_resolve_window_explicit_dates_end_inclusive():
    from datetime import date as date_cls

    from api.routers.admin_llm_ops import _ce_days, _resolve_window

    since, until = _resolve_window(30, date_cls(2026, 9, 16), date_cls(2026, 9, 22))
    assert since.isoformat() == "2026-09-16T00:00:00+00:00"
    assert until.isoformat() == "2026-09-23T00:00:00+00:00"
    assert _ce_days(since, until) == (date_cls(2026, 9, 16), date_cls(2026, 9, 23))


def test_resolve_window_start_only_runs_to_today():
    from datetime import date as date_cls
    from datetime import datetime as dt_cls
    from datetime import timedelta
    from datetime import timezone as tz

    from api.routers.admin_llm_ops import _resolve_window

    since, until = _resolve_window(30, date_cls(2026, 9, 1), None)
    assert since.date() == date_cls(2026, 9, 1)
    assert until.date() == dt_cls.now(tz.utc).date() + timedelta(days=1)


def test_resolve_window_rejects_inverted_and_too_long():
    from datetime import date as date_cls

    from fastapi import HTTPException

    from api.routers.admin_llm_ops import _resolve_window

    with pytest.raises(HTTPException):
        _resolve_window(7, date_cls(2026, 9, 22), date_cls(2026, 9, 16))
    with pytest.raises(HTTPException):
        _resolve_window(7, date_cls(2024, 1, 1), date_cls(2026, 9, 16))


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

    from datetime import date as date_cls

    with patch.object(admin_llm_ops, "verify_admin_secret") as mock_verify, \
         patch.object(admin_llm_ops, "fetch_all_accounts_cost", return_value=fake_result) as mock_fetch, \
         patch.object(admin_llm_ops, "record_snapshot", AsyncMock(return_value=1)) as mock_record:
        result = await admin_llm_ops.check_cost_explorer(
            _make_request(_make_pool(AsyncMock())), x_admin_secret="s3cr3t",
            days=7, start=date_cls(2026, 9, 1), end=date_cls(2026, 9, 22),
        )

    mock_verify.assert_called_once_with("s3cr3t")
    mock_fetch.assert_called_once_with("2026-09-01", "2026-09-23")  # CE End is exclusive
    mock_record.assert_awaited_once()
    assert result["row_count"] == 1
    assert result["errors"] == {}


@pytest.mark.asyncio
async def test_get_cost_explorer_endpoint_no_data():
    from api.routers import admin_llm_ops

    with patch.object(admin_llm_ops, "read_cost_range", AsyncMock(return_value=None)):
        result = await admin_llm_ops.get_cost_explorer(_make_request(_make_pool(AsyncMock())), days=7)

    assert result["has_data"] is False
    assert result["accounts"] == []


@pytest.mark.asyncio
async def test_get_cost_explorer_endpoint_with_data_uses_window():
    from datetime import date as date_cls

    from api.routers import admin_llm_ops

    fake = {"accounts": [{"account_id": "005097885195"}], "services": [], "total_usd": 1.0,
            "bedrock_usd": 0.0, "covered_from": "2026-09-16", "covered_to": "2026-09-22", "fetched_at": "x"}
    with patch.object(admin_llm_ops, "read_cost_range", AsyncMock(return_value=fake)) as mock_read:
        result = await admin_llm_ops.get_cost_explorer(
            _make_request(_make_pool(AsyncMock())), days=7,
            start=date_cls(2026, 9, 16), end=date_cls(2026, 9, 22),
        )

    assert mock_read.await_args.args[1:] == (date_cls(2026, 9, 16), date_cls(2026, 9, 23))
    assert result["has_data"] is True
    assert result["bedrock_usd"] == 0.0
    assert result["period_end_exclusive"] == "2026-09-23"
