"""Tests for .github/scripts/ecs_api_rollback.py (AA-1193). Run from the repo root:

    python3 -m unittest discover -s .github/scripts -p 'test_ecs_api_rollback.py'

Stdlib only. A fake AWS (task definitions, two services, the ECR tags, the SNS topic) and a fake health URL answer
the calls; no network, no AWS. Covers: all targets roll back or none (a refused worker keeps the api where it is);
the health URL must show the previous commit; a re-run is a no-op; the alert reads the topic from the running task
definition, never prints it, and says "not sent" when the topic or the grant is missing. The same file in every
repo that carries the script.
"""

from __future__ import annotations

import importlib.util
import io
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ecs_api_rollback", HERE / "ecs_api_rollback.py")
ear = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ear)

REGISTRY = "123456789012.dkr.ecr.us-west-1.amazonaws.com"
TOPIC = "arn:aws:sns:us-west-1:123456789012:app-dev-ops-alerts"
OLD = "9e1b7c3a5d2f4e6a8c0b1d3f5a7e9c2b4d6f8a0c"
NEW = "3f2a9c1e7b4d6a0f5e8c2b1d9a7f4e6c0b3d5a2e"
ARN = "arn:aws:ecs:us-west-1:123456789012:task-definition/"
RUN_URL = "https://github.example.invalid/org/app/actions/runs/1"


class FakeAws:
    """Two services (app-dev-api, app-dev-worker), each on revision 7 (NEW) with revision 6 (OLD) before it."""

    def __init__(self, prefix="", ecr=(OLD, NEW), topic=TOPIC, publish_denied=False):
        self.prefix = prefix  # "dev-" for CIS-style tags
        self.tds = {}
        for fam, name in (("app-dev-api", "api"), ("app-dev-worker", "worker")):
            self.tds[f"{fam}:6"] = ("ACTIVE", OLD, name)
            self.tds[f"{fam}:7"] = ("ACTIVE", NEW, name)
            self.tds[f"{fam}:5"] = ("ACTIVE", "bootstrap", name)
        self.current = {"app-dev-api": "app-dev-api:7", "app-dev-worker": "app-dev-worker:7"}
        self.ecr = {prefix + t for t in ecr}
        self.topic = topic
        self.publish_denied = publish_denied
        self.writes: list[list[str]] = []
        self.published: list[dict] = []

    def tag(self, td):
        status, sha, _ = self.tds[td]
        return status, (self.prefix + sha if sha != "bootstrap" else sha)

    def __call__(self, args):
        svc, op = args[0], args[1]
        opt = {args[i]: args[i + 1] for i in range(2, len(args) - 1) if args[i].startswith("--")}
        if (svc, op) == ("ecs", "describe-services"):
            name = opt["--services"]
            return {
                "services": [
                    {
                        "status": "ACTIVE",
                        "taskDefinition": ARN + self.current[name],
                        "deployments": [{"rolloutState": "COMPLETED"}],
                        "desiredCount": 1,
                    }
                ]
            }
        if (svc, op) == ("ecs", "describe-task-definition"):
            ref = opt["--task-definition"].rsplit("/", 1)[-1]
            status, tag = self.tag(ref)
            name = self.tds[ref][2]
            env = [{"name": "ALERTS_TOPIC_ARN", "value": self.topic}] if self.topic else []
            image = f"{REGISTRY}/app-dev-api:{tag}"
            defs = [{"name": name, "image": image, "environment": env}]
            return {"taskDefinition": {"status": status, "containerDefinitions": defs}}
        if (svc, op) == ("ecr", "describe-images"):
            tag = opt["--image-ids"].split("=", 1)[1]
            return {"imageDetails": [{"imageTags": [tag]}]} if tag in self.ecr else {"imageDetails": []}
        if (svc, op) == ("ecs", "update-service"):
            self.writes.append(args)
            self.current[opt["--service"]] = opt["--task-definition"]
            return {"service": {}}
        if (svc, op) == ("ecs", "wait"):
            return None
        if (svc, op) == ("ecs", "list-tasks"):
            return {"taskArns": ["arn:task/1"]}
        if (svc, op) == ("ecs", "describe-tasks"):
            # Containers of both services; ecs_smoke_rollback filters by container name (api / worker).
            return {"tasks": [{"containers": [{"name": c, "image": i} for c, i in self._running()]}]}
        if (svc, op) == ("sns", "publish"):
            if self.publish_denied:
                raise ear.esr.AwsError(ear.esr.scrub(f"sns publish failed: AccessDenied on {opt['--topic-arn']}"))
            self.published.append(opt)
            return {"MessageId": "m-1"}
        raise AssertionError(f"unexpected call {args}")

    def _running(self):
        for td in self.current.values():
            _, tag = self.tag(td)
            yield self.tds[td][2], f"{REGISTRY}/app-dev-api:{tag}"


