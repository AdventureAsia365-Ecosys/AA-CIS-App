"""AA-1008 H3 — the per-PR visual job and the "Kit consumers" aggregator are safe to be REQUIRED checks.

A required check must report on every pull request and on every merge-queue group (merge_group), under a name that
never changes (the tech lead's branch-protection script and verify-h3.sh pin the names). So:

- ``visual.yml``: no ``paths:`` on pull_request, a merge_group trigger, the job ``pr`` keeps its name, is skipped on
  pull_request (S271: reported "skipped" = passed) and runs on merge_group; its Scope step (run here with bash in a scratch git repo) matches the old path list exactly and, on no
  match, skips every later step so the job ends green. The skip is step-level: a job-level skip reports "skipped",
  which GitHub counts as passed even when the job it depended on failed.
- ``kit-consumers.yml``: the same triggers, BASE_SHA from the queue group on merge_group, a concurrency group that
  never makes two queue groups cancel each other; the aggregator's verdict is covered by
  test_aa994_kit_sync_bot.py::test_result_job_* and by the extra cases below.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
VISUAL_WF = REPO / ".github" / "workflows" / "visual.yml"
CONSUMERS_WF = REPO / ".github" / "workflows" / "kit-consumers.yml"
VISUAL_NAME = "Visual (next start, public pages + kit gallery)"
SCOPED = "steps.scope.outputs.run == 'true'"

GIT_CFG = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
           "-c", "init.defaultBranch=main"]


def _wf(path: Path) -> dict:
    wf = yaml.safe_load(path.read_text())
    wf["on"] = wf.pop(True)  # YAML 1.1 reads the bare key `on` as a boolean
    return wf


def _step(steps: list[dict], name: str) -> dict:
    return next(s for s in steps if s.get("name") == name)


# --- visual.yml: triggers, names, conditions --------------------------------------------------------------------------

def test_visual_runs_on_every_pr_and_on_the_merge_queue():
    on = _wf(VISUAL_WF)["on"]
    assert "paths" not in (on["pull_request"] or {}) and "paths-ignore" not in (on["pull_request"] or {})
    assert on["pull_request"]["branches"] == ["main"]
    assert on["merge_group"] == {"types": ["checks_requested"]}


def test_visual_pr_job_keeps_its_required_name_and_runs_on_merge_group():
    jobs = _wf(VISUAL_WF)["jobs"]
    pr = jobs["pr"]
    assert pr["name"] == VISUAL_NAME
    cond = " ".join(pr["if"].split())
    # S271 (CI minutes): skipped on pull_request (reports "skipped" = passed, so the PR can enter the queue); the queue
    # group runs it, so every frontend change is still checked before main.
    assert "'pull_request'" not in cond
    assert "github.event_name == 'merge_group'" in cond
    assert "needs" not in pr, "a job-level dependency would turn a failure into a passing 'skipped'"
    deployed = " ".join(jobs["deployed"]["if"].split())
    assert "github.event_name != 'merge_group'" in deployed and "github.event_name != 'pull_request'" in deployed


def test_visual_concurrency_gives_each_queue_group_its_own_group():
    group = _wf(VISUAL_WF)["concurrency"]["group"]
    # A PR keys on its number; every other event (merge_group has no number and no base_url) on its run_id.
    assert group.startswith("visual-${{ github.event_name == 'pull_request' && github.event.pull_request.number ||")
    assert group.rstrip().endswith("|| github.run_id }}")


def test_every_step_after_scope_is_scoped():
    steps = _wf(VISUAL_WF)["jobs"]["pr"]["steps"]
    assert "actions/checkout" in steps[0]["uses"] and steps[0]["with"]["fetch-depth"] == 0
    assert steps[1]["name"] == "Scope" and steps[1]["id"] == "scope" and "if" not in steps[1]
    for s in steps[2:]:
        assert SCOPED in s.get("if", ""), f"{s.get('name')} must be guarded by the Scope step"
    for name in ("Summary", "Upload screenshots and report"):
        assert _step(steps, name)["if"].startswith("always() && "), name
    # The PR comment went with the per-PR run (S271): nothing in the job writes to a PR any more.
    assert not any("comment" in (s.get("name") or "").lower() for s in steps)
    assert _wf(VISUAL_WF)["permissions"] == {"contents": "read"}


def test_changed_pages_diffs_a_queue_group_against_its_base():
    step = _step(_wf(VISUAL_WF)["jobs"]["pr"]["steps"], "Changed pages")
    assert step["env"]["MERGE_BASE_SHA"] == "${{ github.event.merge_group.base_sha }}"
    assert 'changed-pages.mjs "$MERGE_BASE_SHA" HEAD' in step["run"]
    assert '"$EVENT" = "merge_group"' in step["run"]


# --- visual.yml: the Scope step, run with bash ------------------------------------------------------------------------

needs_git = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("git")), reason="needs bash and git")


def _g(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_CFG, "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _scratch_repo(tmp_path: Path, change: dict[str, str | None], rename: tuple[str, str] | None = None) -> tuple[Path, str]:
    """main with a few files; HEAD = main + ``change`` (path -> text, None = delete) and an optional rename."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "-q")
    for f in ("frontend/app/page.tsx", "docs/readme.md", "api/main.py", "tests/e2e/smoke/smoke-helpers.ts"):
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text(f"// {f}\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "base")
    base = _g(repo, "rev-parse", "HEAD")
    _g(repo, "update-ref", "refs/remotes/origin/main", base)
    _g(repo, "checkout", "-qb", "feature")
    for f, text in change.items():
        if text is None:
            (repo / f).unlink()
        else:
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            (repo / f).write_text(text)
    if rename:
        (repo / rename[1]).parent.mkdir(parents=True, exist_ok=True)
        _g(repo, "mv", rename[0], rename[1])
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "change", "--allow-empty")
    return repo, base


