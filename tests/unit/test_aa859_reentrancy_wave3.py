"""AA-859 wave 3 — reentrancy entries for the brand identity tenant branch and publish (see
test_aa859_tenant_pool_reentrancy.py).

Sequential tenant-pool blocks only, every connection released, and the admin pool (publish's daily-limit read and
audit row, brand POST's CTA notice) used only while the request holds no tenant connection. Publish also holds no
tenant connection across Secrets Manager or the channel's HTTP call.
"""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routers import admin_pipeline as ap
from api.routers import v1_publish as pub_mod
from services.acp_publish.idempotency import Claim
from tests.unit.test_aa859_tenant_pool_reentrancy import TENANT_ID, _ReentrancyGuardPool

PUBLISH_ID = str(uuid4())
_ROW = {"piece_id": uuid4(), "content_text": "Body", "status": "approved", "slug": None, "seo_title": None,
        "meta_description": None, "channel": "blog", "angle_name": "A", "superseded": False,
        "secret_key": "acp/cms/x", "system_prompt": "p", "style_guide": "s", "forbidden_words": "[]", "version": 1,
        "updated_at": None, "created_at": None, "brand_name": "b", "brand_type": "", "core_idea": "",
        "customer_segment": "", "customer_mindset": "", "voice_examples": "[]", "good_examples": "",
        "target_markets": [], "default_cta": "", "is_active": True}
_FINISHED = {"publish_id": PUBLISH_ID, "status": "published", "external_id": "42", "external_url": "u",
             "published_at": None, "last_error": None}


def _conn(row):
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchval = AsyncMock(side_effect=lambda sql, *a: a[0] if "set_config" in sql else 1)
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=[row])
    conn.execute = AsyncMock(return_value="OK")
    return conn


class _MainPool:
    """Admin pool that fails when the same task still holds a tenant connection."""

    def __init__(self, tenant_pool):
        self.tenant_pool = tenant_pool
        self.uses = 0

    def _check(self):
        assert asyncio.current_task() not in self.tenant_pool.holders, "MAIN used inside a tenant block"
        self.uses += 1

    def acquire(self):
        @asynccontextmanager
        async def _acquire():
            self._check()
            yield MagicMock()
        return _acquire()

    async def execute(self, *a, **k):
        self._check()
        return "INSERT 0 1"


def _request(tenant_pool):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(return_value="true")
    request.app.state.tenant_pool = tenant_pool
    request.app.state.pool = _MainPool(tenant_pool)
    return request


async def _brand_get(request, held):
    return await ap.get_brand_identity(request, tenant_id=TENANT_ID, tenant_caller=True)


async def _brand_post(request, held):
    pool = request.app.state.tenant_pool

    async def cta(*a, **k):
        held.append(("cta", len(pool.holders)))
    with patch("services.acp_shared.cta_review_notice.check_saved_cta", AsyncMock(side_effect=cta)):
        return await ap.update_brand_identity(ap.BrandIdentityUpdate(system_prompt="x"), request,
                                              tenant_id=TENANT_ID, tenant_caller=True)


async def _publish(request, held):
    pool = request.app.state.tenant_pool

    def secret(key):
        held.append(("secret", len(pool.holders)))
        return {"wp_url": "https://b.com", "username": "u", "app_password": "p"}

    async def adapter(*a, **k):
        held.append(("remote", len(pool.holders)))
        return "42", "u"

    async def limit(conn, tid):
        return 20
    with patch.object(pub_mod, "_get_secret", side_effect=secret), \
            patch.object(pub_mod, "_call_adapter", AsyncMock(side_effect=adapter)), \
            patch.object(pub_mod.daily_limit, "effective_limit", AsyncMock(side_effect=limit)), \
            patch.object(pub_mod, "claim", AsyncMock(return_value=Claim("claimed", publish_id=PUBLISH_ID))), \
            patch.object(pub_mod, "finish", AsyncMock(return_value=dict(_FINISHED))):
        return await pub_mod.publish(uuid4(), request, tenant={"sub": TENANT_ID})


# (handler, tenant-pool acquires, MAIN uses, (step, tenant connections held) seen outside the DB)
HANDLERS = [
    pytest.param(_brand_get, 1, 0, [], id="GET /admin/brand-identity (tenant)"),
    pytest.param(_brand_post, 1, 0, [("cta", 0)], id="POST /admin/brand-identity (tenant)"),
    pytest.param(_publish, 3, 2, [("secret", 0), ("remote", 0)], id="POST /v1/publish-log/{id}/publish"),
]


@pytest.mark.parametrize("handler,tenant_acquires,main_uses,outside", HANDLERS)
@pytest.mark.asyncio
async def test_handler_never_nests_tenant_pool_acquires(handler, tenant_acquires, main_uses, outside):
    pool = _ReentrancyGuardPool(_conn(dict(_ROW)))
    request = _request(pool)
    held = []
    await handler(request, held)
    assert pool.violations == [], "nested tenant-pool acquire (self-deadlock at max_size 1)"
    assert pool.acquires == tenant_acquires
    assert pool.holders == set(), "every tenant connection was released"
    assert request.app.state.pool.uses == main_uses
    assert held == outside, "no tenant connection held across Secrets Manager, HTTP or the CTA notice"
