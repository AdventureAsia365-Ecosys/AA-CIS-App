"""AA-993 V1 (ADR 0016 D3) — the CIS post-deploy verify is red/green and a skipped verify fails Deploy Dev.

- .github/scripts/post_deploy_verify.sh runs for real against fixture JSON: `curl` and `aws` on PATH are stubs that
  answer from the fixture (no network, no AWS). All good exits 0; a wrong git_sha, a FAILED rollout, /health/ready
  503, a missing ADMIN_SECRET, a schema mismatch, desired count 0, a missing worker service, an old image and a
  worker on another SHA each exit 1. The real .github/verify/cis.rows is used.
- post-deploy-verify.yml and deploy-dev.yml parse; the verify runs after `deploy` with its own concurrency group
  (never `deploy-dev-cis`); the `result` job runs always, needs every job, and its step is red when verify is
  skipped (the step's shell runs here with the `needs` JSON GitHub would pass).
- Neither deploy workflow runs on merge_group.
"""
import copy
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "post_deploy_verify.sh"
ROWS = REPO / ".github" / "verify" / "cis.rows"
VERIFY_WF_PATH = REPO / ".github" / "workflows" / "post-deploy-verify.yml"
DEPLOY_WF_PATH = REPO / ".github" / "workflows" / "deploy-dev.yml"

needs_tools = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("jq")), reason="needs bash and jq"
)

SHA = "3f2a9c1e7b4d6a0f5e8c2b1d9a7f4e6c0b3d5a2e"
OLD_SHA = "9e1b7c3a5d2f4e6a8c0b1d3f5a7e9c2b4d6f8a0c"
VERSION = "0.11.0"
REQUIRED = "287"
IMAGE = f"registry.example.invalid/aa-cis-dev-api:dev-{SHA}"
SECRET = "test-admin-secret"
API_SVC, WORKER_SVC = "aa-cis-dev-api", "aa-cis-dev-worker"
KINDS = ["a3_atomize", "s1_rewrite", "noop"]

FAKE_CURL = r'''
import json, os, sys
from urllib.parse import urlparse
args = sys.argv[1:]
out = fmt = None
headers = []
url = None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-w", "-H", "--max-time", "-X"):
        val = args[i + 1]
        if a == "-o":
            out = val
        elif a == "-w":
            fmt = val
        elif a == "-H":
            headers.append(val)
        i += 2
        continue
    if not a.startswith("-"):
        url = a
    i += 1
routes = json.load(open(os.environ["FAKE_HTTP"]))
route = routes.get(urlparse(url).path)
if route is None:
    status, body = 404, {"detail": "not found"}
elif route.get("admin") and ("x-admin-secret: " + os.environ["FAKE_SECRET"]) not in headers:
    status, body = 401, {"detail": "unauthorized"}
else:
    status, body = route["status"], route["body"]
if out:
    with open(out, "w") as f:
        json.dump(body, f)
if fmt:
    sys.stdout.write(str(status))
'''

FAKE_AWS = r'''
import json, os, sys
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1]
ecs = json.load(open(os.environ["FAKE_ECS"]))
if os.environ.get("FAKE_AWS_ERR") and args[:2] == ["ecs", "describe-services"]:
    sys.stderr.write("An error occurred (AccessDeniedException): User: arn:aws:sts::123456789012:assumed-role/x/y "
                     "is not authorized; account 123456789012")
    sys.exit(255)
if args[:2] == ["ecs", "describe-services"]:
    svc = ecs["services"].get(opt("--services"))
    print(json.dumps({"services": [svc]} if svc else {"services": [], "failures": [{"reason": "MISSING"}]}))
elif args[:2] == ["ecs", "describe-task-definition"]:
    td = ecs["taskDefinitions"].get(opt("--task-definition"))
    if td is None:
        sys.stderr.write("task definition not found")
        sys.exit(255)
    print(json.dumps({"taskDefinition": td}))
else:
    sys.stderr.write("unexpected aws call: " + " ".join(args))
    sys.exit(2)
'''


def _service(name, td, rollout="COMPLETED", desired=1, running=1, deployments=1):
    deps = [{"status": "PRIMARY", "taskDefinition": td, "rolloutState": rollout}]
    deps += [{"status": "ACTIVE", "taskDefinition": td + "-old", "rolloutState": "COMPLETED"}] * (deployments - 1)
    return {"serviceName": name, "status": "ACTIVE", "desiredCount": desired, "runningCount": running,
            "taskDefinition": td, "deployments": deps}


