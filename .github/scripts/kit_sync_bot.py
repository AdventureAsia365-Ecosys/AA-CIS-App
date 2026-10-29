#!/usr/bin/env python3
"""AA-994 S3s (root ADR 0016 decisions 5 and 6, plan guards G14 and G17): the UI kit sync bot.

Drives `.github/scripts/kit-sync.sh` (the copy itself, the same code agents run by hand) for the workflows
`kit-sync.yml` and `kit-consumers.yml`. Standard library only; git and, outside --dry-run / --offline, the `gh` CLI.

Subcommands:
  plan    --base <sha> --head <sha> [--force] [--also <path>]...
          Did `frontend/app/_kit/core/**` (or an --also path) change between the two commits? Prints JSON and writes
          `changed`,
          `repos` and `consumers` (JSON) to $GITHUB_OUTPUT. An empty or all-zero base counts as changed (the sync
          is idempotent, so the cost of a wrong "changed" is one no-op run).
  sync    --repo <consumer repo> --dest <checkout> --ref <CIS sha> [--dry-run] [--run-url <url>]
          In the consumer checkout (on its `main`): regenerate branch `bot/kit-sync` from `origin/main`, copy core
          at <ref> into every consumer of that repo (AA-Partner: portal and crm-web, one PR), commit, force-push the
          branch only when its tree changed and open or update the one PR `chore: sync UI kit core to AA-CIS-App
          <sha7>`. No-op (exit 0) when no copied file other than MANIFEST.json changes: the consumer already has
          these bytes; an open sync PR is then closed as superseded. --dry-run does everything up to the local
          commit and prints the PR it would open; it never pushes and never calls `gh`.
          Workflow files: the App token has no Workflows permission, and GitHub refuses a push whose ref update
          adds or changes a file under .github/workflows/. A force-push of a rebuilt branch onto an old
          `bot/kit-sync` carries every workflow change consumer main made since that old branch, so the branch is
          never rebuilt in place: a remote `bot/kit-sync` that is not the head of an open PR is deleted first, a
          missing branch is created at consumer main through the git refs API, and the push then only adds the
          sync commit. When an open PR holds the branch and GitHub still refuses the force-push, that PR is closed
          as superseded, the branch is recreated the same way and a new PR is opened. A refusal after that is a
          clear error (exit 1).
  report  [--head <CIS ref>] [--checkout <repo>=<path>]... [--offline]
          Freshness (report only, never a gate): consumer | lock ref on main | CIS head | core commits behind |
          open sync PR. Locks are read from the consumers' `main` on GitHub (public repos) unless --checkout gives a
          local checkout; --offline skips the PR lookup. Always exits 0.

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
CANON_PATH = "frontend/app/_kit/core"
MANIFEST = "MANIFEST.json"
BRANCH = "bot/kit-sync"
ZERO = "0" * 40
HERE = Path(__file__).resolve().parent
KIT_SYNC = HERE / "kit-sync.sh"
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
    profile: str

    @property
    def app_dir(self) -> str:
        return str(Path(self.lock).parent)


def run(cmd: list[str], cwd: Path | str | None = None, check: bool = True, env: dict | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
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
    """The consumer table, read from kit-sync.sh (its only copy)."""
    out = run(["bash", str(KIT_SYNC), "--list"])
    rows = [line.split("\t") for line in out.splitlines() if line.strip()]
    return [Consumer(*row) for row in rows]


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
        return True, f"{len(files.splitlines())} kit file(s) changed"
    return False, f"no file under {CANON_PATH} changed between {base[:12]} and {head[:12]}"


def cmd_plan(args: argparse.Namespace) -> int:
    cis = Path(args.cis)
    head = git(cis, "rev-parse", f"{args.head}^{{commit}}")
    if args.force:
        changed, why = True, "forced"
    else:
        changed, why = core_changed(cis, args.base, head, tuple(args.also))
    table = consumers()
    repos = sorted({c.repo for c in table})
    plan = {
        "changed": changed,
        "reason": why,
        "head": head,
        "repos": repos,
        "consumers": [
            {"name": c.name, "repo": c.repo, "dest": c.dest, "lock": c.lock, "profile": c.profile,
             "app_dir": c.app_dir}
            for c in table
        ],
    }
    print(json.dumps(plan, indent=2))
    write_output(
        changed="true" if changed else "false",
        repos=json.dumps(repos),
        consumers=json.dumps(plan["consumers"]),
    )
    write_summary(f"### Kit core change\n\n{'Changed' if changed else 'Unchanged'}: {why}.")
    return 0


# --- shared helpers --------------------------------------------------------------------------------------------------

def read_lock(repo: Path, rev: str, lock: str) -> dict:
    text = git(repo, "show", f"{rev}:{lock}", check=False)
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


def profile_paths(cis: Path, head: str, profile: str) -> list[str]:
    """CIS paths whose commits matter to a consumer of this profile (MANIFEST.json alone never does)."""
    if profile != "tokens":
        return [CANON_PATH]
    try:
        manifest = json.loads(git(cis, "show", f"{head}:{CANON_PATH}/{MANIFEST}"))
        files = (manifest.get("profiles") or {}).get("tokens") or []
    except (BotError, ValueError):
        files = []
    files = [f for f in files if f != MANIFEST] or ["tokens.css", "tokens.ts", "brand.ts", "theme.ts"]
    return [f"{CANON_PATH}/{f}" for f in files]


def behind(cis: Path, lock_ref: str, head: str, profile: str) -> str:
    if not lock_ref:
        return "no lock"
    if lock_ref == "local":
        return "lock ref is `local` (not a CIS commit)"
    if not git_ok(cis, "cat-file", "-e", f"{lock_ref}^{{commit}}"):
        return f"unknown ({lock_ref[:7]} not in this checkout)"
    if not git_ok(cis, "merge-base", "--is-ancestor", lock_ref, head):
        return f"{lock_ref[:7]} is not on CIS main"
    count = git(cis, "rev-list", "--count", f"{lock_ref}..{head}", "--", *profile_paths(cis, head, profile))
    return count


def find_pr(repo: str) -> dict | None:
    out = gh("pr", "list", "--repo", f"{OWNER}/{repo}", "--head", BRANCH, "--state", "open",
             "--json", "number,url", "--limit", "1")
    items = json.loads(out or "[]")
    return items[0] if items else None


def is_workflow_refusal(message: str) -> bool:
    """GitHub's answer to a push that would add or change a workflow file without the Workflows permission."""
    text = message.lower()
    return "workflow" in text and ("refusing to allow" in text or "permission" in text)


