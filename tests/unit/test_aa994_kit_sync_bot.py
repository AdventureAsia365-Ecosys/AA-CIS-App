"""AA-994 S3s (root ADR 0016 decisions 5 and 6) — the UI kit sync bot and its workflows.

The bot (.github/scripts/kit_sync_bot.py) and the copy (.github/scripts/kit-sync.sh) run for real against throwaway git
repos in tmp_path: a fake AA-CIS-App with a small kit core and MANIFEST.json, and consumer repos with a bare `origin`.
No network and no `gh`: --dry-run never calls it, --offline skips the PR step but pushes to the local bare origin.

- kit-sync.sh: the consumer table is ADR 0016's, the lock has the format the consumers check, and its fixture suite
  (.github/scripts/test_kit_sync.sh) passes.
- `plan` is unchanged (exit 0, `changed=false`) when a commit touches no core file, changed when it does or when there
  is no base commit.
- `sync` on a consumer that already has the core is a green no-op that leaves no branch behind (dry run and live);
  after a core change it commits the copy on `bot/kit-sync`, a dry run pushes nothing, a live run pushes once and a
  rerun with the same tree does not push again.
- A live sync never rebuilds `bot/kit-sync` in place on GitHub (the App token has no Workflows permission): a stale
  branch without an open PR is deleted and recreated at consumer main through the refs API; when an open PR holds it
  and GitHub refuses the force-push, the PR is closed, the branch recreated and a new PR opened; a refusal after that
  is a clear error. Tested with a fake `gh` and a pre-receive hook that refuses like GitHub.
- A tokens-profile consumer (TripPlanner) is a no-op when only a non-token file (and so MANIFEST.json) changed.
- A first-time core file is called out in the PR body (lesson S256); the body carries `Refs AA-994`.
- `report` prints how many core commits each consumer is behind.
- The workflows: the sync job is main-only, in the `release` environment, mints a token narrowed to one consumer repo
  with Contents + Pull requests only; the PR check reads no secret and has a stable "Kit consumers" result job that is
  green when the plan says unchanged.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BOT = REPO / ".github" / "scripts" / "kit_sync_bot.py"
KIT_SYNC = REPO / ".github" / "scripts" / "kit-sync.sh"
SYNC_WF = REPO / ".github" / "workflows" / "kit-sync.yml"
CONSUMERS_WF = REPO / ".github" / "workflows" / "kit-consumers.yml"
CORE = "frontend/app/_kit/core"
TOKENS = ["tokens.css", "tokens.ts", "brand.ts", "theme.ts", "check-kit-copy.mjs", "eslint-kit.mjs", "MANIFEST.json"]

pytestmark = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("git")), reason="needs bash and git")

GIT_CFG = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
           "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always"]


def g(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_CFG, "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def bot(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    full_env.pop("GITHUB_STEP_SUMMARY", None)
    full_env.pop("GITHUB_OUTPUT", None)
    full_env.update(env or {})
    return subprocess.run([sys.executable, str(BOT), *args], capture_output=True, text=True, env=full_env)


def write_core(cis: Path, files: dict[str, str]) -> None:
    core = cis / CORE
    if core.exists():
        shutil.rmtree(core)
    core.mkdir(parents=True)
    hashes = {}
    for name, text in files.items():
        (core / name).write_text(text)
        hashes[name] = {"sha256": hashlib.sha256(text.encode()).hexdigest()}
    manifest = {"adr": "root ADR 0016", "profiles": {"tokens": TOKENS}, "files": dict(sorted(hashes.items()))}
    (core / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")


BASE_FILES = {name: f"// {name} v1\n" for name in TOKENS if name != "MANIFEST.json"}
BASE_FILES["DataTable.tsx"] = "export const DataTable = 1;\n"


def commit_all(repo: Path, msg: str) -> str:
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", msg)
    return g(repo, "rev-parse", "HEAD")


@pytest.fixture()
def cis(tmp_path: Path) -> Path:
    repo = tmp_path / "cis"
    repo.mkdir()
    g(repo, "init", "-q")
    (repo / "README.md").write_text("fake CIS\n")
    write_core(repo, BASE_FILES)
    commit_all(repo, "feat: kit core v1")
    return repo


def make_consumer(tmp_path: Path, cis: Path, repo_name: str, names: list[str]) -> Path:
    """A consumer repo whose main already carries core at the CIS HEAD, with a bare origin."""
    origin = tmp_path / f"{repo_name}.git"
    subprocess.run(["git", *GIT_CFG, "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / repo_name
    subprocess.run(["git", *GIT_CFG, "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text(f"{repo_name}\n")
    ref = g(cis, "rev-parse", "HEAD")
    for name in names:
        subprocess.run(["bash", str(KIT_SYNC), name, "--cis", str(cis), "--dest-repo", str(work), "--ref", ref,
                        "--no-fetch"], check=True, capture_output=True)
    commit_all(work, "chore: initial kit sync")
    g(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    g(work, "fetch", "-q", "origin")
    return work


def remote_branch(consumer: Path) -> str:
    return g(consumer, "ls-remote", "--heads", "origin", "refs/heads/bot/kit-sync")


# --- kit-sync.sh --------------------------------------------------------------------------------------------------

def test_consumer_table_matches_adr_0016():
    out = subprocess.run(["bash", str(KIT_SYNC), "--list"], capture_output=True, text=True, check=True).stdout
    rows = [line.split("\t") for line in out.splitlines()]
    assert rows == [
        ["portal", "AA-Partner", "portal/app/_kit/core", "portal/kit.lock.json", "full"],
        ["crm-web", "AA-Partner", "web/app/_kit/core", "web/kit.lock.json", "full"],
        ["booking", "AA-Booking", "frontend/ui/core", "frontend/kit.lock.json", "full"],
        ["tripplanner", "AA-TripPlanner-Web", "frontend/ui/core", "frontend/kit.lock.json", "tokens"],
    ]


def test_kit_sync_writes_the_lock_format_the_consumers_check(tmp_path, cis):
    work = make_consumer(tmp_path, cis, "AA-Booking", ["booking"])
    lock = json.loads((work / "frontend" / "kit.lock.json").read_text())
    assert lock == {
        "adr": "root ADR 0016",
        "canonical": {"repo": "AdventureAsia365-Ecosys/AA-CIS-App", "path": CORE},
        "ref": g(cis, "rev-parse", "HEAD"),
        "profile": "full",
        "dest": "frontend/ui/core",
        "manifest_sha256": hashlib.sha256((cis / CORE / "MANIFEST.json").read_bytes()).hexdigest(),
    }
    assert sorted(p.name for p in (work / "frontend/ui/core").iterdir()) == sorted([*BASE_FILES, "MANIFEST.json"])


def test_kit_sync_fixture_suite():
    """The copy script's own fixture suite (moved from the root repo with the script): profiles, stale files, dry run,
    --from-local, stale manifest refused, --ref, no core yet."""
    res = subprocess.run(["bash", str(REPO / ".github" / "scripts" / "test_kit_sync.sh")], capture_output=True,
                         text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "fail=0" in res.stdout


# --- plan ---------------------------------------------------------------------------------------------------------

def test_plan_unchanged_when_no_core_file_changed(tmp_path, cis):
    base = g(cis, "rev-parse", "HEAD")
    (cis / "README.md").write_text("docs only\n")
    head = commit_all(cis, "docs: readme")
    out_file = tmp_path / "out.txt"
    res = bot("--cis", str(cis), "plan", "--base", base, "--head", head, env={"GITHUB_OUTPUT": str(out_file)})
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["changed"] is False
    outputs = dict(line.split("=", 1) for line in out_file.read_text().splitlines())
    assert outputs["changed"] == "false"
    assert json.loads(outputs["repos"]) == ["AA-Booking", "AA-Partner", "AA-TripPlanner-Web"]
    assert {c["name"]: c["app_dir"] for c in json.loads(outputs["consumers"])} == {
        "portal": "portal", "crm-web": "web", "booking": "frontend", "tripplanner": "frontend"}


def test_plan_changed_on_core_change_or_without_base(cis):
    base = g(cis, "rev-parse", "HEAD")
    write_core(cis, {**BASE_FILES, "DataTable.tsx": "export const DataTable = 2;\n"})
    head = commit_all(cis, "feat: kit v2")
    assert json.loads(bot("--cis", str(cis), "plan", "--base", base, "--head", head).stdout)["changed"] is True
    assert json.loads(bot("--cis", str(cis), "plan", "--base", "0" * 40, "--head", head).stdout)["changed"] is True
    assert json.loads(bot("--cis", str(cis), "plan", "--base", "", "--head", head).stdout)["changed"] is True


def test_plan_also_paths_count_as_a_change(cis):
    base = g(cis, "rev-parse", "HEAD")
    (cis / "tool.sh").write_text("echo\n")
    head = commit_all(cis, "chore: tool")
    res = bot("--cis", str(cis), "plan", "--base", base, "--head", head, "--also", "tool.sh")
    assert json.loads(res.stdout)["changed"] is True


# --- sync ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("dry", [True, False])
def test_sync_is_a_green_noop_when_the_consumer_has_the_core(tmp_path, cis, dry):
    work = make_consumer(tmp_path, cis, "AA-Partner", ["portal", "crm-web"])
    main_before = g(work, "rev-parse", "origin/main")
    args = ["--cis", str(cis), "sync", "--repo", "AA-Partner", "--dest", str(work), "--ref", "HEAD", "--offline"]
    res = bot(*args, *(["--dry-run"] if dry else []))
    assert res.returncode == 0, res.stderr
    assert "no-op" in res.stdout
    assert remote_branch(work) == ""
    assert g(work, "status", "--porcelain") == ""
    assert g(work, "rev-parse", "origin/main") == main_before


def test_sync_dry_run_commits_locally_and_pushes_nothing(tmp_path, cis):
    work = make_consumer(tmp_path, cis, "AA-Partner", ["portal", "crm-web"])
    write_core(cis, {**BASE_FILES, "DataTable.tsx": "export const DataTable = 2;\n"})
    new = commit_all(cis, "feat: compact DataTable")
    res = bot("--cis", str(cis), "sync", "--repo", "AA-Partner", "--dest", str(work), "--ref", new, "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "would force-push" in res.stdout
    assert f"chore: sync UI kit core to AA-CIS-App {new[:7]}" in res.stdout
    assert "Refs AA-994" in res.stdout
    assert "feat: compact DataTable" in res.stdout  # the core log since the previous lock ref
    assert remote_branch(work) == ""
    assert g(work, "rev-parse", "--abbrev-ref", "HEAD") == "bot/kit-sync"
    for lock in ("portal/kit.lock.json", "web/kit.lock.json"):
        assert json.loads(g(work, "show", f"HEAD:{lock}"))["ref"] == new
    assert g(work, "log", "-1", "--format=%an|%B", "HEAD").startswith("aa-ecosys-ci-bot[bot]|chore: sync UI kit core")


def test_sync_live_pushes_once_and_not_again_for_the_same_tree(tmp_path, cis):
    work = make_consumer(tmp_path, cis, "AA-Booking", ["booking"])
    write_core(cis, {**BASE_FILES, "DataTable.tsx": "export const DataTable = 2;\n"})
    new = commit_all(cis, "feat: kit v2")
    args = ["--cis", str(cis), "sync", "--repo", "AA-Booking", "--dest", str(work), "--ref", new, "--offline"]
    first = bot(*args)
    assert first.returncode == 0, first.stderr
    assert "force-pushed" in first.stdout
    pushed = remote_branch(work).split("\t")[0]
    assert pushed
    g(work, "checkout", "-q", "main")
    second = bot(*args)
    assert second.returncode == 0, second.stderr
    assert "already has this tree" in second.stdout
    assert remote_branch(work).split("\t")[0] == pushed


REFUSAL = ("refusing to allow a GitHub App to create or update workflow `.github/workflows/ci.yml` without "
           "`workflows` permission")
PRE_RECEIVE = f"""#!/bin/sh
# Refuses like GitHub does for a token without the Workflows permission: a non-fast-forward update of bot/kit-sync
# (it would carry main's workflow changes), or every update of it when the file refuse-all exists.
zero=0000000000000000000000000000000000000000
while read old new ref; do
  [ "$ref" = refs/heads/bot/kit-sync ] || continue
  if [ -f refuse-all ]; then echo "{REFUSAL}" >&2; exit 1; fi
  if [ "$old" != "$zero" ] && ! git merge-base --is-ancestor "$old" "$new"; then echo "{REFUSAL}" >&2; exit 1; fi
