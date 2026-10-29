"""AA-885 — the tenant portal split off the admin app; AA-979 — the portal moved to AdventureAsia365-Ecosys/AA-Partner
(portal/, CRM-P2a) and, at the P2b clean-up (runbook §4.5), the frozen copy left here was deleted.

- portal/ is gone from this repo, with every CI job, pre-push check, CODEOWNERS and .gitignore line that served it.
  The portal's own guards (no admin secret, security headers, slug redirects, legal pages, BFF cross-site) live in
  AA-Partner api/tests/unit/test_portal_guards.py.
- No workflow of this repo deploys the portal or runs its smoke.
- The admin app does not serve the portal: the moved routes are gone, /portal and /tenant-login answer 308 to the
  portal host, the admin gate and rbac no longer mention the portal.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
PORTAL_HOST = "https://portal.lumiguides.it.com"


def test_frozen_portal_copy_is_deleted():
    """AA-979 §4.5 — the frozen copy is gone; portal changes are made in AA-Partner portal/ only."""
    assert not (REPO / "portal").exists(), "portal/ is back: the tenant portal lives in AA-Partner portal/ (AA-979)"
    out = subprocess.run(["git", "ls-files", "portal"], cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "", "git still tracks files under portal/"


def test_nothing_builds_or_checks_the_deleted_portal():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "portal-build:" not in ci and "portal-lint:" not in ci and "Portal Build" not in ci
    assert "working-directory: portal" not in ci and "portal/package-lock.json" not in ci
    prepush = (REPO / "scripts" / "pre-push.sh").read_text()
    assert "portal_tsc" not in prepush and "portal_lint" not in prepush and "portal/*" not in prepush
    assert "/portal/" not in (REPO / ".gitignore").read_text()
    assert not re.search(r"^/portal/", (REPO / ".github" / "CODEOWNERS").read_text(), re.M)


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


def test_no_workflow_deploys_the_portal():
    """AA-979 — the portal deploys from AA-Partner only (Fargate since P2b; the Vercel project was deleted at §4.5)."""
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
