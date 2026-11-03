"""AA-1025 H1b + H1c (S265) — the CIS side of the other apps' app profiles.

- H1b: the ops check ``llm_spend_alert.<app>.near|cap`` (TripPlanner's spend today against its profile's
  ``default_daily_budget_usd``): thresholds, a 0 USD budget, the check names, no check before migration 345.
- H1c: PATCH /admin/ai-gateway/app-profiles/crm relays the whole profile to the CRM (crm.ops.v1 1.10 PUT
  /llm/app-profile) before the CIS row changes; a CRM refusal changes nothing here; other apps are not relayed; the
  relay path is on the console allow-list.
- The card says which apps act on the profile.
No database, no AWS, no network: fakes only; invented texts.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import HTTPException

from api.core import ops_metrics_sampler as sampler
from api.routers import admin_ai_gateway as gw_router
from api.services import crm_console
from shared import ops_checks
from shared.llm_client import app_profile

NOW = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)


# ── H1b: the spend check ──────────────────────────────────────────────────────────────────────────────────────────

def _row(spent, cap, app="tripplanner"):
    return {"app": app, "cap_usd": cap, "spent_usd": spent}


@pytest.mark.parametrize(("spent", "cap", "check"), [
    (0.79, 1.0, None),
    (0.80, 1.0, "llm_spend_alert.tripplanner.near"),
    (0.99, 1.0, "llm_spend_alert.tripplanner.near"),
    (1.00, 1.0, "llm_spend_alert.tripplanner.cap"),
    (3.00, 1.0, "llm_spend_alert.tripplanner.cap"),
    (0.00, 0.0, "llm_spend_alert.tripplanner.cap"),       # a 0 USD budget: the app makes no AI call at all
])
def test_the_thresholds(spent, cap, check):
    found = ops_checks.spend_findings([_row(spent, cap)], "2026-11-03")
    assert [f.check for f in found] == ([check] if check else [])
    for f in found:
        assert f.check in ops_checks.CHECKS
        assert f.details["signal"] == "llm_spend_alert" and f.details["day"] == "2026-11-03"


def test_an_app_outside_the_list_is_ignored():
    assert ops_checks.spend_findings([_row(9.0, 1.0, app="cis")], "2026-11-03") == []


def test_the_summary_and_the_mail_name_the_app_and_the_numbers():
    [f] = ops_checks.spend_findings([_row(4.5, 5.0)], "2026-11-03")
    assert f.summary == "TripPlanner spent 4.5000 USD of its 5.0000 USD daily budget (90.0 %)"
    subject, text = ops_checks.render(f, environment="dev")
    assert subject == "[AA-CIS dev] Ops check: TripPlanner AI spend near its daily cap"
    assert "/admin/ai-gateway" in text and "cap_usd: 5.0" in text


class _Pool:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.error is not None:
            raise self.error
        return self.rows


def test_the_tick_reads_the_profiles_with_a_budget_and_todays_ledger():
    pool = _Pool([_row(1.2, 1.0)])
    found = asyncio.run(ops_checks.app_spend_findings(pool, now=NOW))
    assert [f.check for f in found] == ["llm_spend_alert.tripplanner.cap"]
    (sql, args), = pool.calls
    assert "shared.llm_app_profile" in sql and "default_daily_budget_usd IS NOT NULL" in sql
    assert "COALESCE(l.app, l.quality_signal ->> 'app') = p.app" in sql
    assert args == (NOW, ["tripplanner"])


def test_no_profile_table_is_no_check():
    pool = _Pool(error=asyncpg.exceptions.UndefinedTableError("relation does not exist"))
    assert asyncio.run(ops_checks.app_spend_findings(pool, now=NOW)) == []


def test_another_error_reaches_the_sampler():
    with pytest.raises(RuntimeError):
        asyncio.run(ops_checks.app_spend_findings(_Pool(error=RuntimeError("pool closed")), now=NOW))


def test_the_sampler_alerts_on_the_spend_findings(monkeypatch):
    raised = []

    async def spend(pool):
        return ops_checks.spend_findings([_row(1.0, 1.0)], "2026-11-03")

    async def alert_all(pool, findings, client=None, **kw):
        raised.extend(f.check for f in findings)
        return {f.check: {"bell": True} for f in findings}

    async def flush(pool, client=None, **kw):
        return {}

    monkeypatch.setattr(ops_checks, "app_spend_findings", spend)
    monkeypatch.setattr(ops_checks, "alert_all", alert_all)
    monkeypatch.setattr(ops_checks, "flush_events", flush)
    out: dict = {}
    asyncio.run(sampler._ops_checks_part(object(), None, out))
    assert raised == ["llm_spend_alert.tripplanner.cap"]
    assert out["ops_checks"] == {"llm_spend_alert.tripplanner.cap": {"bell": True}}


# ── H1c: the CRM relay ───────────────────────────────────────────────────────────────────────────────────────────

CRM_ROW = app_profile.AppProfile(app="crm", degrade_mode="manual_reply_needed", default_daily_budget_usd=2.5)


class _Acquire:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def _request():
    pool = SimpleNamespace(acquire=lambda: _Acquire())
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)), scope={"state": {}},
                           headers={"authorization": "Bearer zz"})


@pytest.fixture
def stored(monkeypatch):
    async def read_all(conn):
        return {"crm": CRM_ROW}
    monkeypatch.setattr(app_profile, "read_all", read_all)
    updates = []

    async def update(pool, app, fields, *, actor):
        updates.append((app, fields))
        return {"app": app, **fields}
    monkeypatch.setattr(app_profile, "update", update)
    return updates


def _relay(monkeypatch, status, body):
    sent = []

    async def call(method, path, authorization, *, json=None, params=None):
        sent.append((method, path, json))
        return status, body
    monkeypatch.setattr(crm_console, "crm_ops_call", call)
    return sent


def _patch(app, **fields):
    return asyncio.run(gw_router.patch_app_profile(gw_router.AppProfilePatch(**fields), _request(), app=app,
                                                   x_admin_user_id="staff-1"))


def test_a_crm_edit_is_relayed_whole_before_the_cis_row(monkeypatch, stored):
    sent = _relay(monkeypatch, 200, {"app": "crm", "ai_enabled": False})
    out = _patch("crm", ai_enabled=False, reason="zz provider outage")
    assert sent == [("PUT", "/llm/app-profile", {
        "ai_enabled": False, "bulk_paused": False, "default_daily_budget_usd": "2.5000",
        "degrade_mode": "manual_reply_needed", "disabled_reason": "zz provider outage"})]
    assert stored == [("crm", {"ai_enabled": False, "disabled_reason": "zz provider outage"})]
    assert out["ai_enabled"] is False


@pytest.mark.parametrize(("status", "expected"), [(403, 403), (422, 422), (503, 503), (500, 502), (404, 502)])
def test_a_crm_refusal_changes_nothing_here(monkeypatch, stored, status, expected):
    _relay(monkeypatch, status, {"detail": "missing_group"})
    with pytest.raises(HTTPException) as e:
        _patch("crm", ai_enabled=True)
    assert e.value.status_code == expected and "partner CRM" in e.value.detail
    assert stored == []


def test_a_bad_value_is_refused_before_the_relay(monkeypatch, stored):
    sent = _relay(monkeypatch, 200, {})
    with pytest.raises(HTTPException) as e:
        _patch("crm", ai_enabled=False, reason="x")             # too short a reason
    assert e.value.status_code == 422 and sent == [] and stored == []


def test_other_apps_are_not_relayed(monkeypatch, stored):
    sent = _relay(monkeypatch, 200, {})
    _patch("tripplanner", ai_enabled=False, reason="zz drill")
    assert sent == [] and stored == [("tripplanner", {"ai_enabled": False, "disabled_reason": "zz drill"})]


def test_the_relay_path_is_allowed_and_nothing_else_under_llm():
    assert crm_console.is_allowed("PUT", "/llm/app-profile")
    assert not crm_console.is_allowed("POST", "/llm/app-profile")
    assert not crm_console.is_allowed("PUT", "/llm/calls")


def test_the_body_of_a_profile_without_a_budget():
    body = gw_router.crm_profile_body(app_profile.AppProfile(app="crm", degrade_mode="manual_reply_needed"))
    assert body["default_daily_budget_usd"] is None and body["ai_enabled"] is True


def test_the_card_says_which_apps_act_on_their_profile():
    assert gw_router.ENFORCED_APPS == {"cis", "tripplanner", "crm"}
    assert set(app_profile.APPS) - gw_router.ENFORCED_APPS == {"booking"}