class FakeHealth:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        a = self.answers[min(self.calls - 1, len(self.answers) - 1)]
        if isinstance(a, Exception):
            raise a
        return a


def run(argv, aws, health=None):
    out, err = io.StringIO(), io.StringIO()
    code = ear.main(argv, aws=aws, out=out, err=err, fetch=health or FakeHealth([{}]), sleep=lambda s: None)
    return code, out.getvalue(), err.getvalue()


API = "app-dev-api,api,app-dev-api:6"
WORKER = "app-dev-worker,worker,app-dev-worker:6"


def rb(*targets, deployed=NEW, apply=True):
    argv = ["rollback", "--cluster", "c", "--repository", "app-dev-api", "--deployed-tag", deployed]
    for t in targets or (API, WORKER):
        argv += ["--target", t]
    argv += ["--health-url", "https://api.example.invalid/health", "--health-tries", "3"]
    return argv + (["--apply"] if apply else [])


def outputs(out):
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


class RollbackTests(unittest.TestCase):
    def test_both_services_roll_back_and_health_shows_the_old_commit(self):
        aws = FakeAws()
        code, out, err = run(rb(), aws, FakeHealth([{"git_sha": OLD}]))
        self.assertEqual(code, 0, err)
        self.assertEqual(aws.current, {"app-dev-api": "app-dev-api:6", "app-dev-worker": "app-dev-worker:6"})
        o = outputs(out)
        self.assertEqual(o["rolled_back"], "app-dev-api:6,app-dev-worker:6")
        self.assertTrue(o["outcome"].startswith("rolled back to app-dev-api:6, app-dev-worker:6"), o)
        self.assertNotIn("123456789012", out + err)

    def test_dev_prefixed_tags_compare_the_commit_without_the_prefix(self):
        aws = FakeAws(prefix="dev-")
        code, out, err = run(rb(deployed="dev-" + NEW), aws, FakeHealth([{"git_sha": OLD}]))
        self.assertEqual(code, 0, err)
        self.assertIn(f"/health on {OLD[:12]}", outputs(out)["outcome"])

    def test_a_refused_worker_keeps_the_api_where_it_is(self):
        aws = FakeAws()
        code, out, err = run(rb(API, "app-dev-worker,worker,app-dev-worker:5"), aws)
        self.assertEqual(code, 3, err)
        self.assertEqual(aws.writes, [], "all or none: nothing is written when one target is unsafe")
        self.assertIn("refused: app-dev-worker", outputs(out)["outcome"])

    def test_unknown_previous_revision_is_refused(self):
        aws = FakeAws()
        code, out, _ = run(rb("app-dev-api,api,"), aws)
        self.assertEqual(code, 3)
        self.assertEqual(aws.writes, [])
        self.assertEqual(outputs(out)["rolled_back"], "")

    def test_image_gone_from_ecr_is_refused(self):
        aws = FakeAws(ecr=(NEW,))
        code, out, _ = run(rb(), aws)
        self.assertEqual(code, 3)
        self.assertIn("not in ECR", outputs(out)["outcome"])
        self.assertEqual(aws.writes, [])

    def test_dry_run_writes_nothing(self):
        aws = FakeAws()
        code, out, _ = run(rb(apply=False), aws)
        self.assertEqual(code, 0)
        self.assertEqual(aws.writes, [])
        self.assertTrue(outputs(out)["outcome"].startswith("dry run"))

    def test_health_on_the_wrong_commit_is_red_after_retries(self):
        aws = FakeAws()
        health = FakeHealth([{"git_sha": NEW}])
        code, out, _ = run(rb(), aws, health)
        self.assertEqual(code, 1)
        self.assertEqual(health.calls, 3)
        self.assertIn("but /health answers git_sha", outputs(out)["outcome"])
        self.assertEqual(outputs(out)["rolled_back"], "app-dev-api:6,app-dev-worker:6")

    def test_health_errors_are_retried(self):
        aws = FakeAws()
        health = FakeHealth([OSError("reset"), {"git_sha": OLD}])
        code, _, err = run(rb(), aws, health)
        self.assertEqual(code, 0, err)
        self.assertEqual(health.calls, 2)

    def test_rerun_after_a_rollback_writes_nothing(self):
        aws = FakeAws()
        run(rb(), aws, FakeHealth([{"git_sha": OLD}]))
        writes = len(aws.writes)
        code, _, err = run(rb(), aws, FakeHealth([{"git_sha": OLD}]))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(aws.writes), writes, "a re-run finds the services back: no second write")

    def test_bad_target_is_a_usage_error(self):
        code, _, err = run(rb("only-a-service"), FakeAws())
        self.assertEqual(code, 2)
        self.assertIn("--target must be", err)


