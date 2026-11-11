"""AA-1084 — the test durations bot (.github/scripts/test_durations_bot.py) and its workflow (test-durations.yml).

- The suites the bot splits are the ones ci.yml splits (roots, exclude, workers, file overhead, contiguous, shard env).
- ``refresh``: the median seconds over the runs per discovered file; deleted tests drop out; a malformed or
  out-of-range report is skipped, never trusted; a suite whose runs name < 80% of its files is refused; the file is
  written only when the longest shard drops by >= 5 s or >= 10 files have no record (``--force`` always); the
  verdict goes to $GITHUB_OUTPUT.
- ``publish``: a dry run writes nothing; a live run makes one commit on the latest main that changes only the
  durations files (Git Data API), creates or moves ``bot/test-durations`` (a stale branch without a PR is recreated, a
  workflow refusal closes the old PR and recreates the branch), opens or edits the one PR, and asks for auto-merge
  only with --auto-merge; a refused auto-merge leaves the PR for the orchestrator; main already equal is a no-op.
- ``fetch`` reads only successful merge_group runs of ci.yml and skips a run whose artifact is gone.
- The workflow: main only, nightly, environment release, GITHUB_TOKEN read-only (+ actions: read), the App token minted
  only when there is something to publish and never on a dry run, scoped to Contents + Pull requests.
All gh calls go to a fake: no network.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "test_durations_bot.py"
WORKFLOW = REPO / ".github" / "workflows" / "test-durations.yml"
CI = REPO / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("test_durations_bot", SCRIPT)
bot = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("test_durations_bot", bot)   # dataclasses look the module up by name
_spec.loader.exec_module(bot)


class FakeGh:
    """Records every call; answers from a small in-memory GitHub."""

    def __init__(self, main_files: dict[str, str] | None = None, branch: str = "", prs: list | None = None,
                 refuse_patch: str = "", refuse_merge: bool = False, runs: list | None = None,
                 missing_artifacts: set | None = None):
        self.calls: list[tuple] = []
        self.main_files = main_files or {}
        self.branch = branch
        self.prs = prs or []
        self.refuse_patch = refuse_patch
        self.refuse_merge = refuse_merge
        self.runs = runs or []
        self.missing = missing_artifacts or set()
        self.blobs: dict[str, str] = {}

    def api(self, method, path, body=None):
        self.calls.append(("api", method, path, body))
        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "m" * 40}}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "t" * 40}}
        if method == "GET" and "/contents/" in path:
            rel = path.split("/contents/", 1)[1].split("?", 1)[0]
            if rel not in self.main_files:
                raise bot.BotError("404 Not Found")
            return {"content": base64.b64encode(self.main_files[rel].encode()).decode()}
        if method == "POST" and path.endswith("/git/blobs"):
            sha = f"b{len(self.blobs):039d}"
            self.blobs[sha] = body["content"]
            return {"sha": sha}
        if method == "POST" and path.endswith("/git/trees"):
            return {"sha": "n" * 40}
        if method == "POST" and path.endswith("/git/commits"):
            return {"sha": "c" * 40}
        if method == "GET" and "/git/ref/heads/bot/" in path:
            if not self.branch:
                raise bot.BotError("404 Not Found")
            return {"object": {"sha": self.branch}}
        if method == "PATCH":
            if self.refuse_patch:
                raise bot.BotError(self.refuse_patch)
            self.branch = body["sha"]
            return {}
        if method == "DELETE":
            self.branch = ""
            return {}
        if method == "POST" and path.endswith("/git/refs"):
            self.branch = body["sha"]
            return {}
        raise AssertionError(f"unexpected api call {method} {path}")

    def cli(self, *args):
        self.calls.append(("cli", *args))
        if args[:2] == ("pr", "list"):
            return json.dumps(self.prs)
        if args[:2] == ("pr", "create"):
            return "https://github.com/o/r/pull/77\n"
        if args[:2] == ("pr", "merge"):
            if self.refuse_merge:
                raise bot.BotError("auto merge is not allowed for this repository")
            return ""
        if args[:2] == ("run", "list"):
            return json.dumps(self.runs)
        if args[:2] == ("run", "download"):
            rid, name, dest = args[2], args[args.index("-n") + 1], Path(args[args.index("-D") + 1])
            if (rid, name) in self.missing:
                raise bot.BotError("no artifact matches")
            dest.mkdir(parents=True, exist_ok=True)
            suite = name.removeprefix("test-durations-")
            (dest / f"{suite}-durations.json").write_text("{}")
            return ""
        return ""

    def cli_calls(self, *prefix):
        return [c for c in self.calls if c[0] == "cli" and c[1:1 + len(prefix)] == prefix]

    def api_calls(self, method):
        return [c for c in self.calls if c[0] == "api" and c[1] == method]


# --- the suites match ci.yml ---------------------------------------------------------------------------------------

def _ci_files_args(job: str) -> list[str]:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    run = next(s["run"] for s in jobs[job]["steps"] if "ci_shards.py files" in s.get("run", ""))
    cmd = run[run.index("ci_shards.py files"):].split(")", 1)[0].replace("\\\n", " ")
    return shlex.split(cmd)[2:]


@pytest.mark.parametrize("suite,job", [("unit", "test-unit-shard"), ("integration", "test-integration-shard")])
def test_the_bot_suites_match_ci(suite, job):
    s = next(x for x in bot.SUITES if x.name == suite)
    args = _ci_files_args(job)
    opts = {a: args[i + 1] for i, a in enumerate(args) if a in ("--workers", "--file-overhead", "--durations",
                                                                   "--exclude", "--shards")}
    roots = [a for i, a in enumerate(args) if not a.startswith("-") and args[i - 1] not in (
        "--workers", "--file-overhead", "--durations", "--exclude", "--shards", "--index")]
    assert tuple(roots) == s.roots
    assert opts["--durations"] == s.path
    assert opts["--shards"] == f"${s.shards_env}"
    assert int(opts.get("--workers", 1)) == s.workers
    assert float(opts.get("--file-overhead", 0.0)) == s.overhead
    assert ("--contiguous" in args) == s.contiguous
    assert tuple(a for i, a in enumerate(args) if i and args[i - 1] == "--exclude") == s.exclude
    assert bot.shard_count(s, CI.read_text(encoding="utf-8")) >= 2
    # The aggregate job uploads the artifact the bot downloads.
    assert f"name: {s.artifact}" in CI.read_text(encoding="utf-8")


# --- refresh -------------------------------------------------------------------------------------------------------

UNIT = bot.Suite("unit", ("suite",), "UNIT_SHARDS", workers=1)


def _runs(tmp_path: Path, reports: list[dict]) -> Path:
    for i, rep in enumerate(reports):
        d = tmp_path / "runs" / str(i) / "unit"
        d.mkdir(parents=True)
        (d / "unit-durations.json").write_text(json.dumps(rep))
    return tmp_path / "runs"


def test_merge_takes_the_median_and_drops_deleted_tests():
    runs = [{"a": 1.0, "b": 10.0, "gone": 5.0}, {"a": 3.0, "b": 20.0}, {"a": 2.0}]
    assert bot.merge_runs(runs, ["a", "b", "c"]) == {"a": 2.0, "b": 15.0}


def test_read_runs_skips_a_malformed_or_absurd_report(tmp_path):
    root = _runs(tmp_path, [{"suite/test_a.py": 1.5}, {"suite/test_a.py": "x"}, {"suite/test_a.py": 99999},
                            {"suite/test_a.py": True}])
    (root / "4").mkdir()
    (root / "4" / "unit-durations.json").write_text("[1, 2]")
    assert bot.read_runs(root, UNIT) == [{"suite/test_a.py": 1.5}]


def _stale(files: list[str]) -> dict[str, float]:
    """A committed file that puts both long files (test_0, test_1) in shard 1: test_0 looks long (10 s) and test_1
    short (0.1 s), so the ten 1 s files fill shard 2 to 10 s and test_1 joins test_0 on the tie."""
    return {f: (10.0 if i == 0 else 0.1 if i == 1 else 1.0) for i, f in enumerate(files)}


def test_judge_writes_when_the_longest_shard_drops_or_files_are_unrecorded():
    files = [f"suite/test_{i}.py" for i in range(12)]
    truth = {f: (30.0 if i < 2 else 1.0) for i, f in enumerate(files)}
    stale = _stale(files)
    v = bot.judge(UNIT, [truth], stale, files, 2)
    assert v.write and "drops by" in v.reason and v.after_s < v.before_s
    # Fresh file already committed: nothing to do.
    same = bot.judge(UNIT, [truth], truth, files, 2)
    assert not same.write and "balanced enough" in same.reason
    # Ten files without a record are reason enough on their own.
    partial = {f: truth[f] for f in files[:2]}
    assert bot.judge(UNIT, [truth], partial, files, 2).write
    assert bot.judge(UNIT, [truth], truth, files, 2, force=True).write


def test_judge_refuses_runs_that_name_too_few_files():
    files = [f"suite/test_{i}.py" for i in range(10)]
    v = bot.judge(UNIT, [{"test_0.py": 1.0, "suite/test_1.py": 1.0}], {}, files, 2)
    assert not v.write and v.reason.startswith("refused")
    assert not bot.judge(UNIT, [], {}, files, 2).write


def _fake_repo(tmp_path: Path, committed: dict) -> Path:
    root = tmp_path / "repo"
    for i in range(12):
        p = root / "suite" / f"test_{i}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def test_x():\n    pass\n")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("jobs:\n  x:\n    env:\n      UNIT_SHARDS: 2\n")
    (root / ".github" / "test-durations").mkdir()
    (root / ".github" / "test-durations" / "unit.json").write_text(json.dumps(committed))
    return root


def test_cli_refresh_writes_the_fresh_file_and_reports_changed(tmp_path, monkeypatch):
    files = [f"suite/test_{i}.py" for i in range(12)]
    truth = {f: (30.0 if i < 2 else 1.0) for i, f in enumerate(files)}
    root = _fake_repo(tmp_path, _stale(files))
    runs = _runs(tmp_path, [truth, {**truth, "suite/test_deleted.py": 4.0}])
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(bot, "SUITES", (UNIT,))
    assert bot.main(["--root", str(root), "refresh", "--runs-dir", str(runs)], gh=FakeGh()) == 0
    written = json.loads((root / ".github" / "test-durations" / "unit.json").read_text())
    assert written == truth
    assert "changed=true" in out.read_text()
    # A second run on the fresh file changes nothing.
    out.write_text("")
    assert bot.main(["--root", str(root), "refresh", "--runs-dir", str(runs)], gh=FakeGh()) == 0
    assert "changed=false" in out.read_text()


# --- publish -------------------------------------------------------------------------------------------------------

def _publish_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".github" / "test-durations").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "test-durations" / "unit.json").write_text('{"u": 1}\n')
    (root / ".github" / "test-durations" / "integration.json").write_text('{"i": 2}\n')
    return root


def test_publish_dry_run_writes_nothing(tmp_path):
    gh = FakeGh()
    assert bot.main(["--root", str(_publish_root(tmp_path)), "publish", "--repo", "o/r", "--dry-run"], gh=gh) == 0
    assert not gh.api_calls("POST") and not gh.api_calls("PATCH") and not gh.cli_calls("pr")


def test_publish_is_a_noop_when_main_has_the_files(tmp_path):
    gh = FakeGh(main_files={".github/test-durations/unit.json": '{"u": 1}\n',
                            ".github/test-durations/integration.json": '{"i": 2}\n'})
    assert bot.main(["--root", str(_publish_root(tmp_path)), "publish", "--repo", "o/r", "--auto-merge"], gh=gh) == 0
    assert not gh.api_calls("POST") and not gh.cli_calls("pr", "create")


def test_publish_commits_only_the_changed_durations_on_main_and_opens_the_pr(tmp_path):
    gh = FakeGh(main_files={".github/test-durations/integration.json": '{"i": 2}\n'})
    assert bot.main(["--root", str(_publish_root(tmp_path)), "publish", "--repo", "o/r", "--auto-merge"], gh=gh) == 0
    tree = next(c for c in gh.api_calls("POST") if c[2].endswith("/git/trees"))[3]
    assert tree["base_tree"] == "t" * 40
    assert [e["path"] for e in tree["tree"]] == [".github/test-durations/unit.json"]
    commit = next(c for c in gh.api_calls("POST") if c[2].endswith("/git/commits"))[3]
    assert commit["parents"] == ["m" * 40] and commit["message"].startswith("chore: ")
    assert not re.search(r"\bAA-\d+", commit["message"])
    ref = next(c for c in gh.api_calls("POST") if c[2].endswith("/git/refs"))[3]
    assert ref == {"ref": "refs/heads/bot/test-durations", "sha": "c" * 40}
    create = gh.cli_calls("pr", "create")[0]
    assert "--head" in create and create[create.index("--head") + 1] == "bot/test-durations"
    assert gh.cli_calls("pr", "merge") == [("cli", "pr", "merge", "77", "--repo", "o/r", "--squash", "--auto")]
    assert not (tmp_path / "repo" / ".test-durations-pr-body.md").exists()


def test_publish_moves_the_branch_of_an_open_pr_and_edits_it_without_auto_merge(tmp_path):
    gh = FakeGh(branch="o" * 40, prs=[{"number": 12, "url": "u"}])
    assert bot.main(["--root", str(_publish_root(tmp_path)), "publish", "--repo", "o/r"], gh=gh) == 0
    assert gh.api_calls("PATCH")[0][3] == {"sha": "c" * 40, "force": True}
    assert gh.cli_calls("pr", "edit") and not gh.cli_calls("pr", "create") and not gh.cli_calls("pr", "merge")


def test_publish_recreates_a_stale_branch_without_a_pr(tmp_path):
    gh = FakeGh(branch="o" * 40)
    assert bot.main(["--root", str(_publish_root(tmp_path)), "publish", "--repo", "o/r"], gh=gh) == 0
    assert gh.api_calls("DELETE") and not gh.api_calls("PATCH")
    assert gh.branch == "c" * 40 and gh.cli_calls("pr", "create")


def test_publish_recreates_the_branch_when_github_refuses_the_move_over_workflow_files(tmp_path):
    gh = FakeGh(branch="o" * 40, prs=[{"number": 12, "url": "u"}],
                refuse_patch="refusing to allow a GitHub App to create or update workflow `.github/workflows/ci.yml` "
                             "without `workflows` permission")
    assert bot.main(["--root", str(_publish_root(tmp_path)), "publish", "--repo", "o/r"], gh=gh) == 0
    assert gh.cli_calls("pr", "close") and gh.api_calls("DELETE") and gh.cli_calls("pr", "create")
    assert gh.branch == "c" * 40


def test_publish_fails_on_another_refusal_and_a_refused_auto_merge_leaves_the_pr(tmp_path):
    gh = FakeGh(branch="o" * 40, prs=[{"number": 12, "url": "u"}], refuse_patch="422 Reference update failed")
    assert bot.main(["--root", str(_publish_root(tmp_path)), "publish", "--repo", "o/r"], gh=gh) == 1
    gh = FakeGh(refuse_merge=True)
    assert bot.main(["--root", str(_publish_root(tmp_path)), "publish", "--repo", "o/r", "--auto-merge"], gh=gh) == 0
    assert gh.cli_calls("pr", "create")


# --- fetch ---------------------------------------------------------------------------------------------------------

def test_fetch_reads_successful_merge_queue_runs_and_skips_an_expired_artifact(tmp_path):
    gh = FakeGh(runs=[{"databaseId": 1, "headSha": "a" * 40, "createdAt": "2026-11-11T05:00:00Z"},
                      {"databaseId": 2, "headSha": "b" * 40, "createdAt": "2026-11-11T04:00:00Z"}],
                missing_artifacts={("2", "test-durations-integration")})
    assert bot.main(["fetch", "--repo", "o/r", "--dest", str(tmp_path / "runs")], gh=gh) == 0
    listing = gh.cli_calls("run", "list")[0]
    for flag, value in (("--workflow", "ci.yml"), ("--event", "merge_group"), ("--status", "success")):
        assert listing[listing.index(flag) + 1] == value
    assert (tmp_path / "runs" / "1" / "unit" / "unit-durations.json").is_file()
    assert (tmp_path / "runs" / "2" / "unit" / "unit-durations.json").is_file()
    assert not (tmp_path / "runs" / "2" / "integration").exists()


# --- the workflow --------------------------------------------------------------------------------------------------

def test_the_workflow_is_main_only_nightly_and_mints_a_narrow_token_only_to_publish():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = wf.get("on") or wf.get(True)
    assert "schedule" in on and "workflow_dispatch" in on and "push" not in on and "pull_request" not in on
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["refresh"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "release"
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    steps = {s.get("name") or s.get("uses"): s for s in job["steps"]}
    mint = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/create-github-app-token@"))
    assert "steps.refresh.outputs.changed == 'true'" in mint["if"] and "inputs.dry_run" in mint["if"]
    assert mint["with"]["permission-contents"] == "write"
    assert mint["with"]["permission-pull-requests"] == "write"
    assert set(k for k in mint["with"] if k.startswith("permission-")) == {"permission-contents",
                                                                          "permission-pull-requests"}
    assert "repositories" not in mint["with"]   # this repo only (the default)
    publish = steps["Open or update the durations PR"]
    assert publish["if"] == "steps.app-token.outcome == 'success'"
    assert "secrets." not in json.dumps(steps["Refresh the durations files"])
    checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False