def _run_scope(repo: Path, tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, str, str]:
    script = _step(_wf(VISUAL_WF)["jobs"]["pr"]["steps"], "Scope")["run"]
    out, summary = tmp_path / "out.txt", tmp_path / "summary.md"
    full = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GITHUB_OUTPUT": str(out),
            "GITHUB_STEP_SUMMARY": str(summary), "EVENT": "", "BASE_REF": "", "GROUP_BASE": "", **env}
    # GitHub's default `run` shell is bash -e -o pipefail.
    res = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=repo, env=full,
                         capture_output=True, text=True)
    return res, out.read_text() if out.exists() else "", summary.read_text() if summary.exists() else ""


# The old pull_request `paths:` list: frontend/**, tests/e2e/visual/**, tests/e2e/smoke/smoke-helpers.ts,
# playwright.config.ts, .github/workflows/visual.yml.
SCOPE_CASES = [
    ({"frontend/app/page.tsx": "changed\n"}, True),
    ({"frontend/package-lock.json": "{}\n"}, True),
    ({"tests/e2e/visual/pages.ts": "x\n"}, True),
    ({"tests/e2e/smoke/smoke-helpers.ts": "changed\n"}, True),
    ({"playwright.config.ts": "x\n"}, True),
    ({".github/workflows/visual.yml": "x\n"}, True),
    ({"frontend/app/page.tsx": None}, True),  # a deleted page is a frontend change
    ({"docs/readme.md": "changed\n"}, False),
    ({"api/main.py": "changed\n", "docs/new.md": "x\n"}, False),
    ({"tests/e2e/smoke/admin.spec.ts": "x\n"}, False),
    ({"tests/e2e/visualx/a.ts": "x\n"}, False),
    ({"frontendx/a.ts": "x\n"}, False),
    ({"tools/playwright.config.ts": "x\n"}, False),
    ({".github/workflows/ci.yml": "x\n"}, False),
    ({".github/workflows/visual.yml.bak": "x\n"}, False),
    ({"playwright.config.ts.orig": "x\n"}, False),
]


