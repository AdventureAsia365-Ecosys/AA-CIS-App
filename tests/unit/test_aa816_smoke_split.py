"""AA-816 S2 path: the UI smoke split stays in place.

The unauthenticated smoke (Playwright project ``smoke-public``) needs no session. The signed-in smoke (project
``smoke``, specs tagged ``@staff``) runs as the Cognito E2E user, from ``main`` only, on the callback host. Since the
AA-911 Vercel retirement (S272) both run after each verified ECS deploy of the CIS admin (deploy-admin-dev.yml); the
fixed Preview host and vercel-deploy.yml are gone. Static checks of the workflow and Playwright files; nothing runs.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
SMOKE_DIR = REPO / "tests" / "e2e" / "smoke"
PROD = "https://aa-cis.lumiguides.it.com"
RETIRED_PREVIEW_HOST = "aa-cis-preview.lumiguides.it.com"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _run_lines(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_public_smoke_job_has_no_session_and_no_password_secrets():
    jobs = _workflow("ui-smoke.yml")["jobs"]
    public = jobs["smoke"]
    assert "--project=smoke-public" in _run_lines(public)
    assert "--project=smoke " not in _run_lines(public) + " "
    assert "E2E_ADMIN_USERNAME" not in str(public) and "E2E_ADMIN_PASSWORD" not in str(public)
    assert "id-token" not in str(public.get("permissions", {}))


def test_staff_smoke_job_is_main_only_on_the_mailbox_environment():
    staff = _workflow("ui-smoke.yml")["jobs"]["smoke-staff"]
    assert "inputs.staff_session" in staff["if"]
    assert "github.ref == 'refs/heads/main'" in staff["if"]
    assert staff["environment"] == "e2e"
    assert staff["concurrency"] == {"group": "e2e-cognito", "cancel-in-progress": False}
    assert staff["permissions"]["id-token"] == "write"
    assert staff["env"]["E2ECOGNITO"] == "1"
    runs = _run_lines(staff)
    assert "--project=smoke " in runs
    assert PROD in runs and RETIRED_PREVIEW_HOST not in runs  # host allowlist step: production only
    # Screenshots only: the HTML/JSON reports would carry the typed code (a `fill` step title).
    uploads = [s for s in staff["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert uploads and all("playwright-report" not in s["with"]["path"] for s in uploads)
    assert all(".auth" not in s["with"]["path"] for s in uploads)


def test_admin_deploy_calls_the_staff_smoke_and_visual_after_a_verified_deploy_from_main():
    jobs = _workflow("deploy-admin-dev.yml")["jobs"]
    # GitHub validates nested-job permissions at startup even for skipped jobs: the caller must grant what
    # ui-smoke.yml's smoke-staff and visual.yml's jobs ask for, or the whole workflow fails to start (S247).
    staff_perms = _workflow("ui-smoke.yml")["jobs"]["smoke-staff"]["permissions"]
    visual_perms: dict = {}
    for job in _workflow("visual.yml")["jobs"].values():
        visual_perms.update(job.get("permissions") or {})
    prod = jobs["smoke-production"]
    for scope, level in staff_perms.items():
        assert prod["permissions"].get(scope) == level, scope
    assert prod["uses"] == "./.github/workflows/ui-smoke.yml" and prod["secrets"] == "inherit"
    assert prod["with"] == {"base_url": PROD, "sha": "${{ github.sha }}", "staff_session": True}
    assert prod["needs"] == "verify"
    assert "needs.verify.result == 'success'" in prod["if"] and "github.ref == 'refs/heads/main'" in prod["if"]
    visual = jobs["visual-production"]
    assert visual["uses"] == "./.github/workflows/visual.yml"
    assert visual["with"]["base_url"] == PROD
    for scope, level in visual_perms.items():
        if level == "write":
            assert visual["permissions"].get(scope) == "write", scope
    assert "needs.verify.result == 'success'" in visual["if"] and "github.ref == 'refs/heads/main'" in visual["if"]
    # The deploy verdict stays about the deploy: the smoke and the visual run are reported, not gated, by `result`.
    assert set(jobs["result"]["needs"]) == {"config", "deploy", "verify"}


def test_vercel_is_retired():
    """AA-911 (S272): no workflow deploys to Vercel or names the retired Preview host, and the Vercel config is gone."""
    assert not (WORKFLOWS / "vercel-deploy.yml").exists()
    assert not (REPO / "frontend" / "vercel.json").exists()
    for wf in WORKFLOWS.glob("*.y*ml"):
        text = wf.read_text()
        assert "VERCEL_TOKEN" not in text and "VERCEL_PROJECT_ID" not in text, wf.name
        assert "VERCEL_AUTOMATION_BYPASS_SECRET" not in text, wf.name
        assert RETIRED_PREVIEW_HOST not in text and "booking-preview.lumiguides.it.com" not in text, wf.name


def test_playwright_projects_split_on_the_staff_tag():
    cfg = (REPO / "playwright.config.ts").read_text()
    smoke = re.search(r"name: 'smoke',(.*?)\n    \}", cfg, re.S).group(1)
    public = re.search(r"name: 'smoke-public',(.*?)\n    \}", cfg, re.S).group(1)
    setup = re.search(r"name: 'smoke-setup',(.*?)\n    \}", cfg, re.S).group(1)
    assert "grep: /@staff/" in smoke and "dependencies: ['smoke-setup']" in smoke
    assert "grepInvert: /@staff/" in public and "dependencies" not in public
    assert "retries: 0" in setup and "screenshot: 'off'" in setup and "trace: 'off'" in setup


def test_every_smoke_spec_that_uses_the_session_is_tagged_staff():
    """A spec that loads the storage state without the tag would run on a PR preview with no session (red)."""
    missing = [p.name for p in sorted(SMOKE_DIR.glob("*.spec.ts"))
               if "ADMIN_STATE" in p.read_text() and "tag: '@staff'" not in p.read_text()]
    assert missing == []
