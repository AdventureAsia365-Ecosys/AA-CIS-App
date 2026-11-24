#!/usr/bin/env python3
"""Roll an ECS web service back to the revision it ran before this deploy when the post-deploy smoke is red (AA-1119).

The S282 audit (R32) found that a red post-deploy verify or smoke left the bad build serving: the ECS deployment circuit
breaker only catches a task that never becomes healthy. Each web deploy workflow (AA-CIS-App deploy-admin-dev.yml,
AA-Booking deploy-admin-dev.yml, AA-Partner deploy-web-dev.yml, AA-TripPlanner-Web deploy-web-dev.yml) now records the
revision the service ran before its update-service (`previous`) and, when its verify or smoke job fails, runs
`rollback` with it. Same mechanism as the root runbook docs/ops/runbooks/rollback-ecs.md and its helper
tools/ops/ecs_rollback.sh (target = the revision before the bad deploy), in a form a deploy job can run unattended.

  previous   Reads the service before the deploy touches it. Prints `previous_td=<family:revision>` and
             `previous_tag=<image tag>` for `$GITHUB_OUTPUT` only when the previous revision is KNOWN and SAFE: the
             service exists, exactly one deployment with rollout COMPLETED, the container is there once, and its image
             tag is a real build (`<sha>` or `dev-<sha>`, never a Terraform template tag such as `bootstrap` or
             `latest`). Otherwise it prints empty values and the reason on stderr: an empty `previous_td` means
             "never roll back".
  rollback   Moves the service back to --previous, once, and only when it is safe. Refused (exit 3, nothing written):
             no previous revision; desired count 0; the previous image tag equals the deployed one; the service no
             longer runs the deployed image (someone moved it: a later deploy or a hand rollback); a deployment is
             still in progress;
             the previous revision is not ACTIVE, not a real build, or its image left ECR. Already on --previous:
             exit 0 without a write (a re-run is idempotent). Without --apply it is a dry run that prints the command.
             With --apply: update-service, wait services-stable, then every running task must carry the previous
             image (exit 1 otherwise).

Logs name `family:revision` and image tags only, never a full ARN or image URI (the account id; public logs).
Stdlib only; the AWS CLI does the calls (the deploy job's OIDC credentials). The same file in AA-CIS-App, AA-Booking,
AA-Partner and AA-TripPlanner-Web (tests: .github/scripts/test_ecs_smoke_rollback.py): change it in all of them in the
same wave.
Run: python3 .github/scripts/ecs_smoke_rollback.py previous --cluster C --service S [--container N]
     python3 .github/scripts/ecs_smoke_rollback.py rollback --cluster C --service S [--container N] --repository R
       --previous FAMILY:REV --deployed-tag TAG [--apply]
Exit 0 = done (or nothing to do), 1 = an AWS call or the rollback itself failed, 2 = bad arguments,
3 = refused (unsafe: nothing was changed).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable

# A real build's image tag: the git SHA (Booking, CRM, TripPlanner) or dev-<sha> (CIS). Terraform registers template
# revisions with placeholder tags (bootstrap, latest); they are never a rollback target.
BUILD_TAG = re.compile(r"^(?:dev-)?[0-9a-f]{7,40}$")
TD_REF = re.compile(r"^[A-Za-z0-9_-]+:[0-9]+$")
EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 1, 2, 3

Aws = Callable[[list[str]], object]


class AwsError(Exception):
    pass


class Refused(Exception):
    pass


def aws_cli(args: list[str]) -> object:
    """Run `aws <args> --output json` and return the parsed JSON (None for an empty answer)."""
    proc = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        # The first line only, without anything that looks like an ARN or an account id (public logs).
        first = (proc.stderr.strip().splitlines() or ["(no message)"])[0]
        raise AwsError(f"aws {' '.join(args[:2])} failed: {scrub(first)}")
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def scrub(text: str) -> str:
    text = re.sub(r"arn:aws[a-z-]*:[^\s\"']+", "<arn>", text)
    text = re.sub(r"\b\d{12}\b", "<account>", text)
    return re.sub(r"[0-9]+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/", "", text)


def td_ref(arn: str) -> str:
    """family:revision from a task-definition ARN (or a family:revision already)."""
    return arn.rsplit("/", 1)[-1]


def image_tag(image: str) -> str:
    if "@" in image:
        return ""  # pinned by digest: not a build this workflow made
    last = image.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else ""


def describe_service(aws: Aws, cluster: str, service: str) -> dict:
    data = aws(["ecs", "describe-services", "--cluster", cluster, "--services", service])
    services = (data or {}).get("services") or []
    if len(services) != 1 or services[0].get("status") != "ACTIVE":
        raise Refused(f"service {service} not found or not ACTIVE")
    return services[0]


def container_tag(aws: Aws, td: str, container: str) -> tuple[str, str]:
    """(status, image tag) of `container` in task definition `td`."""
    data = aws(["ecs", "describe-task-definition", "--task-definition", td])
    taskdef = (data or {}).get("taskDefinition") or {}
    defs = taskdef.get("containerDefinitions") or []
    hits = [c for c in defs if c.get("name") == container] if container else defs
    if len(hits) != 1:
        raise Refused(
            f"{td_ref(td)} has no single container {('named ' + container) if container else '(no --container)'}"
        )
    return taskdef.get("status", ""), image_tag(hits[0].get("image", ""))


def previous(aws: Aws, cluster: str, service: str, container: str) -> tuple[str, str]:
    svc = describe_service(aws, cluster, service)
    deployments = svc.get("deployments") or []
    if len(deployments) != 1 or deployments[0].get("rolloutState") != "COMPLETED":
        states = ", ".join(str(d.get("rolloutState")) for d in deployments) or "none"
        raise Refused(f"the service is not settled before the deploy (deployments: {states})")
    td = svc.get("taskDefinition") or ""
    if not td:
        raise Refused("the service names no task definition")
    _, tag = container_tag(aws, td, container)
    if not BUILD_TAG.match(tag):
        raise Refused(f"{td_ref(td)} runs image tag '{tag or '?'}', not a build (Terraform template revision)")
    return td_ref(td), tag


def running_tags(aws: Aws, cluster: str, service: str, container: str) -> list[str]:
    listed = aws(["ecs", "list-tasks", "--cluster", cluster, "--service-name", service, "--desired-status", "RUNNING"])
    arns = (listed or {}).get("taskArns") or []
    if not arns:
        return []
    tasks = (aws(["ecs", "describe-tasks", "--cluster", cluster, "--tasks", *arns]) or {}).get("tasks") or []
    return [
        image_tag(c.get("image", ""))
        for t in tasks
        for c in t.get("containers") or []
        if not container or c.get("name") == container
    ]


def _settled(svc: dict) -> None:
    """Refuse a service that runs nothing or is still rolling out (the circuit breaker acts first)."""
    if not svc.get("desiredCount"):
        raise Refused("the service has desired count 0: no task serves the deployed build")
    deployments = svc.get("deployments") or []
    if len(deployments) != 1 or deployments[0].get("rolloutState") != "COMPLETED":
        states = ", ".join(str(d.get("rolloutState")) for d in deployments) or "none"
        raise Refused(f"a deployment is in progress (deployments: {states}); the circuit breaker acts first")


def _target_tag(aws: Aws, a: argparse.Namespace) -> str:
    """The image tag of --previous once it is a safe target: ACTIVE, a real build, another build, still in ECR."""
    status, prev_tag = container_tag(aws, a.previous, a.container)
    if status != "ACTIVE":
        raise Refused(f"{a.previous} is {status or 'unknown'}, not ACTIVE")
    if not BUILD_TAG.match(prev_tag):
        raise Refused(f"{a.previous} runs image tag '{prev_tag or '?'}', not a build")
    if prev_tag == a.deployed_tag:
        raise Refused(f"{a.previous} runs the same build ({prev_tag}): a rollback would change nothing")
    found = aws(["ecr", "describe-images", "--repository-name", a.repository, "--image-ids", f"imageTag={prev_tag}"])
    if not (found or {}).get("imageDetails"):
        raise Refused(f"image {prev_tag} is not in ECR any more: fix forward")
    return prev_tag


def _apply(aws: Aws, a: argparse.Namespace, prev_tag: str, log: Callable[[str], None]) -> int:
    """update-service to --previous, wait stable, then the service and every running task are on it."""
    aws(["ecs", "update-service", "--cluster", a.cluster, "--service", a.service, "--task-definition", a.previous])
    aws(["ecs", "wait", "services-stable", "--cluster", a.cluster, "--services", a.service])
    now = td_ref(describe_service(aws, a.cluster, a.service).get("taskDefinition") or "")
    if now != a.previous:
        log(f"the service is on {now}, not {a.previous}")
        return EXIT_FAILED
    tags = running_tags(aws, a.cluster, a.service, a.container)
    if not tags or any(t != prev_tag for t in tags):
        log(f"running tasks carry {', '.join(tags) or 'nothing'}, expected {prev_tag}")
        return EXIT_FAILED
    log(f"ROLLED BACK: {a.service} on {a.previous} (image {prev_tag})")
    return 0


def rollback(aws: Aws, a: argparse.Namespace, log: Callable[[str], None]) -> int:
    if not a.previous:
        raise Refused("no previous revision was recorded before the deploy: nothing is rolled back")
    if not TD_REF.match(a.previous):
        raise Refused(f"--previous must be family:revision, got '{a.previous}'")
    svc = describe_service(aws, a.cluster, a.service)
    current = td_ref(svc.get("taskDefinition") or "")
    if current == a.previous:
        log(f"already on {a.previous}: nothing to do")
        return 0
    _settled(svc)
    _, current_tag = container_tag(aws, current, a.container)
    if current_tag != a.deployed_tag:
        raise Refused(
            f"the service runs {current} ({current_tag}), not the deployed {a.deployed_tag}: someone moved it"
        )
    prev_tag = _target_tag(aws, a)
    log(f"target: {a.previous} (image {prev_tag}); now: {current} (image {current_tag})")
    log(f"way back: aws ecs update-service --cluster {a.cluster} --service {a.service} --task-definition {current}")
    if not a.apply:
        log("DRY RUN: nothing changed")
        return 0
    return _apply(aws, a, prev_tag, log)


def main(argv: list[str] | None = None, aws: Aws = aws_cli, out=sys.stdout, err=sys.stderr) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("previous", "rollback"):
        s = sub.add_parser(name)
        s.add_argument("--cluster", required=True)
        s.add_argument("--service", required=True)
        # Empty: the task definition's only container (AA-Partner's web family has one, unnamed in its workflow).
        s.add_argument("--container", default="")
        if name == "rollback":
            s.add_argument("--repository", required=True)
            s.add_argument("--previous", default="")
            s.add_argument("--deployed-tag", required=True)
            s.add_argument("--apply", action="store_true")
    try:
        a = p.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else 0

    def log(msg: str) -> None:
        print(msg, file=err)

    try:
        if a.cmd == "previous":
            try:
                td, tag = previous(aws, a.cluster, a.service, a.container)
            except Refused as exc:
                log(f"no rollback target: {exc}")
                td, tag = "", ""
            else:
                log(f"previous revision: {td} (image {tag})")
            print(f"previous_td={td}", file=out)
            print(f"previous_tag={tag}", file=out)
            return 0
        return rollback(aws, a, log)
    except Refused as exc:
        log(f"REFUSED (nothing changed): {exc}")
        return EXIT_REFUSED
    except AwsError as exc:
        log(f"ERROR: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
