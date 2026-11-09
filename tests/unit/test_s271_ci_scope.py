"""S271 (CI minutes, AA-889 merge queue): the pull-request scope of ci.yml and the events each job runs on.

- .github/scripts/ci_scope.py: every output is true for any event but pull_request, when the diff cannot be read or is
  empty, and when the PR changes .github/; `tests` is false only for a docs-only PR; an area is true when a changed
  path is one of its files or under one of its folders. Run for real (git scratch repo) once, as the workflow runs it.
- .github/workflows/ci.yml: no `push` run (the merge queue already tested the exact tree that lands on main), and the
  job conditions, evaluated for each event: merge_group and workflow_dispatch run every job; a pull request runs the
  fast jobs and the heavy ones only in their area; a failed scope (empty outputs) runs every gated job (fail closed);
  the required checks never wait on a job that cannot report.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "ci_scope.py"
CI = REPO / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("ci_scope", SCRIPT)
ci_scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_scope)

AREAS = {"integration": ["api/migrations/", "tests/integration/"],
         "docker": ["Dockerfile", ".dockerignore", "requirements.txt", ".trivyignore", ".url-wheels/"],
         "frontend": ["frontend/"]}
REQUIRED = ["Lint", "Security Audit", "Unit Tests", "Integration Tests", "Docker Build Check", "Frontend Build",
            "Frontend Lint"]
HEAVY = ["test-integration-shard", "test-integration", "docker-build", "frontend-build"]


# --- ci_scope.py ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("event", ["merge_group", "workflow_dispatch", "push", ""])
def test_every_output_is_true_outside_pull_requests(event):
    out, _ = ci_scope.decide(event, ["docs/a.md"], AREAS)
    assert out == {"tests": True, "integration": True, "docker": True, "frontend": True}


@pytest.mark.parametrize("files", [None, [], ["docs/a.md", ".github/workflows/ci.yml"], [".github/scripts/x.py"]])
def test_a_pull_request_runs_everything_when_unsure_or_ci_changes(files):
    out, _ = ci_scope.decide("pull_request", files, AREAS)
    assert all(out.values()), out


@pytest.mark.parametrize("files,want", [
    (["docs/guide.md"], {"tests": False, "integration": False, "docker": False, "frontend": False}),
    (["docs/api/openapi.json", "README.md", "frontend/README.md"],
     {"tests": False, "integration": False, "docker": False, "frontend": False}),
    (["services/x.py"], {"tests": True, "integration": False, "docker": False, "frontend": False}),
    (["api/migrations/400_x.sql"], {"tests": True, "integration": True, "docker": False, "frontend": False}),
    (["tests/integration/test_x.py", "docs/a.md"], {"tests": True, "integration": True, "docker": False,
                                                     "frontend": False}),
    (["requirements.txt"], {"tests": True, "integration": False, "docker": True, "frontend": False}),
    (["frontend/app/page.tsx"], {"tests": True, "integration": False, "docker": False, "frontend": True}),
    # A file prefix matches only itself; a folder prefix needs its slash.
    (["requirements-dev.txt", "Dockerfile.old", "frontendx/a.ts", "api/migrations_tool.py"],
     {"tests": True, "integration": False, "docker": False, "frontend": False}),
])
def test_pull_request_outputs(files, want):
    out, _ = ci_scope.decide("pull_request", files, AREAS)
    assert out == want


@pytest.mark.parametrize("spec", ["frontend", "=x/", "tests=docs/", "bad name=x/", "frontend="])
def test_area_spec_refusals(spec):
    with pytest.raises(ci_scope.argparse.ArgumentTypeError, match="--area must be"):
        ci_scope.parse_area(spec)


GIT_CFG = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
           "-c", "init.defaultBranch=main"]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
@pytest.mark.parametrize("change,want_tests,want_frontend", [
    ({"docs/new.md": "x\n"}, "false", "false"),
    ({"frontend/app/page.tsx": "y\n"}, "true", "true"),
])
def test_the_cli_writes_github_output_from_the_merge_commit_diff(tmp_path, change, want_tests, want_frontend):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *GIT_CFG, "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    (repo / "frontend" / "app").mkdir(parents=True)
    (repo / "frontend" / "app" / "page.tsx").write_text("x\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    for name, text in change.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git("add", "-A")
    git("commit", "-qm", "change")  # HEAD^1 = the base, as the first parent of GitHub's merge commit
    out = tmp_path / "out.txt"
    env = {**os.environ, "GITHUB_OUTPUT": str(out)}
    res = subprocess.run([sys.executable, str(SCRIPT), "--event", "pull_request", "--area", "frontend=frontend/"],
                         cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert out.read_text().splitlines() == [f"tests={want_tests}", f"frontend={want_frontend}"]


def test_the_cli_runs_everything_when_git_fails(tmp_path):
    out = tmp_path / "out.txt"
    env = {**os.environ, "GITHUB_OUTPUT": str(out)}
    res = subprocess.run([sys.executable, str(SCRIPT), "--event", "pull_request", "--base", "no-such-rev",
                          "--area", "frontend=frontend/"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert "::warning::" in res.stdout
    assert out.read_text().splitlines() == ["tests=true", "frontend=true"]


# --- ci.yml: triggers and job conditions ----------------------------------------------------------------------------

def _ci() -> dict:
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    wf["on"] = wf.pop(True)
    return wf


def _eval(cond: str | None, event: str, outputs: dict[str, str]) -> bool:
    """Evaluate the small expression subset ci.yml uses (==, !=, &&, ||, !cancelled(), parentheses)."""
    if cond is None:
        return True
    expr = cond.strip()
    if expr.startswith("${{"):
        expr = expr[3:-2]
    expr = expr.replace("!cancelled()", "True").replace("&&", " and ").replace("||", " or ")
    expr = expr.replace("github.event_name", repr(event))
    expr = re.sub(r"needs\.quality-paths\.outputs\.(\w+)", lambda m: repr(outputs.get(m.group(1), "")), expr)
    assert re.fullmatch(r"[\sA-Za-z_'()!=]*", expr.replace("True", "")), expr
    return bool(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - literals and operators only (asserted above)


def _runs(event: str, outputs: dict[str, str]) -> dict[str, bool]:
    """Which jobs run (a job with `needs` runs only when its condition holds; `!cancelled()` covers failed needs)."""
    return {jid: _eval(job.get("if"), event, outputs) for jid, job in _ci()["jobs"].items()}


def _scope_outputs(event: str, files: list[str] | None) -> dict[str, str]:
    out, _ = ci_scope.decide(event, files, AREAS)
    return {k: "true" if v else "false" for k, v in out.items()}


def test_no_push_run_the_merge_queue_already_tested_main():
    on = _ci()["on"]
    assert set(on) == {"pull_request", "merge_group", "workflow_dispatch"}
    assert on["merge_group"] == {"types": ["checks_requested"]}
    assert "paths" not in on["pull_request"] and "paths-ignore" not in on["pull_request"]


@pytest.mark.parametrize("event", ["merge_group", "workflow_dispatch"])
def test_the_queue_and_a_dispatch_run_every_job(event):
    runs = _runs(event, _scope_outputs(event, None))
    expected_off = {"migration-preflight"} if event == "workflow_dispatch" else set()
    assert {j for j, r in runs.items() if not r} == expected_off


def test_a_failed_scope_runs_every_gated_job_on_a_pull_request():
    runs = _runs("pull_request", {})  # empty outputs: the scope step or job failed
    assert {j for j, r in runs.items() if not r} == {"frontend-build"}


def test_a_backend_pull_request_runs_the_fast_jobs_only():
    runs = _runs("pull_request", _scope_outputs("pull_request", ["services/x.py"]))
    assert {j for j, r in runs.items() if not r} == {"test-integration-shard", "test-integration", "docker-build",
                                                      "frontend-lint", "frontend-build"}


def test_a_migration_pull_request_also_runs_the_integration_tests():
    runs = _runs("pull_request", _scope_outputs("pull_request", ["api/migrations/400_x.sql"]))
    assert runs["test-integration-shard"] and runs["test-integration"]
    assert not runs["docker-build"] and not runs["frontend-build"]


def test_a_frontend_pull_request_runs_frontend_lint_but_not_the_build():
    runs = _runs("pull_request", _scope_outputs("pull_request", ["frontend/app/page.tsx"]))
    assert runs["frontend-lint"] and not runs["frontend-build"]


def test_a_docs_only_pull_request_runs_no_test_job():
    runs = _runs("pull_request", _scope_outputs("pull_request", ["docs/a.md"]))
    assert {j for j, r in runs.items() if r} == {"lint", "quality-paths", "security-audit", "migration-preflight"}


def test_every_gate_is_fail_closed_and_names_a_declared_output():
    jobs = _ci()["jobs"]
    qp = jobs["quality-paths"]
    scope = next(s for s in qp["steps"] if s.get("id") == "scope")
    assert scope["continue-on-error"] is True
    assert "ci_scope.py" in scope["run"]
    for name, prefixes in AREAS.items():
        assert f"--area {name}={','.join(prefixes)}" in scope["run"], name
    declared = set(qp["outputs"])
    assert declared == {"tests", *AREAS}
    for jid, job in jobs.items():
        cond = job.get("if") or ""
        for out in re.findall(r"needs\.quality-paths\.outputs\.(\w+)", cond):
            assert out in declared, (jid, out)
            assert f"needs.quality-paths.outputs.{out} != 'false'" in cond, (jid, "must test != 'false'")
            assert "quality-paths" in (job["needs"] if isinstance(job["needs"], list) else [job["needs"]])
            assert cond.startswith("${{ !cancelled() && "), jid


def test_required_checks_keep_their_job_names():
    names = {job["name"] for job in _ci()["jobs"].values()}
    assert set(REQUIRED) <= names


def test_heavy_jobs_never_run_on_a_plain_pull_request_and_always_on_the_queue():
    for files in (["services/x.py"], ["frontend/app/page.tsx"], ["docs/a.md"]):
        runs = _runs("pull_request", _scope_outputs("pull_request", files))
        assert not any(runs[j] for j in HEAVY), files
    runs = _runs("merge_group", _scope_outputs("merge_group", None))
    assert all(runs[j] for j in HEAVY)


def test_frontend_lint_runs_the_fast_part_of_frontend_build_on_a_pull_request():
    jobs = _ci()["jobs"]
    lint_steps = {s.get("name"): s for s in jobs["frontend-lint"]["steps"]}
    build_runs = "\n".join(s.get("run", "") for s in jobs["frontend-build"]["steps"])
    kit = lint_steps["Kit core self-check and kit render tests (pull request)"]
    tsc = lint_steps["Type-check (tsc --noEmit, pull request)"]
    for step in (kit, tsc):
        assert "github.event_name == 'pull_request'" in step["if"]
    for line in (kit["run"] + "\n" + tsc["run"]).splitlines():
        if line.strip():
            assert line.strip() in build_runs, f"{line.strip()!r} is not a Frontend Build command"
