"""S280 (AA-1096) — the admin pages that edit a cap, threshold or override outside Platform › Settings: the API answers
they need so the UI shows the truth.

  - route control version history and Tenant 360 overrides name the person (``created_by_name`` / ``granted_by_name``)
    instead of a raw "admin:<staff id>";
  - the override rows carry the feature's unit (an RPM / seats / USD override no longer reads "N a month");
  - the tenant routes answer the platform's daily publish default in force (``daily_publish_limit_default``, the
    setting cis.publish.daily_limit_default; tests/unit/test_aa819_daily_publish_limit.py), so Tenant 360 › Settings
    stops showing a hard-coded 20.

No database: fake connections answer by SQL. Names and ids are invented.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg

from api.routers import admin_llm_route_control as rc_router
from api.routers import admin_tenant_subscription as sub_router
from api.services import ai_gateway as gw
from services.tenant_account import subscription as sub

STAFF = "7a1b2c3d-4e5f-4a6b-9c8d-1e2f3a4b5c6d"
GONE = "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"


class _NamesConn:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def fetch(self, sql, *args):
        assert sql == gw.ACTOR_NAMES_SQL
        if self.fail:
            raise asyncpg.exceptions.UndefinedTableError("no admin_users")
        return [{"id": i, "username": "mai", "name": "Mai Tran"} for i in args[0] if i == STAFF]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool(conn))))


def test_route_versions_name_the_author():
    rows = [{"version": 3, "created_by": f"admin:{STAFF}"}, {"version": 2, "created_by": f"admin:{GONE}"},
            {"version": 1, "created_by": "migration_370"}]
    asyncio.run(rc_router._name_authors(_req(_NamesConn()), rows))
    assert [(r["created_by_kind"], r["created_by_name"]) for r in rows] == [
        ("staff", "Mai Tran"), ("staff", None), ("migration", "Migration 370")]


def test_route_versions_keep_working_when_names_cannot_be_read():
    rows = [{"version": 3, "created_by": f"admin:{STAFF}"}]
    asyncio.run(rc_router._name_authors(_req(_NamesConn(fail=True)), rows))
    assert rows[0]["created_by_kind"] == "staff" and rows[0]["created_by_name"] is None


def test_overrides_name_who_granted_them():
    overrides = [{"feature_key": "crm.seats", "granted_by": f"admin:{STAFF}"},
                 {"feature_key": "ai.budget_usd_monthly", "granted_by": "admin"}]
    asyncio.run(sub_router._name_override_actors(_NamesConn(), overrides))
    assert overrides[0]["granted_by_name"] == "Mai Tran" and overrides[0]["granted_by_kind"] == "staff"
    assert overrides[1]["granted_by_kind"] == "other"


def test_override_rows_carry_the_unit():
    assert "f.unit" in sub.OVERRIDES_SQL


def test_catalogue_settings_names_the_setting_of_the_a1_pass_line():
    """Catalogue settings › Pipeline gates said "hardcoded" for a value that is a registry setting since S279 L2; the
    API now names the key, so the page links to Platform › Settings instead."""
    from api.routers import admin_settings
    from services.quality.registry import threshold_setting

    gates = admin_settings.pipeline_gates()
    assert gates["brand_audit_threshold_setting"] == threshold_setting("a1.min_quality")
    assert isinstance(gates["brand_audit_threshold_setting"], str) and gates["brand_audit_threshold_setting"]


def test_the_new_cap_defaults_of_the_budgets_tab_pass_the_tighten_only_check():
    """The Budgets tab's new provider / job cap starts with the hard stop on (frontend lib/aiGateway NEW_BUDGET_DRAFT,
    the API's own default for a provider row) and a tighten-only role's new central cap too (centralDraftOf). That body
    must pass settings_registry.loosened against no row, or ai_ops keeping the defaults hits a 403; a new cap without
    the stop is what the check refuses."""
    from api.services.settings_registry import loosened

    kinds = {"caps": ("per_run_usd", "per_day_usd", "per_month_usd"), "stops": ("hard_stop",),
             "up_is_looser": ("alert_pct",)}
    provider_default = {"per_run_usd": None, "per_day_usd": 5.0, "hard_stop": True, "alert_pct": 80}
    central_tighten_default = {"per_run_usd": None, "per_day_usd": 5.0, "per_month_usd": None, "hard_stop": True,
                               "alert_pct": 80}
    assert loosened(None, provider_default, **kinds) == []
    assert loosened(None, central_tighten_default, **kinds) == []
    assert loosened(None, {**provider_default, "hard_stop": False}, **kinds) == ["hard_stop"]
