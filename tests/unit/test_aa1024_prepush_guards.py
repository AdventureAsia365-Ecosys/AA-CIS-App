"""AA-1024 l22 (S271 lessons guard G6): the pre-push hook refuses a push mid-rebase, and a push whose changed
integration tests have not passed locally on the pushed code.

- ``.githooks/pre-push`` refuses while ``rebase-merge`` / ``rebase-apply`` exists (S257: only the first replayed commit
  reached GitHub).
- ``scripts/pre-push.sh --integration`` runs the integration test files the diff touches and, on a pass with a clean
  tree, writes a stamp keyed on the code; the hook's ``integration`` check refuses without that stamp
  (``AA_PREPUSH_SKIP_INTEGRATION=1`` skips once, as a warning).
- ``scripts/pre-push.sh --self-test`` (the decision logic, no tool run) passes.

The stamp tests run the real script in a throwaway git repo with a fake Python (no Postgres, no pytest run).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".githooks" / "pre-push"
SCRIPT = REPO / "scripts" / "pre-push.sh"

FAKE_PY = """#!/usr/bin/env bash
# Stands in for the Python pre-push.sh picks: answers the -c probes, records -m pytest calls.
if [[ $1 == -m && $2 == pytest ]]; then echo "pytest ${*:3}" >>"$FAKE_LOG"; exit "${FAKE_PYTEST_RC:-0}"; fi
if [[ $1 == -c ]]; then
  case $2 in *socket*) echo 55999 ;; *version*) echo 3.12.0 ;; esac
  exit 0
fi
exit 0
"""


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo with this repo's hook and script, a base commit, and a branch that changes an integration
    test and a doc."""
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / ".githooks").mkdir()
    (r / "tests" / "integration").mkdir(parents=True)
    (r / "docs").mkdir()
    shutil.copy(SCRIPT, r / "scripts" / "pre-push.sh")
    shutil.copy(HOOK, r / ".githooks" / "pre-push")
    (r / "tests" / "integration" / "test_widget_db.py").write_text("def test_a():\n    assert True\n")
    _git(r, "init", "-q", "-b", "main", ".")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", "base")
    (r / "tests" / "integration" / "test_widget_db.py").write_text("def test_a():\n    assert 1 == 1\n")
    _git(r, "commit", "-qam", "change the integration test")
    fake = tmp_path / "fake-python"
    fake.write_text(FAKE_PY)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return r


