"""S272 incident (AA-1024): a pre-push run rewrote the real clone. Git exports GIT_DIR to its hooks; unit tests that run
`git init` / `git config user.*` / `git commit` in tmp_path inherited it, so they wrote into the clone the hook ran for:
core.bare=true, user ci <ci@example.invalid>, fake "base"/"change" commits and tag, and a rewritten origin/main shared
by every worktree. Two guards, one test each: tests/unit/conftest.py drops the repo env at import, and
scripts/pre-push.sh unsets it before it runs anything."""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFTEST = ROOT / "tests" / "unit" / "conftest.py"


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(*args: str, cwd: Path) -> str:
    env = {**_clean_env(), "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def test_conftest_drops_git_repo_env_so_a_test_cannot_write_the_real_clone(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    _git("init", "-q", ".", cwd=victim)
    before = (victim / ".git" / "config").read_text()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # What a hook-run unit test does: load the unit conftest, then git init + config in its own temp dir.
    code = (
        "import os, runpy, subprocess, sys\n"
        "runpy.run_path(sys.argv[1])\n"
        "assert 'GIT_DIR' not in os.environ and 'GIT_WORK_TREE' not in os.environ\n"
        "subprocess.run(['git', 'init', '-q', sys.argv[2]], check=True)\n"
        "subprocess.run(['git', '-C', sys.argv[2], 'config', 'user.name', 'ci'], check=True)\n"
    )
    env = {**_clean_env(), "GIT_DIR": str(victim / ".git"), "GIT_WORK_TREE": str(victim),
           "PYTHONPATH": str(ROOT)}
    subprocess.run([sys.executable, "-c", code, str(CONFTEST), str(scratch)], env=env, check=True,
                   capture_output=True, text=True, cwd=ROOT)
    assert (victim / ".git" / "config").read_text() == before
    assert (scratch / ".git").is_dir()


def test_prepush_unsets_git_repo_env_before_running_anything():
    text = (ROOT / "scripts" / "pre-push.sh").read_text()
    body = text[text.index("main() {"):]
    unset = re.search(r"unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR", body)
    first_run = re.search(r"pytest|run_check|\$PY ", body)
    assert unset and first_run and unset.start() < first_run.start()
