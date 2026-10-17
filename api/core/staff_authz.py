"""AA-882 (S241 GĐ1) — server-side staff authorization: capabilities per role, policy per route.

Until now the backend only checked the shared X-Admin-Secret (held by the staff BFF) and trusted
whatever came with it, so a reviewer or content user could change a stage's model, a spend budget
or a Jev question by calling /api/admin/* directly: the BFF lets every staff role write by default
(frontend/lib/rbac.ts canWriteAdminPath). This module is the backend half of the rule.

Pieces
------
- ROLE_PERMS: staff role -> capabilities. The three roles are the shared.admin_role enum and the
  frontend/lib/rbac.ts StaffRole union (tests/unit/test_aa882_staff_authz.py keeps them in sync).
  A new kind of staff (ops, sales, content lead) is one more row here, later one Cognito group.
- POLICY: (method, route template) -> the capability the route needs. Keyed by method + template,
  not by router: six routers share the /admin prefix and mix reads every role needs (GET
  /admin/llm-config for the S1 page) with admin-only writes (PATCH /admin/llm-config/{stage}).
- MACHINE_ROUTES: the routes a machine caller (secret, no role header) may call — the CI deploy
  preflight/smoke and the ops-probe Lambda scheduler. Callers that never send x-admin-role must keep
  working, or the next deploy's preflight gets 403 and every CIS deploy is blocked.
- staff_policy: the FastAPI dependency. Attach it router-level next to verify_admin_secret; it
  checks the secret itself too (same function, FastAPI runs it once per request).

Principal (GĐ1, until the staff JWT of GĐ4)
------------------------------------------
- secret + x-admin-role  -> staff principal with that role. The BFF forwards the role from the
  staff JWT it verified (/auth/verify-admin). Header trust ends with the Bearer staff JWT (GĐ4) and
  Cognito (ADR 0006): whoever holds the secret can still claim any role until then.
- secret, no x-admin-role -> machine principal: allowed only on MACHINE_ROUTES.
- a role header that is not a staff role (empty, "tenant", typo) -> 403, never a machine.

Routes with no POLICY entry are not narrowed here (GĐ1 covers the money/model writes only; the
route-policy test in tests/unit/test_aa882_route_policy.py reports the rest). GĐ3 extends POLICY to
every /admin route and turns that test to enforcing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import structlog
from fastapi import Depends, Header, HTTPException, Request

from api.routers.admin import verify_admin_secret

logger = structlog.get_logger()

# ── Capabilities ────────────────────────────────────────────────────────────────────────────────

CONTENT_READ = "content.read"
CONTENT_WRITE = "content.write"              # upload, S1 rewrite, regenerate
REVIEW_ACT = "review.act"
MASTER_ADMIN = "master.admin"
ATOMS_WRITE = "atoms.write"
PHOTOS_WRITE = "photos.write"
A4_UNPUBLISH = "a4.unpublish"
CONTENT_SETTINGS_WRITE = "content.settings.write"   # gates, brand, SEO
TENANT_READ = "tenant.read"
TENANT_WRITE = "tenant.write"
OPS_READ = "ops.read"
OPS_WRITE = "ops.write"                      # job cancel/retry/retention, scheduled ops checks
AI_ADMIN = "ai.admin"                        # stage models + routes, model catalog, Jev questions
SPEND_READ = "spend.read"
SPEND_WRITE = "spend.write"                  # budgets, Jev credit ledger, Cost Explorer fetch
STAFF_READ = "staff.read"
STAFF_WRITE = "staff.write"                  # change a staff role / active flag: owners only
AUDIT_READ = "audit.read"

_CONTENT_WORK = frozenset({CONTENT_READ, CONTENT_WRITE, REVIEW_ACT})

ROLE_PERMS: dict[str, frozenset[str]] = {
    "admin": _CONTENT_WORK | {
        MASTER_ADMIN, ATOMS_WRITE, PHOTOS_WRITE, A4_UNPUBLISH, CONTENT_SETTINGS_WRITE,
        TENANT_READ, TENANT_WRITE, OPS_READ, OPS_WRITE, AI_ADMIN, SPEND_READ, SPEND_WRITE,
        STAFF_READ, AUDIT_READ,
    },
    "reviewer": _CONTENT_WORK,
    "content": _CONTENT_WORK,
}

# staff.write is not a role capability: it needs the owner flag (Nghiệp + one break-glass account,
# S241 Q2), which has no column yet (admin_users.is_owner arrives with GĐ5). No route uses it in GĐ1.
OWNER_PERMS: frozenset[str] = frozenset({STAFF_WRITE})

CAPABILITIES: frozenset[str] = frozenset().union(*ROLE_PERMS.values(), OWNER_PERMS)

STAFF_ROLES: tuple[str, ...] = tuple(ROLE_PERMS)

# ── Route policy (GĐ1: the money / model / Jev / job-control / settings writes) ─────────────────

POLICY: dict[tuple[str, str], str] = {
    # admin_llm_ops — stage model, fallback route, catalog
    ("PATCH", "/admin/llm-config/{stage}"): AI_ADMIN,
    ("PATCH", "/admin/llm-config/{stage}/route"): AI_ADMIN,
    ("PATCH", "/admin/llm-catalog/{model_key}"): AI_ADMIN,
    # admin_llm_ops — on-demand / scheduled spend + Jev probes (the Lambda ones are MACHINE_ROUTES)
    ("POST", "/admin/dfs-balance/check"): OPS_WRITE,
    ("POST", "/admin/cost-explorer/check"): SPEND_WRITE,
    ("POST", "/admin/jev-canary/check"): OPS_WRITE,
    ("POST", "/admin/jev/402-sweep"): OPS_WRITE,
    # admin_budgets
    ("PUT", "/admin/budgets/{provider}/{scope}"): SPEND_WRITE,
    # admin_job_runner — job control + retention
    ("POST", "/admin/job-runner/jobs/{job_id}/cancel"): OPS_WRITE,
    ("POST", "/admin/job-runner/jobs/{job_id}/retry"): OPS_WRITE,
    ("POST", "/admin/job-runner/retention/run"): OPS_WRITE,
    ("POST", "/admin/job-runner/retention/schedule"): OPS_WRITE,
    # admin_decisions — Jev question mode/floors, Jev credit ledger
    ("PUT", "/admin/decisions/questions/{question_key}"): AI_ADMIN,
    ("POST", "/admin/decisions/jev-credit/topups"): SPEND_WRITE,
    ("DELETE", "/admin/decisions/jev-credit/topups/{topup_id}"): SPEND_WRITE,
    # admin_settings — SEO config of the aa_internal tenant
    ("PATCH", "/admin/settings/seo"): CONTENT_SETTINGS_WRITE,
}

# Machine callers send the secret and no x-admin-role. (method, route) -> who calls it.
# The GETs carry no POLICY entry yet (any secret holder may read them); they are listed so the
# allowlist is complete when GĐ3 puts every /admin route under POLICY.
MACHINE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/admin/job-runner/deploy-gate"): "CI deploy-dev.yml: pre-deploy gate + post-deploy smoke",
    ("GET", "/admin/job-runner/summary"): "CI deploy-dev.yml: post-deploy smoke",
    ("GET", "/admin/job-runner/workers"): "CI deploy-dev.yml: post-deploy smoke (worker parity)",
    ("GET", "/admin/overview"): "CI deploy-dev.yml: post-deploy smoke",
    ("POST", "/admin/dfs-balance/check"): "Infra Lambda dfs_balance_check (daily, AA-627)",
    ("POST", "/admin/jev-canary/check"): "Infra Lambda dfs_balance_check (daily, AA-720)",
    ("POST", "/admin/jev/402-sweep"): "Infra Lambda dfs_balance_check (daily, AA-720)",
    ("POST", "/admin/waves/check"): "Infra Lambda dfs_balance_check (AA-727)",
    ("POST", "/admin/job-runner/retention/schedule"): "Infra Lambda dfs_balance_check (daily, AA-825)",
}


# ── Principal + decision ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StaffPrincipal:
    """Who is calling, as far as GĐ1 can tell: a staff role (from x-admin-role) or the machine."""
    kind: str                       # "staff" | "machine"
    role: Optional[str] = None
    perms: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_machine(self) -> bool:
        return self.kind == "machine"


MACHINE = StaffPrincipal(kind="machine")


def principal_from_role(role_header: Optional[str]) -> StaffPrincipal:
    """The principal behind a request that already passed the secret check.

    No header at all -> machine. A header that is not a known staff role -> a staff principal with no
    capabilities (refused on every POLICY route), so a typo can never fall back to machine rights.
    """
    if role_header is None:
        return MACHINE
    role = role_header.strip()
    return StaffPrincipal(kind="staff", role=role, perms=ROLE_PERMS.get(role, frozenset()))


def has_perm(role: Optional[str], capability: str) -> bool:
    return capability in ROLE_PERMS.get(role or "", frozenset())


def denial_reason(principal: StaffPrincipal, method: str, route: str) -> Optional[str]:
    """None when the principal may call (method, route); otherwise the 403 detail."""
    capability = POLICY.get((method, route))
    if capability is None:
        return None  # not narrowed in GĐ1 (reported by the route-policy test)
    if principal.is_machine:
        if (method, route) in MACHINE_ROUTES:
            return None
        return "This route needs a staff role (x-admin-role); machine callers are not allowed"
    if principal.role not in ROLE_PERMS:
        return "Unknown staff role"
    if capability not in principal.perms:
        return f"Role '{principal.role}' lacks the '{capability}' permission"
    return None


def _route_template(request: Request) -> Optional[str]:
    """The matched route's path template, e.g. /admin/llm-config/{stage}.

    FastAPI puts the matched APIRoute in scope["route"]. Its .path holds the APIRouter(prefix=...)
    but, on FastAPI >= 0.143, NOT an include_router(prefix=...): POLICY routers are therefore
    included without a prefix (tests/unit/test_aa882_route_policy.py checks it).
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else None


def staff_policy(
    request: Request,
    _secret: None = Depends(verify_admin_secret),
    x_admin_role: Optional[str] = Header(None),
) -> StaffPrincipal:
    """Router-level dependency: the admin secret, then the route's POLICY capability for the caller.

    503/403 from verify_admin_secret as before; 403 when the role lacks the capability, the role is
    unknown, or a machine caller hits a non-machine POLICY route. Fails closed (403) when the matched
    route can not be read from the request, since the policy can not be looked up then.
    """
    principal = principal_from_role(x_admin_role)
    template = _route_template(request)
    if template is None:
        logger.error("staff_policy_no_route", path=request.url.path, method=request.method)
        raise HTTPException(status_code=403, detail="Route policy unavailable")
    reason = denial_reason(principal, request.method, template)
    if reason is not None:
        logger.warning("staff_policy_denied", method=request.method, route=template,
                       role=principal.role, kind=principal.kind,
                       capability=POLICY.get((request.method, template)),
                       admin_user=request.headers.get("x-admin-user-id"))
        raise HTTPException(status_code=403, detail=reason)
    return principal
