"""AA-1024 G3 (c), S272 review — the deploy job asks the deploy gate again right before it rolls the services.

The preflight guard runs in parallel with the image build, minutes before the service update; an ECS exec script
started in between (an ecsx heartbeat) was killed by the roll, the S234 incident the heartbeat gate is for.
"""
from __future__ import annotations

from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy-dev.yml"


def test_the_gate_is_asked_again_as_the_step_before_the_first_roll():
    steps = yaml.safe_load(WF.read_text(encoding="utf-8"))["jobs"]["deploy"]["steps"]
    names = [s.get("name") for s in steps]
    i = names.index("Deploy api")
    recheck = steps[i - 1]
    assert recheck["name"] == "Re-check the deploy gate just before the roll"
    assert recheck["run"].strip() == "bash .github/scripts/deploy_job_guard.sh"
    assert recheck["env"]["GATE_URL"].endswith("/admin/job-runner/deploy-gate")
    assert "override_running_jobs" in recheck["env"]["OVERRIDE"] and "ADMIN_SECRET" in recheck["env"]["ADMIN_SECRET"]
    # nothing before it rolls a service (the helper step only writes the script)
    assert all("update-service" not in str(s.get("run", "")) for s in steps[:i - 1] if s.get("name") != "Deploy helper")
