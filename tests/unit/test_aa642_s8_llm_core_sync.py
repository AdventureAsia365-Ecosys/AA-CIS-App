"""AA-642 S8 (root ADR 0009 decision 13) — the AI gateway core sync (.github/scripts/llm-core-sync.sh), its bot
(.github/scripts/llm_core_sync_bot.py) and workflows.

The copy and the bot run for real against throwaway git repos in tmp_path: a fake AA-CIS-App holding a byte copy of
the real shared/aa_llm, and consumer repos with a bare `origin`. No network and no `gh` (--dry-run never calls it,
--offline pushes to the local bare origin and skips the PR step).

- The consumer table: TripPlanner backend/aa_llm and AA-Partner api/app/aa_llm, each with its lock.
- A sync copies the folder, writes a deterministic lock whose manifest_sha256 the copied check_copy.py accepts, and a
  rerun changes nothing; --dry-run writes nothing; --from-local records ref "local"; a stale manifest (an edited core
  file) is refused (exit 4) before anything is written; an unknown consumer is a usage error.
- `plan` is unchanged when a commit touches no core file and changed when it does.
- `sync` commits on bot/llm-core-sync, pushes once, does not push the same tree again, and is a no-op that leaves no
  branch behind when the consumer already has the core.
- The workflows: the sync job is main-only, in the `release` environment, with a token narrowed to one consumer repo
  (Contents + Pull requests); the consumers check reads no secret and has a stable result job.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SYNC = REPO / ".github" / "scripts" / "llm-core-sync.sh"
BOT = REPO / ".github" / "scripts" / "llm_core_sync_bot.py"
SYNC_WF = REPO / ".github" / "workflows" / "llm-core-sync.yml"
CONSUMERS_WF = REPO / ".github" / "workflows" / "llm-core-consumers.yml"
CORE = "shared/aa_llm"

pytestmark = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("git")), reason="needs bash and git")

GIT_CFG = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
           "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always"]


def g(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_CFG, "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def sync(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "AA_WORKSPACE"}
    return subprocess.run(["bash", str(SYNC), *args], capture_output=True, text=True, env=env)


def bot(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT")}
    return subprocess.run([sys.executable, str(BOT), *args], capture_output=True, text=True, env=env)


@pytest.fixture
def ws(tmp_path):
    """A workspace: apps/AA-CIS-App (git, core copied, origin/main set) and two consumer repos with bare origins."""
    cis = tmp_path / "apps" / "AA-CIS-App"
    (cis / "shared").mkdir(parents=True)
    shutil.copytree(REPO / CORE, cis / CORE, ignore=shutil.ignore_patterns("__pycache__", ".*"))
    g(tmp_path, "init", "-q", str(cis))
    g(cis, "add", "-A")
    g(cis, "commit", "-qm", "core")
    g(cis, "update-ref", "refs/remotes/origin/main", "HEAD")
    for repo, keep in (("AA-TripPlanner-Web", "backend/README"), ("AA-Partner", "api/README")):
        bare = tmp_path / "remotes" / f"{repo}.git"
        bare.parent.mkdir(exist_ok=True)
        g(tmp_path, "init", "-q", "--bare", str(bare))
        dest = tmp_path / "apps" / repo
        g(tmp_path, "clone", "-q", str(bare), str(dest))
        (dest / keep).parent.mkdir(parents=True)
        (dest / keep).write_text("app\n")
        g(dest, "add", "-A")
        g(dest, "commit", "-qm", "init")
        g(dest, "push", "-q", "origin", "HEAD:main")
        g(dest, "fetch", "-q", "origin")
    return tmp_path


def _apps(ws: Path, name: str) -> Path:
    return ws / "apps" / name


def test_list_names_both_consumers():
    out = sync("--list")
    assert out.returncode == 0, out.stderr
    rows = [line.split("\t") for line in out.stdout.splitlines()]
    assert rows == [["tripplanner", "AA-TripPlanner-Web", "backend/aa_llm", "backend/aa_llm.lock.json", "backend"],
                    ["crm", "AA-Partner", "api/app/aa_llm", "api/aa_llm.lock.json", "api"]]


def test_sync_copies_writes_the_lock_and_is_idempotent(ws):
    cis, tp = _apps(ws, "AA-CIS-App"), _apps(ws, "AA-TripPlanner-Web")
    out = sync("tripplanner", "--cis", str(cis), "--no-fetch")
    assert out.returncode == 0, out.stderr
    copy = tp / "backend" / "aa_llm"
    assert sorted(p.name for p in copy.iterdir()) == sorted(p.name for p in (cis / CORE).iterdir())
    lock = json.loads((tp / "backend" / "aa_llm.lock.json").read_text())
    assert lock["ref"] == g(cis, "rev-parse", "HEAD")
    assert lock["canonical"] == {"repo": "AdventureAsia365-Ecosys/AA-CIS-App", "path": CORE}
    assert lock["dest"] == "backend/aa_llm"
    check = subprocess.run([sys.executable, "-I", str(copy / "check_copy.py"), "--lock",
                            str(tp / "backend" / "aa_llm.lock.json")], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    g(tp, "add", "-A")
    g(tp, "commit", "-qm", "sync")
    assert sync("tripplanner", "--cis", str(cis), "--no-fetch").returncode == 0
    assert g(tp, "status", "--porcelain") == ""


def test_sync_into_partner(ws):
    cis, partner = _apps(ws, "AA-CIS-App"), _apps(ws, "AA-Partner")
    assert sync("crm", "--cis", str(cis), "--no-fetch").returncode == 0
    assert (partner / "api" / "app" / "aa_llm" / "ledger.py").is_file()
    assert json.loads((partner / "api" / "aa_llm.lock.json").read_text())["dest"] == "api/app/aa_llm"


def test_dry_run_writes_nothing(ws):
    cis, tp = _apps(ws, "AA-CIS-App"), _apps(ws, "AA-TripPlanner-Web")
    out = sync("tripplanner", "--cis", str(cis), "--no-fetch", "--dry-run")
    assert out.returncode == 0 and "would be" in out.stdout
    assert not (tp / "backend" / "aa_llm").exists() and not (tp / "backend" / "aa_llm.lock.json").exists()


def test_from_local_records_ref_local(ws):
    cis, tp = _apps(ws, "AA-CIS-App"), _apps(ws, "AA-TripPlanner-Web")
    (cis / CORE / "__pycache__").mkdir()
    (cis / CORE / "__pycache__" / "x.pyc").write_bytes(b"\0")
    assert sync("tripplanner", "--cis", str(cis), "--from-local").returncode == 0
    assert json.loads((tp / "backend" / "aa_llm.lock.json").read_text())["ref"] == "local"
    assert not (tp / "backend" / "aa_llm" / "__pycache__").exists()


def test_stale_manifest_is_refused_before_writing(ws):
    cis, tp = _apps(ws, "AA-CIS-App"), _apps(ws, "AA-TripPlanner-Web")
    with open(cis / CORE / "budget.py", "a") as fh:
        fh.write("# edited without regenerating MANIFEST.json\n")
    out = sync("tripplanner", "--cis", str(cis), "--from-local")
    assert out.returncode == 4
    assert "budget.py: sha256 differs" in out.stderr
    assert not (tp / "backend" / "aa_llm").exists()


def test_usage_errors(ws):
    cis = _apps(ws, "AA-CIS-App")
    assert sync("nope", "--cis", str(cis)).returncode == 2
    assert sync("tripplanner", "--cis", str(cis), "--from-local", "--ref", "HEAD").returncode == 2


def test_plan_sees_core_changes_only(ws, tmp_path):
    cis = _apps(ws, "AA-CIS-App")
    base = g(cis, "rev-parse", "HEAD")
    (cis / "README").write_text("x\n")
    g(cis, "add", "-A")
    g(cis, "commit", "-qm", "other")
    out = bot("--cis", str(cis), "plan", "--base", base, "--head", "HEAD")
    assert out.returncode == 0 and json.loads(out.stdout)["changed"] is False
    with open(cis / CORE / "ports.py", "a") as fh:
        fh.write("\n")
    g(cis, "add", "-A")
    g(cis, "commit", "-qm", "core")
    plan = json.loads(bot("--cis", str(cis), "plan", "--base", base, "--head", "HEAD").stdout)
    assert plan["changed"] is True
    assert plan["repos"] == ["AA-Partner", "AA-TripPlanner-Web"]
    assert json.loads(bot("--cis", str(cis), "plan", "--base", "", "--head", "HEAD").stdout)["changed"] is True


def test_bot_sync_pushes_once_then_is_a_no_op(ws):
    cis, tp = _apps(ws, "AA-CIS-App"), _apps(ws, "AA-TripPlanner-Web")
    ref = g(cis, "rev-parse", "HEAD")
    dry = bot("--cis", str(cis), "sync", "--repo", "AA-TripPlanner-Web", "--dest", str(tp), "--ref", ref,
              "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "Refs AA-642" in dry.stdout
    assert g(tp, "ls-remote", "--heads", "origin", "bot/llm-core-sync") == ""
    live = bot("--cis", str(cis), "sync", "--repo", "AA-TripPlanner-Web", "--dest", str(tp), "--ref", ref,
               "--offline")
    assert live.returncode == 0, live.stderr
    assert "force-pushed" in live.stdout
    pushed = g(tp, "ls-remote", "--heads", "origin", "bot/llm-core-sync").split("\t")[0]
    assert pushed
    again = bot("--cis", str(cis), "sync", "--repo", "AA-TripPlanner-Web", "--dest", str(tp), "--ref", ref,
                "--offline")
    assert "already has this tree" in again.stdout
    # Consumer main takes the sync: the next run is a no-op.
    g(tp, "push", "-q", "origin", f"{pushed}:refs/heads/main")
    g(tp, "fetch", "-q", "origin")
    noop = bot("--cis", str(cis), "sync", "--repo", "AA-TripPlanner-Web", "--dest", str(tp), "--ref", ref,
               "--offline")
    assert noop.returncode == 0 and "no-op" in noop.stdout


def test_bot_rejects_a_repo_without_a_consumer(ws):
    cis = _apps(ws, "AA-CIS-App")
    out = bot("--cis", str(cis), "sync", "--repo", "AA-Booking", "--dest", str(ws), "--ref", "HEAD", "--offline")
    assert out.returncode == 1 and "no aa_llm consumer" in out.stderr


def test_sync_workflow_is_main_only_with_a_narrow_token():
    wf = yaml.safe_load(SYNC_WF.read_text())
    trigger = wf.get("on") or wf.get(True)
    assert trigger["push"]["paths"] == ["shared/aa_llm/**"]
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["sync"]
    assert job["environment"] == "release"
    assert "refs/heads/main" in job["if"]
    token = next(s for s in job["steps"] if s.get("id") == "app-token")["with"]
    assert token["repositories"] == "${{ matrix.repo }}"
    assert {k for k in token if k.startswith("permission-")} == {"permission-contents", "permission-pull-requests"}
    # The bot never merges: no merge subcommand of the PR CLI in the workflow or the bot.
    for text in (SYNC_WF.read_text(), BOT.read_text()):
        assert not re.search(r"\bpr\b[\"', ]+merge\b", text)


def test_consumers_workflow_reads_no_secret_and_has_a_result_job():
    text = CONSUMERS_WF.read_text()
    assert "secrets." not in text
    wf = yaml.safe_load(text)
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["llm-core-consumers"]["name"] == "Llm core consumers"
    assert wf["jobs"]["llm-core-consumers"]["if"] == "always()"