def delete_branch(repo: str) -> None:
    gh("api", "-X", "DELETE", f"repos/{OWNER}/{repo}/git/refs/heads/{BRANCH}")


def create_branch(repo: str, sha: str) -> None:
    """Create `bot/kit-sync` at a commit consumer main already has, so the push after it only adds the sync commit."""
    gh("api", "-X", "POST", f"repos/{OWNER}/{repo}/git/refs", "-f", f"ref=refs/heads/{BRANCH}", "-f", f"sha={sha}")


def push_branch(dest: Path, repo: str, base: str, remote: str, pr: dict | None, offline: bool,
                short: str) -> tuple[str, dict | None]:
    """Push HEAD (consumer main + the sync commit) to `bot/kit-sync`; returns (what happened, the open PR or None).

    The branch is never rebuilt in place on GitHub (see the module docstring, "Workflow files"). --offline (tests)
    pushes straight to origin without the refs API.
    """
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
           f"Superseded: `{BRANCH}` is recreated from `main` for AA-CIS-App `{short}` (GitHub refused the update "
           "because `main` changed workflow files since this branch was cut; the sync token has no Workflows "
           "permission). A new sync PR follows (kit-sync.yml).")
        delete_branch(repo)
        create_branch(repo, base)
        notes.append(f"closed PR #{pr['number']} and recreated `{BRANCH}` at main `{base[:7]}` (workflow files "
                     "changed on main)")
        pr = None
        try:
            push()
        except BotError as exc2:
            raise BotError(
                f"GitHub refused the push of `{BRANCH}` to {repo} even onto a branch freshly cut from main "
                f"`{base[:7]}`: {exc2}. The sync commit should touch only the kit core and lock files; check the "
                f"diff, then delete `{BRANCH}` in {repo} by hand and re-run Kit Sync.") from exc2
    return "; ".join([*notes, f"force-pushed `{BRANCH}`"]), pr


def freshness_table(rows: list[tuple[str, str, str, str, str]]) -> str:
    lines = ["| Consumer | Lock ref (main) | CIS head | Core commits behind | Open sync PR |",
             "|---|---|---|---|---|"]
    lines += [f"| {' | '.join(r)} |" for r in rows]
    return "\n".join(lines)


# --- sync ----------------------------------------------------------------------------------------------------------

def changed_paths(dest: Path, path: str) -> list[str]:
    out = git(dest, "status", "--porcelain=v1", "--untracked-files=all", "--", path)
    return [line[3:].split(" -> ")[-1] for line in out.splitlines() if line.strip()]


def core_log(cis: Path, old_ref: str, ref: str) -> list[str]:
    if not old_ref or old_ref == "local" or not git_ok(cis, "cat-file", "-e", f"{old_ref}^{{commit}}"):
        return []
    out = git(cis, "log", "--no-merges", f"--max-count={LOG_LIMIT}", "--format=%h %s", f"{old_ref}..{ref}",
              "--", CANON_PATH)
    return out.splitlines()


