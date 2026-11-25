"""S292 (follow-up of the S291 hotfix, release safety) — the admin route check (.github/workflows/admin-routes.yml).

Every CIS admin page the administrator may open is rendered as the administrator on a throwaway `next start` whose API
is a stub, with no real credential. These tests pin what makes it a safe REQUIRED check and keep the stub out of the
product:

- triggers: every pull request (no ``paths:``), the merge queue (merge_group) and dispatch; the job name never changes
  (branch protection pins it); every step after Scope is scoped (a step-level skip keeps the job green on a PR without
  a frontend change; a job-level skip would report "skipped");
- the stub and the stand-in session live only in test code: nothing under frontend/ (the runtime bundle) names them,
  and the build that talks to the stub sets its API URLs in this workflow only;
- the Playwright project exists and the legacy "chromium" project ignores the check's folder.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WF = REPO / ".github" / "workflows" / "admin-routes.yml"
CHECK = REPO / "tests" / "e2e" / "admin-routes"
JOB_NAME = "Admin routes (next start, stub API, administrator)"
SCOPED = "steps.scope.outputs.run == 'true'"


def _wf() -> dict:
    wf = yaml.safe_load(WF.read_text())
    wf["on"] = wf.pop(True)  # YAML 1.1 reads the bare key `on` as a boolean
    return wf


def _steps() -> list[dict]:
    return _wf()["jobs"]["check"]["steps"]


def _step(name: str) -> dict:
    return next(s for s in _steps() if s.get("name") == name)


def test_runs_on_every_pr_the_merge_queue_and_dispatch():
    on = _wf()["on"]
    assert on["pull_request"] == {"branches": ["main"]}, "no paths filter: a required check reports on every PR"
    assert on["merge_group"] == {"types": ["checks_requested"]}
    assert "workflow_dispatch" in on


def test_job_keeps_its_required_name_and_has_no_job_level_skip():
    job = _wf()["jobs"]["check"]
    assert job["name"] == JOB_NAME
    assert "if" not in job and "needs" not in job, "a job-level skip or dependency reports 'skipped' (= passed)"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] <= 15
    assert _wf()["permissions"] == {"contents": "read"}


def test_concurrency_gives_each_queue_group_its_own_group():
    group = _wf()["concurrency"]["group"]
    assert group.startswith("admin-routes-${{ github.event_name == 'pull_request' && github.event.pull_request.number ||")
    assert group.rstrip().endswith("|| github.run_id }}")


def test_every_step_after_scope_is_scoped():
    steps = _steps()
    assert "actions/checkout" in steps[0]["uses"] and steps[0]["with"]["fetch-depth"] == 0
    assert steps[1]["name"] == "Scope" and steps[1]["id"] == "scope" and "if" not in steps[1]
    for s in steps[2:]:
        assert SCOPED in s.get("if", ""), f"{s.get('name')} must be guarded by the Scope step"
    for name in ("Summary", "Upload failure screenshots and per-page results"):
        assert _step(name)["if"].startswith("always() && "), name


def test_scope_matches_the_frontend_and_the_checks_own_files():
    run = _step("Scope")["run"]
    for path in ("frontend/", "tests/e2e/admin-routes/", r"tests/e2e/visual/pages\.ts$", r"playwright\.config\.ts$",
                 r"\.github/workflows/admin-routes\.yml$"):
        assert path in run, path
    assert "merge_group) BASE=$GROUP_BASE" in run


def test_the_stub_build_is_configured_here_only():
    build = _step("Build the frontend against the stub and start it")
    env = build["env"]
    assert env["NEXT_PUBLIC_API_URL"] == env["API_URL"] == "http://127.0.0.1:${{ env.STUB_PORT }}"
    assert "next start -p 3001" in build["run"]
    assert "stub-api.mjs" in _step("Start the stub API")["run"]
    run = _step("Render every admin page as the administrator")["run"]
    assert "--project=admin-routes" in run


def test_no_stub_or_stand_in_session_in_the_runtime_code():
    """The mock never ships: nothing the app builds (frontend/ minus tests) names the stub or the stand-in session."""
    needles = ("admin-route-check", "admin_route_check_stub", "stub-api", "tests/e2e/admin-routes")
    hits = []
    for p in (REPO / "frontend").rglob("*"):
        if "node_modules" in p.parts or ".next" in p.parts or not p.is_file():
            continue
        if p.suffix not in {".ts", ".tsx", ".js", ".mjs", ".json"} or p.name.endswith(".test.mjs"):
            continue
        text = p.read_text(errors="ignore")
        hits += [f"{p.relative_to(REPO)}: {n}" for n in needles if n in text]
    assert hits == []


def test_playwright_project_and_the_legacy_project_ignores_the_folder():
    cfg = (REPO / "playwright.config.ts").read_text()
    assert "name: 'admin-routes'" in cfg
    assert re.search(r"testMatch: /admin-routes\\/\.\*\\\.spec\\\.ts\$/", cfg)
    ignore = re.search(r"name: 'chromium',\s*testIgnore: (/.*?/),", cfg)
    assert ignore and "admin-routes" in ignore.group(1)


def test_the_check_files_exist_and_the_self_test_plants_the_s291_error():
    for f in ("admin-routes.spec.ts", "routes.ts", "stub-api.mjs", "summary.mjs"):
        assert (CHECK / f).is_file(), f
    spec = (CHECK / "admin-routes.spec.ts").read_text()
    assert "useToast must be used within a <ToastProvider>" in spec
    assert "self-test" in spec
