#!/usr/bin/env python3
"""Deploy Dev ``schema-gate`` (AA-993 M4b, root ADR 0017 decision 7; design D2.7; critic points 2, 4, 23).

Before the rollout of a commit, wait (bounded) until the Dev database holds every migration key that commit needs, and
until the Infra ``migration-apply`` run that applied something last is green (its blocking Isolation Probe included).
Red **before** the rollout otherwise, so code never rolls onto a schema it needs but does not have.

  python3 .github/scripts/schema_gate.py --app booking --readiness-file backend/app/db.py \\
      --migrations-dir migrations --ready-url https://api-booking.lumiguides.it.com/booking/health/ready

What the commit needs (read from the checked-out tree, i.e. the commit being deployed):
  * booking / crm: every key of ``REQUIRED_SCHEMA_VERSIONS`` in the readiness file;
    cis: every number from ``LEGACY_CIS_BASELINE`` + 1 up to ``REQUIRED_CIS_SCHEMA`` (api/core/readiness.py);
  * plus every migration file of the commit numbered at or below the highest required key (above the CIS baseline):
    with the strict-prefix apply of ADR 0017 d5 nothing below the required key may be missing (critic 2).

What Dev has: ``registered_keys`` on the readiness URL (AA-993 M4a). A task that predates M4a has no such field: the
gate then falls back to ``schema_version`` (the newest key) and ``missing_schema_versions`` (critic 23).

The Infra run check (critic 4): GitHub's public Actions API of AA-CIS-Infra (``--infra-repo``), workflow
``migration-apply.yml`` on main. A run whose "Apply on Dev" job was queued or ran and that is not finished yet (apply,
probe) is waited for; then the newest finished run whose apply job ran must have concluded ``success``. Runs whose
apply job was skipped (``DB_AUTO_APPLY`` not 'true': list only, today's default) do not count, so with auto-apply off
the gate is exactly "every key is registered", which the manual DB Migrate flow already guarantees. When the Infra
repo is not readable (private again, ADR 0014: auto-apply is then off too) the run check is skipped with a notice.

Exit 0 = every key registered (and the Infra run green); 1 = timeout (the summary names the missing keys or the red
run); 2 = bad input (readiness file unreadable). Standard library only. Identical file in AA-CIS-App, AA-Booking and
AA-Partner; tests: the repo's unit test ``test_schema_gate_script.py``.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

PREFIX = {"cis": "", "booking": "bk-", "crm": "crm-"}
FILE_RE = re.compile(r"^([0-9]{3})_[^/]*\.sql$")
APPLY_JOB = "Apply on Dev"
WORKFLOW = "migration-apply.yml"
USER_AGENT = "aa-schema-gate"


# ── what the deployed commit needs ──────────────────────────────────────────────────────────────────────────────────


def key_number(key: str) -> int:
    m = re.search(r"([0-9]+)$", str(key))
    if not m:
        raise ValueError(f"not a migration key: {key!r}")
    return int(m.group(1))


def norm_key(app: str, key) -> str:
    """'bk-034' / 'crm-007' stay as they are; a CIS key is its number without padding ('301')."""
    return str(key_number(key)) if app == "cis" else f"{PREFIX[app]}{key_number(key):03d}"


def _assignments(source: str) -> dict:
    """Module-level ``NAME = <literal>`` assignments (plain or annotated) whose value is a literal."""
    out: dict = {}
    for node in ast.parse(source).body:
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = [t for t in node.targets if isinstance(t, ast.Name)], node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            targets, value = [node.target], node.value
        for t in targets:
            try:
                out[t.id] = ast.literal_eval(value)
            except ValueError:
                continue
    return out


def readiness_keys(app: str, source: str) -> set:
    """The keys the readiness code of this commit requires (see the module docstring)."""
    found = _assignments(source)
    if app == "cis":
        try:
            baseline, head = int(found["LEGACY_CIS_BASELINE"]), int(found["REQUIRED_CIS_SCHEMA"])
        except KeyError as exc:
            raise ValueError(f"readiness file has no literal {exc.args[0]}") from None
        return {str(n) for n in range(baseline + 1, head + 1)}
    versions = found.get("REQUIRED_SCHEMA_VERSIONS")
    if not isinstance(versions, (tuple, list)) or not versions:
        raise ValueError("readiness file has no literal REQUIRED_SCHEMA_VERSIONS tuple")
    return {norm_key(app, v) for v in versions}


def cis_baseline(source: str) -> int:
    return int(_assignments(source).get("LEGACY_CIS_BASELINE", 0))


def required_keys(app: str, source: str, file_names: list) -> list:
    """Readiness keys plus every migration file key at or below the highest one (above the CIS baseline), sorted."""
    keys = readiness_keys(app, source)
    top = max(key_number(k) for k in keys)
    floor = cis_baseline(source) if app == "cis" else 0
    for name in file_names:
        m = FILE_RE.match(name)
        if m and floor < int(m.group(1)) <= top:
            keys.add(norm_key(app, m.group(1)))
    return sorted(keys, key=key_number)


# ── what Dev has ────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Schema:
    missing: list                      # required keys Dev lacks ([] = all there)
    how: str                           # "registered_keys" or "fallback: ..."


def evaluate(app: str, required: list, body: dict) -> Schema:
    """Missing required keys from one readiness body (any HTTP status; a 503 still carries the body)."""
    keys = body.get("registered_keys")
    if isinstance(keys, list):
        have = set()
        for k in keys:
            try:
                have.add(norm_key(app, k))
            except ValueError:
                continue
        return Schema([k for k in required if k not in have], "registered_keys")
    # critic 23: a task from before M4a. Newest key + the old code's missing list; strict-prefix apply makes
    # "newest >= required" imply no gap for keys the old code did not list.
    head = body.get("schema_version")
    listed = set()
    for k in body.get("missing_schema_versions") or []:
        try:
            listed.add(norm_key(app, k))
        except ValueError:
            continue
    try:
        head_n = key_number(head) if head is not None else -1
    except ValueError:
        head_n = -1
    missing = [k for k in required if k in listed or key_number(k) > head_n]
    return Schema(missing, f"fallback: schema_version={head!r} (the running task has no registered_keys)")


# ── the Infra migration-apply runs ──────────────────────────────────────────────────────────────────────────────────


@dataclass
class RunState:
    ok: bool
    reason: str
    unreadable: bool = False
    url: str = ""


@dataclass
class Infra:
    """GitHub REST reads of the Infra repo's migration-apply runs. ``get(path) -> (status, json | None)``."""

    repo: str
    get: Callable
    window: int = 30
    _jobs: dict = field(default_factory=dict)

    def jobs(self, run: dict) -> list | None:
        cache_key = (run["id"], run.get("run_attempt"), run.get("status"), run.get("updated_at"))
        if run.get("status") == "completed" and cache_key in self._jobs:
            return self._jobs[cache_key]
        status, data = self.get(f"/repos/{self.repo}/actions/runs/{run['id']}/jobs?per_page=100")
        if status != 200 or not isinstance(data, dict):
            return None
        jobs = data.get("jobs") or []
        if run.get("status") == "completed":
            self._jobs[cache_key] = jobs
        return jobs

    def state(self) -> RunState:
        status, data = self.get(
            f"/repos/{self.repo}/actions/workflows/{WORKFLOW}/runs?branch=main&per_page={self.window}")
        if status in (403, 404):
            return RunState(True, f"{self.repo} Actions not readable (HTTP {status}): the repo is private, so "
                            "auto-apply is off (ADR 0017 d8); keys alone are checked", unreadable=True)
        if status != 200 or not isinstance(data, dict):
            return RunState(False, f"cannot read {self.repo} {WORKFLOW} runs (HTTP {status})")
        for run in data.get("workflow_runs") or []:
            jobs = self.jobs(run)
            if jobs is None:
                return RunState(False, f"cannot read the jobs of {self.repo} run {run['id']}")
            apply = [j for j in jobs if str(j.get("name", "")).startswith(APPLY_JOB)]
            ran = [j for j in apply if j.get("conclusion") != "skipped"]
            url = run.get("html_url", "")
            if run.get("status") != "completed":
                if ran:
                    return RunState(False, f"migration-apply run {run['id']} is applying or probing", url=url)
                continue            # plan only so far (or list only): nothing this deploy waits for
            if not ran:
                continue            # list only (DB_AUTO_APPLY off) or nothing pending
            if run.get("conclusion") == "success":
                return RunState(True, f"the last migration-apply run that applied files is green: {url}", url=url)
            return RunState(False, f"the last migration-apply run that applied files concluded "
                            f"{run.get('conclusion')!r} (apply or the blocking Isolation Probe): {url}. Fix it and "
                            "re-run its failed jobs; the gate passes once that run is green", url=url)
        return RunState(True, f"no migration-apply run applied files in the last {self.window} runs "
                        "(auto-apply off or nothing pending): the keys came from DB Migrate")


