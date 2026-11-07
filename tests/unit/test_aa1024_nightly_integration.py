"""AA-1024 l22 (S271 lessons guard G8): the nightly integration run in a random module order.

- ``ci_shards.py files --shuffle SEED`` prints the same files as without it, in an order fixed by SEED.
- .github/workflows/nightly-integration.yml runs the whole suite (minus test_007, as CI) in one job on CI's Postgres,
  on a schedule and by hand with a seed, never on a pull request, and prints the seed.
- Its job "report" keeps one open issue labelled nightly-red while the nightly is red and closes it on a green run
  (the run script is executed here with a fake gh).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SHARDS = REPO / ".github" / "scripts" / "ci_shards.py"
NIGHTLY = REPO / ".github" / "workflows" / "nightly-integration.yml"


def _files(cwd: Path, *extra: str) -> list[str]:
    out = subprocess.run([sys.executable, str(SHARDS), "files", "--shards", "1", "--index", "1", *extra, "suite"],
                         cwd=cwd, capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_shuffle_keeps_the_files_and_is_fixed_by_the_seed(tmp_path: Path):
    (tmp_path / "suite").mkdir()
    for i in range(12):
        (tmp_path / "suite" / f"test_{i:02d}.py").write_text("def test_x():\n    pass\n")
    plain = _files(tmp_path)
    a, b, c = (_files(tmp_path, "--shuffle", s) for s in ("7", "7", "8"))
    assert sorted(a) == plain and a == b
    assert a != plain or c != plain       # 12 files: a seed that keeps path order twice is not a shuffle
    assert a != c


def _job() -> dict:
    wf = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    triggers = wf.get("on", wf.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}, "nightly only: never a PR check"
    assert "seed" in triggers["workflow_dispatch"]["inputs"]
    assert wf["permissions"] == {"contents": "read"}
    return wf["jobs"]["random-order"]


def test_nightly_runs_the_whole_suite_in_one_job_on_ci_postgres():
    job = _job()
    ci = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    shard = ci["jobs"]["test-integration-shard"]
    assert job["services"]["postgres"]["image"] == shard["services"]["postgres"]["image"]
    run = next(s for s in job["steps"] if "pytest" in s.get("run", ""))
    assert "--shards 1 --index 1 --shuffle" in run["run"]
    assert "--exclude tests/integration/test_007_rls_isolation.py" in run["run"]
    assert "GITHUB_STEP_SUMMARY" in run["run"] and "seed" in run["run"]
    # Same environment as a CI shard: the dummy values come by reference from the job env (no credential-shaped
    # literal on a new line, which the PR gitleaks check refuses), DATABASE_URL is exported in the script.
    shard_env = next(s for s in shard["steps"] if s.get("name") == "Run integration tests (this shard)")["env"]
    job_env = job["env"]

    def resolve(v: str) -> str:
        m = re.fullmatch(r"\$\{\{ env\.([A-Z0-9_]+) \}\}", str(v))
        return job_env[m.group(1)] if m else v

    env = {k: resolve(v) for k, v in run["env"].items() if k != "INPUT_SEED"}
    db = re.search(r'export DATABASE_URL="([^"]+)"', run["run"]).group(1).replace("${CI_DB_PASS}", job_env["CI_DB_PASS"])
    assert {**env, "DATABASE_URL": db} == shard_env


FAKE_GH = """#!/usr/bin/env bash
# Stands in for gh: logs the call; `issue list` prints $FAKE_OPEN (the open nightly-red issue number, or nothing).
echo "gh $*" >>"$FAKE_LOG"
case "$1 $2" in
  "issue list") echo "${FAKE_OPEN:-}" ;;
  "issue create") echo "https://github.com/o/r/issues/77" ;;
esac
exit 0
"""


def _report(tmp_path: Path, result: str, open_issue: str = "") -> tuple[str, str]:
    job = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))["jobs"]["report"]
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "gh").write_text(FAKE_GH)
    (bindir / "gh").chmod(0o755)
    log, summary = tmp_path / "gh.log", tmp_path / "summary.md"
    log.write_text("")
    summary.write_text("")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_LOG": str(log), "FAKE_OPEN": open_issue,
           "GITHUB_STEP_SUMMARY": str(summary), "RESULT": result, "SEED": "4242", "EVENT": "schedule",
           "RUN_URL": "https://github.com/o/r/actions/runs/1"}
    p = subprocess.run(["bash", "-c", job["steps"][0]["run"]], env=env, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stdout + p.stderr
    return log.read_text(), p.stdout


def test_report_job_writes_issues_only_and_runs_after_every_finished_night():
    wf = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    job = wf["jobs"]["report"]
    assert job["needs"] == "random-order" and job["if"] == "${{ !cancelled() && github.ref == 'refs/heads/main' }}"
    assert job["permissions"] == {"issues": "write"}
    assert not any("uses" in s for s in job["steps"]), "the report job runs no action and checks nothing out"
    assert wf["jobs"]["random-order"]["outputs"]["seed"] == "${{ steps.suite.outputs.seed }}"
    run = next(s for s in wf["jobs"]["random-order"]["steps"] if s.get("id") == "suite")["run"]
    assert 'echo "seed=$SEED" >> "$GITHUB_OUTPUT"' in run
    assert run.index("GITHUB_OUTPUT") < run.index("pytest "), "the seed is an output even when pytest fails"


def test_first_red_night_opens_the_issue_and_later_ones_comment(tmp_path: Path):
    calls, out = _report(tmp_path, "failure")
    assert "gh label create nightly-red" in calls and "gh issue create" in calls and "--label nightly-red" in calls
    assert "4242" in calls and "issue #77" in out
    calls, _ = _report(tmp_path, "failure", open_issue="12")
    assert "gh issue comment 12" in calls and "issue create" not in calls


def test_green_night_closes_the_open_issue_or_does_nothing(tmp_path: Path):
    calls, _ = _report(tmp_path, "success", open_issue="12")
    assert "gh issue close 12" in calls
    calls, _ = _report(tmp_path, "success")
    assert calls.strip().splitlines() == ["gh issue list --label nightly-red --state open --limit 1 --json number "
                                          "--jq .[0].number // empty"]