def pr_body(repo: str, ref: str, group: list[Consumer], old: dict[str, dict], files: dict[str, list[tuple[str, str]]],
            logs: dict[str, list[str]], run_url: str) -> str:
    short = ref[:7]
    parts = [
        f"Automated sync of the UI kit core from AA-CIS-App [`{short}`](https://github.com/{CIS_REPO}/commit/{ref}) "
        f"(`{CANON_PATH}`, root ADR 0016 decision 6). Written by `kit-sync.yml` with `.github/scripts/kit-sync.sh`.",
        "",
        "| Consumer | Destination | Profile | Lock ref before | Lock ref after |",
        "|---|---|---|---|---|",
    ]
    for c in group:
        before = (old.get(c.name) or {}).get("ref") or "none"
        parts.append(f"| {c.name} | `{c.dest}` | {c.profile} | `{before[:7]}` | `{short}` |")
    parts += ["", "### Core commits since the previous sync", ""]
    for prev, lines in logs.items():
        if not lines:
            parts.append(f"- (no log: previous ref `{prev[:7] or 'none'}` is unknown here)")
        else:
            parts += [f"- {line}" for line in lines]
            if len(lines) >= LOG_LIMIT:
                parts.append(f"- (first {LOG_LIMIT} commits only)")
    parts += ["", "### Files", "", "| Consumer | Change | File |", "|---|---|---|"]
    new_files: list[str] = []
    for c in group:
        for status, path in files.get(c.name, []):
            parts.append(f"| {c.name} | {status} | `{path}` |")
            if status == "added" and not path.endswith(f"/{MANIFEST}"):
                new_files.append(f"`{path}`")
    if new_files:
        parts += [
            "",
            "> [!IMPORTANT]",
            "> First-time core files in this app: " + ", ".join(new_files) + ". Check this app's lint overrides "
            "(paths), its guards (issue IDs, account IDs) and its barrel for them before merging (lesson S256).",
        ]
    parts += [
        "",
        "### Merge",
        "",
        "Normal-tier PR (ADR 0011 section 4): merge through the merge queue once CI is green. Do not edit files in "
        "this branch: the copy is checked byte for byte (Kit core integrity / provenance). Fix the kit in "
        "AA-CIS-App; the next CIS merge regenerates this branch.",
        "",
        "Refs AA-994",
    ]
    if run_url:
        parts += ["", f"Opened by [kit-sync run]({run_url})."]
    return "\n".join(parts) + "\n"


def cmd_sync(args: argparse.Namespace) -> int:
    cis, dest = Path(args.cis), Path(args.dest)
    group = [c for c in consumers() if c.repo == args.repo]
    if not group:
        raise BotError(f"no kit consumer lives in {args.repo}")
    ref = git(cis, "rev-parse", f"{args.ref}^{{commit}}")
    short = ref[:7]
    base = git(dest, "rev-parse", "origin/main^{commit}") if git_ok(dest, "rev-parse", "-q", "--verify",
                                                                   "origin/main") else git(dest, "rev-parse", "HEAD")
    old = {c.name: read_lock(dest, base, c.lock) for c in group}
    git(dest, "checkout", "-q", "-B", BRANCH, base)

    for c in group:
        run(["bash", str(KIT_SYNC), c.name, "--cis", str(cis), "--dest-repo", str(dest), "--ref", ref, "--no-fetch"])

    meaningful = {c.name: [p for p in changed_paths(dest, c.dest) if Path(p).name != MANIFEST] for c in group}
    head_cis = git(cis, "rev-parse", "HEAD")
    pr = None
    mode = "dry run" if args.dry_run else "live"

    if not any(meaningful.values()):
        git(dest, "reset", "-q", "--hard", base)
        git(dest, "clean", "-fdq", "--", *[c.dest for c in group])
        result = f"no-op: `{args.repo}` main already has the core files of `{short}`"
        if not args.dry_run and not args.offline:
            pr = find_pr(args.repo)
            if pr:
                gh("pr", "close", str(pr["number"]), "--repo", f"{OWNER}/{args.repo}", "--delete-branch", "--comment",
                   f"Superseded: `main` already carries the UI kit core of AA-CIS-App `{short}` (kit-sync.yml).")
                result += f"; closed superseded PR #{pr['number']}"
                pr = None
        print(f"kit-sync-bot: {result} ({mode})")
        rows = [(c.name, f"`{(old[c.name].get('ref') or 'none')[:7]}`", f"`{head_cis[:7]}`",
                 behind(cis, old[c.name].get("ref", ""), head_cis, c.profile), "-") for c in group]
        write_summary(f"### {args.repo}\n\n{result} ({mode}).\n\n{freshness_table(rows)}")
        return 0

    paths = [p for c in group for p in (c.dest, c.lock)]
    git(dest, "add", "-A", "--", *paths)
    title = f"chore: sync UI kit core to AA-CIS-App {short}"
    run(["git", "-C", str(dest), "-c", f"user.name={args.author_name}", "-c", f"user.email={args.author_email}",
         "commit", "-q", "-m", f"{title}\n\nRefs AA-994"])

    files: dict[str, list[tuple[str, str]]] = {}
    names = {"A": "added", "M": "changed", "D": "removed"}
    for c in group:
        out = git(dest, "diff", "--name-status", "--no-renames", base, "HEAD", "--", c.dest)
        files[c.name] = [(names.get(line[0], line[0]), line.split("\t", 1)[1]) for line in out.splitlines() if line]
    logs: dict[str, list[str]] = {}
    for c in group:
        prev = old[c.name].get("ref", "")
        if prev not in logs:
            logs[prev] = core_log(cis, prev, ref)
    body = pr_body(args.repo, ref, group, old, files, logs, args.run_url)

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
        print(f"kit-sync-bot: {result}")
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
                pr = {"number": url.rstrip("/").rsplit("/", 1)[-1], "url": url}
                action = f"opened PR #{pr['number']}"
        finally:
            os.unlink(body_file)
        result = f"{push}; {action}"
        print(f"kit-sync-bot: {result}")

    pr_cell = f"#{pr['number']}" if pr else ("(dry run)" if args.dry_run else "-")
    rows = [(c.name, f"`{(old[c.name].get('ref') or 'none')[:7]}`", f"`{head_cis[:7]}`",
             behind(cis, old[c.name].get("ref", ""), head_cis, c.profile), pr_cell) for c in group]
    write_summary(f"### {args.repo}\n\n{result} ({mode}).\n\n{freshness_table(rows)}")
    return 0


