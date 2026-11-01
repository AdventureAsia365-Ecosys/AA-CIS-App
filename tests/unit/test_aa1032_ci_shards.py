"""AA-1032 — CI test shards: the split never drops or doubles a test file, and the required checks keep their names.

- .github/scripts/ci_shards.py: a partition of the discovered files (real suites and synthetic ones), deterministic,
  longest-first balanced, a file without a recorded duration weighs the median, path order inside a shard, junit
  durations (xunit1 ``file`` attribute and the xunit2 classname fallback), the CLI refusals.
- .github/workflows/ci.yml: "Unit Tests" and "Integration Tests" are aggregate jobs that need their shard matrix, run
  when a shard failed (never "skipped", which a required check counts as passed) and fail unless every shard passed;
  the matrix size equals the shard count each shard splits by; test_007 stays out of the integration run.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "ci_shards.py"
CI = REPO / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("ci_shards", SCRIPT)
shards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shards)


def _cli(*args, cwd=REPO):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True)


def _tree(tmp_path: Path, names: list[str]) -> Path:
    for name in names:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def test_x():\n    pass\n")
    return tmp_path


def test_discover_takes_pytest_default_names_only(tmp_path, monkeypatch):
    _tree(tmp_path, ["suite/test_a.py", "suite/sub/b_test.py", "suite/helper.py", "suite/conftest.py",
                     "suite/__pycache__/test_old.py", "suite/test_skip.py"])
    monkeypatch.chdir(tmp_path)
    assert shards.discover(["suite"], ["suite/test_skip.py"]) == ["suite/sub/b_test.py", "suite/test_a.py"]


def test_discover_refuses_a_missing_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        shards.discover(["nope"], [])


@pytest.mark.parametrize("n", [1, 2, 3, 4, 7])
def test_plan_is_a_partition_in_path_order(n):
    files = [f"t/test_{i:02d}.py" for i in range(23)]
    durations = {f: float((i * 7) % 11) for i, f in enumerate(files)}
    buckets = shards.plan(files, durations, n)
    assert len(buckets) == n
    flat = [f for b in buckets for f in b]
    assert sorted(flat) == files and len(flat) == len(set(flat))
    assert all(b == sorted(b) for b in buckets)
    shards.check_partition(files, buckets)


def test_plan_is_deterministic_and_balanced_longest_first():
    files = ["t/test_a.py", "t/test_b.py", "t/test_c.py", "t/test_d.py", "t/test_e.py"]
    durations = {"t/test_a.py": 50, "t/test_b.py": 30, "t/test_c.py": 20, "t/test_d.py": 20, "t/test_e.py": 10}
    first = shards.plan(files, durations, 2)
    assert first == shards.plan(list(reversed(files)), durations, 2)
    # 50 -> s1, 30 -> s2, 20 -> s2 (50), 20 -> s1 (tie on 50: the lower index), 10 -> s2.
    assert first == [["t/test_a.py", "t/test_d.py"], ["t/test_b.py", "t/test_c.py", "t/test_e.py"]]


def test_a_file_without_a_duration_weighs_the_median():
    w = shards.weights(["t/test_a.py", "t/test_b.py", "t/test_c.py", "t/test_new.py"],
                       {"t/test_a.py": 1, "t/test_b.py": 5, "t/test_c.py": 9})
    assert w["t/test_new.py"] == 5
    assert shards.weights(["t/test_x.py"], {}) == {"t/test_x.py": 1.0}


def test_workers_divide_the_test_seconds_and_the_overhead_counts_per_file():
    w = shards.weights(["t/test_a.py"], {"t/test_a.py": 8.0}, workers=4, overhead=0.5)
    assert w == {"t/test_a.py": 2.5}
    with pytest.raises(ValueError):
        shards.weights(["t/test_a.py"], {}, workers=0)
    with pytest.raises(ValueError):
        shards.plan(["t/test_a.py"], {}, 0)


@pytest.mark.parametrize("n", [1, 2, 3, 4, 6, 9])
def test_contiguous_ranges_keep_the_path_order_and_cover_every_file(n):
    files = [f"t/test_{i:02d}.py" for i in range(9)]
    durations = {f: float(1 + (i * 5) % 7) for i, f in enumerate(files)}
    buckets = shards.plan_contiguous(list(reversed(files)), durations, n)
    assert len(buckets) == n and all(buckets)
    assert [f for b in buckets for f in b] == files  # concatenated = the unsharded order
    shards.check_partition(files, buckets)


def test_contiguous_ranges_minimise_the_largest_shard():
    files = ["t/test_a.py", "t/test_b.py", "t/test_c.py", "t/test_d.py", "t/test_e.py"]
    durations = {"t/test_a.py": 1, "t/test_b.py": 1, "t/test_c.py": 6, "t/test_d.py": 1, "t/test_e.py": 1}
    # Best largest range is 6 (c alone); ties keep the earliest cut.
    assert shards.plan_contiguous(files, durations, 3) == [
        ["t/test_a.py", "t/test_b.py"], ["t/test_c.py"], ["t/test_d.py", "t/test_e.py"]]
    # Two ranges: a+b | c+d+e and a+b+c | d+e both peak at 8; the earliest cut wins.
    assert shards.plan_contiguous(files, durations, 2) == [
        ["t/test_a.py", "t/test_b.py"], ["t/test_c.py", "t/test_d.py", "t/test_e.py"]]
    # More shards than files: the extra shards are empty (the CLI refuses to run an empty one).
    assert shards.plan_contiguous(files[:2], durations, 3) == [["t/test_a.py"], ["t/test_b.py"], []]


def test_check_partition_refuses_a_lost_or_doubled_file():
    with pytest.raises(SystemExit):
        shards.check_partition(["a", "b"], [["a"], []])
    with pytest.raises(SystemExit):
        shards.check_partition(["a", "b"], [["a", "b"], ["b"]])


def test_junit_durations_xunit1_file_and_xunit2_classname(tmp_path):
    x1 = tmp_path / "x1.xml"
    x1.write_text('<testsuites><testsuite name="pytest">'
                  '<testcase classname="tests.unit.test_a" file="tests/unit/test_a.py" name="t1" time="1.25"/>'
                  '<testcase classname="tests.unit.test_a.TestK" file="tests/unit/test_a.py" name="t2" time="0.75"/>'
                  '</testsuite></testsuites>')
    x2 = tmp_path / "x2.xml"
    x2.write_text('<testsuites><testsuite name="pytest">'
                  '<testcase classname="tests.integration.test_b.TestK" name="t" time="3"/>'
                  '<testcase classname="" name="odd" time="9"/>'
                  '</testsuite></testsuites>')
    assert shards.junit_durations([str(x1), str(x2)]) == {"tests/integration/test_b.py": 3.0,
                                                          "tests/unit/test_a.py": 2.0}


def test_cli_files_of_every_shard_cover_the_suite(tmp_path):
    root = _tree(tmp_path, [f"suite/test_{i}.py" for i in range(9)])
    seen = []
    for i in (1, 2, 3):
        out = _cli("files", "--shards", "3", "--index", str(i), "suite", cwd=root)
        assert out.returncode == 0, out.stderr
        seen += out.stdout.split()
    assert sorted(seen) == [f"suite/test_{i}.py" for i in range(9)]
    assert _cli("files", "--shards", "3", "--index", "4", "suite", cwd=root).returncode != 0
    # More shards than files: an empty shard is refused, not run green with nothing in it.
    assert _cli("files", "--shards", "12", "--index", "12", "suite", cwd=root).returncode != 0


def test_cli_durations_writes_the_merged_file_and_tolerates_a_missing_report(tmp_path):
    rep = tmp_path / "junit-1.xml"
    rep.write_text('<testsuites><testsuite><testcase file="tests/unit/test_a.py" classname="x" name="t" time="2"/>'
                   '</testsuite></testsuites>')
    out_file = tmp_path / "d.json"
    out = _cli("durations", "--out", str(out_file), str(rep), str(tmp_path / "junit-2.xml"))
    assert out.returncode == 0, out.stderr
    assert json.loads(out_file.read_text()) == {"tests/unit/test_a.py": 2.0}
    assert _cli("durations", "--out", str(out_file), str(tmp_path / "none.xml")).returncode == 0


def _suite_args(job: dict) -> list[str]:
    run = next(s["run"] for s in job["steps"] if "ci_shards.py files" in s.get("run", ""))
    return run


@pytest.mark.parametrize("roots,exclude,durations", [
    (["tests/unit", "tests/acp"], [], ".github/test-durations/unit.json"),
    (["tests/integration"], ["tests/integration/test_007_rls_isolation.py"], ".github/test-durations/integration.json"),
])
def test_the_real_suites_split_into_a_partition(roots, exclude, durations, monkeypatch):
    monkeypatch.chdir(REPO)
    files = shards.discover(roots, exclude)
    assert files and not set(exclude) & set(files)
    known = shards.load_durations(durations)
    assert known, f"{durations} is empty"
    for n in (3, 4):
        for split in (shards.plan, shards.plan_contiguous):
            buckets = split(files, known, n)
            shards.check_partition(files, buckets)
            assert all(buckets)


def _ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


@pytest.mark.parametrize("aggregate,shard_job,count_env", [
    ("test-unit", "test-unit-shard", "UNIT_SHARDS"),
    ("test-integration", "test-integration-shard", "INTEGRATION_SHARDS"),
])
def test_required_check_names_are_aggregates_of_their_shards(aggregate, shard_job, count_env):
    jobs = _ci()["jobs"]
    agg, shard = jobs[aggregate], jobs[shard_job]
    assert agg["name"] == {"test-unit": "Unit Tests", "test-integration": "Integration Tests"}[aggregate]
    assert agg["needs"] == shard_job
    assert agg["if"] == "${{ !cancelled() }}"
    gate = agg["steps"][0]
    assert gate["env"]["RESULT"] == f"${{{{ needs.{shard_job}.result }}}}"
    assert '[ "$RESULT" != "success" ]' in gate["run"] and "exit 1" in gate["run"]
    count = int(shard["env"][count_env])
    assert shard["strategy"]["matrix"]["shard"] == list(range(1, count + 1))
    assert shard["strategy"]["fail-fast"] is False
    assert f"/{count}" in shard["name"]
    run = _suite_args(shard)
    assert f'--shards "${count_env}"' in run and '"$SHARD"' in run
    # No shard name may take a required check's name.
    assert "Tests" not in shard["name"]


def test_integration_shards_keep_test_007_out_run_consecutive_ranges_and_own_their_database():
    shard = _ci()["jobs"]["test-integration-shard"]
    run = _suite_args(shard)
    assert "--exclude tests/integration/test_007_rls_isolation.py" in run
    # The integration modules are not independent: each shard must be a stretch of the single job's order.
    assert "files --contiguous" in run
    assert set(shard["services"]) == {"postgres", "redis"}


def test_unit_shards_measure_the_same_coverage_and_the_aggregate_ratchets_it():
    jobs = _ci()["jobs"]
    run = _suite_args(jobs["test-unit-shard"])
    for flag in ("--cov=services", "--cov=shared", "-n 4 --dist loadfile", "tests/unit tests/acp"):
        assert flag in run, flag
    names = [s.get("name", "") for s in jobs["test-unit"]["steps"]]
    for step in ("Combine coverage of the shards", "Coverage ratchet", "Q0 red on base"):
        assert step in names, step
    assert names.index("Combine coverage of the shards") < names.index("Coverage ratchet")
