"""S237 — the E2E test tenants' key copies follow a key rotation (services/acp_shared/e2e_tenant_key_sync.py)."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routers import admin
from services.acp_shared import e2e_tenant_key_sync as sync

_SECRET = "test-admin-secret"
TENANT_ID = uuid.uuid4()


class _NotFound(Exception):
    pass


def _fake_client(exists=True, put_error=None):
    client = MagicMock()
    client.exceptions.ResourceNotFoundException = _NotFound
    if not exists:
        client.describe_secret.side_effect = _NotFound()
    if put_error:
        client.put_secret_value.side_effect = put_error
    return client


@pytest.fixture
def on_ecs(monkeypatch):
    monkeypatch.setenv("AWS_EXECUTION_ENV", "AWS_ECS_FARGATE")


@pytest.mark.asyncio
async def test_existing_test_secret_is_overwritten(on_ecs, monkeypatch):
    client = _fake_client()
    monkeypatch.setattr(sync, "_client", lambda: client)
    assert await sync.sync_test_tenant_key("atlas-hearth", "cis_new") == "synced"
    client.describe_secret.assert_called_once_with(SecretId="aa-cis/dev/e2e-tenant/atlas-hearth")
    client.put_secret_value.assert_called_once_with(SecretId="aa-cis/dev/e2e-tenant/atlas-hearth",
                                                    SecretString="cis_new")


@pytest.mark.asyncio
async def test_real_tenant_without_a_test_secret_is_never_written(on_ecs, monkeypatch):
    client = _fake_client(exists=False)
    monkeypatch.setattr(sync, "_client", lambda: client)
    assert await sync.sync_test_tenant_key("real-agency", "cis_new") == "not_test_tenant"
    client.put_secret_value.assert_not_called()
    client.create_secret.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_write_never_raises(on_ecs, monkeypatch):
    monkeypatch.setattr(sync, "_client", lambda: _fake_client(put_error=RuntimeError("AccessDenied")))
    assert await sync.sync_test_tenant_key("atlas-hearth", "cis_new") == "failed"


@pytest.mark.asyncio
async def test_skipped_off_ecs_and_without_slug(monkeypatch):
    monkeypatch.delenv("AWS_EXECUTION_ENV", raising=False)
    called = MagicMock()
    monkeypatch.setattr(sync, "_client", called)
    assert await sync.sync_test_tenant_key("atlas-hearth", "cis_new") == "skipped"
    monkeypatch.setenv("AWS_EXECUTION_ENV", "AWS_ECS_FARGATE")
    assert await sync.sync_test_tenant_key(None, "cis_new") == "skipped"
    called.assert_not_called()


def test_note_names_the_secret_and_never_the_key():
    assert "aa-cis/dev/e2e-tenant/atlas-hearth" in sync.sync_note("synced", "atlas-hearth")
    assert "by hand" in sync.sync_note("failed", "atlas-hearth")
    assert sync.sync_note("not_test_tenant", "x") == "" and sync.sync_note("skipped", "x") == ""


class _TxnCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _request(conn):
    pool = MagicMock()
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = acq
    req = MagicMock()
    req.app.state.pool = pool
    return req


@pytest.mark.asyncio
async def test_generate_key_syncs_after_the_commit(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _SECRET)
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxnCM())
    conn.fetchrow.return_value = {"tenant_id": TENANT_ID, "name": "Atlas & Hearth", "slug": "atlas-hearth",
                                  "cancelled_at": None}
    seen = {}

    async def fake_sync(slug, key):
        seen.update(slug=slug, key=key)
        return "synced"

    monkeypatch.setattr(admin, "sync_test_tenant_key", fake_sync)
    out = await admin.generate_api_key(TENANT_ID, _request(conn), x_admin_secret=_SECRET,
                                       x_admin_user_id="u-1", x_admin_role="admin")
    assert seen == {"slug": "atlas-hearth", "key": out.api_key}
    assert out.test_secret_sync == "synced"
    assert "aa-cis/dev/e2e-tenant/atlas-hearth" in out.message and out.api_key not in out.message
