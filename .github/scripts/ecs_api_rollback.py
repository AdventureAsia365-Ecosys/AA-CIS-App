#!/usr/bin/env python3
"""Roll an API deploy back when its post-deploy verify is red, and tell the ops alerts topic (AA-1193).

AA-1119 gave the four web/admin deploy workflows an unattended rollback (ecs_smoke_rollback.py). The S295
Well-Architected review (OPS06) found the API deploys still without one: the ECS circuit breaker only catches a task
that never becomes healthy, so an API build that starts, passes its health check and then fails a real check kept
serving. This script is the API form, run by the `rollback` job of deploy-dev.yml in AA-CIS-App (api + worker, one
image), AA-Booking (api) and AA-Partner (CRM api). It reuses ecs_smoke_rollback.py (same directory) for every
per-service safety rule and adds what an API rollback needs on top:

  rollback   Moves every --target back to its previous revision, all or none: each target is first checked with a
             dry run (ecs_smoke_rollback.rollback without --apply), and when ANY of them is refused nothing is
             written (CIS api and worker must keep one image). Then, with --apply, each target in the given order:
             update-service, wait services-stable, the running tasks carry the previous image. Last, the public
             health URL must answer the previous build's commit in `git_sha` (the tag without its `dev-` prefix).
             Prints `outcome=<one line>` and `rolled_back=<family:revision,...>` for $GITHUB_OUTPUT.
  alert      Publishes one message on the alerts SNS topic (the ops topic of every alarm and in-app check): the app,
             the commit, the failing checks, the rollback outcome and the run link. The topic comes from the
             ALERTS_TOPIC_ARN environment entry of the task definition the service runs (Terraform sets it on every
             API task), so no GitHub setting holds it; it is never printed. Without --apply it prints the message only.

A target is `SERVICE,CONTAINER,FAMILY:REVISION` (CONTAINER may be empty: the task definition's only container; an
empty FAMILY:REVISION means "unknown before the deploy" and refuses the whole rollback).
Never a loop: the rollback is an update-service to a revision that already exists. It builds nothing, pushes nothing
and starts no workflow, so it cannot trigger another deploy; a re-run finds the services already back (no write).
Logs name `family:revision` and image tags only, never an ARN, an image URI or the topic (public logs).
Stdlib only; the AWS CLI makes the calls with the job's OIDC credentials. The same file in AA-CIS-App, AA-Booking and
AA-Partner (tests: .github/scripts/test_ecs_api_rollback.py): change it in all of them in the same wave.

Run: python3 .github/scripts/ecs_api_rollback.py rollback --cluster C --repository R --deployed-tag TAG
       --target SVC,CONTAINER,FAMILY:REV [--target ...] --health-url URL [--health-tries N] [--health-delay S] [--apply]
     python3 .github/scripts/ecs_api_rollback.py alert --cluster C --service S [--container N] --app NAME
       --commit SHA --failed-checks LIST --outcome TEXT --run-url URL [--drill] [--apply]
Exit 0 = done (or nothing to do), 1 = an AWS call, the rollback or the health check failed, 2 = bad arguments,
3 = refused (unsafe: nothing was changed), 4 = alert not sent (no topic on the task definition, or publish denied).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "ecs_smoke_rollback", Path(__file__).resolve().parent / "ecs_smoke_rollback.py"
)
esr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(esr)

EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED, EXIT_NOT_SENT = 1, 2, 3, 4
TOPIC_ARN = re.compile(r"^arn:aws[a-z-]*:sns:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_-]{1,256}$")
SHA = re.compile(r"^[0-9a-f]{7,40}$")
SUBJECT_MAX = 100  # SNS e-mail subject limit (ASCII, no newline)
FIELD_MAX = 400  # one free-text field of the alert (failing checks, outcome)

Fetch = Callable[[str], dict]
Log = Callable[[str], None]


def fetch_json(url: str) -> dict:
    """GET url and return its JSON object ({} when the answer is not one)."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "deploy-rollback"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (https URL from the workflow)
        body = json.loads(resp.read().decode("utf-8") or "{}")
    return body if isinstance(body, dict) else {}


def parse_target(raw: str) -> argparse.Namespace:
    parts = raw.split(",")
    if len(parts) != 3 or not parts[0]:
        raise ValueError(f"--target must be SERVICE,CONTAINER,FAMILY:REVISION, got '{raw}'")
    return argparse.Namespace(service=parts[0], container=parts[1], previous=parts[2])


