"""AA-1193 — Deploy Dev rolls api + worker back when the post-deploy verify is red, alerts the ops topic, never loops.

- deploy-dev.yml: the deploy job records the api and worker revisions (numbers only: the family name is a secret)
  BEFORE any update-service; `rollback` runs only on main, after a successful deploy and a FAILED verify, re-asks the
  deploy gate, rolls both services with .github/scripts/ecs_api_rollback.py (--apply, the /health URL), then alerts;
  `report` keeps the deploy-smoke-red issue; `result` waits on neither.
- The rollback step's shell runs here against stub scripts: a held gate writes nothing and reads "held by the deploy
  gate"; a rollback outcome has the secret service names replaced by api / worker (a job output with a secret value
  is dropped by GitHub).
- The drill input reaches post-deploy-verify.yml, whose drill step turns the verify red after the real checks; the
  failed check names come back as the reusable workflow's output; post_deploy_verify.sh writes them.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY_WF = REPO / ".github" / "workflows" / "deploy-dev.yml"
VERIFY_WF = REPO / ".github" / "workflows" / "post-deploy-verify.yml"

needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")


def _load(path):
    wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    wf["on"] = wf.pop(True, wf.get("on"))
    return wf


DEPLOY = _load(DEPLOY_WF)
VERIFY = _load(VERIFY_WF)
JOBS = DEPLOY["jobs"]


def _step(job, name):
    hits = [s for s in JOBS[job]["steps"] if s.get("name") == name]
    assert len(hits) == 1, (job, name)
    return hits[0]


def test_drill_input_is_a_dispatch_only_boolean_off_by_default():
    inp = DEPLOY["on"]["workflow_dispatch"]["inputs"]["drill_force_smoke_fail"]
    assert inp["type"] == "boolean" and inp["default"] is False and inp["required"] is False
    assert "push" in DEPLOY["on"] and "inputs" not in (DEPLOY["on"]["push"] or {})


def test_revisions_are_recorded_before_the_first_update_service():
    names = [s.get("name", "") for s in JOBS["deploy"]["steps"]]
    record = names.index("Record the revisions before this deploy (rollback targets)")
    assert record < names.index("Deploy api") and record < names.index("Deploy worker")
    run = _step("deploy", "Record the revisions before this deploy (rollback targets)")["run"]
    assert "ecs_smoke_rollback.py previous" in run
    assert 'record api_rev "$ECS_SERVICE" api' in run and 'record worker_rev "$ECS_WORKER_SERVICE" worker' in run
    outputs = JOBS["deploy"]["outputs"]
    assert outputs == {
        "api_rev": "${{ steps.before.outputs.api_rev }}",
        "worker_rev": "${{ steps.before.outputs.worker_rev }}",
    }, "numbers only: an output holding the secret family name would be dropped"


def test_rollback_runs_only_after_a_successful_deploy_and_a_failed_verify_on_main():
    job = JOBS["rollback"]
    assert job["needs"] == ["deploy", "verify"]
    cond = " ".join(job["if"].split())
    for part in ("always()", "github.ref == 'refs/heads/main'", "needs.deploy.result == 'success'",
                 "needs.verify.result == 'failure'"):
        assert part in cond, part
    assert job["environment"] == "dev"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}


def test_rollback_respects_the_deploy_gate_and_moves_both_services():
    step = _step("rollback", "Roll back api and worker to the revisions before this deploy")
    run, env = step["run"], step["env"]
    assert "bash .github/scripts/deploy_job_guard.sh" in run
    assert env["OVERRIDE"] == "${{ inputs.override_running_jobs }}"
    assert env["GATE_URL"] == _step("deploy", "Re-check the deploy gate just before the roll")["env"]["GATE_URL"]
    assert "ecs_api_rollback.py rollback" in run and "--apply" in run
    assert run.index("deploy_job_guard.sh") < run.index("ecs_api_rollback.py rollback")
    assert '--target "${ECS_SERVICE},api,' in run and '--target "${WORKER},worker,' in run
    assert "--health-url https://api-cis.lumiguides.it.com/health" in run
    assert '--deployed-tag "dev-${GITHUB_SHA}"' in run


def test_alert_always_runs_and_report_keeps_the_issue():
    alert = _step("rollback", "Alert the ops topic")
    assert alert["if"] == "always()"
    assert "ecs_api_rollback.py alert" in alert["run"] and "--apply" in alert["run"]
    assert alert["env"]["FAILED_CHECKS"] == "${{ needs.verify.outputs.failed_checks }}"
    report = JOBS["report"]
    assert report["needs"] == ["deploy", "verify", "rollback"]
    assert report["permissions"] == {"issues": "write"}
    assert "deploy-smoke-red" in report["steps"][0]["run"]


def test_nothing_in_the_rollback_can_start_a_deploy():
    text = "\n".join(s.get("run", "") for s in JOBS["rollback"]["steps"])
    for forbidden in ("gh workflow", "workflow_dispatch", "docker push", "register-task-definition", "git push"):
        assert forbidden not in text, forbidden


def _run_rollback_step(tmp_path, guard_rc, guard_msg, script_out):
    scripts = tmp_path / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "deploy_job_guard.sh").write_text(f"echo '{guard_msg}'\nexit {guard_rc}\n")
    (scripts / "ecs_api_rollback.py").write_text(
        "import sys\n"
        "sys.stderr.write('called ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        f"print({script_out!r})\n"
        "sys.exit(0)\n"
    )
    out, summary = tmp_path / "out.txt", tmp_path / "summary.md"
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary),
        "ECS_CLUSTER": "cluster-x", "ECS_SERVICE": "app-dev-api", "ECR_REPOSITORY": "repo-x",
        "GITHUB_SHA": "3f2a9c1e7b4d6a0f5e8c2b1d9a7f4e6c0b3d5a2e", "API_REV": "6", "WORKER_REV": "4",
        "OVERRIDE": "", "ADMIN_SECRET": "x", "GATE_URL": "http://gate.test",
    }
    run = _step("rollback", "Roll back api and worker to the revisions before this deploy")["run"]
    run = run.replace("/tmp/", f"{tmp_path}/")
    r = subprocess.run(["bash", "-c", run], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    return r, out.read_text() if out.exists() else ""


@needs_bash
def test_a_held_gate_rolls_nothing_back_and_says_so(tmp_path):
    r, out = _run_rollback_step(tmp_path, 1, "::error::Deploy blocked: a rerun wave is open", "outcome=unused")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "called" not in r.stdout, "the rollback script must not run while the gate holds"
    assert "outcome=not rolled back (held by the deploy gate: a rerun wave is open)" in out


@needs_bash
def test_the_outcome_output_never_holds_the_service_names(tmp_path):
    r, out = _run_rollback_step(
        tmp_path, 0, "safe", "outcome=rolled back to app-dev-api:6, app-dev-worker:4 (/health on 9e1b7c3a5d2f)"
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--target app-dev-api,api,app-dev-api:6" in r.stdout  # the step cats the script log
    assert "--target app-dev-worker,worker,app-dev-worker:4" in r.stdout
    assert "outcome=rolled back to api:6, worker:4 (/health on 9e1b7c3a5d2f)" in out
    assert "app-dev-api" not in out


def test_verify_workflow_carries_the_drill_and_the_failed_checks():
    call = VERIFY["on"]["workflow_call"]
    assert call["inputs"]["drill_force_fail"]["default"] is False
    assert "drill_force_fail" not in VERIFY["on"]["workflow_dispatch"]["inputs"]
    assert call["outputs"]["failed_checks"]["value"] == "${{ jobs.verify.outputs.failed_checks }}"
    job = VERIFY["jobs"]["verify"]
    assert job["outputs"]["failed_checks"] == "${{ steps.failed.outputs.checks }}"
    names = [s.get("name", "") for s in job["steps"]]
    drill = job["steps"][names.index("Drill — force the post-deploy verify red")]
    assert drill["if"] == "inputs.drill_force_fail" and "exit 1" in drill["run"]
    assert names.index("Verify Dev") < names.index("Drill — force the post-deploy verify red") < names.index(
        "Failing checks")
    assert job["steps"][names.index("Failing checks")]["if"] == "always()"


@needs_bash
def test_failing_checks_step_keeps_names_only(tmp_path):
    step = [s for s in VERIFY["jobs"]["verify"]["steps"] if s.get("name") == "Failing checks"][0]
    failed, out = tmp_path / "failed.txt", tmp_path / "out.txt"
    failed.write_text("health\nworker-sha\nhealth\n$(rm -rf x)\ndrill-forced-fail\n")
    env = {"PATH": os.environ["PATH"], "VERIFY_FAILED_FILE": str(failed), "GITHUB_OUTPUT": str(out)}
    r = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert out.read_text() == "checks=health,worker-sha,drill-forced-fail\n"