# ── the loop ────────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    code: int
    lines: list


def run_gate(app: str, required: list, fetch_ready: Callable, infra: Infra | None, *, timeout: float,
             interval: float, unreachable_grace: float, clock: Callable = time.monotonic,
             sleep: Callable = time.sleep, log: Callable = print) -> Outcome:
    start = clock()
    reached = False
    last = "nothing read yet"
    polls = 0
    while True:
        polls += 1
        status, body = fetch_ready()
        if isinstance(body, dict):
            reached = True
            schema = evaluate(app, required, body)
            if schema.missing:
                last = (f"Dev lacks {', '.join(schema.missing)} ({schema.how}; readiness HTTP {status})")
            else:
                run = infra.state() if infra is not None else RunState(True, "Infra run check off")
                if run.ok:
                    lines = [f"every required key is registered on Dev ({len(required)} keys, newest "
                             f"{required[-1]}; {schema.how})", run.reason, f"polls: {polls}"]
                    if run.unreadable:
                        log(f"::notice::{run.reason}")
                    return Outcome(0, lines)
                last = f"keys registered, but {run.reason}"
        else:
            last = f"readiness not readable (HTTP {status})"
            if not reached and clock() - start >= unreachable_grace:
                return Outcome(1, [f"the Dev API readiness answered no JSON for {int(unreachable_grace)} s "
                                   f"({last}): is the service scaled to 0 or down? Nothing was deployed."])
        elapsed = clock() - start
        log(f"schema-gate: waiting ({int(elapsed)} s): {last}")
        if elapsed + interval > timeout:
            return Outcome(1, [
                f"required schema not on Dev after {int(timeout)} s: {last}.",
                "An expand file (`-- migrate: expand`) is copied to AA-CIS-Infra by the migration-sync bot and "
                "applied by Migration Apply when DB_AUTO_APPLY is true; otherwise, and for every contract or "
                "destructive file, a person applies it with DB Migrate (MIG-4: code that needs a contract file merges "
                "only after that apply). Nothing was deployed; re-run this workflow once the key is registered.",
            ])
        sleep(interval)