@needs_git
@pytest.mark.parametrize("change,want", SCOPE_CASES)
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_scope_matches_the_old_path_filter(tmp_path, change, want, event):
    repo, base = _scratch_repo(tmp_path, change)
    env = {"EVENT": event, "BASE_REF": "main"} if event == "pull_request" else {"EVENT": event, "GROUP_BASE": base}
    res, out, summary = _run_scope(repo, tmp_path, **env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert out.strip() == f"run={'true' if want else 'false'}"
    assert ("no frontend change" in summary) is (not want)


@needs_git
def test_scope_counts_a_rename_out_of_the_frontend(tmp_path):
    repo, _ = _scratch_repo(tmp_path, {}, rename=("frontend/app/page.tsx", "docs/page.tsx"))
    res, out, _ = _run_scope(repo, tmp_path, EVENT="pull_request", BASE_REF="main")
    assert res.returncode == 0 and out.strip() == "run=true"


@needs_git
@pytest.mark.parametrize("env", [
    {"EVENT": "workflow_dispatch"},
    {"EVENT": "merge_group", "GROUP_BASE": ""},  # never "...HEAD" (an empty diff would skip silently)
    {"EVENT": "merge_group", "GROUP_BASE": "0" * 40},
    {"EVENT": "pull_request", "BASE_REF": ""},
    {"EVENT": "pull_request", "BASE_REF": "no-such-branch"},
])
def test_scope_runs_everything_when_it_cannot_decide(tmp_path, env):
    repo, _ = _scratch_repo(tmp_path, {"docs/readme.md": "changed\n"})
    res, out, summary = _run_scope(repo, tmp_path, **env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert out.strip() == "run=true"
    assert "no frontend change" not in summary
    if env["EVENT"] != "workflow_dispatch":
        assert "::warning::" in res.stdout


# --- kit-consumers.yml ------------------------------------------------------------------------------------------------

def test_kit_consumers_runs_on_every_pr_and_on_the_merge_queue():
    wf = _wf(CONSUMERS_WF)
    assert set(wf["on"]) == {"pull_request", "merge_group"}
    assert not wf["on"]["pull_request"], "no paths: filter (the plan job decides)"
    assert wf["on"]["merge_group"] == {"types": ["checks_requested"]}
    assert wf["concurrency"]["group"] == "kit-consumers-${{ github.event.pull_request.number || github.run_id }}"
    assert "Non-required" not in CONSUMERS_WF.read_text()


def test_kit_consumers_plan_diffs_a_queue_group_against_its_base():
    jobs = _wf(CONSUMERS_WF)["jobs"]
    plan = _step(jobs["plan"]["steps"], "Did the kit core (or the sync code) change?")
    assert plan["env"]["BASE_SHA"] == "${{ github.event.merge_group.base_sha || github.event.pull_request.base.sha }}"
    assert '--head HEAD' in plan["run"]
    # The leg checks out the event's own commit (the PR merge commit, or the queue commit), never a fixed ref.
    checkout = _step(jobs["consumer"]["steps"], "Check out this PR")
    assert "ref" not in checkout["with"] and checkout["with"]["path"] == "cis"


def test_kit_consumers_aggregator_keeps_its_required_name():
    jobs = _wf(CONSUMERS_WF)["jobs"]
    agg = jobs["kit-consumers"]
    assert agg["name"] == "Kit consumers"
    assert agg["if"] == "always()" and agg["needs"] == ["plan", "consumer"]
    assert "if" not in jobs["plan"]


@pytest.mark.parametrize("plan,changed,legs,want", [
    ("success", "false", "skipped", 0),
    ("success", "true", "success", 0),
    ("success", "true", "failure", 1),
    ("success", "true", "cancelled", 1),
    ("success", "true", "skipped", 1),
    ("failure", "", "skipped", 1),
    ("cancelled", "", "skipped", 1),
    ("skipped", "", "skipped", 1),
    ("success", "false", "failure", 1),
    ("success", "", "skipped", 0),  # an empty output reads as unchanged only when the plan job succeeded
])
def test_kit_consumers_aggregator_verdict(tmp_path, plan, changed, legs, want):
    step = _wf(CONSUMERS_WF)["jobs"]["kit-consumers"]["steps"][0]
    env = {"PATH": os.environ["PATH"], "PLAN": plan, "CHANGED": changed, "LEGS": legs,
           "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
    res = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert res.returncode == want, res.stdout + res.stderr