# --- report --------------------------------------------------------------------------------------------------------

def remote_lock(repo: str, lock: str) -> dict:
    text = gh("api", f"repos/{OWNER}/{repo}/contents/{lock}?ref=main", "-H", "Accept: application/vnd.github.raw+json")
    return json.loads(text)


def cmd_report(args: argparse.Namespace) -> int:
    cis = Path(args.cis)
    checkouts = dict(item.split("=", 1) for item in args.checkout)
    head = git(cis, "rev-parse", f"{args.head}^{{commit}}")
    rows = []
    prs: dict[str, str] = {}
    for c in consumers():
        try:
            lock = (read_lock(Path(checkouts[c.repo]), "HEAD", c.lock) if c.repo in checkouts
                    else remote_lock(c.repo, c.lock))
            ref = lock.get("ref", "")
            lag = behind(cis, ref, head, c.profile)
        except (BotError, ValueError) as exc:
            ref, lag = "", f"error: {str(exc)[:120]}"
        if c.repo not in prs:
            if args.offline:
                prs[c.repo] = "n/a (offline)"
            else:
                try:
                    pr = find_pr(c.repo)
                    prs[c.repo] = f"[#{pr['number']}]({pr['url']})" if pr else "none"
                except (BotError, ValueError) as exc:
                    prs[c.repo] = f"error: {str(exc)[:80]}"
        rows.append((c.name, f"`{(ref or 'none')[:7]}`", f"`{head[:7]}`", lag, prs[c.repo]))
    table = freshness_table(rows)
    print(table)
    write_summary("### UI kit freshness (report only)\n\n" + table)
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
    sp.add_argument("--also", action="append", default=[], help="another path that counts as a kit change")

    ss = sub.add_parser("sync")
    ss.add_argument("--repo", required=True)
    ss.add_argument("--dest", required=True)
    ss.add_argument("--ref", required=True)
    ss.add_argument("--dry-run", action="store_true")
    ss.add_argument("--offline", action="store_true", help="push, but never call gh (tests)")
    ss.add_argument("--run-url", default="")
    ss.add_argument("--author-name", default=BOT_NAME)
    ss.add_argument("--author-email", default=BOT_EMAIL)

    sr = sub.add_parser("report")
    sr.add_argument("--head", default="HEAD")
    sr.add_argument("--checkout", action="append", default=[], metavar="REPO=PATH")
    sr.add_argument("--offline", action="store_true")

    args = p.parse_args(argv)
    args.cis = args.cis or str(default_cis())
    try:
        return {"plan": cmd_plan, "sync": cmd_sync, "report": cmd_report}[args.cmd](args)
    except BotError as exc:
        print(f"kit-sync-bot: {exc}", file=sys.stderr)
        write_summary(f"### kit-sync-bot failed\n\n```\n{exc}\n```")
        return 1


if __name__ == "__main__":
    sys.exit(main())