def _run(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    base_env = {k: v for k, v in os.environ.items() if not k.startswith(("AA_PREPUSH", "CIS_TEST_DB", "VIRTUAL_ENV"))}
    base_env.update({"AA_PREPUSH_PYTHON": str(repo.parent / "fake-python"), "FAKE_LOG": str(repo.parent / "fake.log"),
                     "TMPDIR": str(repo.parent)})
    base_env.update(env)
    return subprocess.run(["bash", "scripts/pre-push.sh", "--base", "base", *args], cwd=repo, env=base_env,
                          capture_output=True, text=True, timeout=60)


def test_self_test_passes():
    p = subprocess.run(["bash", str(SCRIPT), "--self-test"], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "integration targets" in p.stdout


def test_hook_refuses_a_push_mid_rebase(repo: Path):
    line = "refs/heads/main " + _git(repo, "rev-parse", "HEAD").strip() + " refs/heads/main " + "0" * 40 + "\n"
    rebase_dir = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "rebase-merge").strip())
    rebase_dir.mkdir()
    p = subprocess.run(["bash", ".githooks/pre-push", "origin", "x"], cwd=repo, input=line, capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 1 and "a rebase is in progress" in p.stderr
    rebase_dir.rmdir()
    # The same push without the rebase state goes on to scripts/pre-push.sh (here: the stamp check refuses).
    p = subprocess.run(["bash", ".githooks/pre-push", "origin", "x"], cwd=repo, input=line, capture_output=True,
                       text=True, timeout=60, env={**os.environ, "AA_PREPUSH_PYTHON": str(repo.parent / "fake-python"),
                                                   "AA_PREPUSH_BASE": "base"})
    assert "a rebase is in progress" not in p.stderr
    assert "[integration]" in p.stdout


def test_no_stamp_blocks_and_skip_warns(repo: Path):
    p = _run(repo, "--only", "integration")
    assert p.returncode == 1, p.stdout
    assert "have not passed locally" in p.stdout and "scripts/pre-push.sh --integration" in p.stdout
    p = _run(repo, "--only", "integration", AA_PREPUSH_SKIP_INTEGRATION="1")
    assert p.returncode == 0, p.stdout
    assert "WARN" in p.stdout and "AA_PREPUSH_SKIP_INTEGRATION=1" in p.stdout


def test_integration_run_writes_the_stamp_the_hook_accepts(repo: Path):
    p = _run(repo, "--integration", CIS_TEST_DB_PORT="55998", FAKE_PYTEST_RC="1")
    assert p.returncode == 1 and "no stamp written" in p.stdout
    assert _run(repo, "--only", "integration").returncode == 1

    p = _run(repo, "--integration", CIS_TEST_DB_PORT="55998")
    assert p.returncode == 0 and "stamp" in p.stdout, p.stdout
    assert "tests/integration/test_widget_db.py" in (repo.parent / "fake.log").read_text()
    p = _run(repo, "--only", "integration")
    assert p.returncode == 0 and "passed locally" in p.stdout, p.stdout

    # A docs-only commit keeps the stamp; a code change needs a new run.
    (repo / "docs" / "note.md").write_text("n\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs")
    assert _run(repo, "--only", "integration").returncode == 0
    (repo / "tests" / "integration" / "test_widget_db.py").write_text("def test_a():\n    assert 2 == 2\n")
    _git(repo, "commit", "-qam", "code")
    assert _run(repo, "--only", "integration").returncode == 1


def test_integration_refuses_the_system_port(repo: Path):
    p = _run(repo, "--integration", CIS_TEST_DB_PORT="5432")
    assert p.returncode == 1 and "5432" in p.stdout


FAKE_PG_TOOL = """#!/usr/bin/env bash
# Stands in for initdb / pg_ctl / psql: logs the call; initdb makes the data dir.
echo "$(basename "$0") $*" >>"$FAKE_LOG"
if [[ $(basename "$0") == initdb ]]; then mkdir -p "$2"; fi
exit 0
"""


def test_throwaway_cluster_is_stopped_and_removed(repo: Path):
    """Without CIS_TEST_DB_PORT the run starts its own cluster and must stop and delete it after the run. The first
    version started it inside $(...), so the cleanup state never reached the script and every run leaked a
    postgres process (S271 l22)."""
    bindir = repo.parent / "pgbin"
    bindir.mkdir()
    for tool in ("initdb", "pg_ctl", "psql"):
        (bindir / tool).write_text(FAKE_PG_TOOL)
        (bindir / tool).chmod(0o755)
    p = _run(repo, "--integration", AA_PREPUSH_INITDB=str(bindir / "initdb"))
    assert p.returncode == 0, p.stdout + p.stderr
    log = (repo.parent / "fake.log").read_text()
    assert "pg_ctl -D" in log and " start" in log
    stop = [line for line in log.splitlines() if line.startswith("pg_ctl") and " stop" in line]
    assert stop, log
    assert not list(repo.parent.glob("pre-push-pg.*")), "the throwaway cluster dir was left behind"
    assert "-p 55999" in log  # the port from free_port reached pg_ctl and pytest's DATABASE_URL


def test_ci_mode_never_asks_for_the_stamp():
    p = subprocess.run(["bash", str(SCRIPT), "--self-test"], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert "ok   --ci, docs only: never skips by diff" in p.stdout


SMOKE = "tests/integration/test_aa1024_replay_smoke.py"


@pytest.mark.parametrize("changed, body", [
    ("conftest.py", "from _replay_guard import first_line\n"),
    ("_replay_guard.py", "def first_line(e):\n    return str(e)\n"),
])
def test_conftest_or_its_helper_needs_the_smoke_stamp(tmp_path: Path, changed: str, body: str):
    """A change to tests/integration/conftest.py, or to a helper conftest imports, used to print only a WARN (no test
    file names them): the hook now asks for a stamp of the fixed smoke file, and --integration runs exactly it."""
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / ".githooks").mkdir()
    (r / "tests" / "integration").mkdir(parents=True)
    shutil.copy(SCRIPT, r / "scripts" / "pre-push.sh")
    shutil.copy(HOOK, r / ".githooks" / "pre-push")
    (r / "tests" / "integration" / "conftest.py").write_text("from _replay_guard import first_line\n")
    (r / "tests" / "integration" / "_replay_guard.py").write_text("def first_line(e):\n    return ''\n")
    (r / SMOKE).write_text("def test_smoke(db_conn):\n    assert db_conn\n")
    (r / "tests" / "integration" / "test_other_db.py").write_text("def test_b():\n    assert True\n")
    _git(r, "init", "-q", "-b", "main", ".")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", "base")
    (r / "tests" / "integration" / changed).write_text(body + "# changed\n")
    _git(r, "commit", "-qam", f"change {changed}")
    fake = tmp_path / "fake-python"
    fake.write_text(FAKE_PY)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    p = _run(r, "--only", "integration")
    assert p.returncode == 1, p.stdout
    assert "have not passed locally" in p.stdout and SMOKE in p.stdout
    assert "WARN" not in p.stdout

    p = _run(r, "--integration", CIS_TEST_DB_PORT="55998")
    assert p.returncode == 0 and "stamp" in p.stdout, p.stdout
    ran = (tmp_path / "fake.log").read_text()
    assert SMOKE in ran and "test_other_db.py" not in ran
    p = _run(r, "--only", "integration")
    assert p.returncode == 0 and "passed locally" in p.stdout, p.stdout


FAKE_GITLEAKS = """#!/usr/bin/env bash
# Stands in for gitleaks: prints a version, records the scan arguments and the rules file it was given.
if [[ $1 == version ]]; then echo "${FAKE_GL_VERSION:-8.30.1}"; exit 0; fi
echo "gitleaks $*" >>"$FAKE_LOG"
while (( $# )); do [[ $1 == -c ]] && cat "$2" >>"$FAKE_LOG"; shift; done
exit "${FAKE_GL_RC:-0}"
"""


def test_gitleaks_scans_the_pushed_range_with_the_base_rules(repo: Path):
    """AA-1024 G14: every local push scans base..head with gitleaks v8.30.1 and the BASE's .gitleaks.toml (a branch
    cannot allowlist its own leak); a finding, a missing binary or another version blocks; AA_SKIP_GITLEAKS warns."""
    (repo / ".gitleaks.toml").write_text("# branch rules: allow everything\n")
    _git(repo, "add", ".gitleaks.toml")
    _git(repo, "commit", "-qm", "branch rules")
    gl = repo.parent / "fake-gitleaks"
    gl.write_text(FAKE_GITLEAKS)
    gl.chmod(gl.stat().st_mode | stat.S_IEXEC)
    log = repo.parent / "fake.log"

    p = _run(repo, "--only", "gitleaks", AA_GITLEAKS=str(gl))
    assert p.returncode == 0, p.stdout
    ran = log.read_text()
    assert "git . --log-opts=--no-renames base..HEAD" in ran and "--redact" in ran
    assert "branch rules" not in ran  # the base has no .gitleaks.toml, so no -c file at all

    p = _run(repo, "--only", "gitleaks", AA_GITLEAKS=str(gl), FAKE_GL_RC="1")
    assert p.returncode == 1 and "FAIL" in p.stdout and "secret-scan.yml" in p.stdout
    p = _run(repo, "--only", "gitleaks", AA_GITLEAKS=str(gl), FAKE_GL_VERSION="8.18.0")
    assert p.returncode == 1 and "CI uses 8.30.1" in p.stdout
    p = _run(repo, "--only", "gitleaks", AA_GITLEAKS=str(repo.parent / "missing"))
    assert p.returncode == 1 and "AA_SKIP_GITLEAKS=1" in p.stdout
    p = _run(repo, "--only", "gitleaks", AA_GITLEAKS=str(repo.parent / "missing"), AA_SKIP_GITLEAKS="1")
    assert p.returncode == 0 and "WARN" in p.stdout


def test_gitleaks_is_not_in_ci_mode(repo: Path):
    p = _run(repo, "--ci", "--dry-run")
    assert "gitleaks" not in p.stdout.split("checks:", 1)[1].splitlines()[0]
    p = _run(repo, "--dry-run")
    assert "gitleaks" in p.stdout.split("checks:", 1)[1].splitlines()[0]
