"""Tests for .github/scripts/ecs_smoke_rollback.py (AA-1119). Run from the repo root:

    python3 -m unittest discover -s .github/scripts -p 'test_ecs_smoke_rollback.py'

Stdlib only. A fake AWS (a dict of task definitions, the service, the ECR tags) answers the calls; no network, no AWS.
Covers: `previous` prints a target only for a settled service on a real build; `rollback` is refused (exit 3, no
write) whenever the previous revision is unknown or unsafe, is a no-op when already there, and with --apply moves
the service back and checks the running image. The same file in every repo that carries the script.
"""

from __future__ import annotations

import importlib.util
import io
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ecs_smoke_rollback", HERE / "ecs_smoke_rollback.py")
esr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(esr)

REGISTRY = "123456789012.dkr.ecr.us-west-1.amazonaws.com"
OLD = "9e1b7c3a5d2f4e6a8c0b1d3f5a7e9c2b4d6f8a0c"
NEW = "3f2a9c1e7b4d6a0f5e8c2b1d9a7f4e6c0b3d5a2e"
FAMILY = "app-dev-web"
ARN = "arn:aws:ecs:us-west-1:123456789012:task-definition/"


class FakeAws:
    """The calls ecs_smoke_rollback.py makes, answered from in-memory state."""

    def __init__(
        self, current=f"{FAMILY}:7", rollout="COMPLETED", deployments=1, desired=1, ecr=(OLD, NEW), stable_runs=None
    ):
        self.tds = {
            f"{FAMILY}:5": ("ACTIVE", "bootstrap"),
            f"{FAMILY}:6": ("ACTIVE", OLD),
            f"{FAMILY}:7": ("ACTIVE", NEW),
            f"{FAMILY}:4": ("INACTIVE", "1234567"),
        }
        self.current = current
        self.rollout = rollout
        self.deployments = deployments
        self.desired = desired
        self.ecr = set(ecr)
        self.stable_runs = stable_runs  # image tag the tasks run after the wait (None: the service's)
        self.single = False  # one container per task definition (no sidecar)
        self.calls: list[list[str]] = []
        self.writes: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        svc, op = args[0], args[1]
        opt = {args[i]: args[i + 1] for i in range(2, len(args) - 1) if args[i].startswith("--")}
        if (svc, op) == ("ecs", "describe-services"):
            deps = [{"rolloutState": self.rollout} for _ in range(self.deployments)]
            return {
                "services": [
                    {
                        "status": "ACTIVE",
                        "taskDefinition": ARN + self.current,
                        "deployments": deps,
                        "desiredCount": self.desired,
                    }
                ]
            }
        if (svc, op) == ("ecs", "describe-task-definition"):
            ref = opt["--task-definition"].rsplit("/", 1)[-1]
            status, tag = self.tds[ref]
            defs = [{"name": "web", "image": f"{REGISTRY}/{FAMILY}:{tag}"}]
            if not self.single:
                defs.append({"name": "sidecar", "image": "x:1"})
            return {"taskDefinition": {"status": status, "containerDefinitions": defs}}
        if (svc, op) == ("ecr", "describe-images"):
            tag = opt["--image-ids"].split("=", 1)[1]
            return {"imageDetails": [{"imageTags": [tag]}]} if tag in self.ecr else {"imageDetails": []}
        if (svc, op) == ("ecs", "update-service"):
            self.writes.append(args)
            self.current = opt["--task-definition"]
            return {"service": {}}
        if (svc, op) == ("ecs", "wait"):
            return None
        if (svc, op) == ("ecs", "list-tasks"):
            return {"taskArns": ["arn:task/1"] if self.desired else []}
        if (svc, op) == ("ecs", "describe-tasks"):
            tag = self.stable_runs or self.tds[self.current][1]
            return {"tasks": [{"containers": [{"name": "web", "image": f"{REGISTRY}/{FAMILY}:{tag}"}]}]}
        raise AssertionError(f"unexpected call {args}")


