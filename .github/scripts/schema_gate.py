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

The Infra run check (critic 4): GitHub's Actions API of AA-CIS-Infra (``--infra-repo``, public), workflow
``migration-apply.yml`` on main, judged on its "Apply on Dev" and "Isolation probe" jobs only (the dump dispatch and the
copy-PR summary jobs do not guard the schema). A run whose apply or probe is queued or running, or any re-run in
flight, is waited for. Otherwise the newest run that applied files decides; a red apply or probe can only hide in a run
concluded failure / cancelled / timed out (last ``lookback_days``, default 45), and such a run blocks until it is
re-run green or a newer run applies files with apply and probe green. When only the probe of that run is red (its apply
green), the newest standalone ``db-isolation-probe.yml`` run on main created after the run finished clears it when
green (waited for while in flight; a red one keeps the block): the
probe re-proves the same database, so a probe-only failure (S258: the probe of run 40922869610 had no AWS role) does
not hold every deploy until the next expand file. Runs whose apply job was skipped
(``DB_AUTO_APPLY`` not 'true': list only, today's default) never count, so with auto-apply off the gate is exactly
"every key is registered", which the manual DB Migrate flow already guarantees. Only a 404 of the repo itself
(anonymous: private again, ADR 0014, auto-apply then off too) skips the run check, with a notice; a 403/429 (rate or
abuse limit), any other error and a read cap mean "cannot decide": never a pass, the gate checks again every
``--infra-interval`` seconds until the timeout.

Timeout: ``--timeout``, default the repo variable ``SCHEMA_GATE_TIMEOUT_S`` (workflow env) or 2400 s, at most 2580 s
(the job's ``timeout-minutes: 45`` less a margin). The chain it covers starts at the merge: migration-sync (cron
*/10, often late), the copy PR's checks and the ci-bot merge, the Migration Apply plan, the apply and probe tasks.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = {"cis": "", "booking": "bk-", "crm": "crm-"}
FILE_RE = re.compile(r"^([0-9]{3})_[^/]*\.sql$")
APPLY_JOB = "Apply on Dev"
WORKFLOW = "migration-apply.yml"
PROBE_WORKFLOW = "db-isolation-probe.yml"   # the standalone probe (dispatch, nightly, after DB Migrate)
USER_AGENT = "aa-schema-gate"
DEFAULT_TIMEOUT = 2400.0                # ADR 0017 d7 (S257 review: 20 min is too short for the chain)
MAX_TIMEOUT = 2580.0                    # the schema-gate job has timeout-minutes: 45


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

PROBE_JOB = "Isolation probe"          # a reusable workflow: "Isolation probe (blocking) / Isolation probe"
IN_FLIGHT = ("in_progress", "queued", "pending", "waiting")
RED_RUNS = ("failure", "cancelled", "timed_out")   # a red apply or probe can only hide in a run concluded like this
OK_JOB = ("success", "skipped")
PAGE = 100


@dataclass
class RunState:
    ok: bool
    reason: str
    unreadable: bool = False
    url: str = ""


class Unread(Exception):
    """A read the run check cannot decide on (HTTP error, rate limit, read cap): never a pass, the gate polls again."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Private(Exception):
    """The Infra repo itself answers 404 anonymously: private again (ADR 0014), so auto-apply is off (ADR 0017 d8)."""


def guard_verdict(jobs: list, run_completed: bool) -> str:
    """'none' | 'pending' | 'red' | 'green' from the "Apply on Dev" and "Isolation probe" jobs only.

    The dump dispatch and the copy-PR summary jobs do not guard the schema, so they neither block nor delay the gate
    (ADR 0017 d7: the run that registered the keys must have apply and probe green). 'none' = apply skipped or absent
    (list only, nothing pending, or the plan has not finished)."""
    applied = [j for j in jobs if str(j.get("name", "")).startswith(APPLY_JOB) and j.get("conclusion") != "skipped"]
    if not applied:
        return "none"
    probe = [j for j in jobs if str(j.get("name", "")).startswith(PROBE_JOB)]
    guard = applied + probe
    if any(j.get("status") != "completed" for j in guard):
        return "pending"
    if any(j.get("conclusion") not in OK_JOB for j in guard):
        return "red"
    if not probe and not run_completed:
        return "pending"                # the apply just finished; the probe job is not created yet
    return "green"


def _newest(run: dict) -> tuple:
    return (str(run.get("created_at") or ""), int(run.get("id") or 0))


@dataclass
class Infra:
    """GitHub REST reads of the Infra repo's migration-apply runs. ``get(path) -> (status, json | None)``.

    Cost per check: 7 list calls (4 in-flight statuses, 3 red conclusions, each one page) plus one jobs call per
    in-flight run and per red-concluded run until the newest one that applied files decides. Jobs of completed runs
    are cached for the whole gate, and a check is never cut short into a pass: a cap or an unreadable page is a
    "cannot decide" (fail closed)."""

    repo: str
    get: Callable
    max_jobs: int = 80                 # jobs reads per check, cache misses only
    lookback_days: int = 45            # red runs older than this are not read (see the module docstring)
    now: Callable = time.time
    _jobs: dict = field(default_factory=dict)
    _budget: int = 0

    def _read(self, path: str) -> dict:
        status, data = self.get(path)
        if status == 200 and isinstance(data, dict):
            return data
        if status == 404:
            repo_status, _ = self.get(f"/repos/{self.repo}")
            if repo_status == 404:
                raise Private()
            name = path.split("/workflows/")[1].split("/")[0] if "/workflows/" in path else WORKFLOW
            raise Unread(f"{path.split('?')[0]} answered 404 while {self.repo} is readable (HTTP {repo_status}): "
                         f"is {name} renamed?")
        why = "rate limited or refused" if status in (403, 429) else ("network error" if status == 0 else "error")
        raise Unread(f"cannot read {self.repo} Actions (HTTP {status}, {why}) at {path.split('?')[0]}")

    def _runs(self, query: str, workflow: str = WORKFLOW) -> list:
        data = self._read(f"/repos/{self.repo}/actions/workflows/{workflow}/runs?branch=main&per_page={PAGE}&{query}")
        return list(data.get("workflow_runs") or [])

    def jobs(self, run: dict) -> list:
        key = (run["id"], run.get("run_attempt"))
        done = run.get("status") == "completed"
        if done and key in self._jobs:
            return self._jobs[key]
        if self._budget <= 0:
            raise Unread(f"read the jobs of {self.max_jobs} runs without finding the newest run that applied files")
        self._budget -= 1
        jobs = list(self._read(f"/repos/{self.repo}/actions/runs/{run['id']}/jobs?per_page=100").get("jobs") or [])
        if done:
            self._jobs[key] = jobs
        return jobs

    def state(self) -> RunState:
        self._budget = self.max_jobs
        try:
            return self._state()
        except Private:
            return RunState(True, f"{self.repo} is not readable anonymously (private: auto-apply is off, ADR 0017 "
                            "d8); keys alone are checked", unreadable=True)
        except Unread as exc:
            return RunState(False, f"{exc.reason}; not a pass, checking again")

    def _state(self) -> RunState:
        # 1. A run applying or probing now (a re-run of an old run included) is waited for; a fresh run still in its
        #    plan job applied nothing yet.
        inflight: dict = {}
        for status in IN_FLIGHT:
            for run in self._runs(f"status={status}"):
                inflight[run["id"]] = run
        for run in sorted(inflight.values(), key=_newest, reverse=True):
            url = run.get("html_url", "")
            if int(run.get("run_attempt") or 1) > 1:
                return RunState(False, f"migration-apply run {run['id']} is being re-run (attempt "
                                f"{run.get('run_attempt')}): waiting for it", url=url)
            verdict = guard_verdict(self.jobs(run), False)
            if verdict == "pending":
                return RunState(False, f"migration-apply run {run['id']} is applying or probing", url=url)
            if verdict == "red":
                return self._red(run)
            if verdict == "green":
                return RunState(True, f"the newest migration-apply run that applied files has apply and probe green "
                                f"(its other jobs may still run): {url}", url=url)

        # 2. A red apply or probe hides only in a run concluded failure / cancelled / timed out. The newest such run
        #    that applied files decides: green (only its dump or summary job failed) = nothing red is left after it;
        #    red = blocking unless a newer run applied files with apply and probe green (_red).
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now() - self.lookback_days * 86400))
        reds: dict = {}
        full = []
        for status in RED_RUNS:
            runs = self._runs(f"status={status}&created=%3E%3D{since}")
            if len(runs) >= PAGE:
                full.append(status)
            for run in runs:
                reds[run["id"]] = run
        for run in sorted(reds.values(), key=_newest, reverse=True):
            if run.get("status") != "completed":
                continue
            verdict = guard_verdict(self.jobs(run), True)
            if verdict == "green":
                return RunState(True, f"the newest red-concluded run that applied files has apply and probe green "
                                f"(only its dump or summary job failed): {run.get('html_url', '')}")
            if verdict != "none":
                return self._red(run)
        if full:
            raise Unread(f"a full page of {PAGE} runs concluded {', '.join(full)} in {self.lookback_days} days and "
                         "none of them applied files: older ones were not read")
        return RunState(True, f"no migration-apply run in the last {self.lookback_days} days has a red apply or probe "
                        "(list-only runs, with DB_AUTO_APPLY off, never count)")

    def _red(self, red: dict) -> RunState:
        url = red.get("html_url", "")
        if red.get("status") == "completed" and red.get("created_at"):
            since = str(red["created_at"])
            newer = [r for r in self._runs(f"status=success&created=%3E%3D{since}") if r["id"] != red["id"]]
            for run in sorted(newer, key=_newest, reverse=True):
                if guard_verdict(self.jobs(run), True) == "green":
                    return RunState(True, f"run {red['id']} has a red apply or probe, but the newer run {run['id']} "
                                    f"applied files with apply and probe green: {run.get('html_url', '')}")
            probe = self._standalone_probe(red)
            if probe is not None:
                return probe
        return RunState(False, f"migration-apply run {red['id']} has a red apply or Isolation Probe ({url}). Fix it "
                        "and re-run its failed jobs, or (probe red only) run DB Isolation Probe green on main; the "
                        "gate passes once its apply and probe are green", url=url)

    def _standalone_probe(self, red: dict) -> RunState | None:
        """A probe-only red run (apply green) is cleared when the NEWEST standalone probe created after the run finished
        is green (its "Isolation probe" job included). A probe run in flight is waited for; a newer red one blocks."""
        jobs = self.jobs(red)
        applied = [j for j in jobs if str(j.get("name", "")).startswith(APPLY_JOB) and j.get("conclusion") != "skipped"]
        if not applied or any(j.get("conclusion") != "success" for j in applied):
            return None                 # a red apply is never cleared by a probe
        finished = max([str(j.get("completed_at") or "") for j in jobs]
                       + [str(red.get("updated_at") or ""), str(red["created_at"])])
        since = f"created=%3E%3D{finished}"
        for status in IN_FLIGHT:
            for run in self._runs(f"status={status}&{since}", PROBE_WORKFLOW):
                if str(run.get("created_at") or "") >= finished:
                    return RunState(False, f"standalone DB Isolation Probe run {run['id']} is in flight: waiting for "
                                    "it", url=run.get("html_url", ""))
        done = [r for r in self._runs(f"status=completed&{since}", PROBE_WORKFLOW)
                if str(r.get("created_at") or "") >= finished]
        if not done:
            return None
        newest = max(done, key=_newest)
        probe_jobs = [j for j in self.jobs(newest) if str(j.get("name", "")).startswith(PROBE_JOB)]
        if newest.get("conclusion") != "success" or not probe_jobs or any(
                j.get("conclusion") != "success" for j in probe_jobs):
            return None                 # the newest standalone probe is red (or ran no probe job): it decides
        return RunState(True, f"run {red['id']} has a red Isolation Probe only (apply green); the newest standalone "
                        f"DB Isolation Probe run {newest['id']} created after it is green: "
                        f"{newest.get('html_url', '')}")


# ── the loop ────────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    code: int
    lines: list


def run_gate(app: str, required: list, fetch_ready: Callable, infra: Infra | None, *, timeout: float,
             interval: float, unreachable_grace: float, infra_interval: float = 0.0,
             clock: Callable = time.monotonic, sleep: Callable = time.sleep, log: Callable = print) -> Outcome:
    start = clock()
    reached = False
    last = "nothing read yet"
    polls = 0
    keys_at: float | None = None
    run: RunState | None = None
    run_at = 0.0
    while True:
        polls += 1
        status, body = fetch_ready()
        if isinstance(body, dict):
            reached = True
            schema = evaluate(app, required, body)
            if schema.missing:
                keys_at = None
                last = (f"Dev lacks {', '.join(schema.missing)} ({schema.how}; readiness HTTP {status})")
            else:
                if keys_at is None:
                    keys_at = clock() - start
                if infra is None:
                    run = RunState(True, "Infra run check off")
                elif run is None or run.ok or clock() - run_at >= infra_interval:
                    run, run_at = infra.state(), clock()       # a not-ok answer is re-read every infra_interval
                if run.ok:
                    lines = [f"every required key is registered on Dev ({len(required)} keys, newest "
                             f"{required[-1]}; {schema.how})", run.reason,
                             f"timing: keys registered after {int(keys_at)} s, gate passed after "
                             f"{int(clock() - start)} s, {polls} polls"]
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
                "only after that apply). Nothing was deployed; re-run this workflow once the key is registered (and "
                "the Migration Apply run is green).",
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


def github_get(path: str, token: str, fetch: Callable = http_json) -> tuple:
    """GitHub REST GET. A 401/403/404/429 with this run's token is retried anonymously (the token of an app repo may not
    reach another repo; the Infra Actions API is public). Run-check meaning of the final answer (Infra._read): only a
    404 of the repo itself = private; a 403/429 (rate or abuse limit) or anything else = cannot decide, poll again."""
    url = "https://api.github.com" + path
    status, data = fetch(url, token)
    if token and status in (401, 403, 404, 429):
        status, data = fetch(url, "")
    return status, data


def timeout_from(env: dict) -> float:
    """--timeout default: repo variable SCHEMA_GATE_TIMEOUT_S (via the workflow env), else DEFAULT_TIMEOUT."""
    raw = str(env.get("SCHEMA_GATE_TIMEOUT_S") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT
    except ValueError:
        value = DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--app", required=True, choices=sorted(PREFIX))
    p.add_argument("--readiness-file", required=True, help="the file that declares the required keys")
    p.add_argument("--migrations-dir", required=True, help="this repo's migrations folder")
    p.add_argument("--ready-url", required=True, help="the Dev readiness URL (/health/ready of the app)")
    p.add_argument("--infra-repo", default="AdventureAsia365-Ecosys/AA-CIS-Infra",
                   help="owner/name of the Infra repo; empty = no run check")
    p.add_argument("--timeout", type=float, default=timeout_from(os.environ),
                   help=f"seconds (env SCHEMA_GATE_TIMEOUT_S, default {int(DEFAULT_TIMEOUT)}; "
                   f"at most {int(MAX_TIMEOUT)})")
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--infra-interval", type=float, default=120.0,
                   help="seconds between Infra run checks while the last one was not ok")
    p.add_argument("--unreachable-grace", type=float, default=300.0,
                   help="seconds without a JSON readiness answer before failing early")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    a = p.parse_args(argv)

    if a.timeout > MAX_TIMEOUT:
        print(f"::warning::schema-gate: timeout {int(a.timeout)} s is above the job limit; using {int(MAX_TIMEOUT)} s")
        a.timeout = MAX_TIMEOUT
    try:
        source = Path(a.readiness_file).read_text(encoding="utf-8")
        names = sorted(f.name for f in Path(a.migrations_dir).glob("*.sql"))
        required = required_keys(a.app, source, names)
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"::error::schema-gate: cannot read what this commit needs: {exc}")
        return 2
    print(f"schema-gate: this commit needs {len(required)} keys, newest {required[-1]}; "
          f"waiting up to {int(a.timeout)} s")

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    infra = None
    if a.infra_repo:
        infra = Infra(a.infra_repo, lambda path: github_get(path, token))
    out = run_gate(a.app, required, lambda: http_json(a.ready_url), infra, timeout=a.timeout,
                   interval=a.interval, unreachable_grace=a.unreachable_grace, infra_interval=a.infra_interval)
    head = "passed" if out.code == 0 else "failed (nothing deployed)"
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Schema gate: {head}\n\n" + "".join(f"- {line}\n" for line in out.lines))
    for line in out.lines:
        print(("::error::" if out.code else "") + f"schema-gate: {line}")
    return out.code


if __name__ == "__main__":
    sys.exit(main())
