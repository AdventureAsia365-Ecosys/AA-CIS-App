"""AA-1139 — the TripPlanner module of the agency app (services/tenant_account/tripplanner_site.py: module_view,
set_agency_lead_route, set_appearance) on real Postgres over the CIS 379 + 390 site table and CIS 428 domains.

Builds on the 390 module's fixtures (379 then 390 applied the DB Migrate way, undone afterwards). Each test runs on one
asyncpg connection inside an outer transaction that is rolled back. Invented names only.

  * the module answers the stored route, the channel in force, the theme, the locale and the domains with their state
    (a host being removed is not listed), and the contract shape;
  * the agency owner's route and appearance writes are audited as the CRM member and keep each other's columns;
  * a write without an active Trip planner, or for a closing tenant, is refused before anything is written;
  * an internal e-mail domain is refused, a theme token outside the brand layer too.
"""
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import yaml
from jsonschema import Draft202012Validator

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from test_aa1038_migration_390 import applied_390  # noqa: E402,F401  (module fixture: 379 + 390 applied)
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401
from services.tenant_account import tripplanner_site as tp  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ACTOR = "crm-member:1139bb00-0000-4000-8000-0000000000aa"
CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "tenant.addons.v1.openapi.yaml"
DOC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
MODULE = Draft202012Validator({"$ref": "#/components/schemas/TripPlannerModule", "components": DOC["components"]})


@pytest.fixture(autouse=True)
def _seen_hours(monkeypatch):
    """AA-1160: module_view reads cis.tenant.crm_agency_seen_hours. Its compiled default ships with the CIS 474
    migration PR (MIG-4 chain), so the reader is pinned to the seed default (3 hours) on a tree without it."""
    from services.tenant_account import crm_agency
    from services.tenant_account.crm_agency_settings import CRM_AGENCY_SEEN_HOURS

    real_get = crm_agency.settings.get
    monkeypatch.setattr(crm_agency.settings, "get", lambda key, tenant_id=None: 3
                        if key == CRM_AGENCY_SEEN_HOURS else real_get(key, tenant_id))


@pytest_asyncio.fixture
async def conn(applied_390):  # noqa: F811
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(conn, *, tripplanner=True, status=None) -> str:
    tid = str(uuid.uuid4())
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                       "is_active, kind) VALUES ($1::uuid, 'Fernhollow Trails synthetic', $2, "
                       "'starter'::plan_tier_enum, 3, 60, true, 'agency')", tid, f"zz1139-{tid[:12]}")
    if tripplanner:
        await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, updated_by) "
                           "VALUES ($1::uuid, 'tripplanner', 'tripplanner.hosted', 'active', 'test:aa1139')", tid)
    if status:
        await conn.execute("UPDATE shared.tenants SET account_status = $2 WHERE tenant_id = $1::uuid", tid, status)
    return tid


def _valid(body):
    errors = sorted(MODULE.iter_errors(body), key=str)
    assert not errors, [e.message for e in errors]