def build_sha(tag: str) -> str:
    """The commit of a build tag (`<sha>` or `dev-<sha>`)."""
    return tag[4:] if tag.startswith("dev-") else tag


def _ns(a: argparse.Namespace, t: argparse.Namespace, apply: bool) -> argparse.Namespace:
    return argparse.Namespace(
        cluster=a.cluster,
        service=t.service,
        container=t.container,
        repository=a.repository,
        previous=t.previous,
        deployed_tag=a.deployed_tag,
        apply=apply,
    )


def _health(url: str, want: str, tries: int, delay: float, fetch: Fetch, sleep: Callable[[float], None]) -> str:
    """'' when url answers git_sha == want within `tries`, else what it answered last."""
    seen = "no answer"
    for i in range(max(tries, 1)):
        try:
            got = str(fetch(url).get("git_sha") or "")
        except Exception as exc:  # noqa: BLE001 (network, HTTP or JSON error: retried, then reported)
            seen = f"error {type(exc).__name__}"
        else:
            if got == want:
                return ""
            seen = f"git_sha {got[:12] or 'missing'}"
        if i + 1 < tries:
            sleep(delay)
    return seen


def rollback(
    aws: esr.Aws, a: argparse.Namespace, log: Log, out: Log, fetch: Fetch, sleep: Callable[[float], None]
) -> int:
    """All targets back to their previous revision, or none; then the health URL must show the previous commit."""

    def finish(code: int, outcome: str, done: list[str]) -> int:
        out(f"outcome={outcome}")
        out(f"rolled_back={','.join(done)}")
        return code

    targets = [parse_target(raw) for raw in a.target]
    # 1. Every target must be safe before anything is written (api and worker keep one image).
    prev_tags: list[str] = []
    for t in targets:
        try:
            esr.rollback(aws, _ns(a, t, apply=False), lambda m, s=t.service: log(f"[{s}] {m}"))
            _, tag = esr.container_tag(aws, t.previous, t.container)
        except esr.Refused as exc:
            log(f"REFUSED (nothing changed): {t.service}: {exc}")
            return finish(EXIT_REFUSED, f"not rolled back (refused: {t.service}: {esr.scrub(str(exc))[:200]})", [])
        except esr.AwsError as exc:
            log(f"ERROR: {exc}")
            return finish(EXIT_FAILED, f"rollback FAILED before any change ({t.service}: AWS error)", [])
        prev_tags.append(tag)
    want = build_sha(prev_tags[0])
    if not a.apply:
        log("DRY RUN: every target is safe; nothing changed")
        return finish(0, f"dry run: would roll back to {', '.join(t.previous for t in targets)}", [])
    # 2. Apply in order; stop at the first failure (the alert names what moved).
    done: list[str] = []
    for t in targets:
        try:
            code = esr.rollback(aws, _ns(a, t, apply=True), lambda m, s=t.service: log(f"[{s}] {m}"))
        except (esr.Refused, esr.AwsError) as exc:
            log(f"ERROR: {t.service}: {exc}")
            code = EXIT_FAILED
        if code != 0:
            moved = f"; already rolled back: {', '.join(done)}" if done else ""
            return finish(EXIT_FAILED, f"rollback FAILED at {t.service}{moved}", done)
        done.append(t.previous)
    # 3. The public health check answers the previous build.
    seen = _health(a.health_url, want, a.health_tries, a.health_delay, fetch, sleep)
    if seen:
        log(f"health: {a.health_url} answers {seen}, expected git_sha {want[:12]}")
        return finish(EXIT_FAILED, f"rolled back to {', '.join(done)} but /health answers {seen}", done)
    log(f"health: {a.health_url} answers git_sha {want[:12]} (the previous build)")
    return finish(0, f"rolled back to {', '.join(done)} (/health on {want[:12]})", done)


def topic_of(aws: esr.Aws, cluster: str, service: str, container: str) -> str:
    """ALERTS_TOPIC_ARN of the task definition the service runs ('' when absent or malformed). Never logged."""
    svc = esr.describe_service(aws, cluster, service)
    data = aws(["ecs", "describe-task-definition", "--task-definition", svc.get("taskDefinition") or ""])
    defs = ((data or {}).get("taskDefinition") or {}).get("containerDefinitions") or []
    for c in defs:
        if container and c.get("name") != container:
            continue
        for env in c.get("environment") or []:
            if env.get("name") == "ALERTS_TOPIC_ARN" and TOPIC_ARN.match(str(env.get("value") or "")):
                return str(env["value"])
    return ""