def _good():
    """A Dev that runs SHA everywhere: (http routes, ecs state)."""
    http = {
        "/health": {"status": 200, "body": {"status": "ok", "service": "aa-cis-api", "version": VERSION,
                                            "git_sha": SHA}},
        "/health/ready": {"status": 200, "body": {"status": "ok", "db": "ok", "schema_version": "290",
                                                  "required_schema": REQUIRED, "git_sha": SHA}},
        "/admin/job-runner/summary": {"admin": True, "status": 200, "body": {"kinds": [{"kind": k} for k in KINDS]}},
        "/admin/job-runner/deploy-gate": {"admin": True, "status": 200,
                                          "body": {"safe_to_deploy": True, "open_waves": 0, "blocked_by": []}},
        "/admin/overview": {"admin": True, "status": 200, "body": {"tours": 1}},
        # AA-993 G1: jobs released on shutdown (1 by the deploy, not counted toward MAX_RELEASES)
        "/admin/job-runner/released": {"admin": True, "status": 200, "body": {
            "since": "2026-10-27T00:00:00+00:00", "released": 1, "counted": 0, "jobs": 1,
            "by_kind": {"noop": {"released": 1, "counted": 0, "jobs": 1}}, "max_releases": 3}},
        "/admin/job-runner/workers": {"admin": True, "status": 200, "body": {"workers": [
            {"alive": True, "git_sha": SHA, "caps": {k: 1 for k in KINDS}},
            # an old-revision worker still draining: neither its SHA nor its caps make the verify red
            {"alive": True, "git_sha": OLD_SHA, "caps": {"retired_kind": 1}},
        ]}},
    }
    ecs = {
        "services": {API_SVC: _service(API_SVC, "td-api:2"), WORKER_SVC: _service(WORKER_SVC, "td-worker:2")},
        "taskDefinitions": {"td-api:2": {"containerDefinitions": [{"image": IMAGE}]},
                            "td-worker:2": {"containerDefinitions": [{"image": IMAGE}]}},
    }
    return http, ecs


def _stub(bin_dir, name, source):
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{source}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def run_verify(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "curl", FAKE_CURL)
    _stub(bin_dir, "aws", FAKE_AWS)

    def run(http, ecs, **env_over):
        (tmp_path / "http.json").write_text(json.dumps(http))
        (tmp_path / "ecs.json").write_text(json.dumps(ecs))
        summary = tmp_path / "summary.md"
        summary.write_text("")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_HTTP": str(tmp_path / "http.json"), "FAKE_ECS": str(tmp_path / "ecs.json"),
            "FAKE_SECRET": SECRET,
            "EXPECTED_SHA": SHA, "EXPECTED_VERSION": VERSION, "REQUIRED_SCHEMA": REQUIRED,
            "EXPECTED_IMAGE": IMAGE, "ADMIN_SECRET": SECRET, "ECS_CLUSTER": "aa-cis-dev-cluster",
            "ECS_SERVICE": API_SVC, "ECS_WORKER_SERVICE": WORKER_SVC, "AWS_REGION": "us-west-1",
            "BASE": "http://verify.test", "ROWS_FILE": str(ROWS), "GITHUB_STEP_SUMMARY": str(summary),
            "VERIFY_TRIES": "2", "VERIFY_RETRY_DELAY": "0", "VERIFY_INITIAL_WAIT": "0",
        }
        env.update(env_over)
        r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
        return r, summary.read_text()

    return run


def _failed_rows(summary):
    return {line.split("|")[1].strip() for line in summary.splitlines() if "| **FAIL** |" in line}


@needs_tools
def test_all_good_exits_zero_and_prints_the_table(run_verify):
    r, summary = run_verify(*_good())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "### CIS post-deploy verify: PASS" in summary
    for row in ("api-rollout", "api-image", "worker-rollout", "worker-image", "same-image", "health", "health-ready",
                "job-runner-summary", "deploy-gate", "overview", "worker-sha", "worker-kinds"):
        assert f"| {row} | PASS |" in summary, (row, summary)
    assert "**FAIL**" not in summary
    assert "registry.example.invalid" not in summary + r.stdout, "the registry (a secret in CI) must not be printed"
    assert SECRET not in summary + r.stdout