done
"""


def load_bot(monkeypatch):
    name = "kit_sync_bot_under_test"
    spec = importlib.util.spec_from_file_location(name, BOT)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)  # @dataclass looks its module up in sys.modules
    spec.loader.exec_module(mod)
    return mod


class FakeGh:
    """`gh` for the push path: PR list/create/edit/close and the git refs API, applied to the bare origin."""

    def __init__(self, origin: Path, prs: list[dict]):
        self.origin, self.prs, self.calls = origin, prs, []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        ref = "refs/heads/bot/kit-sync"
        if args[:2] == ("pr", "list"):
            return json.dumps(self.prs)
        if args[:3] == ("api", "-X", "DELETE"):
            subprocess.run(["git", "--git-dir", str(self.origin), "update-ref", "-d", ref], check=True)
            return ""
        if args[:3] == ("api", "-X", "POST"):
            sha = next(a[4:] for a in args if a.startswith("sha="))
            subprocess.run(["git", "--git-dir", str(self.origin), "update-ref", ref, sha], check=True)
            return ""
        if args[:2] == ("pr", "close"):
            self.prs.clear()
            return ""
        if args[:2] == ("pr", "create"):
            return "https://github.com/AdventureAsia365-Ecosys/AA-Booking/pull/7"
        if args[:2] == ("pr", "edit"):
            return ""
        raise AssertionError(f"unexpected gh call {args}")

    def kinds(self) -> list[str]:
        return [" ".join(a[:3]) if a[0] == "api" else " ".join(a[:2]) for a in self.calls]


@pytest.fixture()
def stale_branch(tmp_path, cis):
    """AA-Booking with an old bot/kit-sync, then a main commit that changes a workflow file and a new core commit."""
    work = make_consumer(tmp_path, cis, "AA-Booking", ["booking"])
    write_core(cis, {**BASE_FILES, "DataTable.tsx": "export const DataTable = 2;\n"})
    v2 = commit_all(cis, "feat: kit v2")
    args = ["--cis", str(cis), "sync", "--repo", "AA-Booking", "--dest", str(work), "--ref", v2, "--offline"]
    assert bot(*args).returncode == 0
    g(work, "checkout", "-q", "main")
    (work / ".github" / "workflows").mkdir(parents=True)
    (work / ".github" / "workflows" / "ci.yml").write_text("name: CI\n")
    main = commit_all(work, "ci: a workflow change on main")
    g(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    write_core(cis, {**BASE_FILES, "DataTable.tsx": "export const DataTable = 3;\n"})
    v3 = commit_all(cis, "feat: kit v3")
    origin = tmp_path / "AA-Booking.git"
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(PRE_RECEIVE)
    hook.chmod(0o755)
    return work, origin, main, v3


def run_live(monkeypatch, cis: Path, work: Path, ref: str, fake: FakeGh) -> int:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    mod = load_bot(monkeypatch)
    monkeypatch.setattr(mod, "gh", fake)
    return mod.main(["--cis", str(cis), "sync", "--repo", "AA-Booking", "--dest", str(work), "--ref", ref])


def test_stale_branch_without_pr_is_recreated_from_main(stale_branch, cis, monkeypatch, capsys):
    work, origin, main, v3 = stale_branch
    fake = FakeGh(origin, [])
    assert run_live(monkeypatch, cis, work, v3, fake) == 0, capsys.readouterr().err
    assert fake.kinds() == ["pr list", "api -X DELETE", "api -X POST", "pr create"]
    tip = remote_branch(work).split("\t")[0]
    assert g(work, "rev-parse", f"{tip}^") == main  # one sync commit on top of current main
    assert "deleted the stale `bot/kit-sync`" in capsys.readouterr().out


def test_refused_push_with_open_pr_closes_it_and_opens_a_new_one(stale_branch, cis, monkeypatch, capsys):
    work, origin, main, v3 = stale_branch
    fake = FakeGh(origin, [{"number": 5, "url": "https://example.invalid/pull/5"}])
    assert run_live(monkeypatch, cis, work, v3, fake) == 0, capsys.readouterr().err
    assert fake.kinds() == ["pr list", "pr close", "api -X DELETE", "api -X POST", "pr create"]
    close = next(a for a in fake.calls if a[:2] == ("pr", "close"))
    assert close[2] == "5" and "--delete-branch" not in close
    tip = remote_branch(work).split("\t")[0]
    assert g(work, "rev-parse", f"{tip}^") == main
    assert "closed PR #5" in capsys.readouterr().out


@pytest.mark.parametrize("prs", [[], [{"number": 5, "url": "https://example.invalid/pull/5"}]])
def test_push_refused_after_recreate_is_a_clear_error(stale_branch, cis, monkeypatch, capsys, prs):
    work, origin, main, v3 = stale_branch
    (origin / "refuse-all").write_text("")
    assert run_live(monkeypatch, cis, work, v3, FakeGh(origin, prs)) == 1
    err = capsys.readouterr().err
    assert "GitHub refused the push of `bot/kit-sync` to AA-Booking" in err
    if prs:
        assert "freshly cut from main" in err and "delete `bot/kit-sync` in AA-Booking by hand" in err


def test_tokens_consumer_ignores_a_manifest_only_change(tmp_path, cis):
    work = make_consumer(tmp_path, cis, "AA-TripPlanner-Web", ["tripplanner"])
    write_core(cis, {**BASE_FILES, "DataTable.tsx": "export const DataTable = 2;\n"})
    new = commit_all(cis, "feat: kit v2")
    res = bot("--cis", str(cis), "sync", "--repo", "AA-TripPlanner-Web", "--dest", str(work), "--ref", new,
              "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "no-op" in res.stdout
    write_core(cis, {**BASE_FILES, "DataTable.tsx": "export const DataTable = 2;\n", "tokens.css": "/* v2 */\n"})
    new2 = commit_all(cis, "feat: tokens v2")
    res = bot("--cis", str(cis), "sync", "--repo", "AA-TripPlanner-Web", "--dest", str(work), "--ref", new2,
              "--dry-run")
    assert "would force-push" in res.stdout


def test_first_time_core_file_is_called_out(tmp_path, cis):
    work = make_consumer(tmp_path, cis, "AA-Booking", ["booking"])
    files = {k: v for k, v in BASE_FILES.items() if k != "DataTable.tsx"}
    write_core(cis, {**files, "FormControls.tsx": "export const Field = 1;\n"})
    new = commit_all(cis, "feat: form controls, DataTable moved out")
    res = bot("--cis", str(cis), "sync", "--repo", "AA-Booking", "--dest", str(work), "--ref", new, "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "First-time core files in this app: `frontend/ui/core/FormControls.tsx`" in res.stdout
    assert "| booking | removed | `frontend/ui/core/DataTable.tsx` |" in res.stdout


def test_sync_writes_the_step_summary(tmp_path, cis):
    work = make_consumer(tmp_path, cis, "AA-Booking", ["booking"])
    summary = tmp_path / "summary.md"
    res = bot("--cis", str(cis), "sync", "--repo", "AA-Booking", "--dest", str(work), "--ref", "HEAD", "--dry-run",
              env={"GITHUB_STEP_SUMMARY": str(summary)})
    assert res.returncode == 0, res.stderr
    text = summary.read_text()
    assert "### AA-Booking" in text and "| booking |" in text and "| 0 |" in text


def test_sync_unknown_repo_fails_cleanly(tmp_path, cis):
    res = bot("--cis", str(cis), "sync", "--repo", "AA-CIS-Infra", "--dest", str(tmp_path), "--ref", "HEAD",
              "--dry-run")
    assert res.returncode == 1
    assert "no kit consumer lives in AA-CIS-Infra" in res.stderr


# --- report -------------------------------------------------------------------------------------------------------

def test_report_counts_core_commits_behind(tmp_path, cis):
    partner = make_consumer(tmp_path, cis, "AA-Partner", ["portal", "crm-web"])
    booking = make_consumer(tmp_path, cis, "AA-Booking", ["booking"])
    tp = make_consumer(tmp_path, cis, "AA-TripPlanner-Web", ["tripplanner"])
    write_core(cis, {**BASE_FILES, "DataTable.tsx": "export const DataTable = 2;\n"})
    commit_all(cis, "feat: kit v2")
    (cis / "README.md").write_text("docs\n")
    commit_all(cis, "docs: not a kit commit")
    res = bot("--cis", str(cis), "report", "--offline", "--checkout", f"AA-Partner={partner}",
              "--checkout", f"AA-Booking={booking}", "--checkout", f"AA-TripPlanner-Web={tp}")
    assert res.returncode == 0, res.stderr
    rows = {line.split("|")[1].strip(): [c.strip() for c in line.split("|")[2:-1]]
            for line in res.stdout.splitlines()[2:]}
    assert rows["portal"][2] == "1" and rows["crm-web"][2] == "1" and rows["booking"][2] == "1"
    assert rows["tripplanner"][2] == "0"  # DataTable is not in the tokens profile


# --- workflows ----------------------------------------------------------------------------------------------------

def _wf(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    data["on"] = data.get("on", data.get(True))
    return data


def test_sync_workflow_token_is_main_only_and_narrowed_to_one_consumer():
    wf = _wf(SYNC_WF)
    assert set(wf["on"]) == {"push", "schedule", "workflow_dispatch"}
    assert wf["on"]["push"] == {"branches": ["main"], "paths": [f"{CORE}/**"]}
    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"]["cancel-in-progress"] is False
    # Push runs (and live manual runs) have their own group, so a report or dry run never replaces a pending push run.
    group = wf["concurrency"]["group"]
    assert "'kit-sync-push'" in group and "'kit-sync-report'" in group
    assert "github.event_name == 'push'" in group and "!inputs.dry_run" in group
    assert wf["on"]["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    sync = wf["jobs"]["sync"]
    assert sync["environment"] == "release"
    assert "github.ref == 'refs/heads/main'" in sync["if"]
    mint = next(s for s in sync["steps"] if "create-github-app-token" in s.get("uses", ""))
    assert mint["with"]["repositories"] == "${{ matrix.repo }}"
    perms = {k: v for k, v in mint["with"].items() if k.startswith("permission-")}
    assert perms == {"permission-contents": "write", "permission-pull-requests": "write"}
    text = SYNC_WF.read_text()
    assert "AA-CIS-Infra" not in text.replace("covers AA-CIS-Infra", "")
    assert "secrets." in text and text.count("secrets.") == 1  # only the App key, only in the sync job


def test_consumers_workflow_reads_no_secret_and_has_a_stable_result_job():
    wf = _wf(CONSUMERS_WF)
    assert set(wf["on"]) == {"pull_request"}
    assert wf["permissions"] == {"contents": "read"}
    text = CONSUMERS_WF.read_text()
    assert "secrets." not in text and "create-github-app-token" not in text
    assert "environment" not in text
    result = wf["jobs"]["kit-consumers"]
    assert result["name"] == "Kit consumers"
    assert result["if"] == "always()"
    assert wf["jobs"]["consumer"]["if"] == "needs.plan.outputs.changed == 'true'"
    assert wf["jobs"]["consumer"]["timeout-minutes"] <= 30


def test_consumers_workflow_runs_each_consumer_unit_tests(tmp_path):
    """Lesson S256: a first-time core file trips a consumer's own guard tests, so every leg runs them, time-bounded."""
    steps = _wf(CONSUMERS_WF)["jobs"]["consumer"]["steps"]
    step = next(s for s in steps if s["name"].startswith("Consumer unit tests"))
    assert step["if"] == "${{ !cancelled() }}" and step["timeout-minutes"] <= 10
    script = step["run"]

    def pick(scripts: dict) -> str:
        app = tmp_path / "app"
        app.mkdir(exist_ok=True)
        (app / "package.json").write_text(json.dumps({"scripts": scripts}))
        line = next(line for line in script.splitlines() if line.strip().startswith("script="))
        return subprocess.run(["bash", "-c", line + '; printf %s "$script"'], cwd=app, capture_output=True,
                              text=True, check=True).stdout

    if not shutil.which("node"):
        pytest.skip("needs node")
    assert pick({"test:unit": "node --test", "test:layout": "playwright test"}) == "test:unit"  # Booking
    assert pick({"test": "node --test lib/*.test.ts"}) == "test"  # TripPlanner, portal
    assert pick({"lint": "eslint"}) == ""


def test_result_job_is_green_when_core_unchanged(tmp_path):
    """Runs the result step's shell with the values GitHub passes when the plan says unchanged (legs skipped)."""
    step = _wf(CONSUMERS_WF)["jobs"]["kit-consumers"]["steps"][0]
    summary = tmp_path / "summary.md"

    def run_step(plan: str, changed: str, legs: str) -> int:
        env = {**os.environ, "PLAN": plan, "CHANGED": changed, "LEGS": legs, "GITHUB_STEP_SUMMARY": str(summary)}
        return subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True).returncode

    assert run_step("success", "false", "skipped") == 0
    assert "unchanged" in summary.read_text()
    assert run_step("success", "true", "success") == 0
    assert run_step("success", "true", "failure") == 1
    assert run_step("failure", "", "skipped") == 1