def al(*extra, apply=True):
    argv = [
        "alert", "--cluster", "c", "--service", "app-dev-api", "--container", "api", "--app", "CIS api",
        "--commit", NEW, "--failed-checks", "health,worker-sha", "--outcome", "rolled back to app-dev-api:6",
        "--run-url", RUN_URL, *extra,
    ]
    return argv + (["--apply"] if apply else [])


class AlertTests(unittest.TestCase):
    def test_publishes_to_the_topic_of_the_running_task_definition(self):
        aws = FakeAws()
        code, out, err = run(al(), aws)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(aws.published), 1)
        msg = aws.published[0]
        self.assertEqual(msg["--topic-arn"], TOPIC)
        self.assertEqual(msg["--subject"], "[aa-deploy] CIS api Dev: post-deploy check red, rolled back")
        self.assertIn("Failing checks: health,worker-sha", msg["--message"])
        self.assertIn("Rollback: rolled back to app-dev-api:6", msg["--message"])
        self.assertIn(RUN_URL, msg["--message"])
        self.assertNotIn(TOPIC, out + err, "the topic ARN names the account: never printed")
        self.assertNotIn("123456789012", out + err)

    def test_drill_and_failed_rollback_show_in_the_subject(self):
        aws = FakeAws()
        argv = al("--drill")
        argv[argv.index("--outcome") + 1] = "rollback FAILED at app-dev-worker"
        code, _, _ = run(argv, aws)
        self.assertEqual(code, 0)
        subject = aws.published[0]["--subject"]
        self.assertTrue(subject.startswith("[aa-deploy drill] "), subject)
        self.assertTrue(subject.endswith("ROLLBACK FAILED"), subject)
        self.assertLessEqual(len(subject), ear.SUBJECT_MAX)
        self.assertIn("Drill: yes", aws.published[0]["--message"])

    def test_refused_rollback_reads_not_rolled_back(self):
        aws = FakeAws()
        argv = al()
        argv[argv.index("--outcome") + 1] = "not rolled back (held by the deploy gate: a wave is open)"
        run(argv, aws)
        self.assertTrue(aws.published[0]["--subject"].endswith("not rolled back"))

    def test_no_topic_on_the_task_definition_is_not_sent(self):
        aws = FakeAws(topic="")
        code, _, err = run(al(), aws)
        self.assertEqual(code, 4)
        self.assertEqual(aws.published, [])
        self.assertIn("no ALERTS_TOPIC_ARN", err)

    def test_publish_denied_is_not_sent_and_scrubbed(self):
        aws = FakeAws(publish_denied=True)
        code, out, err = run(al(), aws)
        self.assertEqual(code, 4)
        self.assertIn("NOT SENT", err)
        self.assertNotIn("123456789012", out + err)

    def test_dry_run_prints_the_message_only(self):
        aws = FakeAws()
        code, _, err = run(al(apply=False), aws)
        self.assertEqual(code, 0)
        self.assertEqual(aws.published, [])
        self.assertIn("DRY RUN", err)

    def test_a_non_sha_commit_is_not_echoed(self):
        aws = FakeAws()
        argv = al()
        argv[argv.index("--commit") + 1] = "$(whoami)"
        run(argv, aws)
        self.assertIn("after deploying unknown", aws.published[0]["--message"])


if __name__ == "__main__":
    unittest.main()