# ── I/O ─────────────────────────────────────────────────────────────────────────────────────────────────────────────


def http_json(url: str, token: str = "", timeout: float = 15.0) -> tuple:
    """(HTTP status, parsed JSON or None). Network errors are status 0."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https URLs from the workflow)
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--app", required=True, choices=sorted(PREFIX))
    p.add_argument("--readiness-file", required=True, help="the file that declares the required keys")
    p.add_argument("--migrations-dir", required=True, help="this repo's migrations folder")
    p.add_argument("--ready-url", required=True, help="the Dev readiness URL (/health/ready of the app)")
    p.add_argument("--infra-repo", default="AdventureAsia365-Ecosys/AA-CIS-Infra",
                   help="owner/name of the Infra repo; empty = no run check")
    p.add_argument("--timeout", type=float, default=1200.0, help="seconds (ADR 0017 d7: 20 min)")
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--unreachable-grace", type=float, default=300.0,
                   help="seconds without a JSON readiness answer before failing early")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    a = p.parse_args(argv)

    try:
        source = Path(a.readiness_file).read_text(encoding="utf-8")
        names = sorted(f.name for f in Path(a.migrations_dir).glob("*.sql"))
        required = required_keys(a.app, source, names)
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"::error::schema-gate: cannot read what this commit needs: {exc}")
        return 2
    print(f"schema-gate: this commit needs {len(required)} keys, newest {required[-1]}")

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    infra = None
    if a.infra_repo:
        infra = Infra(a.infra_repo, lambda path: http_json("https://api.github.com" + path, token))
    out = run_gate(a.app, required, lambda: http_json(a.ready_url), infra, timeout=a.timeout,
                   interval=a.interval, unreachable_grace=a.unreachable_grace)
    head = "passed" if out.code == 0 else "failed (nothing deployed)"
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Schema gate: {head}\n\n" + "".join(f"- {line}\n" for line in out.lines))
    for line in out.lines:
        print(("::error::" if out.code else "") + f"schema-gate: {line}")
    return out.code


if __name__ == "__main__":
    sys.exit(main())
