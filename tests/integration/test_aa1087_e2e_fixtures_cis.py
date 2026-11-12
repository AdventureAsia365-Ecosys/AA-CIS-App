"""AA-1087 (S278 lane l18) on real Postgres: the tenant_provision saga registers a synthetic agency under the id
another app already holds (Dev, demo only), the catalog view then marks it synthetic, and the same id twice is
``tenant_exists``. One outer transaction, rolled back (as tests/integration/test_aa1028_tenant_provision.py).
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from test_aa1028_tenant_provision import _Pool, conn  # noqa: E402,F401 — fixture used by name
from services.tenant_account import provision as pv  # noqa: E402
from services.tenant_account.e2e_fixtures import CrmAgency  # noqa: E402

AGENCY = CrmAgency("zz-l18-lantern", "Lantern Cove Travel (synthetic)")


def _req(key: str, slug: str):
    return pv.parse({"request_key": key, "mode": "create", "name": AGENCY.name, "slug": slug,
                     "plan_tier": "starter", "posts_per_week": 1, "kind": "demo", "tenant_id": AGENCY.tenant_id})


@pytest.mark.asyncio
async def test_register_under_a_given_id_then_tenant_exists(conn, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ENVIRONMENT", "dev")
    out = await pv.provision(_Pool(conn), _req("zz-l18-register-1", "zz-l18-lantern"), job_id=str(uuid.uuid4()))
    assert out["tenant_id"] == AGENCY.tenant_id
    row = await conn.fetchrow("SELECT kind, is_canary, is_pilot, account_status FROM shared.tenants "
                              "WHERE tenant_id = $1::uuid", AGENCY.tenant_id)
    assert dict(row) == {"kind": "demo", "is_canary": True, "is_pilot": False, "account_status": "active"}
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_subscription WHERE tenant_id = $1::uuid "
                               "AND product = 'content'", AGENCY.tenant_id) == 1
    view = await conn.fetchval("SELECT to_regclass('catalog.v_tenant_account_v1') IS NOT NULL")
    if view:
        synthetic = await conn.fetchrow(
            "SELECT * FROM catalog.v_tenant_account_v1 WHERE tenant_id = $1::uuid", AGENCY.tenant_id)
        if synthetic is not None and "is_synthetic" in synthetic.keys():
            assert synthetic["is_synthetic"] is True
    # Another request for the same id: refused, nothing written.
    with pytest.raises(pv.ProvisionError) as err:
        await pv.provision(_Pool(conn), _req("zz-l18-register-2", "zz-l18-lantern-b"), job_id=str(uuid.uuid4()))
    assert err.value.code == "tenant_exists"


@pytest.mark.asyncio
async def test_register_is_refused_when_the_job_runs_outside_dev(conn, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ENVIRONMENT", "dev")
    req = _req("zz-l18-register-3", "zz-l18-lantern-c")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    with pytest.raises(pv.ProvisionError) as err:
        await pv.provision(_Pool(conn), req, job_id=str(uuid.uuid4()))
    assert err.value.code == "register_id_dev_demo_only"
    assert await conn.fetchval("SELECT count(*) FROM shared.tenants WHERE tenant_id = $1::uuid",
                               AGENCY.tenant_id) == 0
