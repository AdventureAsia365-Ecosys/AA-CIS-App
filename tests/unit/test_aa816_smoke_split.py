"""AA-816 S2 path: the UI smoke split stays in place.

Per-PR ``*.vercel.app`` previews are not Cognito callbacks and the legacy password login goes away at S2, so a PR
preview runs only the unauthenticated smoke (Playwright project ``smoke-public``). The signed-in smoke (project
``smoke``, specs tagged ``@staff``) runs as the Cognito E2E user, from ``main`` only, on the two callback hosts.
Static checks of the workflow and Playwright files; nothing runs.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
SMOKE_DIR = REPO / "tests" / "e2e" / "smoke"
PROD = "https://aa-cis.lumiguides.it.com"
PREVIEW_HOST = "https://aa-cis-preview.lumiguides.it.com"


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
    assert PROD in runs and PREVIEW_HOST in runs  # host allowlist step
    # Screenshots only: the HTML/JSON reports would carry the typed code (a `fill` step title).
    uploads = [s for s in staff["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert uploads and all("playwright-report" not in s["with"]["path"] for s in uploads)
    assert all(".auth" not in s["with"]["path"] for s in uploads)


def test_vercel_deploy_calls_the_staff_smoke_only_from_main_callback_hosts():
    jobs = _workflow("vercel-deploy.yml")["jobs"]
    pr = jobs["smoke"]
    assert "staff_session" not in pr.get("with", {})
    # GitHub validates nested-job permissions at startup even for skipped jobs: the caller must grant what
    # ui-smoke.yml's smoke-staff asks for, or the whole workflow fails to start (S247 startup_failure).
    staff_perms = _workflow("ui-smoke.yml")["jobs"]["smoke-staff"]["permissions"]
    for job in (pr, jobs["smoke-production"], jobs["smoke-preview-host"]):
        for scope, level in staff_perms.items():
            assert job["permissions"].get(scope) == level, (job.get("name"), scope)
    prod = jobs["smoke-production"]
    assert prod["with"] == {"base_url": PROD, "sha": "${{ needs.deploy.outputs.sha }}", "staff_session": True}
    assert "github.ref == 'refs/heads/main'" in prod["if"] and "'production'" in prod["if"]
    host = jobs["smoke-preview-host"]
    assert host["with"]["base_url"] == PREVIEW_HOST and host["with"]["staff_session"] is True
    assert "github.ref == 'refs/heads/main'" in host["if"] and "workflow_dispatch" in host["if"]


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
