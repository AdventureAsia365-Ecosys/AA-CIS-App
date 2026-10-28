"""AA-882 (S241 GĐ3) — one or two sample routes per router, per principal, through the real app.

test_aa882_staff_authz.py checks the decision for every POLICY route (pure) and the refusals through
the app. This file checks the other half on a sample of each router: a principal that HAS the
capability gets past staff_policy into the endpoint (the DB pool raises, a body error is a 422, or the
endpoint answers on its own), and one that does not is refused with the policy's 403 — for admin,
reviewer, content and the machine (secret without x-admin-role).
"""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.staff_authz import MACHINE_ROUTES, POLICY, ROLE_PERMS
from api.main import app
from api.routers import admin, admin_settings

_SECRET = "aa882-gd3-test-admin-secret"
_UUID = "00000000-0000-0000-0000-00000000aa82"

# (router module, method, route template, json body). Bodies are minimal: an allowed principal may get a
# 422 from the endpoint's own validation, which is past the policy too.
SAMPLES = [
    ("admin", "PATCH", "/admin/master/{tour_id}/trash", None),
    ("admin", "PUT", "/admin/notifications/read-all", None),
    ("admin", "GET", "/admin/tenants", None),
    ("admin", "POST", "/admin/tenants/{tenant_id}/generate-key", None),
    ("admin_tenant360", "GET", "/admin/tenants/{tenant_id}/cost", None),
    ("admin_pipeline", "POST", "/admin/review-queue/{review_id}/approve", {}),
    ("admin_pipeline", "POST", "/admin/run-tour-async", {}),
    ("admin_pipeline", "POST", "/admin/brands", {}),
    ("admin_pipeline", "GET", "/admin/infra/status", None),
    ("admin_atoms", "PATCH", "/admin/atoms/{atom_id}", {"text": "x"}),
    ("admin_a4", "POST", "/admin/a4/publish-log/{publish_id}/unpublish", {}),
    ("admin_content_quality", "PATCH", "/admin/content-quality/place-cards/{card_id}", {}),
    ("admin_photos", "POST", "/admin/photos/{photo_id}/assign", {}),
    ("admin_places", "POST", "/admin/places/backfill", {"limit": 999}),
    ("admin_supply_content", "POST", "/admin/supply-content/{entity_id}/versions/{version_id}/publish", None),
    ("admin_segment_research", "POST", "/admin/segment-research/run", {}),
    ("admin_waves", "POST", "/admin/waves/{wave_id}/close", None),
    ("admin_waves", "POST", "/admin/waves/check", None),
    ("admin_overview", "GET", "/admin/overview", None),
    ("admin_budgets", "GET", "/admin/budgets", None),
    ("admin_llm_ops", "GET", "/admin/spend/daily", None),
    ("admin_dashboard", "GET", "/admin/dashboard/summary", None),
    ("admin_search", "GET", "/admin/search", None),
    ("admin_seo", "GET", "/admin/seo-intelligence", None),
    ("admin_platform", "GET", "/admin/platform/staff", None),
    ("admin_platform", "GET", "/admin/platform/staff/{staff_id}", None),
    ("admin_platform_ops", "GET", "/admin/platform/apps", None),      # AA-884
    ("admin_platform_ops", "GET", "/admin/platform/audit", None),
    ("admin_platform_ops", "GET", "/admin/platform/cost", None),
    ("admin_alert_recipients", "GET", "/admin/platform/alert-recipients", None),      # AA-913
    ("admin_alert_recipients", "POST", "/admin/platform/alert-recipients/refresh", None),
    ("admin_crm_console", "GET", "/admin/platform/crm/agencies", None),      # AA-941
    ("admin_crm_console", "POST", "/admin/platform/crm/agencies/{tenant_id}/lock", {"ticket_ref": "AA-941"}),
    ("admin_data_model", "GET", "/admin/data-model", None),      # AA-894
    ("admin_docs", "GET", "/admin/docs/manifest", None),         # AA-898
    ("admin_data_coverage", "GET", "/admin/data-coverage", None),      # AA-992
    ("admin_aa_content", "PUT", "/admin/aa-content/{content_id}/draft",      # AA-977
     {"title": "t", "expected_updated_at": "2026-10-24T08:00:00Z"}),
    ("admin_aa_content", "POST", "/admin/aa-content/{content_id}/publish",
     {"expected_updated_at": "2026-10-24T08:00:00Z"}),
    ("admin_writing_memory", "GET", "/admin/writing-memory/signals", None),      # AA-917
    ("admin_writing_memory", "POST", "/admin/writing-memory/candidates/{rule_id}/reject", {"reason": "not useful"}),
    ("admin_writing_memory", "POST", "/admin/writing-memory/candidates/{rule_id}/activate", None),      # AA-918
    ("admin_writing_memory", "POST", "/admin/writing-memory/releases/{release_id}/activate", None),      # AA-919
    ("admin_writing_memory", "GET", "/admin/writing-memory/releases/active", None),
    ("v1_trip_page", "POST", "/admin/trip-pages/{tour_id}/publish", None),
    ("v1_pipeline", "POST", "/v1/pipeline/review-queue/{review_id}/approve", {}),
    ("v1_rules", "PATCH", "/v1/rules/{rule_id}", {"is_active": False}),
]

