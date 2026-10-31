#!/usr/bin/env python3
"""AA-642 S8 (root ADR 0009 decision 13): the AI gateway core sync bot.

Drives `.github/scripts/llm-core-sync.sh` (the copy itself, the same code agents run by hand) for the workflows
`llm-core-sync.yml` and `llm-core-consumers.yml`. Modelled on `kit_sync_bot.py` (same branch handling, see its
docstring "Workflow files"); standard library only; git and, outside --dry-run / --offline, the `gh` CLI.

Subcommands:
  plan    --base <sha> --head <sha> [--force] [--also <path>]...
          Did `shared/aa_llm/**` (or an --also path) change between the two commits? Prints JSON and writes `changed`,
          `repos` and `consumers` (JSON) to $GITHUB_OUTPUT. An empty or all-zero base counts as changed.
  sync    --repo <consumer repo> --dest <checkout> --ref <CIS sha> [--dry-run] [--offline] [--run-url <url>]
          In the consumer checkout: regenerate branch `bot/llm-core-sync` from `origin/main`, copy the core at <ref>,
          commit, force-push only when the tree changed and open or update the one PR `chore: sync AI gateway core
          aa_llm to AA-CIS-App <sha7>`. No-op (exit 0) when no file other than the lock changes; an open sync PR is
          then closed as superseded. The bot never merges: the orchestrating agent merges after the consumer's CI.

Common option: --cis <AA-CIS-App checkout> (default: the repo this script is in). Exit codes: 0 done (including a
no-op), 1 a git / gh / copy step failed, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

OWNER = "AdventureAsia365-Ecosys"
CIS_REPO = f"{OWNER}/AA-CIS-App"
CANON_PATH = "shared/aa_llm"
BRANCH = "bot/llm-core-sync"
ZERO = "0" * 40
HERE = Path(__file__).resolve().parent
SYNC = HERE / "llm-core-sync.sh"
BOT_NAME = "aa-ecosys-ci-bot[bot]"
BOT_EMAIL = "aa-ecosys-ci-bot[bot]@users.noreply.github.com"
LOG_LIMIT = 50


class BotError(Exception):
    """A step failed; the message says which and why."""


@dataclass(frozen=True)
class Consumer:
    name: str
    repo: str
    dest: str
    lock: str
    app_dir: str


def run(cmd: list[str], cwd: Path | str | None = None, check: bool = True) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise BotError(f"`{' '.join(cmd)}` failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(repo), *args], check=check)


def git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def gh(*args: str) -> str:
    return run(["gh", *args])


def consumers() -> list[Consumer]:
    """The consumer table, read from llm-core-sync.sh (its only copy)."""
    out = run(["bash", str(SYNC), "--list"])
    return [Consumer(*line.split("\t")) for line in out.splitlines() if line.strip()]


def default_cis() -> Path:
    return Path(run(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"]))


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


# --- plan ----------------------------------------------------------------------------------------------------------

def core_changed(cis: Path, base: str, head: str, also: tuple[str, ...] = ()) -> tuple[bool, str]:
    if not base or base == ZERO:
        return True, "no base commit (first push or manual run): treated as changed"
    if not git_ok(cis, "cat-file", "-e", f"{base}^{{commit}}"):
        return True, f"base {base[:12]} is not in this checkout: treated as changed"
    files = git(cis, "diff", "--name-only", base, head, "--", CANON_PATH, *also)
    if files:
        return True, f"{len(files.splitlines())} core file(s) changed"
    return False, f"no file under {CANON_PATH} changed between {base[:12]} and {head[:12]}"


def cmd_plan(args: argparse.Namespace) -> int:
    cis = Path(args.cis)
    head = git(cis, "rev-parse", f"{args.head}^{{commit}}")
    changed, why = (True, "forced") if args.force else core_changed(cis, args.base, head, tuple(args.also))
    table = consumers()
    repos = sorted({c.repo for c in table})
    plan = {"changed": changed, "reason": why, "head": head, "repos": repos,
            "consumers": [c.__dict__ for c in table]}
    print(json.dumps(plan, indent=2))
    write_output(changed="true" if changed else "false", repos=json.dumps(repos),
                 consumers=json.dumps(plan["consumers"]))
    write_summary(f"### AI gateway core change\n\n{'Changed' if changed else 'Unchanged'}: {why}.")
    return 0


# --- sync ----------------------------------------------------------------------------------------------------------

def read_lock(repo: Path, rev: str, lock: str) -> dict:
    text = git(repo, "show", f"{rev}:{lock}", check=False)
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def find_pr(repo: str) -> dict | None:
    out = gh("pr", "list", "--repo", f"{OWNER}/{repo}", "--head", BRANCH, "--state", "open",
             "--json", "number,url", "--limit", "1")
    items = json.loads(out or "[]")
    return items[0] if items else None


def is_workflow_refusal(message: str) -> bool:
    text = message.lower()
    return "workflow" in text and ("refusing to allow" in text or "permission" in text)


def delete_branch(repo: str) -> None:
    gh("api", "-X", "DELETE", f"repos/{OWNER}/{repo}/git/refs/heads/{BRANCH}")


def create_branch(repo: str, sha: str) -> None:
    gh("api", "-X", "POST", f"repos/{OWNER}/{repo}/git/refs", "-f", f"ref=refs/heads/{BRANCH}", "-f", f"sha={sha}")


def push_branch(dest: Path, repo: str, base: str, remote: str, pr: dict | None, offline: bool,
                short: str) -> tuple[str, dict | None]:
    """Push HEAD (consumer main + the sync commit) to the bot branch; never rebuilt in place on GitHub (the App
    token has no Workflows permission, see kit_sync_bot.py). --offline (tests) pushes straight to origin."""
    def push() -> None:
        git(dest, "push", "-q", "--force", "origin", f"HEAD:refs/heads/{BRANCH}")

    if offline:
        push()
        return f"force-pushed `{BRANCH}`", pr
    notes = []
    if remote and pr is None:
        delete_branch(repo)
        notes.append(f"deleted the stale `{BRANCH}` (no open PR)")
        remote = ""
    if not remote:
        create_branch(repo, base)
        notes.append(f"created `{BRANCH}` at main `{base[:7]}`")
    try:
        push()
    except BotError as exc:
        if not (pr and is_workflow_refusal(str(exc))):
            raise BotError(f"GitHub refused the push of `{BRANCH}` to {repo}: {exc}") from exc
        gh("pr", "close", str(pr["number"]), "--repo", f"{OWNER}/{repo}", "--comment",
           f"Superseded: `{BRANCH}` is recreated from `main` for AA-CIS-App `{short}` (main changed workflow files "
           "since this branch was cut). A new sync PR follows (llm-core-sync.yml).")
        delete_branch(repo)
        create_branch(repo, base)
        notes.append(f"closed PR #{pr['number']} and recreated `{BRANCH}` at main `{base[:7]}`")
        pr = None
        push()
    return "; ".join([*notes, f"force-pushed `{BRANCH}`"]), pr


def core_log(cis: Path, old_ref: str, ref: str) -> list[str]:
    if not old_ref or old_ref == "local" or not git_ok(cis, "cat-file", "-e", f"{old_ref}^{{commit}}"):
        return []
    out = git(cis, "log", "--no-merges", f"--max-count={LOG_LIMIT}", "--format=%h %s", f"{old_ref}..{ref}",
              "--", CANON_PATH)
    return out.splitlines()


def pr_body(c: Consumer, ref: str, old_ref: str, files: list[tuple[str, str]], log: list[str], run_url: str) -> str:
    short = ref[:7]
    parts = [
        f"Automated sync of the AI gateway core from AA-CIS-App "
        f"[`{short}`](https://github.com/{CIS_REPO}/commit/{ref}) "
        f"(`{CANON_PATH}` -> `{c.dest}`, root ADR 0009 decision 13). Written by `llm-core-sync.yml` with "
        "`.github/scripts/llm-core-sync.sh`.",
        "",
        f"Previous lock ref: `{(old_ref or 'none')[:7]}`.",
        "",
        "Files:",
        *[f"- {what} `{path}`" for what, path in files],
    ]
    if log:
        parts += ["", "Core commits since the previous lock:", *[f"- {line}" for line in log]]
    parts += ["", "Never edit the copy on this branch: the integrity test checks it byte for byte against "
              "`MANIFEST.json` and the lock. Fix the core in AA-CIS-App, merge, and the bot syncs again.",
              "", "Refs AA-642"]
    if run_url:
        parts += ["", f"Opened by [llm-core-sync run]({run_url})."]
    return "\n".join(parts) + "\n"


def changed_paths(dest: Path, path: str) -> list[str]:
    out = git(dest, "status", "--porcelain=v1", "--untracked-files=all", "--", path)
    return [line[3:].split(" -> ")[-1] for line in out.splitlines() if line.strip()]


def cmd_sync(args: argparse.Namespace) -> int:
    cis, dest = Path(args.cis), Path(args.dest)
    group = [c for c in consumers() if c.repo == args.repo]
    if not group:
        raise BotError(f"no aa_llm consumer lives in {args.repo}")
    c = group[0]
    ref = git(cis, "rev-parse", f"{args.ref}^{{commit}}")
    short = ref[:7]
    base = (git(dest, "rev-parse", "origin/main^{commit}") if git_ok(dest, "rev-parse", "-q", "--verify",
                                                                     "origin/main")
            else git(dest, "rev-parse", "HEAD"))
    old_ref = read_lock(dest, base, c.lock).get("ref", "")
    git(dest, "checkout", "-q", "-B", BRANCH, base)
    run(["bash", str(SYNC), c.name, "--cis", str(cis), "--dest-repo", str(dest), "--ref", ref, "--no-fetch"])
    mode = "dry run" if args.dry_run else "live"

    if not changed_paths(dest, c.dest):
        git(dest, "reset", "-q", "--hard", base)
        git(dest, "clean", "-fdq", "--", c.dest)
        result = f"no-op: `{args.repo}` main already has the core files of `{short}`"
        if not args.dry_run and not args.offline:
            pr = find_pr(args.repo)
            if pr:
                gh("pr", "close", str(pr["number"]), "--repo", f"{OWNER}/{args.repo}", "--delete-branch", "--comment",
                   f"Superseded: `main` already carries the AI gateway core of AA-CIS-App `{short}`.")
                result += f"; closed superseded PR #{pr['number']}"
        print(f"llm-core-sync-bot: {result} ({mode})")
        write_summary(f"### {args.repo}\n\n{result} ({mode}).")
        return 0

    git(dest, "add", "-A", "--", c.dest, c.lock)
    title = f"chore: sync AI gateway core aa_llm to AA-CIS-App {short}"
    run(["git", "-C", str(dest), "-c", f"user.name={args.author_name}", "-c", f"user.email={args.author_email}",
         "commit", "-q", "-m", f"{title}\n\nRefs AA-642"])
    names = {"A": "added", "M": "changed", "D": "removed"}
    diff = git(dest, "diff", "--name-status", "--no-renames", base, "HEAD", "--", c.dest, c.lock)
    files = [(names.get(line[0], line[0]), line.split("\t", 1)[1]) for line in diff.splitlines() if line]
    body = pr_body(c, ref, old_ref, files, core_log(cis, old_ref, ref), args.run_url)

    remote = ""
    if git_ok(dest, "remote", "get-url", "origin"):
        remote = git(dest, "ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}").split("\t")[0]
    same_tree = False
    if remote:
        git(dest, "fetch", "-q", "origin", f"refs/heads/{BRANCH}")
        same_tree = git(dest, "rev-parse", "FETCH_HEAD^{tree}") == git(dest, "rev-parse", "HEAD^{tree}")

    if args.dry_run:
        push = "unchanged (no push)" if same_tree else f"would force-push `{BRANCH}`"
        result = f"dry run: {push}; would open or update PR \"{title}\""
        print(f"llm-core-sync-bot: {result}")
        print(body)
    else:
        pr = None if args.offline else find_pr(args.repo)
        if same_tree:
            push = f"`{BRANCH}` already has this tree (no push, no CI rerun)"
        else:
            push, pr = push_branch(dest, args.repo, base, remote, pr, args.offline, short)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write(body)
            body_file = fh.name
        try:
            if args.offline:
                action = "PR step skipped (--offline)"
            elif pr:
                gh("pr", "edit", str(pr["number"]), "--repo", f"{OWNER}/{args.repo}", "--title", title,
                   "--body-file", body_file)
                action = f"updated PR #{pr['number']}"
            else:
                url = gh("pr", "create", "--repo", f"{OWNER}/{args.repo}", "--base", "main", "--head", BRANCH,
                         "--title", title, "--body-file", body_file)
                action = f"opened PR {url}"
        finally:
            os.unlink(body_file)
        result = f"{push}; {action}"
        print(f"llm-core-sync-bot: {result}")
    write_summary(f"### {args.repo}\n\n{result} ({mode}).")
    return 0


# --- CLI -----------------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cis", default=None, help="AA-CIS-App checkout (default: the repo of this script)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan")
    sp.add_argument("--base", default="")
    sp.add_argument("--head", default="HEAD")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--also", action="append", default=[], help="another path that counts as a core change")

    ss = sub.add_parser("sync")
    ss.add_argument("--repo", required=True)
    ss.add_argument("--dest", required=True)
    ss.add_argument("--ref", required=True)
    ss.add_argument("--dry-run", action="store_true")
    ss.add_argument("--offline", action="store_true", help="push, but never call gh (tests)")
    ss.add_argument("--run-url", default="")
    ss.add_argument("--author-name", default=BOT_NAME)
    ss.add_argument("--author-email", default=BOT_EMAIL)

    args = p.parse_args(argv)
    args.cis = args.cis or str(default_cis())
    try:
        return {"plan": cmd_plan, "sync": cmd_sync}[args.cmd](args)
    except BotError as exc:
        print(f"llm-core-sync-bot: {exc}", file=sys.stderr)
        write_summary(f"### llm-core-sync-bot failed\n\n```\n{exc}\n```")
        return 1


if __name__ == "__main__":
    sys.exit(main())
