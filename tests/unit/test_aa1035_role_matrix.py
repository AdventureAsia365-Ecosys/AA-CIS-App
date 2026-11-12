"""AA-1035 (R2 slice 1, root ADR 0019 d3) — the staff role x route matrix is deny by default.

The design is root docs/audits/2026-11-03-S265-ecosystem-restructure/clarifications.md section 3 (role table and
permission matrix). This file pins:
- each role's capability set (one explicit set per role, nothing inherited by accident);
- the decision for EVERY staff route of the app x every role (the POLICY routes and the unlisted GETs), as a snapshot
  (tests/unit/snapshots/aa1035_role_route_matrix.json): a route a role newly reaches shows up in review;
- the "may not" column of the matrix as hard rules, independent of the snapshot;
- an unknown role reaches nothing, an unlisted GET included.

After a deliberate change regenerate the snapshot with:

    python -m tests.unit.test_aa1035_role_matrix
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from api.core import staff_authz
from api.core.staff_authz import POLICY, ROLE_PERMS, STAFF_PATH_PREFIXES, denial_reason, principal_from_role
from api.core.tenant_scope import TENANT_SCOPE
from tests.unit.test_s241_admin_routes_require_auth import _served_routes, _walk

SNAPSHOT = Path(__file__).parent / "snapshots" / "aa1035_role_route_matrix.json"

R2_ROLES = ("content_lead", "tenant_qa", "customer_success", "support", "finance", "ai_ops")
ROLES = ("admin", "content", "viewer") + R2_ROLES

EXPECTED_PERMS = {
    "content_lead": {"content.read", "content.write", "review.act", "master.admin", "atoms.write", "photos.write",
                     "a4.unpublish", "quality.write", "content.settings.write", "catalog.read", "tenant_content.read",
                     "lab.read", "wave.run", "notifications.read",
                     "reference_price.read", "reference_price.write"},   # AA-1091
    "content": {"content.read", "content.write", "review.act", "catalog.read", "notifications.read",
                "reference_price.read"},   # AA-1091
    "tenant_qa": {"content.read", "catalog.read", "tenant_content.read", "quality.write", "a4.unpublish", "lab.read",
                  "notifications.read"},
    # slice 2: + read-only view-as of the own book (tenant.view_as) and the bell (notifications.read)
    # close-out: + the Tenant QA reads of the own book (tenant_content.book.read)
    "customer_success": {"tenant.book.read", "tenant.book.account.read", "tenant.book.write", "tenant.member.write",
                         "tenant.member.resend", "tenant.view_as", "notifications.read", "tenant_content.book.read",
                         # AA-1091: the customer-service team enters the tours' reference prices
                         "reference_price.read", "reference_price.write"},
    # S267 fix pass: support only sends a non-owner invitation again (no tenant.member.write); slice 2: + view-as
    "support": {"tenant.book.read", "tenant.member.resend", "tenant.view_as", "notifications.read"},
    "finance": {"billing.read", "billing.write", "spend.read", "notifications.read", "reference_price.read"},
    # slice 2: the logged Writing Lab read of tenant text (lab.tenant_text.read, never tenant_content.read)
    "ai_ops": {"content.read", "catalog.read", "ai.admin", "spend.read", "spend.write", "lab.read",
               "lab.tenant_text.read", "notifications.read"},
    "viewer": {"content.read", "notifications.read"},
}


def _is_staff_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in STAFF_PATH_PREFIXES)


def _staff_keys() -> list[tuple[str, str]]:
    from api.main import app

    keys = set()
    for path, route, _ in _walk(_served_routes(app)):
        if isinstance(route, APIRoute) and _is_staff_path(path):
            keys |= {(m, path) for m in route.methods - {"HEAD", "OPTIONS"}}
    return sorted(keys)


def current_matrix() -> dict[str, list[str]]:
    """role -> every "METHOD /path" of the app it passes the capability check on (scope comes after, per tenant)."""
    keys = _staff_keys()
    return {role: [f"{m} {p}" for m, p in keys if denial_reason(principal_from_role(role), m, p) is None]
            for role in ROLES}


def _allowed(role: str) -> set[str]:
    return set(current_matrix()[role])


@pytest.mark.parametrize("role", sorted(EXPECTED_PERMS))
def test_each_role_has_exactly_its_capabilities(role):
    assert set(ROLE_PERMS[role]) == EXPECTED_PERMS[role]


def test_every_role_has_a_tenant_scope_and_nothing_else_does():
    assert set(TENANT_SCOPE) == set(ROLE_PERMS) == set(ROLES)


def test_matrix_snapshot():
    assert SNAPSHOT.exists(), "run: python -m tests.unit.test_aa1035_role_matrix"
    assert current_matrix() == json.loads(SNAPSHOT.read_text()), \
        "role x route matrix changed: review, then run python -m tests.unit.test_aa1035_role_matrix"


@pytest.mark.parametrize("role", ["", "tenant", "Admin", "content_lead,admin", "superuser"])
def test_an_unknown_role_reaches_no_staff_route(role):
    p = principal_from_role(role)
    assert all(denial_reason(p, m, path) is not None for m, path in _staff_keys())


def test_admin_reaches_every_route_but_the_owner_only_writes():
    owner_only = {k for k, v in POLICY.items() if v in staff_authz.OWNER_PERMS}
    blocked = {k for k in _staff_keys() if denial_reason(principal_from_role("admin"), *k) is not None}
    assert blocked == owner_only


def _any(allowed: set[str], *prefixes: str, methods: tuple[str, ...] = ()) -> list[str]:
    return sorted(k for k in allowed for pre in prefixes
                  if k.split(" ", 1)[1].startswith(pre) and (not methods or k.split(" ", 1)[0] in methods))


# ── the "may not" column of clarifications.md section 3 ───────────────────────────────────────────────────────────

ADMIN_ONLY_AREAS = ("/admin/platform/staff", "/admin/platform/audit", "/admin/platform/alert-recipients",
                    "/admin/job-runner/erasure", "/admin/data-model", "/admin/docs", "/admin/overview",
                    "/admin/reports")

# AA-1035 R2 close-out (root ADR 0019 d4): the CRM L1 console reads of the book. The desk roles read, never write;
# support never the account reads (usage, entitlements).
CRM_CONSOLE_BOOK_READS = (
    "GET /admin/platform/crm/agencies", "GET /admin/platform/crm/agencies/{tenant_id}",
    "GET /admin/platform/crm/agencies/{tenant_id}/data-requests", "GET /admin/platform/crm/agencies/{tenant_id}/jobs",
    "GET /admin/platform/crm/agencies/{tenant_id}/members", "GET /admin/platform/crm/console",
)
CRM_CONSOLE_ACCOUNT_READS = (
    "GET /admin/platform/crm/agencies/{tenant_id}/entitlements", "GET /admin/platform/crm/agencies/{tenant_id}/usage",
)


@pytest.mark.parametrize("role", [r for r in ROLES if r != "admin"])
def test_only_admin_reaches_staff_ops_and_platform_pages(role):
    allowed = _allowed(role)
    assert _any(allowed, *ADMIN_ONLY_AREAS) == []
    crm = _any(allowed, "/admin/platform/crm")
    expected = {"customer_success": sorted(CRM_CONSOLE_BOOK_READS + CRM_CONSOLE_ACCOUNT_READS),
                "support": sorted(CRM_CONSOLE_BOOK_READS)}.get(role, [])
    assert crm == expected
    # place fixes and job control change what every app reads (ops.write)
    writes = _any(allowed, "/admin/places/", "/admin/job-runner/", methods=("POST", "PUT", "PATCH", "DELETE"))
    # ai_ops runs the AI jobs (ai.admin: the LLM probe, re-embed, contract smoke, model lifecycle), nothing else
    assert all(POLICY[tuple(k.split(" ", 1))] == staff_authz.AI_ADMIN for k in writes) if role == "ai_ops" \
        else writes == []


# AA-1035 R2 close-out (matrix "Tenant QA: customer_success R (own)"): the reads the routes filter to the book.
TENANT_QA_BOOK_READS = (
    "GET /admin/content-quality/pieces", "GET /admin/content-quality/pieces/{piece_id}",
    "GET /admin/content-quality/stats", "GET /admin/content-quality/tour-versions",
    "GET /admin/content-quality/tour-versions/{version_id}",
)


@pytest.mark.parametrize("role", ["customer_success", "support", "finance"])
def test_the_tenant_desk_never_reaches_the_catalogue_or_tenant_text(role):
    allowed = _allowed(role)
    assert _any(allowed, "/admin/tours", "/admin/review-queue", "/admin/run-tour", "/admin/atoms", "/admin/master",
                "/admin/a4", "/admin/writing-memory", "/admin/tenant-qa", "/admin/llm-config", "/v1/pipeline") == []
    # customer_success: the Tenant QA lists / details of its book only (no labels, place cards or writes)
    assert _any(allowed, "/admin/content-quality") == (sorted(TENANT_QA_BOOK_READS) if role == "customer_success"
                                                       else [])


@pytest.mark.parametrize("role", ["customer_success", "support"])
def test_the_book_roles_reach_tenants_but_not_money_ai_or_account_writes(role):
    allowed = _allowed(role)
    assert "GET /admin/tenants" in allowed and "GET /admin/tenants/{tenant_id}/members" in allowed
    assert "POST /admin/tenants/{tenant_id}/members/{membership_id}/invitation/send" in allowed
    assert _any(allowed, "/admin/ai-gateway", methods=("POST", "PUT", "PATCH", "DELETE")) == []
    assert _any(allowed, "/admin/budgets", "/admin/cost-explorer", "/admin/spend", "/admin/billing") == []
    for write in ("POST /admin/tenants/{tenant_id}/offboard", "POST /admin/tenants/{tenant_id}/restore",
                  "POST /admin/tenants/{tenant_id}/provision", "POST /admin/tenants",
                  "PUT /admin/tenants/{tenant_id}/assignments/{admin_user_id}",
                  "PUT /admin/tenants/{tenant_id}/entitlement-overrides/{feature_key}"):
        assert write not in allowed, write


# AA-1035 (S267 fix pass): customer_success writes the non-billing Tenant 360 of its book (tenant.book.write; PATCH is
# field-level in the route) and every member action; support only sends a non-owner invitation again.
BOOK_WRITES = ("PATCH /admin/tenants/{tenant_id}", "PUT /admin/tenants/{tenant_id}/config",
               "PUT /admin/tenants/{tenant_id}/brand-identity")
MEMBER_WRITES = ("POST /admin/tenants/{tenant_id}/members/invitations",
                 "PATCH /admin/tenants/{tenant_id}/members/{membership_id}",
                 "POST /admin/tenants/{tenant_id}/members/{membership_id}/remove")


@pytest.mark.parametrize("route", BOOK_WRITES + MEMBER_WRITES)
def test_customer_success_writes_its_book_and_support_does_not(route):
    assert route in _allowed("customer_success")
    assert route not in _allowed("support")


def test_support_only_sends_invitations_again():
    writes = _any(_allowed("support"), "/admin", methods=("POST", "PUT", "PATCH", "DELETE"))
    assert writes == [
        "POST /admin/tenants/{tenant_id}/members/{membership_id}/invitation/send",
        "POST /admin/tenants/{tenant_id}/view-as",                 # slice 2: read-only view-as (own book + pool)
        "POST /admin/view-as/sessions/{session_id}/end",
        "PUT /admin/notifications/read-all",                       # slice 2: the bell (own rows only)
        "PUT /admin/notifications/{notif_id}/read",
    ]


# AA-1035 (S267 review): clarifications.md section 3 gives support "-" on plans / invoices, Tenant QA and AI & spend
# (Tenant 360 "R + member actions" only); customer_success has plans and add-ons R, AI & spend R and Tenant QA R.
ACCOUNT_READS = ("GET /admin/tenants/{tenant_id}/usage", "GET /admin/tenants/{tenant_id}/subscription",
                 "GET /admin/tenants/{tenant_id}/cost", "GET /admin/tenants/{tenant_id}/quality",
                 "GET /admin/ai-gateway/tenant-budgets/{tenant_id}", "GET /admin/plans", "GET /admin/plans/features")
SUPPORT_TENANT_360 = ("GET /admin/tenants/{tenant_id}/details", "GET /admin/tenants/{tenant_id}/config",
                      "GET /admin/tenants/{tenant_id}/audit", "GET /admin/tenants/{tenant_id}/members",
                      "GET /admin/tenants/{tenant_id}/members/access", "GET /admin/tenants/{tenant_id}/assignments",
                      "GET /admin/tenants/{tenant_id}/markets", "GET /admin/tenants/{tenant_id}/rewrite-activity")


@pytest.mark.parametrize("route", ACCOUNT_READS)
def test_support_never_reads_plans_usage_cost_quality_or_ai_budget(route):
    assert route not in _allowed("support")
    assert route in _allowed("customer_success")


@pytest.mark.parametrize("route", SUPPORT_TENANT_360)
def test_support_reads_the_non_money_tenant_360_tabs(route):
    assert route in _allowed("support") and route in _allowed("customer_success")


def test_only_support_lacks_the_money_columns_of_the_directory():
    from api.core.staff_authz import sees_tenant_money

    assert sees_tenant_money(None)                    # the machine
    assert not sees_tenant_money("support")
    for role in ("admin", "customer_success", "finance", "content", "viewer", "ai_ops"):
        assert sees_tenant_money(role), role
    assert not sees_tenant_money("superuser")         # unknown role


def test_finance_reads_billing_and_changes_only_billing_fields():
    allowed = _allowed("finance")
    for read in ("GET /admin/tenants", "GET /admin/tenants/{tenant_id}/usage",
                 "GET /admin/tenants/{tenant_id}/subscription", "GET /admin/billing", "GET /admin/plans"):
        assert read in allowed, read
    assert "PATCH /admin/tenants/{tenant_id}" in allowed            # field-level in the route
    # S267 fix pass: Tenant 360 opens on /details (header only for finance) for the Billing / Subscription tabs
    assert "GET /admin/tenants/{tenant_id}/details" in allowed
    for write in BOOK_WRITES[1:]:
        assert write not in allowed, write
    for refused in ("GET /admin/tenants/{tenant_id}/members", "POST /admin/tenants/{tenant_id}/members/invitations",
                    "GET /admin/tenants/{tenant_id}/audit", "POST /admin/tenants/{tenant_id}/offboard",
                    "PATCH /admin/llm-config/{stage}", "PUT /admin/budgets/{provider}/{scope}"):
        assert refused not in allowed, refused


@pytest.mark.parametrize("role", ["content", "viewer", "tenant_qa", "content_lead", "ai_ops"])
def test_the_content_side_never_reads_the_tenants_directory_or_money(role):
    allowed = _allowed(role)
    assert "GET /admin/tenants" not in allowed
    assert _any(allowed, "/admin/tenants/{tenant_id}/usage", "/admin/tenants/{tenant_id}/members",
                "/admin/tenants/{tenant_id}/cost", "/admin/billing") == []
    if role != "ai_ops":     # AI & spend W is ai_ops'
        assert _any(allowed, "/admin/budgets", "/admin/ai-gateway", "/admin/cost-explorer") == []


# AA-1035 (S267 fix pass): the shared tenant picker (names only) for every role whose pages pick a tenant; never the
# pure content roles (content, reviewer alias, viewer), which see no tenant but aa_internal.
@pytest.mark.parametrize("role", ["admin", "content_lead", "tenant_qa", "ai_ops", "customer_success", "support",
                                  "finance"])
def test_the_tenant_picker_is_open_to_every_role_that_picks_a_tenant(role):
    assert "GET /admin/tenant-names" in _allowed(role)


@pytest.mark.parametrize("role", ["content", "viewer"])
def test_the_pure_content_roles_get_no_tenant_names(role):
    assert "GET /admin/tenant-names" not in _allowed(role)


def test_ai_ops_owns_ai_and_spend_but_no_tenant_text():
    allowed = _allowed("ai_ops")
    assert "PATCH /admin/llm-config/{stage}" in allowed and "PUT /admin/budgets/{provider}/{scope}" in allowed
    assert _any(allowed, "/admin/content-quality", "/admin/a4", "/admin/tenant-qa") == []
    # slice 2: the tenant text of the Writing Lab only, every read logged (lab.tenant_text.read,
    # test_aa1035_s2_lab_read)
    assert "GET /admin/writing-memory/signals" in allowed
    assert "GET /admin/writing-memory/tenants/{tenant_id}/voice" not in allowed   # Tenant 360 voice: not the Lab
    assert "GET /admin/writing-memory/releases" in allowed            # ids, statuses, numbers


def test_tenant_qa_and_content_lead_do_tenant_qa_but_only_the_lead_runs_the_catalogue():
    qa, lead = _allowed("tenant_qa"), _allowed("content_lead")
    for key in ("POST /admin/content-quality/pieces/{piece_id}/sentences/apply", "GET /admin/tenant-qa/compliance",
                "POST /admin/tenant-qa/compliance/{flag_id}/release", "GET /admin/a4/content-log"):
        assert key in qa and key in lead, key
    for key in ("PATCH /admin/master/{tour_id}/deactivate", "POST /admin/waves", "PATCH /admin/atoms/{atom_id}"):
        assert key in lead and key not in qa, key
    assert "POST /admin/run-tour-async" in lead and "POST /admin/run-tour-async" not in qa


def test_content_writes_but_never_retires_masters_or_edits_atoms():
    allowed = _allowed("content")
    assert "POST /admin/run-tour-async" in allowed and "GET /admin/atoms" in allowed
    assert "PATCH /admin/master/{tour_id}/trash" not in allowed and "PATCH /admin/atoms/{atom_id}" not in allowed


if __name__ == "__main__":
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(current_matrix(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {SNAPSHOT}")