PRINCIPALS = ["admin", "reviewer", "content", None]  # None = machine (secret, no role header)

_POLICY_DENIALS = ("Role '", "This route needs a staff role", "Unknown staff role")


def _concrete(path: str) -> str:
    return re.sub(r"\{\w+\}", _UUID, path)


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ADMIN_SECRET", _SECRET)        # /v1 staff guards read it per call
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=AssertionError("the DB must not be reached"))
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        setattr(pool, name, AsyncMock(side_effect=AssertionError("the DB must not be reached")))
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    return TestClient(app, raise_server_exceptions=True)


def _outcome(client, method, path, body, role) -> str:
    """"denied" when staff_policy refused the call, "reached" when it got past the policy."""
    headers = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "aa882-gd3"}
    if role is not None:
        headers["x-admin-role"] = role
    try:
        resp = client.request(method, _concrete(path), json=body, headers=headers)
    except Exception:  # noqa: BLE001 — the endpoint ran (DB pool / missing app state raised)
        return "reached"
    if resp.status_code == 403 and str(resp.json().get("detail", "")).startswith(_POLICY_DENIALS):
        return "denied"
    return "reached"


def _expected(method, path, role) -> str:
    capability = POLICY.get((method, path))
    if capability is None:
        return "reached"
    if role is None:
        return "reached" if (method, path) in MACHINE_ROUTES else "denied"
    return "reached" if capability in ROLE_PERMS[role] else "denied"


def test_every_router_with_routes_has_a_sample():
    from tests.unit.test_aa882_route_policy import _staff_routes

    modules = {r.endpoint.__module__.rsplit(".", 1)[-1] for _, _, r, _ in _staff_routes()}
    sampled = {m for m, *_ in SAMPLES}
    # admin_api_docs (GET openapi.json, admin-only by its own inline check), admin_decisions,
    # admin_job_runner and admin_settings are covered route by route in test_aa882_staff_authz.py.
    assert modules - sampled <= {"admin_api_docs", "admin_decisions", "admin_job_runner", "admin_settings"}


def test_samples_are_real_routes():
    from tests.unit.test_aa882_route_policy import _staff_routes

    keys = {(m, p) for m, p, _, _ in _staff_routes()}
    assert not [s for s in SAMPLES if (s[1], s[2]) not in keys]


@pytest.mark.parametrize("role", PRINCIPALS, ids=lambda r: r or "machine")
@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: f"{s[1]} {s[2]}")
def test_sample_route_per_principal(client, sample, role):
    _, method, path, body = sample
    assert _outcome(client, method, path, body, role) == _expected(method, path, role), \
        (role, method, path, POLICY.get((method, path)))


def test_reviewer_keeps_the_review_queue_and_loses_the_master_lifecycle(client):
    """The two behaviours a reviewer notices: Review Queue actions work, Master Content trash does not."""
    assert _outcome(client, "POST", "/admin/review-queue/{review_id}/approve", {}, "reviewer") == "reached"
    assert _outcome(client, "PATCH", "/admin/master/{tour_id}/trash", None, "reviewer") == "denied"


def test_brand_identity_tenant_jwt_path_is_not_narrowed(client):
    """AA-424: a tenant edits its own brand with a Bearer JWT and no secret. staff_policy_if_staff
    must leave that request to the route's own guard (here 401 for a bad token), not deny it."""
    resp = client.post("/admin/brand-identity", json={}, headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code != 403 or not str(resp.json().get("detail", "")).startswith(_POLICY_DENIALS)
    staff = client.post("/admin/brand-identity", json={},
                        headers={"X-Admin-Secret": _SECRET, "x-admin-role": "content"})
    assert staff.status_code == 403
    assert staff.json()["detail"] == "Role 'content' lacks the 'content.settings.write' permission"


@pytest.mark.parametrize("path", ["/v1/rules", "/v1/pipeline/review-queue"])
def test_v1_staff_reads_keep_their_401_without_credentials(client, path):
    assert client.get(path).status_code == 401
