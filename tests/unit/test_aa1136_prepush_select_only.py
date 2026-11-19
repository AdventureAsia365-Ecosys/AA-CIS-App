"""AA-1136 (agent change hooks, app-repo slice): ``scripts/pre-push.sh --select-only``.

It prints the unit and integration test files fast mode and ``--integration`` would pick for the changes since the
merge base, committed or not (a lane may stop before it commits), as ``unit <path>`` / ``integration <path>`` lines,
and exits 0. It runs no tool (no Python, no pytest, no Postgres) and writes nothing: S284 hooks advisory 4.3, a
pre-push step never writes files. Run here on a throwaway repo with a copy of the script.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "pre-push.sh"


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "AA_PREPUSH", "VIRTUAL_ENV"))}
    return env | {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                  "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=_env(), check=True, capture_output=True, text=True).stdout


def _select(repo: Path, base: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "scripts/pre-push.sh", "--base", base, "--select-only"], cwd=repo,
                          env=_env() | env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / "services").mkdir()
    (r / "tests" / "unit").mkdir(parents=True)
    (r / "tests" / "integration").mkdir(parents=True)
    shutil.copy(SCRIPT, r / "scripts" / "pre-push.sh")
    for name in ("lantern", "harbor", "orchard", "meadow"):
        (r / "services" / f"{name}.py").write_text("")
        (r / "tests" / "unit" / f"test_{name}.py").write_text(f"import services.{name}\n")
    (r / "tests" / "integration" / "test_quarry_db.py").write_text("def test_a():\n    assert True\n")
    (r / ".gitignore").write_text("scratch_*\n")
    _git(r, "init", "-q", "-b", "main", ".")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", "base")
    _git(r, "switch", "-q", "-c", "lane")
    return r


def test_select_only_lists_committed_staged_unstaged_and_untracked_changes(repo: Path):
    (repo / "services" / "lantern.py").write_text("x = 1\n")
    _git(repo, "commit", "-qam", "committed")
    (repo / "services" / "harbor.py").write_text("y = 1\n")
    _git(repo, "add", "services/harbor.py")
    (repo / "services" / "orchard.py").write_text("z = 1\n")
    (repo / "tests" / "integration" / "test_quarry_db.py").write_text("def test_a():\n    assert 1 == 1\n")
    (repo / "services" / "scratch_ignored.py").write_text("")
    status_before = _git(repo, "status", "--porcelain", "--untracked-files=all")

    # A Python that cannot start proves no tool runs.
    p = _select(repo, "base", AA_PREPUSH_PYTHON=str(repo / "no-such-python"))

    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.splitlines() == [
        "unit tests/unit/test_harbor.py",
        "unit tests/unit/test_lantern.py",
        "unit tests/unit/test_orchard.py",
        "integration tests/integration/test_quarry_db.py",
    ]
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == status_before


def test_select_only_without_a_base_is_a_note_and_exit_0(repo: Path):
    p = _select(repo, "no-such-ref")
    assert p.returncode == 0
    assert p.stdout.strip() == "note cannot diff HEAD against no-such-ref (git fetch origin?): nothing selected"