def _health_sha_mismatch(http, ecs):
    http["/health"]["body"]["git_sha"] = OLD_SHA


def _rollout_failed(http, ecs):
    ecs["services"][API_SVC]["deployments"][0]["rolloutState"] = "FAILED"


def _ready_503(http, ecs):
    http["/health/ready"] = {"status": 503, "body": {"status": "schema_behind", "db": "ok", "schema_version": "280",
                                                     "required_schema": REQUIRED, "git_sha": SHA}}


def _schema_required_mismatch(http, ecs):
    http["/health/ready"]["body"]["required_schema"] = "280"   # the running code is not the deployed commit


def _schema_behind_but_200(http, ecs):
    http["/health/ready"]["body"]["schema_version"] = "286"


def _desired_zero(http, ecs):
    ecs["services"][WORKER_SVC] = _service(WORKER_SVC, "td-worker:2", desired=0, running=0)


def _worker_service_missing(http, ecs):
    del ecs["services"][WORKER_SVC]


def _worker_old_image(http, ecs):
    ecs["taskDefinitions"]["td-worker:2"]["containerDefinitions"][0]["image"] = IMAGE.replace(SHA, OLD_SHA)


def _old_deployment_left(http, ecs):
    ecs["services"][API_SVC] = _service(API_SVC, "td-api:2", deployments=2)


def _no_worker_on_sha(http, ecs):
    http["/admin/job-runner/workers"]["body"]["workers"] = [
        {"alive": True, "git_sha": OLD_SHA, "caps": {k: 1 for k in KINDS}}]


def _worker_missing_a_kind(http, ecs):
    http["/admin/job-runner/workers"]["body"]["workers"][0]["caps"].pop("s1_rewrite")


def _admin_row_500(http, ecs):
    http["/admin/overview"] = {"admin": True, "status": 500, "body": {"detail": "boom"}}


def _released_missing(http, ecs):
    """An image without the AA-993 G1 endpoint (404)."""
    del http["/admin/job-runner/released"]


@needs_tools
@pytest.mark.parametrize("breaker, red_rows", [
    (_health_sha_mismatch, {"health"}),
    (_rollout_failed, {"api-rollout"}),
    (_ready_503, {"health-ready"}),
    (_schema_required_mismatch, {"health-ready"}),
    (_schema_behind_but_200, {"health-ready"}),
    (_desired_zero, {"worker-rollout"}),
    (_worker_service_missing, {"worker-rollout", "worker-image", "same-image"}),
    (_worker_old_image, {"worker-image", "same-image"}),
    (_old_deployment_left, {"api-rollout"}),
    (_no_worker_on_sha, {"worker-sha", "worker-kinds"}),
    (_worker_missing_a_kind, {"worker-kinds"}),
    (_admin_row_500, {"overview"}),
    (_released_missing, {"job-runner-released"}),
])
def test_each_failure_is_red(run_verify, breaker, red_rows):
    http, ecs = copy.deepcopy(_good())
    breaker(http, ecs)
    r, summary = run_verify(http, ecs)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "### CIS post-deploy verify: FAIL" in summary
    assert _failed_rows(summary) == red_rows, summary
    assert "::error::verify [" in r.stdout


@needs_tools
def test_stale_expected_sha_is_red_on_the_sha_rows(run_verify):
    """Live proof 2 in miniature: Dev runs SHA, the dispatch expects OLD_SHA."""
    http, ecs = _good()
    http["/admin/job-runner/workers"]["body"]["workers"].pop()   # no draining worker on OLD_SHA left
    r, summary = run_verify(http, ecs, EXPECTED_SHA=OLD_SHA, EXPECTED_IMAGE=IMAGE.replace(SHA, OLD_SHA))
    assert r.returncode == 1
    red = {"health", "health-ready", "api-image", "worker-image", "worker-sha", "worker-kinds"}
    assert red <= _failed_rows(summary), summary


@needs_tools
def test_missing_admin_secret_is_red_and_admin_rows_report_not_run(run_verify):
    r, summary = run_verify(*_good(), ADMIN_SECRET="")
    assert r.returncode == 1, r.stdout
    assert _failed_rows(summary) == {"admin-secret", "job-runner-summary", "deploy-gate", "overview",
                                     "job-runner-released", "worker-sha", "worker-kinds"}, summary
    # the public and ECS rows still ran
    for row in ("api-rollout", "worker-image", "health", "health-ready"):
        assert f"| {row} | PASS |" in summary, row


