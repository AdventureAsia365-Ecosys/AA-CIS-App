"""AA-1038 R5 slice 3 follow-up (S272) on real Postgres — the Tenant 360 lead route editor
(services/tenant_account/tripplanner_site.py) over the CIS 390 columns of shared.tenant_tripplanner_site.

Builds on the 390 module's fixtures (379 then 390 applied the DB Migrate way, undone afterwards). Each test runs on
one asyncpg connection inside an outer transaction that is rolled back. Names are invented.

  * a route is stored (the site row is created with the table's defaults when absent), read back and audited;
  * a non-public webhook URL that the 390 CHECK alone would accept is refused before any write;
  * every URL the editor accepts also satisfies the 390 CHECK (no 500 on a check_violation).
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from test_aa1038_migration_390 import applied_390  # noqa: E402,F401  (module fixture: 379 + 390 applied)
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401
from services.tenant_account import tripplanner_site as tp  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ACTOR = "admin:zz-aa1038-route"
# Accepted by the 390 CHECK (https://[A-Za-z0-9.-]+...), refused by the editor.
CHECK_ONLY_REFUSES_NOTHING = ["https://127.0.0.1/hook", "https://10.0.3.7:8443/leads", "https://169.254.169.254/x",
                              "https://100.64.0.1/h", "https://127.1/h", "https://2130706433/h",
                              "https://localhost/h", "https://printer.local/h", "https://crm.corp.internal/h"]
PUBLIC = ["https://larchfield-tours.example.com/hooks/tripplanner", "https://93.184.215.14/leads",
          "https://api.wrenhollow.example.org:8443/v2/leads?src=tp"]


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(conn, *, tripplanner=True) -> str:
    tid = str(uuid.uuid4())
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                       "is_active, kind) VALUES ($1::uuid, 'Ottervale Journeys synthetic', $2, "
                       "'starter'::plan_tier_enum, 3, 60, true, 'agency')", tid, f"zz1038r-{tid[:12]}")
    if tripplanner:
        await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, updated_by) "
                           "VALUES ($1::uuid, 'tripplanner', 'tripplanner.hosted', 'active', 'test:aa1038')", tid)
    return tid


@pytest.mark.asyncio
async def test_store_read_and_audit(conn):
    tid = await _tenant(conn)
    first = await tp.lead_route(conn, tid)
    assert first["site"] is False and first["lead_channel"] == "none" and first["has_tripplanner"] is True

    out = await tp.set_lead_route(conn, tid, channel="webhook", webhook_url=PUBLIC[0], email=None, actor=ACTOR)
    assert out["site"] is True and out["lead_channel"] == "webhook" and out["lead_webhook_url"] == PUBLIC[0]
    row = await conn.fetchrow("SELECT theme, locale, lead_channel, lead_webhook_url, lead_email, updated_by "
                              "FROM shared.tenant_tripplanner_site WHERE tenant_id = $1::uuid", tid)
    assert row["locale"] == "en" and row["lead_channel"] == "webhook" and row["updated_by"] == ACTOR

    out = await tp.set_lead_route(conn, tid, channel="email", webhook_url=PUBLIC[0],
                                  email="desk@ottervale.example.com", actor=ACTOR)
    assert (out["lead_channel"], out["lead_email"]) == ("email", "desk@ottervale.example.com")
    assert (await tp.lead_route(conn, tid))["lead_email"] == "desk@ottervale.example.com"
    audits = await conn.fetch("SELECT details FROM acp_shared.audit_log WHERE tenant_id = $1 AND action = $2 "
                              "ORDER BY created_at, id", tid, tp.LEAD_ROUTE_CHANGED)
    assert len(audits) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("url", CHECK_ONLY_REFUSES_NOTHING)
async def test_a_url_the_check_accepts_but_is_not_public_is_refused_before_writing(conn, url):
    tid = await _tenant(conn)
    with pytest.raises(tp.LeadRouteError) as e:
        await tp.set_lead_route(conn, tid, channel="webhook", webhook_url=url, email=None, actor=ACTOR)
    assert e.value.code == tp.URL_NOT_PUBLIC and e.value.field == "lead_webhook_url"
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_tripplanner_site "
                               "WHERE tenant_id = $1::uuid", tid) == 0
    # the CHECK alone would have stored it (rolled back with the test): the editor is the guard
    await conn.execute("INSERT INTO shared.tenant_tripplanner_site (tenant_id, lead_channel, lead_webhook_url, "
                       "updated_by) VALUES ($1::uuid, 'webhook', $2, 'test:aa1038')", tid, url)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", PUBLIC)
async def test_every_accepted_url_satisfies_the_check(conn, url):
    tid = await _tenant(conn)
    out = await tp.set_lead_route(conn, tid, channel="webhook", webhook_url=url, email=None, actor=ACTOR)
    assert out["lead_webhook_url"] == url


@pytest.mark.asyncio
async def test_tenant_refusals(conn):
    with pytest.raises(tp.LeadRouteError) as e:
        await tp.set_lead_route(conn, str(uuid.uuid4()), channel="none", webhook_url=None, email=None, actor=ACTOR)
    assert e.value.status == 404
    assert await tp.lead_route(conn, str(uuid.uuid4())) is None
    tid = await _tenant(conn, tripplanner=False)
    assert (await tp.lead_route(conn, tid))["has_tripplanner"] is False