def run(argv, aws):
    out, err = io.StringIO(), io.StringIO()
    code = esr.main(argv, aws=aws, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


BASE = ["--cluster", "c", "--service", "app-dev-web", "--container", "web"]


def rb(previous=f"{FAMILY}:6", deployed=NEW, apply=True):
    argv = ["rollback", *BASE, "--repository", FAMILY, "--previous", previous, "--deployed-tag", deployed]
    return argv + (["--apply"] if apply else [])


class PreviousTests(unittest.TestCase):
    def test_settled_service_on_a_build_is_the_target(self):
        code, out, err = run(["previous", *BASE], FakeAws(current=f"{FAMILY}:6"))
        self.assertEqual(code, 0)
        self.assertIn(f"previous_td={FAMILY}:6\n", out)
        self.assertIn(f"previous_tag={OLD}\n", out)
        self.assertNotIn("123456789012", out + err)

    def test_template_revision_is_never_a_target(self):
        code, out, err = run(["previous", *BASE], FakeAws(current=f"{FAMILY}:5"))
        self.assertEqual(code, 0)
        self.assertIn("previous_td=\n", out)
        self.assertIn("not a build", err)

    def test_unsettled_service_gives_no_target(self):
        for kwargs in ({"rollout": "IN_PROGRESS"}, {"deployments": 2}):
            code, out, _ = run(["previous", *BASE], FakeAws(current=f"{FAMILY}:6", **kwargs))
            self.assertEqual(code, 0)
            self.assertIn("previous_td=\n", out)

    def test_aws_error_fails_without_leaking_the_account(self):
        def broken(args):
            raise esr.AwsError(esr.scrub("denied for arn:aws:iam::123456789012:role/x in 123456789012"))

        code, out, err = run(["previous", *BASE], broken)
        self.assertEqual(code, 1)
        self.assertNotIn("123456789012", out + err)


class RollbackTests(unittest.TestCase):
    def test_rolls_back_and_checks_the_running_image(self):
        aws = FakeAws()
        code, _, err = run(rb(), aws)
        self.assertEqual(code, 0, err)
        self.assertEqual(aws.current, f"{FAMILY}:6")
        self.assertEqual(len(aws.writes), 1)
        self.assertIn("ROLLED BACK", err)
        self.assertIn(
            f"way back: aws ecs update-service --cluster c --service app-dev-web --task-definition {FAMILY}:7", err
        )
        self.assertNotIn("123456789012", err)

    def test_dry_run_writes_nothing(self):
        aws = FakeAws()
        code, _, err = run(rb(apply=False), aws)
        self.assertEqual(code, 0)
        self.assertEqual(aws.writes, [])
        self.assertIn("DRY RUN", err)

    def test_already_on_previous_is_a_no_op(self):
        aws = FakeAws(current=f"{FAMILY}:6")
        code, _, err = run(rb(), aws)
        self.assertEqual(code, 0)
        self.assertEqual(aws.writes, [])
        self.assertIn("nothing to do", err)

    def test_refusals_change_nothing(self):
        cases = {
            "unknown previous": (rb(previous=""), {}),
            "not family:revision": (rb(previous="arn:aws:ecs:x"), {}),
            "moved by someone else": (rb(deployed="abcdef1234567"), {}),
            "deployment in progress": (rb(), {"rollout": "IN_PROGRESS"}),
            "two deployments": (rb(), {"deployments": 2}),
            "template target": (rb(previous=f"{FAMILY}:5"), {}),
            "inactive target": (rb(previous=f"{FAMILY}:4"), {}),
            "same build": (rb(previous=f"{FAMILY}:7", deployed=NEW), {"current": f"{FAMILY}:6"}),
            "image left ECR": (rb(), {"ecr": (NEW,)}),
        }
        for name, (argv, kwargs) in cases.items():
            with self.subTest(name):
                aws = FakeAws(**kwargs)
                if name == "same build":
                    aws.tds[f"{FAMILY}:6"] = ("ACTIVE", NEW)
                code, _, err = run(argv, aws)
                self.assertEqual(code, 3, err)
                self.assertEqual(aws.writes, [])
                self.assertIn("REFUSED", err)

    def test_wrong_image_after_the_wait_fails(self):
        aws = FakeAws(stable_runs=NEW)
        code, _, err = run(rb(), aws)
        self.assertEqual(code, 1)
        self.assertIn("expected", err)

    def test_desired_zero_is_refused(self):
        aws = FakeAws(desired=0)
        code, _, err = run(rb(), aws)
        self.assertEqual(code, 3, err)
        self.assertEqual(aws.writes, [])

    def test_without_container_the_only_container_is_used(self):
        aws = FakeAws()
        aws.single = True
        argv = [
            "rollback",
            "--cluster",
            "c",
            "--service",
            "app-dev-web",
            "--repository",
            FAMILY,
            "--previous",
            f"{FAMILY}:6",
            "--deployed-tag",
            NEW,
            "--apply",
        ]
        code, _, err = run(argv, aws)
        self.assertEqual(code, 0, err)
        self.assertEqual(aws.current, f"{FAMILY}:6")

    def test_without_container_two_containers_are_refused(self):
        argv = [
            "rollback",
            "--cluster",
            "c",
            "--service",
            "app-dev-web",
            "--repository",
            FAMILY,
            "--previous",
            f"{FAMILY}:6",
            "--deployed-tag",
            NEW,
            "--apply",
        ]
        aws = FakeAws()
        code, _, err = run(argv, aws)
        self.assertEqual(code, 3, err)
        self.assertEqual(aws.writes, [])

    def test_bad_arguments(self):
        code, _, _ = run(["rollback", *BASE], FakeAws())
        self.assertEqual(code, 2)


class WorkflowTests(unittest.TestCase):
    """Every deploy workflow that rolls back records the target first, acts only on main and reports (text checks)."""

    def workflows(self):
        found = {}
        for path in sorted((HERE.parent / "workflows").glob("*.yml")):
            text = path.read_text()
            if "ecs_smoke_rollback.py rollback" in text:
                found[path.name] = text
        return found

    def test_at_least_one_deploy_workflow_rolls_back(self):
        self.assertTrue(self.workflows())

    def test_shape(self):
        for name, text in self.workflows().items():
            with self.subTest(name):
                prev = text.index("ecs_smoke_rollback.py previous")
                self.assertLess(prev, text.index("ecs update-service"), "record the target before update-service")
                self.assertIn("previous_td: ${{ steps.before.outputs.previous_td }}", text)
                job = text[text.index("\n  rollback:\n") :]
                job = job[: job.index("\n  report:\n")]
                self.assertIn("github.ref == 'refs/heads/main'", job)
                self.assertIn("needs.deploy.result == 'success'", job)
                self.assertIn('--previous "$PREVIOUS_TD"', job)
                self.assertIn("--apply", job)
                self.assertIn('[ "$status" = 0 ] || [ "$status" = 3 ]', job)
                report = text[text.index("\n  report:\n") :]
                report = report[: report.index("\n  result:\n")]
                self.assertIn("issues: write", report)
                self.assertNotIn("id-token", report)
                self.assertNotIn("actions/checkout", report)
                self.assertIn("--label deploy-smoke-red", report)


class HelperTests(unittest.TestCase):
    def test_image_tag(self):
        self.assertEqual(esr.image_tag(f"{REGISTRY}/r:dev-{OLD}"), f"dev-{OLD}")
        self.assertEqual(esr.image_tag("r@sha256:abc"), "")
        self.assertEqual(esr.image_tag("localhost:5000/r"), "")

    def test_build_tag(self):
        for tag in (OLD, f"dev-{OLD}", OLD[:7]):
            self.assertTrue(esr.BUILD_TAG.match(tag), tag)
        for tag in ("bootstrap", "latest", "", "dev-", "v1.2.3"):
            self.assertFalse(esr.BUILD_TAG.match(tag), tag)


if __name__ == "__main__":
    unittest.main()