@needs_tools
@pytest.mark.parametrize("over", [
    {"EXPECTED_SHA": ""}, {"EXPECTED_SHA": SHA[:12]}, {"REQUIRED_SCHEMA": ""}, {"EXPECTED_VERSION": ""},
    {"EXPECTED_IMAGE": ""}, {"EXPECTED_IMAGE": IMAGE.replace(SHA, OLD_SHA)}, {"ECS_CLUSTER": ""},
    {"ECS_WORKER_SERVICE": ""},
])
def test_bad_inputs_are_red_and_nothing_is_reported_green(run_verify, over):
    r, summary = run_verify(*_good(), **over)
    assert r.returncode == 1, r.stdout
    assert "| checks | **FAIL** | not run" in summary
    assert "| health | PASS |" not in summary


def test_rows_file_is_get_only_with_known_auth():
    rows = [ln for ln in ROWS.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    names = set()
    assert rows
    for ln in rows:
        name, auth, req, want, _expr = ln.split("|", 4)
        assert auth in ("public", "admin"), ln
        assert req.split(" ", 1)[0] == "GET" and req.split(" ", 1)[1].startswith("/"), ln
        assert want.isdigit(), ln
        assert name not in names, f"duplicate row name {name}"
        names.add(name)
    assert {"health", "health-ready"} <= names


# ── workflow wiring ───────────────────────────────────────────────────────────────────────────────────────────────

def _load(path):
    wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as the boolean True.
    wf["on"] = wf.pop(True, wf.get("on"))
    return wf


VERIFY_WF = _load(VERIFY_WF_PATH)
DEPLOY_WF = _load(DEPLOY_WF_PATH)


def test_verify_workflow_is_callable_and_dispatchable_with_expected_sha():
    on = VERIFY_WF["on"]
    assert set(on) == {"workflow_call", "workflow_dispatch"}
    assert on["workflow_call"]["inputs"]["expected_sha"]["required"] is True
    assert on["workflow_dispatch"]["inputs"]["expected_sha"]["required"] is True
    assert "from_deploy" not in on["workflow_dispatch"]["inputs"]
    assert "concurrency" not in VERIFY_WF, "workflow-level concurrency would be shared with every caller"
    job = VERIFY_WF["jobs"]["verify"]
    assert job["environment"] == "dev"   # the deploy role trusts the subject ...:environment:dev
    group = job["concurrency"]["group"]
    assert job["concurrency"]["cancel-in-progress"] is False
    assert "'post-deploy-verify-cis'" in group and "inputs.from_deploy" in group and "github.run_id" in group
    assert "deploy-dev-cis" not in group
    run = "\n".join(s.get("run", "") for s in job["steps"])
    assert "bash .github/scripts/post_deploy_verify.sh" in run


def test_deploy_dev_calls_verify_after_deploy_and_result_needs_every_job():
    jobs = DEPLOY_WF["jobs"]
    verify = jobs["verify"]
    assert verify["uses"] == "./.github/workflows/post-deploy-verify.yml"
    assert verify["needs"] in ("deploy", ["deploy"])
    assert verify["with"] == {"expected_sha": "${{ github.sha }}", "from_deploy": True}
    assert verify["secrets"] == "inherit"
    assert "if" not in verify, "verify must run only after a successful deploy (the default)"
    result = jobs["result"]
    assert result["if"] == "always()"
    assert set(result["needs"]) == set(jobs) - {"result"}
    # the post-deploy steps left the deploy job
    deploy_steps = [s.get("name", "") for s in jobs["deploy"]["steps"]]
    assert "Smoke test" not in deploy_steps and "Verify api/worker same image SHA" not in deploy_steps
    assert deploy_steps[-1] == "Wait stable"


def test_deploy_dev_keeps_its_trigger_guard_and_override():
    on = DEPLOY_WF["on"]
    assert set(on) == {"push", "workflow_dispatch"}
    assert on["push"]["branches"] == ["main"]
    assert "override_running_jobs" in on["workflow_dispatch"]["inputs"]
    assert DEPLOY_WF["concurrency"] == {"group": "deploy-dev-cis", "cancel-in-progress": False}
    assert set(DEPLOY_WF["jobs"]["deploy"]["needs"]) == {"build-and-push", "preflight", "schema-gate"}  # AA-993 M4b
    guard = DEPLOY_WF["jobs"]["preflight"]["steps"][-1]
    assert guard["run"] == "bash .github/scripts/deploy_job_guard.sh"
    assert guard["env"]["OVERRIDE"] == "${{ inputs.override_running_jobs }}"


@pytest.mark.parametrize("wf", [VERIFY_WF, DEPLOY_WF], ids=["post-deploy-verify", "deploy-dev"])
def test_deploy_workflows_never_run_on_merge_group(wf):
    assert "merge_group" not in wf["on"]


def _result_step(needs):
    step = DEPLOY_WF["jobs"]["result"]["steps"][-1]
    env = {"PATH": os.environ["PATH"], "NEEDS": json.dumps(needs), "EVENT": "push"}
    return subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=60)


