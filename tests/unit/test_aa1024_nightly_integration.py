"""AA-1024 l22 (S271 lessons guard G8): the nightly integration run in a random module order.

- ``ci_shards.py files --shuffle SEED`` prints the same files as without it, in an order fixed by SEED.
- .github/workflows/nightly-integration.yml runs the whole suite (minus test_007, as CI) in one job on CI's Postgres,
  on a schedule and by hand with a seed, never on a pull request, and prints the seed.
"""
from __future__ import annotations

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
