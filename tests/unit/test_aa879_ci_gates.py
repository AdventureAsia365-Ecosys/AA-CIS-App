"""AA-879 — CI gate parity: the coverage ratchet and the .trivyignore expiry check.

Runs the real stdlib scripts:
- .github/scripts/coverage_ratchet.py: below the floor fails, a lowered floor fails, at/above passes,
  below the monthly goal only warns; the committed config parses and its floor is sane.
- .github/scripts/check_trivyignore.py: an entry without `exp:`, without a reason comment, expired, or
  more than 90 days ahead fails; a well-formed entry and the committed file pass.
- deploy-dev.yml no longer redeploys ECS on a frontend-only change.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RATCHET = REPO / ".github" / "scripts" / "coverage_ratchet.py"
RATCHET_CFG = REPO / ".github" / "coverage-ratchet.json"
TRIVY_CHECK = REPO / ".github" / "scripts" / "check_trivyignore.py"


def _cov_xml(tmp_path: Path, rate: float) -> Path:
    p = tmp_path / "coverage.xml"
    p.write_text(f'<?xml version="1.0" ?><coverage line-rate="{rate}" branch-rate="0" version="7"></coverage>')
    return p


def _cfg(tmp_path: Path, **over) -> Path:
    cfg = {"floor": 85, "baseline": 85, "set_on": "2026-10-22", "monthly_step": 5, "cap": 90}
    cfg.update(over)
    p = tmp_path / "ratchet.json"
    p.write_text(json.dumps(cfg))
    return p


def _ratchet(*args, today="2026-10-22"):
    env = {**os.environ, "TODAY_OVERRIDE": today}
    env.pop("GITHUB_STEP_SUMMARY", None)
    return subprocess.run([sys.executable, str(RATCHET), *args], env=env, capture_output=True, text=True)


def test_ratchet_fails_below_floor(tmp_path):
    r = _ratchet(str(_cov_xml(tmp_path, 0.80)), "--config", str(_cfg(tmp_path)))
    assert r.returncode == 1
    assert "below the floor" in r.stdout


def test_ratchet_passes_at_floor_and_suggests_raise(tmp_path):
    r = _ratchet(str(_cov_xml(tmp_path, 0.8568)), "--config", str(_cfg(tmp_path)))
    assert r.returncode == 0, r.stdout
    r = _ratchet(str(_cov_xml(tmp_path, 0.87)), "--config", str(_cfg(tmp_path)))
    assert r.returncode == 0
    assert "raise 'floor'" in r.stdout


def test_ratchet_goal_only_warns(tmp_path):
    # Two whole months later the goal is min(85 + 10, 90) = 90; 86% is above the floor: warn, pass.
    r = _ratchet(str(_cov_xml(tmp_path, 0.86)), "--config", str(_cfg(tmp_path)), today="2026-12-23")
    assert r.returncode == 0
    assert "::warning::" in r.stdout and "goal 90%" in r.stdout


def test_ratchet_rejects_lowered_floor(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    cfg_rel = "ratchet.json"
    (repo / cfg_rel).write_text(json.dumps({"floor": 85, "set_on": "2026-10-22"}))
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    (repo / cfg_rel).write_text(json.dumps({"floor": 80, "set_on": "2026-10-22"}))
    xml = _cov_xml(tmp_path, 0.9)
    env = {**os.environ, "TODAY_OVERRIDE": "2026-10-22"}
    env.pop("GITHUB_STEP_SUMMARY", None)
    r = subprocess.run(
        [sys.executable, str(RATCHET), str(xml), "--config", cfg_rel, "--base", "HEAD"],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "floor lowered from 85 to 80" in r.stdout


def test_committed_ratchet_config_is_sane():
    cfg = json.loads(RATCHET_CFG.read_text())
    assert 30 < cfg["floor"] <= cfg["cap"] <= 100
    assert cfg["monthly_step"] > 0


def _trivy(tmp_path: Path, body: str, today="2026-10-22"):
    p = tmp_path / ".trivyignore"
    p.write_text(body)
    env = {**os.environ, "TODAY_OVERRIDE": today}
    return subprocess.run([sys.executable, str(TRIVY_CHECK), str(p)], env=env, capture_output=True, text=True)


def test_trivyignore_good_entry_passes(tmp_path):
    r = _trivy(tmp_path, "# base image fix not published yet; rebuild removes it\nCVE-2026-1 exp:2026-12-01\n")
    assert r.returncode == 0, r.stdout


def test_trivyignore_rejects_missing_expiry_reason_expired_and_far(tmp_path):
    assert "exp:YYYY-MM-DD" in _trivy(tmp_path, "# reason\nCVE-2026-1\n").stdout
    assert "reason" in _trivy(tmp_path, "CVE-2026-1 exp:2026-12-01\n").stdout
    r = _trivy(tmp_path, "# reason\nCVE-2026-1 exp:2026-10-01\n")
    assert r.returncode == 1 and "expired" in r.stdout
    r = _trivy(tmp_path, "# reason\nCVE-2026-1 exp:2027-06-01\n")
    assert r.returncode == 1 and "more than 90 days" in r.stdout


def test_committed_trivyignore_passes():
    r = subprocess.run(
        [sys.executable, str(TRIVY_CHECK), str(REPO / ".trivyignore")], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stdout


def test_deploy_dev_skips_frontend_only_changes():
    text = (REPO / ".github" / "workflows" / "deploy-dev.yml").read_text()
    paths_block = text.split("paths:", 1)[1].split("workflow_dispatch", 1)[0]
    listed = [
        ln.strip().strip("-").strip().strip("'\"") for ln in paths_block.splitlines() if ln.strip().startswith("-")
    ]
    assert listed, "deploy-dev.yml lost its path filter"
    assert not any(p.startswith("frontend") or p.startswith("portal") for p in listed), listed
