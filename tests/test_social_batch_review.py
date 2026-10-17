"""
Tests for POST /v1/social/batch-review endpoint (AA-111).

Tests:
  1. test_batch_review_all_approved   — 3 approved decisions → processed=3, approved=3
  2. test_batch_review_mixed          — 2 approved + 1 rejected → correct counts
  3. test_batch_review_invalid_status — status='pending' → 422
  4. test_batch_review_invalid_uuid   — bad UUID → 422
  5. test_batch_review_empty_decisions — [] → 422
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


SOCIAL_ID_1 = "aaaaaaaa-0000-0000-0000-000000000001"
SOCIAL_ID_2 = "aaaaaaaa-0000-0000-0000-000000000002"
SOCIAL_ID_3 = "aaaaaaaa-0000-0000-0000-000000000003"
TENANT_ID = "aa_internal"
REVIEWER = "trang"
ADMIN_SECRET = "test-secret"


def _make_fetchrow_result(social_id: str, tenant_id: str = TENANT_ID):
    return {"social_id": social_id, "tenant_id": tenant_id}


def _build_app():
    from fastapi import FastAPI
    from api.routers.v1_social import router

    app = FastAPI()
    app.include_router(router)
    return app


def _build_app_with_pool(fetchrow_side_effects: list):
    """Wire a mock pool whose fetchrow returns successive values from the list."""
    app = _build_app()

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effects)
    conn.executemany = AsyncMock(return_value=None)

    # Transaction context manager
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)

    pool_mock = AsyncMock()
    pool_mock.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=conn),
        __aexit__=AsyncMock(return_value=None),
    ))
    app.state.pool = pool_mock
    return app


class TestBatchReview:
    @pytest.mark.asyncio
    async def test_batch_review_all_approved(self):
        """3 approved decisions → processed=3, approved=3, rejected=0."""
        from httpx import AsyncClient, ASGITransport

        app = _build_app_with_pool([
            _make_fetchrow_result(SOCIAL_ID_1),
            _make_fetchrow_result(SOCIAL_ID_2),
            _make_fetchrow_result(SOCIAL_ID_3),
        ])

        payload = {
            "decisions": [
                {"social_id": SOCIAL_ID_1, "status": "approved"},
                {"social_id": SOCIAL_ID_2, "status": "approved"},
                {"social_id": SOCIAL_ID_3, "status": "approved"},
            ],
            "reviewer_id": REVIEWER,
        }

        with patch.dict("os.environ", {"ADMIN_SECRET": ADMIN_SECRET}):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/social/batch-review",
                    json=payload,
                    headers={"X-Admin-Secret": ADMIN_SECRET, "x-admin-role": "admin"},  # AA-882: BFF-forwarded role
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["processed"] == 3
        assert data["approved"] == 3
        assert data["rejected"] == 0
        assert data["errors"] == []

    @pytest.mark.asyncio
    async def test_batch_review_mixed(self):
        """2 approved + 1 rejected → correct counts."""
        from httpx import AsyncClient, ASGITransport

        app = _build_app_with_pool([
            _make_fetchrow_result(SOCIAL_ID_1),
            _make_fetchrow_result(SOCIAL_ID_2),
            _make_fetchrow_result(SOCIAL_ID_3),
        ])

        payload = {
            "decisions": [
                {"social_id": SOCIAL_ID_1, "status": "approved"},
                {"social_id": SOCIAL_ID_2, "status": "approved"},
                {"social_id": SOCIAL_ID_3, "status": "rejected", "notes": "poor quality"},
            ],
            "reviewer_id": REVIEWER,
        }

        with patch.dict("os.environ", {"ADMIN_SECRET": ADMIN_SECRET}):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/social/batch-review",
                    json=payload,
                    headers={"X-Admin-Secret": ADMIN_SECRET, "x-admin-role": "admin"},  # AA-882: BFF-forwarded role
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["processed"] == 3
        assert data["approved"] == 2
        assert data["rejected"] == 1

    @pytest.mark.asyncio
    async def test_batch_review_invalid_status(self):
        """status='pending' is not allowed → 422."""
        from httpx import AsyncClient, ASGITransport

        app = _build_app()
        app.state.pool = MagicMock()

        payload = {
            "decisions": [{"social_id": SOCIAL_ID_1, "status": "pending"}],
            "reviewer_id": REVIEWER,
        }

        with patch.dict("os.environ", {"ADMIN_SECRET": ADMIN_SECRET}):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/social/batch-review",
                    json=payload,
                    headers={"X-Admin-Secret": ADMIN_SECRET, "x-admin-role": "admin"},  # AA-882: BFF-forwarded role
                )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_batch_review_invalid_uuid(self):
        """Malformed UUID in social_id → 422."""
        from httpx import AsyncClient, ASGITransport

        app = _build_app()
        app.state.pool = MagicMock()

        payload = {
            "decisions": [{"social_id": "not-a-uuid", "status": "approved"}],
            "reviewer_id": REVIEWER,
        }

        with patch.dict("os.environ", {"ADMIN_SECRET": ADMIN_SECRET}):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/social/batch-review",
                    json=payload,
                    headers={"X-Admin-Secret": ADMIN_SECRET, "x-admin-role": "admin"},  # AA-882: BFF-forwarded role
                )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_batch_review_empty_decisions(self):
        """Empty decisions list → 422."""
        from httpx import AsyncClient, ASGITransport

        app = _build_app()
        app.state.pool = MagicMock()

        payload = {"decisions": [], "reviewer_id": REVIEWER}

        with patch.dict("os.environ", {"ADMIN_SECRET": ADMIN_SECRET}):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/social/batch-review",
                    json=payload,
                    headers={"X-Admin-Secret": ADMIN_SECRET, "x-admin-role": "admin"},  # AA-882: BFF-forwarded role
                )

        assert resp.status_code == 422