def _clean(text: str, limit: int = FIELD_MAX) -> str:
    text = esr.scrub(" ".join(str(text).split()))
    return text[:limit] if text else ""


def message(a: argparse.Namespace) -> tuple[str, str]:
    """(subject, body) of the alert. Subject: ASCII, one line, at most SUBJECT_MAX characters."""
    outcome = _clean(a.outcome) or "rollback did not run"
    low = outcome.lower()
    if low.startswith("rolled back") and "but" not in low:
        short = "rolled back"
    elif "failed" in low or " but " in low:
        short = "ROLLBACK FAILED"
    else:
        short = "not rolled back"
    commit = a.commit if SHA.match(a.commit or "") else "unknown"
    tag = "[aa-deploy drill]" if a.drill else "[aa-deploy]"
    subject = f"{tag} {_clean(a.app, 30)} Dev: post-deploy check red, {short}"
    subject = subject.encode("ascii", "replace").decode("ascii")[:SUBJECT_MAX]
    lines = [
        f"{_clean(a.app, 30)} Dev: the post-deploy verify is red after deploying {commit[:12]}.",
        f"Failing checks: {_clean(a.failed_checks) or 'see the run (the verify job did not name them)'}",
        f"Rollback: {outcome}",
        f"Run: {_clean(a.run_url, 300)}",
    ]
    if a.drill:
        lines.append("Drill: yes. The run forced the verify red on purpose (drill_force_smoke_fail); nothing broke.")
    lines += [
        "",
        "A rollback leaves main ahead of what runs: fix forward on main (revert or fix); the next verified deploy",
        "closes the deploy-smoke-red issue. Runbook: docs/ops/runbooks/rollback-ecs.md in the root repo.",
    ]
    return subject, "\n".join(lines)


def alert(aws: esr.Aws, a: argparse.Namespace, log: Log) -> int:
    subject, body = message(a)
    log(f"subject: {subject}")
    log(body)
    if not a.apply:
        log("DRY RUN: nothing published")
        return 0
    try:
        topic = topic_of(aws, a.cluster, a.service, a.container)
    except (esr.Refused, esr.AwsError) as exc:
        log(f"NOT SENT: cannot read the service's task definition ({exc})")
        return EXIT_NOT_SENT
    if not topic:
        log("NOT SENT: the running task definition has no ALERTS_TOPIC_ARN entry")
        return EXIT_NOT_SENT
    try:
        aws(["sns", "publish", "--topic-arn", topic, "--subject", subject, "--message", body])
    except esr.AwsError as exc:
        log(f"NOT SENT: {exc}")
        return EXIT_NOT_SENT
    log("SENT to the alerts topic")
    return 0


def main(
    argv: list[str] | None = None,
    aws: esr.Aws = esr.aws_cli,
    out=sys.stdout,
    err=sys.stderr,
    fetch: Fetch = fetch_json,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rollback")
    r.add_argument("--cluster", required=True)
    r.add_argument("--repository", required=True)
    r.add_argument("--deployed-tag", required=True)
    r.add_argument("--target", action="append", required=True)
    r.add_argument("--health-url", required=True)
    # How long the health check may take to show the previous build after services-stable (CI timing, not a product
    # threshold: the ALB has already drained the old target by then, so the first try normally answers).
    r.add_argument("--health-tries", type=int, default=6)
    r.add_argument("--health-delay", type=float, default=10.0)
    r.add_argument("--apply", action="store_true")
    s = sub.add_parser("alert")
    s.add_argument("--cluster", required=True)
    s.add_argument("--service", required=True)
    s.add_argument("--container", default="")
    s.add_argument("--app", required=True)
    s.add_argument("--commit", default="")
    s.add_argument("--failed-checks", default="")
    s.add_argument("--outcome", default="")
    s.add_argument("--run-url", default="")
    s.add_argument("--drill", action="store_true")
    s.add_argument("--apply", action="store_true")
    try:
        a = p.parse_args(argv)
        if a.cmd == "rollback":
            for raw in a.target:
                parse_target(raw)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else 0
    except ValueError as exc:
        print(f"ERROR: {exc}", file=err)
        return EXIT_USAGE

    def log(msg: str) -> None:
        print(msg, file=err)

    def emit(msg: str) -> None:
        print(msg, file=out)

    if a.cmd == "rollback":
        return rollback(aws, a, log, emit, fetch, sleep)
    return alert(aws, a, log)


if __name__ == "__main__":
    sys.exit(main())
