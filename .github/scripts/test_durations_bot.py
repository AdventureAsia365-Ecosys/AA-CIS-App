#!/usr/bin/env python3
"""AA-1084: keep .github/test-durations/{unit,integration}.json fresh without a person.

The CI shards (.github/scripts/ci_shards.py) split the unit and integration suites on recorded seconds per test file.
A stale file makes every new test weigh the median and the shards drift apart (S277: 3 unit shards at 4m23s / 5m09s /
5m59s, the file 5 days and 170 test files old). The CI aggregate jobs already upload this run's per-file seconds as the
artifacts ``test-durations-unit`` / ``test-durations-integration``; the nightly workflow ``test-durations.yml`` runs:

  fetch    --repo <owner/repo> --dest <dir> [--runs 3]
           Download those artifacts of the last N successful ``merge_group`` runs of ci.yml (the queue runs every job
           on the exact tree that lands on main; a pull_request run skips the heavy jobs) into <dir>/<run id>/.
  refresh  --runs-dir <dir> [--out-dir .github/test-durations] [--force]
           Per suite: the median seconds of each test file over the downloaded runs, kept only for files the suite
           discovers today (a deleted test drops out). Refuses a suite whose runs name under 80% of its files (a
           junit path bug would turn the split into a file count). Writes the file only when it matters: the longest
           shard predicted on the fresh seconds drops by >= MIN_GAIN_S with the fresh file, or >= MIN_UNKNOWN files
           have no record in the committed one. Prints a summary (also to $GITHUB_STEP_SUMMARY) and writes
           ``changed=true|false`` to $GITHUB_OUTPUT.
  publish  --repo <owner/repo> [--dry-run] [--run-url <url>]
           One commit on the latest main that changes only the durations files (Git Data API: no clone, no push of
           workflow files), on the branch ``bot/test-durations``; opens or updates the one PR. The bot never merges
           (root ADR 0011a lists the only two PR kinds ci-bot may merge): the orchestrating agent queues it like any
           other PR, and the merge queue runs the FULL CI with the new split before it lands, so a split that exposes
           an order-dependent test fails there and the PR stays open.

Only these two JSON files ever change: they are read by the CI shard step alone (no deploy path, no runtime code).
Standard library only; ``gh`` is the only external command (``fetch``, ``publish``).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import ci_shards  # noqa: E402  (stdlib-only sibling module)

BRANCH = "bot/test-durations"
OUT_DIR = ".github/test-durations"
CI_WORKFLOW = "ci.yml"
MIN_GAIN_S = 5.0          # predicted seconds off the longest shard that justify a PR
MIN_UNKNOWN = 10          # discovered files without a recorded duration that justify a PR
MIN_COVERAGE = 0.8        # share of the discovered files the fresh runs must name
MAX_SECONDS = 3600.0      # one test file never takes an hour: a larger value is a broken report
TITLE = "chore: refresh the CI test durations from the merge queue runs"


class BotError(Exception):
    """A step failed; the message says which and why."""


@dataclass(frozen=True)
class Suite:
    """How ci.yml splits one suite (``test_the_bot_suites_match_ci`` keeps this in step with ci.yml)."""
    name: str
    roots: tuple[str, ...]
    shards_env: str
    workers: int = 1
    overhead: float = 0.0
    contiguous: bool = False
    exclude: tuple[str, ...] = field(default_factory=tuple)

    @property
    def path(self) -> str:
        return f"{OUT_DIR}/{self.name}.json"

    @property
    def artifact(self) -> str:
        return f"test-durations-{self.name}"


SUITES = (
    Suite("unit", ("tests/unit", "tests/acp"), "UNIT_SHARDS", workers=4, overhead=0.45),
    Suite("integration", ("tests/integration",), "INTEGRATION_SHARDS", contiguous=True,
          exclude=("tests/integration/test_007_rls_isolation.py",)),
)


def write_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in values.items():
                fh.write(f"{k}={v}\n")


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n\n")


# --- gh -------------------------------------------------------------------------------------------------------------

def _run_gh(args: list[str], stdin: Optional[str] = None) -> str:
    proc = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise BotError(f"`gh {' '.join(args[:3])} ...` failed ({proc.returncode}): {detail}")
    return proc.stdout


class Gh:
    """The few gh calls the bot makes; tests pass a fake with the same methods."""

    def __init__(self, runner: Callable[[list[str], Optional[str]], str] = _run_gh):
        self.runner = runner

    def api(self, method: str, path: str, body: Optional[dict] = None) -> dict | list:
        args = ["api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        if body is not None:
            args += ["--input", "-"]
        out = self.runner(args, json.dumps(body) if body is not None else None)
        return json.loads(out) if out.strip() else {}

    def cli(self, *args: str) -> str:
        return self.runner(list(args), None)


# --- fetch ----------------------------------------------------------------------------------------------------------

def cmd_fetch(args: argparse.Namespace, gh: Gh) -> int:
    runs = json.loads(gh.cli("run", "list", "--repo", args.repo, "--workflow", CI_WORKFLOW, "--event", "merge_group",
                             "--status", "success", "--limit", str(args.runs), "--json", "databaseId,headSha,createdAt")
                      or "[]")
    dest = Path(args.dest)
    got = []
    for run in runs:
        rid = str(run["databaseId"])
        names = []
        for suite in SUITES:
            try:
                gh.cli("run", "download", rid, "--repo", args.repo, "-n", suite.artifact,
                       "-D", str(dest / rid / suite.name))
                names.append(suite.name)
            except BotError as exc:  # expired (14 days) or a run without that job's artifact
                print(f"run {rid}: no {suite.artifact} ({str(exc)[:120]})")
        if names:
            got.append(f"{rid} ({run.get('createdAt', '')[:16]}, {run.get('headSha', '')[:8]}): {', '.join(names)}")
    print("\n".join(got) if got else "no artifact downloaded")
    write_summary("### Test durations: merge-queue runs read\n\n" + ("\n".join(f"- {g}" for g in got) or "- none"))
    return 0


# --- refresh --------------------------------------------------------------------------------------------------------

def read_runs(runs_dir: Path, suite: Suite) -> list[dict[str, float]]:
    """Every ``<suite>-durations.json`` under ``runs_dir``; a malformed file is skipped with a note, never trusted."""
    out = []
    for path in sorted(runs_dir.rglob(f"{suite.name}-durations.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            clean = {}
            for k, v in data.items():
                if not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError(f"bad entry {k!r}")
                if not 0 <= float(v) <= MAX_SECONDS:
                    raise ValueError(f"{k}: {v} s is out of range")
                clean[k] = float(v)
            out.append(clean)
        except (OSError, ValueError) as exc:
            print(f"skip {path}: {exc}")
    return out


def merge_runs(runs: list[dict[str, float]], files: list[str]) -> dict[str, float]:
    """Median seconds per discovered file over the runs that name it (files no run names are left out)."""
    keep = set(files)
    merged: dict[str, list[float]] = {}
    for run in runs:
        for k, v in run.items():
            if k in keep:
                merged.setdefault(k, []).append(v)
    return {k: round(statistics.median(v), 2) for k, v in sorted(merged.items())}


def shard_count(suite: Suite, ci_text: str) -> int:
    m = re.search(rf"^\s+{suite.shards_env}: (\d+)\s*$", ci_text, re.M)
    if not m:
        raise BotError(f"{CI_WORKFLOW} sets no {suite.shards_env}")
    return int(m.group(1))


def longest_shard(suite: Suite, files: list[str], plan_on: dict[str, float], truth: dict[str, float],
                  shards: int) -> float:
    """Predicted seconds of the longest shard when the split is made on ``plan_on`` and the tests take ``truth``."""
    split = ci_shards.plan_contiguous if suite.contiguous else ci_shards.plan
    buckets = split(files, plan_on, shards, suite.workers, suite.overhead)
    secs = ci_shards.seconds(files, truth)
    if suite.contiguous:
        weight = ci_shards.weights(files, truth, suite.workers, suite.overhead)
        return max(ci_shards.estimate(b, weight) for b in buckets)
    return max(ci_shards.bucket_cost(b, secs, suite.workers, suite.overhead) for b in buckets)


@dataclass
class Verdict:
    suite: str
    write: bool
    reason: str
    files: int = 0
    runs: int = 0
    before_s: float = 0.0
    after_s: float = 0.0
    unknown: int = 0
    fresh: dict = field(default_factory=dict)


def judge(suite: Suite, runs: list[dict[str, float]], committed: dict[str, float], files: list[str], shards: int,
          force: bool = False) -> Verdict:
    if not runs:
        return Verdict(suite.name, False, "no run report found")
    fresh = merge_runs(runs, files)
    v = Verdict(suite.name, False, "", files=len(files), runs=len(runs), fresh=fresh,
                unknown=sum(1 for f in files if f not in committed))
    if len(fresh) < MIN_COVERAGE * len(files):
        v.reason = (f"refused: the runs name {len(fresh)} of {len(files)} test files (< {MIN_COVERAGE:.0%}; "
                    "a junit path change?)")
        return v
    v.before_s = round(longest_shard(suite, files, committed, fresh, shards), 1)
    v.after_s = round(longest_shard(suite, files, fresh, fresh, shards), 1)
    gain = v.before_s - v.after_s
    if force:
        v.write, v.reason = True, "forced"
    elif gain >= MIN_GAIN_S:
        v.write, v.reason = True, f"the longest shard drops by ~{gain:.0f} s"
    elif v.unknown >= MIN_UNKNOWN:
        v.write, v.reason = True, f"{v.unknown} test files have no recorded duration"
    else:
        v.reason = f"balanced enough (gain ~{gain:.0f} s, {v.unknown} files without a record)"
    return v


def render(verdicts: list[Verdict], shards: dict[str, int]) -> str:
    rows = ["| Suite | Runs | Files | Shards | Longest shard now | With the fresh file | Unrecorded | Decision |",
            "|---|---|---|---|---|---|---|---|"]
    for v in verdicts:
        rows.append(f"| {v.suite} | {v.runs} | {v.files} | {shards.get(v.suite, '?')} | ~{v.before_s:.0f} s "
                    f"| ~{v.after_s:.0f} s | {v.unknown} | {'write' if v.write else 'keep'}: {v.reason} |")
    return "\n".join(rows)


def cmd_refresh(args: argparse.Namespace, gh: Gh) -> int:
    root = Path(args.root)
    ci_text = (root / ".github" / "workflows" / CI_WORKFLOW).read_text(encoding="utf-8")
    verdicts, shards, changed = [], {}, []
    cwd = os.getcwd()
    os.chdir(root)  # discover() returns cwd-relative paths, the keys ci.yml splits on
    try:
        for suite in SUITES:
            shards[suite.name] = shard_count(suite, ci_text)
            files = ci_shards.discover(list(suite.roots), list(suite.exclude))
            committed = ci_shards.load_durations(suite.path)
            v = judge(suite, read_runs(Path(cwd, args.runs_dir), suite), committed, files, shards[suite.name],
                      args.force)
            verdicts.append(v)
            if v.write:
                out = Path(args.out_dir or OUT_DIR) / f"{suite.name}.json"
                text = json.dumps(v.fresh, indent=1, sort_keys=True) + "\n"
                if not out.is_file() or out.read_text(encoding="utf-8") != text:
                    out.write_text(text, encoding="utf-8")
                    changed.append(str(out))
    finally:
        os.chdir(cwd)
    table = render(verdicts, shards)
    print(table)
    write_summary("### Test durations refresh\n\n" + table)
    write_output(changed="true" if changed else "false", files=json.dumps(changed))
    return 0


# --- publish --------------------------------------------------------------------------------------------------------

def is_workflow_refusal(message: str) -> bool:
    text = message.lower()
    return "workflow" in text and ("refusing to allow" in text or "permission" in text)


def pr_body(changed: list[str], summary: str, run_url: str) -> str:
    parts = [
        "Automated refresh of the CI test durations (`.github/workflows/test-durations.yml`, "
        "`.github/scripts/test_durations_bot.py`): the median seconds per test file over the last successful "
        "merge-queue runs of CI, so the unit and integration shards stay balanced.",
        "",
        "Files:", *[f"- `{p}`" for p in changed],
        "",
        summary,
        "",
        "Only the shard split reads these files (no deploy path, no runtime code). The merge queue runs the full CI "
        "with the new split before this lands: an integration module that leans on its old neighbour fails there "
        "(fix the test, not the split). The bot never merges: the orchestrating agent queues this PR.",
    ]
    if run_url:
        parts += ["", f"Opened by [test-durations run]({run_url})."]
    return "\n".join(parts) + "\n"


def cmd_publish(args: argparse.Namespace, gh: Gh) -> int:
    root = Path(args.root)
    repo = args.repo
    contents = {s.path: (root / s.path).read_text(encoding="utf-8") for s in SUITES if (root / s.path).is_file()}
    main_sha = gh.api("GET", f"repos/{repo}/git/ref/heads/main")["object"]["sha"]
    main_tree = gh.api("GET", f"repos/{repo}/git/commits/{main_sha}")["tree"]["sha"]
    changed = []
    for path, text in contents.items():
        try:
            current = gh.api("GET", f"repos/{repo}/contents/{path}?ref={main_sha}")
            if base64.b64decode(current.get("content", "")).decode("utf-8") == text:
                continue
        except BotError:
            pass  # not on main yet
        changed.append(path)
    if not changed:
        print("test-durations-bot: main already has these durations: nothing to publish")
        write_summary("### Test durations PR\n\nmain already has these durations: no PR.")
        return 0
    summary = args.summary or ""
    body = pr_body(changed, summary, args.run_url)
    if args.dry_run:
        print(f"test-durations-bot: dry run: would commit {', '.join(changed)} on `{BRANCH}` from main "
              f"{main_sha[:8]} and open or update PR \"{TITLE}\"")
        print(body)
        write_summary(f"### Test durations PR (dry run)\n\nwould commit {', '.join(changed)}")
        return 0

    entries = []
    for path in changed:
        blob = gh.api("POST", f"repos/{repo}/git/blobs", {"content": contents[path], "encoding": "utf-8"})
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = gh.api("POST", f"repos/{repo}/git/trees", {"base_tree": main_tree, "tree": entries})
    commit = gh.api("POST", f"repos/{repo}/git/commits",
                    {"message": f"{TITLE}\n\nWritten by test-durations.yml from the CI artifacts.",
                     "tree": tree["sha"], "parents": [main_sha]})

    prs = json.loads(gh.cli("pr", "list", "--repo", repo, "--head", BRANCH, "--state", "open",
                            "--json", "number,url", "--limit", "1") or "[]")
    pr = prs[0] if prs else None
    try:
        existing = gh.api("GET", f"repos/{repo}/git/ref/heads/{BRANCH}")["object"]["sha"]
    except BotError:
        existing = ""
    notes = []
    if existing and pr is None:
        gh.api("DELETE", f"repos/{repo}/git/refs/heads/{BRANCH}")
        notes.append(f"deleted the stale `{BRANCH}` (no open PR)")
        existing = ""
    if existing:
        try:
            gh.api("PATCH", f"repos/{repo}/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": True})
            notes.append(f"moved `{BRANCH}` to {commit['sha'][:8]}")
        except BotError as exc:
            if not is_workflow_refusal(str(exc)):
                raise
            # The branch's old base lacks workflow changes main has since gained; the App token (no Workflows
            # permission) may not move it across them. Recreate the branch on main instead (as kit_sync_bot.py).
            gh.cli("pr", "close", str(pr["number"]), "--repo", repo, "--comment",
                   "Superseded: `bot/test-durations` is recreated on the latest main (main changed workflow files "
                   "since this branch was cut). A new durations PR follows.")
            gh.api("DELETE", f"repos/{repo}/git/refs/heads/{BRANCH}")
            existing, pr = "", None
            notes.append("closed the old PR and recreated the branch")
    if not existing:
        gh.api("POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
        notes.append(f"created `{BRANCH}` at {commit['sha'][:8]} (main {main_sha[:8]} + durations)")

    body_file = Path(args.root, ".test-durations-pr-body.md")
    body_file.write_text(body, encoding="utf-8")
    try:
        if pr:
            gh.cli("pr", "edit", str(pr["number"]), "--repo", repo, "--title", TITLE, "--body-file", str(body_file))
            number, action = str(pr["number"]), f"updated PR #{pr['number']}"
        else:
            url = gh.cli("pr", "create", "--repo", repo, "--base", "main", "--head", BRANCH, "--title", TITLE,
                         "--body-file", str(body_file)).strip()
            number, action = url.rstrip("/").rsplit("/", 1)[-1], f"opened PR {url}"
    finally:
        body_file.unlink(missing_ok=True)
    result = "; ".join([*notes, action, f"the orchestrating agent merges PR #{number} (the bot never merges)"])
    print(f"test-durations-bot: {result}")
    write_summary(f"### Test durations PR\n\n{result}.")
    return 0


# --- CLI ------------------------------------------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None, gh: Optional[Gh] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", default=str(REPO_ROOT), help="the AA-CIS-App checkout (default: this script's repo)")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--repo", required=True)
    f.add_argument("--dest", required=True)
    f.add_argument("--runs", type=int, default=3)
    r = sub.add_parser("refresh")
    r.add_argument("--runs-dir", required=True)
    r.add_argument("--out-dir", default=None, help="default: <root>/.github/test-durations")
    r.add_argument("--force", action="store_true", help="write the fresh files even when the split barely moves")
    u = sub.add_parser("publish")
    u.add_argument("--repo", required=True)
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--run-url", default="")
    u.add_argument("--summary", default="", help="the refresh table, quoted in the PR body")
    args = p.parse_args(argv)
    try:
        return {"fetch": cmd_fetch, "refresh": cmd_refresh, "publish": cmd_publish}[args.cmd](args, gh or Gh())
    except BotError as exc:
        print(f"test-durations-bot: {exc}", file=sys.stderr)
        write_summary(f"### test-durations-bot failed\n\n```\n{exc}\n```")
        return 1


if __name__ == "__main__":
    sys.exit(main())