@pytest.mark.asyncio
async def test_module_reads_route_appearance_and_domains(conn):
    tid = await _tenant(conn)
    first = await tp.module_view(conn, tid)
    _valid(first)
    assert first["active"] is True and first["theme"] == {} and first["locale"] == "en"
    assert first["lead_route"]["lead_channel"] == "none" and first["lead_route"]["effective_channel"] == "none"
    for domain, status in (("plan.fernhollow.example.com", "active"), ("trips.fernhollow.example.com", "validating"),
                           ("old.fernhollow.example.com", "removing")):
        await conn.execute("INSERT INTO shared.tenant_tripplanner_domain (tenant_id, domain, status, updated_by) "
                           "VALUES ($1::uuid, $2, $3, 'test:aa1139')", tid, domain, status)
    await tp.set_agency_lead_route(conn, tid, channel="email", webhook_url=None, email="desk@fernhollow.example.com",
                                   actor=ACTOR)
    await tp.set_appearance(conn, tid, theme={"accent": "#2A6F97"}, locale="vi", actor=ACTOR)
    out = await tp.module_view(conn, tid)
    _valid(out)
    assert out["domains"] == [{"domain": "plan.fernhollow.example.com", "status": "active"},
                              {"domain": "trips.fernhollow.example.com", "status": "validating"}]
    assert out["lead_route"]["effective_channel"] == "email" and out["lead_route"]["lead_email"] == \
        "desk@fernhollow.example.com"
    assert (out["theme"], out["locale"]) == ({"accent": "#2a6f97"}, "vi")
    assert await tp.module_view(conn, str(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_writes_are_audited_as_the_member_and_keep_each_other(conn):
    tid = await _tenant(conn)
    await tp.set_appearance(conn, tid, theme={"accent": "#2a6f97"}, locale="vi", actor=ACTOR)
    await tp.set_agency_lead_route(conn, tid, channel="webhook",
                                   webhook_url="https://hooks.fernhollow.example.com/tp", email=None, actor=ACTOR)
    row = await conn.fetchrow("SELECT theme::text AS theme, locale, lead_channel, updated_by "
                              "FROM shared.tenant_tripplanner_site WHERE tenant_id = $1::uuid", tid)
    assert (row["locale"], row["lead_channel"], row["updated_by"]) == ("vi", "webhook", ACTOR)
    assert "2a6f97" in row["theme"]
    # One transaction: both rows have the same now() and id is a uuid, so chain_seq is the write order.
    actions = [r["action"] for r in await conn.fetch(
        "SELECT action FROM acp_shared.audit_log WHERE tenant_id = $1 ORDER BY chain_seq NULLS LAST, created_at", tid)]
    assert actions == [tp.APPEARANCE_CHANGED, tp.LEAD_ROUTE_CHANGED]
    actors = {r["actor"] for r in await conn.fetch("SELECT actor FROM acp_shared.audit_log WHERE tenant_id = $1", tid)}
    assert actors == {ACTOR}


@pytest.mark.asyncio
async def test_refusals_write_nothing(conn):
    no_planner = await _tenant(conn, tripplanner=False)
    closing = await _tenant(conn, status="offboarding")
    for tid, code in ((no_planner, "tripplanner_not_active"), (closing, "tenant_offboarded")):
        with pytest.raises(tp.LeadRouteError) as e:
            await tp.set_appearance(conn, tid, theme={}, locale="vi", actor=ACTOR)
        assert (e.value.status, e.value.code) == (409, code)
        with pytest.raises(tp.LeadRouteError) as e:
            await tp.set_agency_lead_route(conn, tid, channel="none", webhook_url=None, email=None, actor=ACTOR)
        assert e.value.code == code
    tid = await _tenant(conn)
    with pytest.raises(tp.LeadRouteError) as e:
        await tp.set_agency_lead_route(conn, tid, channel="email", webhook_url=None, email="desk@printer.local",
                                       actor=ACTOR)
    assert e.value.code == tp.EMAIL_NOT_PUBLIC
    with pytest.raises(tp.LeadRouteError) as e:
        await tp.set_appearance(conn, tid, theme={"background": "#ffffff"}, locale="en", actor=ACTOR)
    assert e.value.code == "theme_token_unknown"
    n = await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_tripplanner_site "
                            "WHERE tenant_id = ANY($1::uuid[])", [no_planner, closing, tid])
    assert n == 0


@pytest.mark.asyncio
async def test_crm_only_with_a_crm_agency(conn):
    """AA-1160 (CIS 474): the CRM add-on alone is not the CRM channel. Without a CRM agency (S285: crm.core, no agency,
    leads skipped) the module says the agency's own route; once the CRM's snapshot read or the onboarding records the
    agency (shared.tenant_crm_agency), crm, and only while that sighting is inside cis.tenant.crm_agency_seen_hours."""
    from services.tenant_account import crm_agency

    tid = await _tenant(conn)
    await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, updated_by) "
                       "VALUES ($1::uuid, 'crm', 'crm.core', 'active', 'test:aa1160')", tid)
    await tp.set_agency_lead_route(conn, tid, channel="email", webhook_url=None, email="desk@fernhollow.example.com",
                                   actor=ACTOR)
    before = await tp.module_view(conn, tid)
    _valid(before)
    assert before["crm_core"] is True and before["lead_route"]["effective_channel"] == "email"
    assert (await tp.lead_route(conn, tid))["crm_agency"] is False
    assert await crm_agency.record(conn, tid, "crm_pull") is True
    assert await crm_agency.record(conn, tid, "crm_pull") is False                # throttled: written once
    assert await crm_agency.record(conn, tid, "crm_onboarding") is True           # another source: written
    after = await tp.module_view(conn, tid)
    assert after["lead_route"]["effective_channel"] == "crm" and after["lead_route"]["lead_channel"] == "email"
    assert (await tp.lead_route(conn, tid))["crm_agency"] is True
    assert await crm_agency.record(conn, str(uuid.uuid4()), "crm_pull") is False  # no such tenant: nothing written
    # The agency is deleted / offboarded in the CRM: never read again, so its sighting ages out of the window
    # (cis.tenant.crm_agency_seen_hours, default 3) and the module says the agency's own route again.
    await conn.execute("UPDATE shared.tenant_crm_agency SET first_seen_at = now() - interval '4 hours', "
                       "last_seen_at = now() - interval '4 hours' WHERE tenant_id = $1::uuid", tid)
    gone = await tp.module_view(conn, tid)
    assert gone["lead_route"]["effective_channel"] == "email"
    assert (await tp.lead_route(conn, tid))["crm_agency"] is False
    # The CRM reads its snapshot again (an agency restored or re-created): crm once more.
    assert await crm_agency.record(conn, tid, "crm_pull") is True
    assert (await tp.module_view(conn, tid))["lead_route"]["effective_channel"] == "crm"