def _needs(**over):
    base = {j: {"result": "success", "outputs": {}} for j in ("schema-gate", "preflight", "build-and-push", "deploy", "verify")}
    for job, res in over.items():
        base[job.replace("_", "-")]["result"] = res
    return base


@needs_tools
def test_result_is_green_only_when_every_job_succeeded():
    assert _result_step(_needs()).returncode == 0


@needs_tools
@pytest.mark.parametrize("over", [
    {"verify": "skipped"},
    {"verify": "failure"},
    {"verify": "cancelled"},
    {"deploy": "failure", "verify": "skipped"},
    {"preflight": "failure", "deploy": "skipped", "verify": "skipped"},
    {"schema_gate": "failure", "preflight": "skipped", "deploy": "skipped", "verify": "skipped"},  # AA-993 M4b
    {"build_and_push": "skipped", "deploy": "skipped", "verify": "skipped"},
])
def test_result_is_red_when_verify_did_not_pass(over):
    r = _result_step(_needs(**over))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "::error::Deploy Dev (push) is not verified" in r.stdout


@needs_tools
def test_result_is_red_when_a_job_result_is_missing():
    needs = _needs()
    del needs["verify"]
    assert _result_step(needs).returncode == 1


def test_required_schema_sed_reads_the_live_constant():
    """The verify reads REQUIRED_CIS_SCHEMA with sed (post-deploy-verify.yml); a reformatted line would turn every
    deploy red with 'not set: REQUIRED_SCHEMA'. Keep the sed and the constant in step."""
    import re
    import subprocess
    from api.core import readiness

    wf = (REPO / ".github/workflows/post-deploy-verify.yml").read_text()
    expr = re.search(r"sed -n '([^']+)'", wf).group(1)
    out = subprocess.run(["sed", "-n", expr, str(REPO / "api/core/readiness.py")], capture_output=True, text=True,
                         check=True).stdout.splitlines()
    assert out and int(out[0]) == int(readiness.REQUIRED_CIS_SCHEMA)


def test_aws_errors_are_scrubbed_before_the_summary():
    script = (REPO / ".github/scripts/post_deploy_verify.sh").read_text()
    assert "s/[0-9]{12}/<acct>/g" in script and "s/arn:aws:[^ ]*/<arn>/g" in script
    assert "got '${EXPECTED_SHA}'" not in script


@needs_tools
def test_aws_error_text_is_scrubbed_from_the_output(run_verify):
    r, summary = run_verify(*_good(), FAKE_AWS_ERR="1")
    assert r.returncode == 1
    assert "api-rollout" in _failed_rows(summary)
    for leak in ("123456789012", "arn:aws"):
        assert leak not in summary and leak not in r.stdout and leak not in r.stderr


@needs_tools
@pytest.mark.parametrize("image", [IMAGE, IMAGE.replace(SHA, "not-a-sha-SENTINEL")])
def test_an_invalid_expected_sha_is_never_echoed(run_verify, image):
    r, summary = run_verify(*_good(), EXPECTED_SHA="not-a-sha-SENTINEL", EXPECTED_IMAGE=image)
    assert r.returncode == 1
    assert "Expected commit `invalid`" in summary
    assert "SENTINEL" not in summary and "SENTINEL" not in r.stdout and "SENTINEL" not in r.stderr
