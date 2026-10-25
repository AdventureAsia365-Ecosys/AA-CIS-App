"""AA-885 — guards for the tenant portal split (portal/ = its own Next.js app and Vercel project), and AA-979 —
the portal source moved to AdventureAsia365-Ecosys/AA-Partner (portal/, CRM-P2a, root ADR 0010 §6).

- The copy of portal/ left in this repo is FROZEN: it is not deployed from here any more and stays only until it is
  deleted (P2b + 7 days). Any change under portal/ fails `test_portal_copy_is_frozen`: make portal changes in
  AA-Partner. The vendored UI-kit byte checks moved there too ("Vendored CIS copies", PORTAL_COPIES).
- No workflow of this repo deploys the portal or runs its smoke.
- The frozen portal app holds no admin secret and no staff session code.
- The admin app does not serve the portal: the moved routes are gone, /portal and /tenant-login answer 308 to the
  portal host, the admin gate and rbac no longer mention the portal.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
PORTAL = REPO / "portal"
PORTAL_HOST = "https://portal.lumiguides.it.com"

# AA-979 — tree id of portal/ at the cut (AA-CIS-App d1e0eb43, 118 files); AA-Partner's portal/ started from the
# identical tree. Do not re-pin it to make a portal change pass: make the change in AA-Partner portal/.
FROZEN_PORTAL_TREE = "ba736844f356f5d13aa40e4fc378c460e5efbfb7"

FORBIDDEN_IN_PORTAL = ["ADMIN_SECRET", "INTERNAL_API_KEY", "x-admin-secret", "COGNITO_", "cis_admin_token"]

SECURITY_HEADERS = [
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "X-Frame-Options",
    "Content-Security-Policy",
    "Permissions-Policy",
]


def _portal_source_files():
    for p in PORTAL.rglob("*"):
        rel = p.relative_to(PORTAL).parts
        if not p.is_file() or rel[0] in {"node_modules", ".next"}:
            continue
        if p.suffix in {".ts", ".tsx", ".mjs", ".js", ".json", ".css"} and p.name != "package-lock.json":
            yield p


def test_portal_holds_no_admin_secret_or_staff_session():
    hits = []
    for p in _portal_source_files():
        text = p.read_text(errors="ignore")
        hits += [f"{p.relative_to(REPO)}: {word}" for word in FORBIDDEN_IN_PORTAL if word in text]
    assert hits == [], "the portal app must not carry admin secrets or staff session code:\n" + "\n".join(hits)


def test_portal_routes_left_the_admin_app():
    for gone in [
        "frontend/app/(tenant)",
        "frontend/app/tenant-login",
        "frontend/app/api/playground",
        "frontend/app/api/auth/tenant-login",
        "frontend/app/api/auth/tenant-logout",
        "frontend/app/api/tenant/me",
    ]:
        assert not (REPO / gone).exists(), f"{gone} still exists in the admin app (AA-885 moved it to portal/)"


def test_admin_redirects_old_portal_urls_with_308():
    cfg = (FRONTEND / "next.config.ts").read_text()
    apps = (FRONTEND / "lib" / "apps.ts").read_text()
    assert f'export const PORTAL_BASE_URL = "{PORTAL_HOST}";' in apps
    for source, dest in [
        ("/portal", "/portal"),
        ("/portal/:path*", "/portal/:path*"),
        ("/tenant-login", "/tenant-login"),
    ]:
        pattern = (
            r'\{\s*source:\s*"' + re.escape(source) + r'",\s*destination:\s*`\$\{PORTAL_BASE_URL\}'
            + re.escape(dest) + r'`,\s*permanent:\s*true\s*\}'
        )
        assert re.search(pattern, cfg), f"next.config.ts: missing permanent redirect {source} → portal host {dest}"


# AA-759 — every old T-number (and flat utility) slug answers 308 to its new path on the portal host.
OLD_PORTAL_SLUGS = {
    "/portal/t1-rewrite": "/portal/tours",
    "/portal/t4-pool": "/portal/tours/mine",
    "/portal/marketplace": "/portal/tours/marketplace",
    "/portal/t0-brand": "/portal/brand",
    "/portal/t7-planning": "/portal/create/social",
    "/portal/t8-angle-gate": "/portal/create/write",
    "/portal/t10-review": "/portal/content",
    "/portal/t11-publish": "/portal/content/publish",
    "/portal/t11-publish/connection": "/portal/account/integrations",
    "/portal/activity": "/portal/account/activity",
    "/portal/billing": "/portal/account/billing",
    "/portal/settings": "/portal/account/settings",
    "/portal/api": "/portal/account/api",
}


def test_portal_redirects_old_slugs_with_308():
    cfg = (PORTAL / "next.config.ts").read_text()
    assert "...OLD_SLUG_REDIRECTS.map((r) => ({ ...r, permanent: true }))" in cfg
    for source, dest in OLD_PORTAL_SLUGS.items():
        pattern = r'\{\s*source:\s*"' + re.escape(source) + r'",\s*destination:\s*"' + re.escape(dest) + r'"\s*\}'
        assert re.search(pattern, cfg), f"portal/next.config.ts: missing 308 {source} -> {dest}"
        # The new page exists and the old route folder is gone.
        assert (PORTAL / "app" / dest.lstrip("/") / "page.tsx").is_file(), f"no page for {dest}"
        assert not (PORTAL / "app" / source.lstrip("/") / "page.tsx").exists(), f"old page {source} still exists"


def test_portal_nav_has_no_t_number_slug():
    nav = (PORTAL / "app" / "portal" / "_shell" / "portalNav.tsx").read_text()
    assert not re.search(r'"/portal/t\d+-', nav), "portalNav.tsx still links a T-number slug"
    for dest in set(OLD_PORTAL_SLUGS.values()) - {"/portal/create/write"}:
        assert f'"{dest}"' in nav, f"portalNav.tsx ROUTES misses {dest}"


def test_admin_gate_and_rbac_no_longer_serve_the_portal():
    gate = (FRONTEND / "proxy.ts").read_text()
    matcher = gate[gate.index("export const config"):]
    assert "/portal" not in matcher and "/tenant-login" not in matcher
    assert "cis_tenant_token" not in gate and "tenantVerifyCache" not in gate
    rbac = (FRONTEND / "lib" / "rbac.ts").read_text()
    assert 'prefix: "/portal"' not in rbac
    assert '"tenant"' not in rbac, "rbac.ts is staff-only since AA-885"
    auth = (FRONTEND / "lib" / "auth-server.ts").read_text()
    assert "function requireTenant" not in auth and "const tenantVerifyCache" not in auth


def test_admin_tenant_proxy_is_staff_only():
    route = (FRONTEND / "app" / "api" / "tenant" / "[...path]" / "route.ts").read_text()
    code = route[route.index("import "):]  # skip the header comment (it explains the removed branch)
    assert "requireTenant" not in code and "cis_tenant_token" not in code, "no tenant branch in the admin proxy"
    assert "requireAdmin(req, API_RULES.tenantProxy)" in route


def test_portal_proxy_is_tenant_only():
    gate = (PORTAL / "proxy.ts").read_text()
    assert "export async function proxy" in gate
    assert '"/portal/:path*"' in gate and '"/tenant-login"' in gate
    route = (PORTAL / "app" / "api" / "tenant" / "[...path]" / "route.ts").read_text()
    assert "requireTenant" in route and "proxyPath" in route


def test_portal_sets_security_headers():
    cfg = (PORTAL / "next.config.ts").read_text()
    for header in SECURITY_HEADERS:
        assert f'key: "{header}"' in cfg, f"portal/next.config.ts: missing {header}"
    assert "frame-ancestors 'none'" in cfg
    assert 'source: "/(.*)"' in cfg


def test_portal_lockfile_is_tracked():
    out = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "portal/package-lock.json"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert out.returncode == 0, "portal/package-lock.json must be tracked (Vercel + CI run npm ci in portal/)"


def test_portal_copy_is_frozen():
    """AA-979 — the portal moved to AA-Partner; this copy is frozen until it is deleted (P2b + 7 days)."""
    out = subprocess.run(
        ["git", "rev-parse", "HEAD:portal"], cwd=REPO, capture_output=True, text=True,
    )
    assert out.returncode == 0, f"git rev-parse HEAD:portal failed: {out.stderr.strip()}"
    assert out.stdout.strip() == FROZEN_PORTAL_TREE, (
        "portal/ changed. The tenant portal lives in AdventureAsia365-Ecosys/AA-Partner/portal since AA-979 "
        "(CRM-P2a); this copy is frozen and no longer deployed. Make the change there instead."
    )


def test_no_workflow_deploys_the_portal():
    """AA-979 — the portal deploys from AA-Partner only (one deployer for the Vercel project aa-cis-portal)."""
    workflows = REPO / ".github" / "workflows"
    assert not (workflows / "vercel-deploy-portal.yml").exists()
    assert not (workflows / "ui-smoke-portal.yml").exists()
    for wf in workflows.glob("*.y*ml"):
        text = wf.read_text()
        assert "VERCEL_PORTAL_PROJECT_ID" not in text, f"{wf.name} still deploys the portal project"
        assert "smoke-portal" not in text, f"{wf.name} still runs the portal smoke"
    admin = (workflows / "vercel-deploy.yml").read_text()
    assert '"portal/**"' not in admin
    deploy_dev = (workflows / "deploy-dev.yml").read_text()
    assert "portal/" not in deploy_dev, "a portal change must never redeploy ECS"
