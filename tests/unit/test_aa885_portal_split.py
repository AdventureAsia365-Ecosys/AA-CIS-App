"""AA-885 — guards for the tenant portal split (portal/ = its own Next.js app and Vercel project).

- The UI-kit files the portal vendors stay byte-identical to their admin source (edit
  frontend/app/_kit, copy into portal/ in the same PR — the list is in CONTEXT.md).
- The portal app holds no admin secret and no staff session code.
- The admin app no longer serves the portal: the moved routes are gone, /portal and /tenant-login
  answer 308 to the portal host, the admin gate and rbac no longer mention the portal.
- The portal sends the security headers, its lockfile is tracked, and its Vercel workflow deploys
  another project on disjoint paths (and never redeploys ECS).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
PORTAL = REPO / "portal"
PORTAL_HOST = "https://portal.lumiguides.it.com"

# portal path → admin source path (both relative to the repo root). Keep in sync with CONTEXT.md.
VENDORED = {
    "portal/app/_kit/tokens.ts": "frontend/app/_kit/tokens.ts",
    "portal/app/_kit/primitives.tsx": "frontend/app/_kit/primitives.tsx",
    "portal/app/_kit/Modal.tsx": "frontend/app/_kit/Modal.tsx",
    "portal/app/_kit/Drawer.tsx": "frontend/app/_kit/Drawer.tsx",
    "portal/app/_kit/Toast.tsx": "frontend/app/_kit/Toast.tsx",
    "portal/app/_kit/api.ts": "frontend/app/_kit/api.ts",
    "portal/app/_kit/useMediaQuery.ts": "frontend/app/_kit/useMediaQuery.ts",
    "portal/app/_kit/Providers.tsx": "frontend/app/_kit/Providers.tsx",
    "portal/app/_kit/DataTable.tsx": "frontend/app/_kit/DataTable.tsx",
    "portal/app/_kit/csv.ts": "frontend/app/_kit/csv.ts",
    "portal/app/_kit/savedViews.ts": "frontend/app/_kit/savedViews.ts",
    # AA-759 — theme + sidebar-collapse stores for the PortalShell.
    "portal/app/_kit/theme.ts": "frontend/app/_kit/theme.ts",
    "portal/app/_kit/useTheme.ts": "frontend/app/_kit/useTheme.ts",
    "portal/app/_kit/sidebarStore.ts": "frontend/app/_kit/sidebarStore.ts",
    "portal/app/_kit/useSidebar.ts": "frontend/app/_kit/useSidebar.ts",
    "portal/app/_kit/useHydrated.ts": "frontend/app/_kit/useHydrated.ts",
    "portal/app/_brand/tokens.ts": "frontend/app/_brand/tokens.ts",
    "portal/lib/bffPath.ts": "frontend/lib/bffPath.ts",
    "portal/lib/proxyResponse.ts": "frontend/lib/proxyResponse.ts",
    "portal/lib/sameOrigin.ts": "frontend/lib/sameOrigin.ts",  # AA-911
    "portal/lib/crossSiteGuard.ts": "frontend/lib/crossSiteGuard.ts",  # AA-911
}

# Portal-owned files in the vendored folders (not copies).
PORTAL_OWNED_IN_KIT = {"index.ts"}

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


@pytest.mark.parametrize("portal_path,source_path", sorted(VENDORED.items()))
def test_vendored_file_is_byte_identical(portal_path, source_path):
    copy, source = REPO / portal_path, REPO / source_path
    assert source.is_file(), f"admin source {source_path} is gone — update VENDORED and CONTEXT.md"
    assert copy.is_file(), f"{portal_path} missing — copy it from {source_path}"
    assert copy.read_bytes() == source.read_bytes(), (
        f"{portal_path} drifted from {source_path}. The portal vendors the admin UI kit byte for byte "
        f"(AA-885): copy the admin file over the portal one in the same PR (cp {source_path} {portal_path})."
    )


def test_every_portal_kit_file_is_vendored_or_portal_owned():
    """A new file in portal/app/_kit must be either a guarded copy or explicitly portal-owned."""
    for p in (PORTAL / "app" / "_kit").iterdir():
        rel = str(p.relative_to(REPO))
        assert rel in VENDORED or p.name in PORTAL_OWNED_IN_KIT, f"{rel} is neither vendored nor portal-owned"


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


def test_portal_workflow_deploys_its_own_project_on_disjoint_paths():
    admin = (REPO / ".github" / "workflows" / "vercel-deploy.yml").read_text()
    portal = (REPO / ".github" / "workflows" / "vercel-deploy-portal.yml").read_text()
    admin_id = re.search(r"VERCEL_PROJECT_ID:\s*(\S+)", admin).group(1)
    portal_id = re.search(r"VERCEL_PROJECT_ID:\s*(.+?)\s*(#|$)", portal, re.M).group(1)
    assert portal_id != admin_id
    assert "vars.VERCEL_PORTAL_PROJECT_ID" in portal_id
    assert '"portal/**"' in portal and '"frontend/**"' not in portal
    assert '"portal/**"' not in admin
    assert "./.github/workflows/ui-smoke-portal.yml" in portal
    deploy_dev = (REPO / ".github" / "workflows" / "deploy-dev.yml").read_text()
    assert "portal/" not in deploy_dev, "a portal change must never redeploy ECS"


def test_context_lists_the_vendored_files():
    ctx = (REPO / "CONTEXT.md").read_text()
    for portal_path in VENDORED:
        assert portal_path in ctx, f"CONTEXT.md must list the vendored file {portal_path}"
