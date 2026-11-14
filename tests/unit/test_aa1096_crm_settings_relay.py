"""AA-1096 L4 — the CRM tab of Platform › Settings is relayed to the partner CRM's own registry (crm.ops.v1 1.13,
AA-Partner crm-064) with the staff member's own Cognito token; CIS writes its audit row of each relayed change.
No network (httpx.MockTransport on crm_console), no DB (the audit writer is replaced)."""
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from api.routers import admin_settings_registry as router
from api.services import crm_console, crm_settings_relay as relay
from api.services import settings_registry as reg

VID = "00000000-0000-4000-8000-00000000a196"
AGENCY = "00000000-0000-4000-8000-0000000000a1"
REASON = "zz a reason of at least twenty characters"


def _jwt(claims: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"


TOKEN = _jwt({"iss": "https://cognito-idp.example.test/pool", "sub": "zz-staff"})


def _request(role="admin", token=TOKEN):
    identity = SimpleNamespace(role=role, admin_id="zz-admin-1", auth="cognito", e2e_read_only=False)
    return SimpleNamespace(scope={"state": {"staff_identity": identity, "staff_access_token": token}},
                           headers={}, app=SimpleNamespace(state=SimpleNamespace(pool=object())))


def _row(**over):
    return {"id": VID, "key": "crm.rate.ai_per_minute", "tenant_id": None, "value": 7, "version": 2,
            "status": "approved", "effective_from": "2026-11-13T05:00:00+00:00", "proposed_by": "platform:zz-staff",
            "proposed_at": "2026-11-13T05:00:00+00:00", "reason": REASON, "evidence_ref": None, "self_approved": True,
            "decided_by": "platform:zz-staff", "decided_at": "2026-11-13T05:00:00+00:00", "decision_note": None,
            **over}


@pytest.fixture
def crm(monkeypatch):
    """The CRM's answers by (method, path); the calls it saw; the CIS audit rows written."""
    state = {"answers": {}, "calls": [], "audits": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/crm/ops/v1", 1)[-1]
        state["calls"].append((request.method, path, dict(request.url.params),
                               json.loads(request.content) if request.content else None,
                               request.headers.get("authorization")))
        status, body = state["answers"].get((request.method, path), (404, {"detail": "not_scripted"}))
        return httpx.Response(status, json=body)

    async def audit(conn, **kw):
        state["audits"].append(kw)

    monkeypatch.setattr(crm_console, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(relay, "write_audit_log", audit)
    monkeypatch.setitem(crm_console.APPS["crm"].extra, "ops_base", "https://crm.example.test/crm/ops/v1")
    from api.core.staff_identity import current_staff
    token = current_staff.set(None)
    yield state
    current_staff.reset(token)


@pytest.mark.asyncio
async def test_the_crm_tab_lists_the_crm_settings_in_the_page_shape(crm):
    setting = {"key": "crm.rate.ai_per_minute", "app": "crm", "area": "rate_limit", "pending": [_row(status="proposed")]}
    crm["answers"][("GET", "/settings")] = (200, {"app": "crm", "areas": ["rate_limit"], "settings": [setting],
                                                  "two_person_approval": False, "reader_error": None})
    out = await router.list_registry(_request(), app="crm", area="rate_limit")
    assert out["settings"][0]["proposals"][0]["status"] == "proposed" and "pending" not in out["settings"][0]
    method, path, params, _body, auth = crm["calls"][0]
    assert (method, path, params, auth) == ("GET", "/settings", {"area": "rate_limit"}, f"Bearer {TOKEN}")


# CIS 416 / #1486: the page explains a setting from its definition's guide; the CRM serves the guide of each CRM key
# (AA-Partner app/platform_settings/guides.json) and the relay hands it on untouched, list and history alike.
GUIDE = {lang: {p: f"zz {lang} {p} text" for p in ("what", "raise", "lower", "when", "example")} for lang in ("en", "vi")}


@pytest.mark.asyncio
async def test_the_crm_guide_reaches_the_page(crm):
    setting = {"key": "crm.rate.ai_per_minute", "app": "crm", "area": "rate_limit", "guide": GUIDE, "pending": []}
    crm["answers"][("GET", "/settings")] = (200, {"app": "crm", "areas": ["rate_limit"], "settings": [setting],
                                                  "two_person_approval": False, "reader_error": None})
    crm["answers"][("GET", "/settings/crm.rate.ai_per_minute/history")] = (
        200, {"key": "crm.rate.ai_per_minute", "tenant_id": None, "history": [],
              "definition": {"key": "crm.rate.ai_per_minute", "guide": GUIDE}})
    out = await router.list_registry(_request(), app="crm", area=None)
    assert out["settings"][0]["guide"] == GUIDE
    hist = await router.key_history(_request(), "crm.rate.ai_per_minute", tenant_id=None)
    assert hist["definition"]["guide"] == GUIDE


@pytest.mark.asyncio
async def test_history_and_a_crm_refusal_pass_through(crm):
    crm["answers"][("GET", "/settings/crm.rate.intake_form_per_hour/history")] = (
        200, {"key": "crm.rate.intake_form_per_hour", "tenant_id": AGENCY, "definition": {}, "history": []})
    out = await router.key_history(_request(), "crm.rate.intake_form_per_hour", tenant_id=AGENCY)
    assert out["tenant_id"] == AGENCY and crm["calls"][-1][2] == {"tenant_id": AGENCY}
    crm["answers"][("GET", "/settings")] = (503, {"detail": "settings_unavailable", "message": "not yet (crm-064)"})
    with pytest.raises(HTTPException) as e:
        await router.list_registry(_request(), app="crm", area=None)
    assert e.value.status_code == 503 and e.value.detail == {"code": "settings_unavailable",
                                                             "message": "not yet (crm-064)"}
    crm["answers"][("GET", "/settings")] = (500, {"detail": "boom"})
    with pytest.raises(HTTPException) as e:
        await router.list_registry(_request(), app="crm", area=None)
    assert e.value.status_code == 502


@pytest.mark.asyncio
async def test_a_password_session_cannot_reach_the_crm(crm):
    with pytest.raises(HTTPException) as e:
        await router.list_registry(_request(token="not-a-jwt"), app="crm", area=None)
    assert e.value.status_code == 401 and crm["calls"] == []


@pytest.mark.asyncio
async def test_a_crm_change_is_relayed_and_audited_in_cis(crm):
    crm["answers"][("POST", "/settings/crm.rate.ai_per_minute/changes")] = (201, _row())
    body = router.ProposalBody(value=7, reason=REASON, base_version=1, apply=True)
    out = await router.propose(_request(), "crm.rate.ai_per_minute", body)
    assert out["status"] == "approved"
    sent = crm["calls"][-1][3]
    assert sent == {"value": 7, "reason": REASON, "evidence_ref": None, "effective_from": None, "tenant_id": None,
                    "base_version": 1, "apply": True}
    actions = [a["action"] for a in crm["audits"]]
    assert actions == [reg.PlatformAuditAction.SETTING_PROPOSED, reg.PlatformAuditAction.SETTING_APPROVED]
    assert all(a["actor"] == "admin:zz-admin-1" and a["details"]["app"] == "crm" for a in crm["audits"])


@pytest.mark.asyncio
async def test_a_crm_decision_names_its_app_and_agency(crm):
    crm["answers"][("POST", f"/settings/changes/{VID}/reject")] = (
        200, _row(status="rejected", tenant_id=AGENCY, decision_note="not now", self_approved=False))
    out = await router.reject(_request(), VID, router.DecisionBody(note="not now", app="crm", tenant_id=AGENCY))
    assert out["status"] == "rejected" and crm["calls"][-1][3] == {"note": "not now", "tenant_id": AGENCY}
    audit = crm["audits"][-1]
    assert audit["action"] == reg.PlatformAuditAction.SETTING_REJECTED and audit["tenant_id"] is None
    assert audit["resource_id"] == f"crm.rate.ai_per_minute@{AGENCY}" and audit["details"]["agency_id"] == AGENCY
    crm["answers"][("POST", f"/settings/changes/{VID}/approve")] = (409, {"detail": "second_person_required",
                                                                         "message": "needs a second person"})
    with pytest.raises(HTTPException) as e:
        await router.approve(_request(), VID, router.DecisionBody(app="crm"))
    assert e.value.status_code == 409 and e.value.detail["code"] == "second_person_required"


@pytest.mark.asyncio
async def test_a_failed_cis_audit_never_reports_the_crm_change_as_failed(crm, monkeypatch):
    async def broken(conn, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(relay, "write_audit_log", broken)
    crm["answers"][("POST", "/settings/crm.rate.ai_per_minute/changes")] = (201, _row(status="proposed"))
    out = await router.propose(_request(), "crm.rate.ai_per_minute",
                               router.ProposalBody(value=7, reason=REASON, base_version=1))
    assert out["status"] == "proposed"


@pytest.mark.asyncio
async def test_a_role_that_may_not_approve_cannot_apply_a_crm_change(crm):
    with pytest.raises(HTTPException) as e:
        await router.propose(_request(role="ai_ops"), "crm.rate.ai_per_minute",
                             router.ProposalBody(value=7, reason=REASON, base_version=1, apply=True))
    assert e.value.status_code == 403 and crm["calls"] == []


def test_the_console_reaches_only_the_settings_routes():
    assert crm_console.is_allowed("GET", "/settings")
    assert crm_console.is_allowed("GET", "/settings/crm.rate.ai_per_minute/history")
    assert crm_console.is_allowed("POST", "/settings/crm.rate.ai_per_minute/changes")
    assert crm_console.is_allowed("POST", f"/settings/changes/{VID}/approve")
    assert not crm_console.is_allowed("POST", "/settings/cis.publish.daily_limit_default/changes")
    assert not crm_console.is_allowed("DELETE", "/settings")
    assert not crm_console.is_allowed("POST", "/settings/changes/not-a-uuid/approve")


# ── S280 UX fix round ────────────────────────────────────────────────────────────────────────────────────────────
class _NamesPool:
    """pool.acquire() whose connection answers the staff-name read (cognito_sub -> name)."""

    def __init__(self, names: dict):
        self.names, self.asked = names, []

    def acquire(self):
        pool = self

        class _Conn:
            async def fetch(self, _sql, subs):
                pool.asked.append(list(subs))
                return [{"cognito_sub": s, "name": pool.names[s]} for s in subs if s in pool.names]

        class _Ctx:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _request_with_pool(pool):
    req = _request()
    req.app.state.pool = pool
    return req


@pytest.mark.asyncio
async def test_the_crm_list_names_the_staff_and_says_who_may_write(crm):
    setting = {"key": "crm.rate.ai_per_minute", "app": "crm", "area": "rate_limit", "last_change": _row(),
               "pending": [_row(status="proposed", proposed_by="platform:zz-other", decided_by=None)]}
    crm["answers"][("GET", "/settings")] = (200, {"app": "crm", "areas": ["rate_limit"], "settings": [setting],
                                                  "two_person_approval": False, "reader_error": None})
    crm["answers"][("GET", "/console")] = (200, {"can_write": False})
    pool = _NamesPool({"zz-staff": "Zz Staff Member"})
    out = await router.list_registry(_request_with_pool(pool), app="crm", area=None)
    row = out["settings"][0]
    last = row["last_change"]
    assert (last["proposed_by_name"], last["decided_by_name"]) == ("Zz Staff Member", "Zz Staff Member")
    assert row["proposals"][0]["proposed_by_name"] is None and row["proposals"][0]["decided_by_name"] is None
    assert pool.asked == [["zz-other", "zz-staff"]] and out["can_write"] is False
    crm["answers"][("GET", "/console")] = (503, {"detail": "crm_unavailable"})
    assert (await router.list_registry(_request_with_pool(pool), app="crm", area=None))["can_write"] is None


@pytest.mark.asyncio
async def test_the_crm_history_names_the_staff_and_a_broken_name_read_never_fails_it(crm):
    crm["answers"][("GET", "/settings/crm.rate.ai_per_minute/history")] = (
        200, {"key": "crm.rate.ai_per_minute", "tenant_id": None, "definition": {}, "history": [_row()]})
    names = _NamesPool({"zz-staff": "Zz Staff"})
    out = await router.key_history(_request_with_pool(names), "crm.rate.ai_per_minute", tenant_id=None)
    assert out["history"][0]["decided_by_name"] == "Zz Staff"
    out = await router.key_history(_request(), "crm.rate.ai_per_minute", tenant_id=None)   # pool = object(): no names
    assert out["history"][0]["decided_by_name"] is None


@pytest.mark.asyncio
async def test_a_crm_change_dated_in_the_past_is_refused_before_the_relay(crm):
    from datetime import datetime, timedelta, timezone
    past = datetime.now(timezone.utc) - timedelta(days=1)
    with pytest.raises(HTTPException) as e:
        await router.propose(_request(), "crm.rate.ai_per_minute",
                             router.ProposalBody(value=7, reason=REASON, base_version=1, effective_from=past))
    assert e.value.status_code == 422 and e.value.detail["code"] == "effective_in_past" and crm["calls"] == []
    crm["answers"][("POST", "/settings/crm.rate.ai_per_minute/changes")] = (201, _row(status="proposed"))
    just_now = datetime.now(timezone.utc) - timedelta(seconds=30)        # the form's minute picker: counts as now
    await router.propose(_request(), "crm.rate.ai_per_minute",
                         router.ProposalBody(value=7, reason=REASON, base_version=1, effective_from=just_now))
    assert crm["calls"][-1][3]["effective_from"] is None
