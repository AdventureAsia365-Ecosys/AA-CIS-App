"""AA-1135 — the computation of the delivery metrics, shared by the collector and the CIS custom-range endpoint.

The collector (tools/delivery/delivery_metrics.py, AA-1108 / AA-1125) reads GitHub, keeps the normalised events and
calls this module to compute the summary. The CIS API computes a custom date range (Platform › Delivery, AA-1135) from
the collector's published daily event files with a BYTE COPY of this file (AA-CIS-App api/services/delivery_engine.py),
so a custom range and a preset of the same window can never give different numbers:

  * the summary carries `definitions.engine_sha256` (this file's sha256) and the collector's `config`;
  * CIS refuses a custom range while its copy's sha256 differs from the published one (sync with
    tools/delivery/sync_engine.py, which also writes the copy's lock file);
  * tools/delivery/test_delivery_metrics.py pins custom == preset on the fixture (the parity test);
  * the summary also carries `definitions.store_fingerprint` (per repo: the event count and a sha256 of the store the
    presets were computed from); `custom_range` refuses (StoreMismatch) day files that do not rebuild exactly that
    store (a failed upload, a lost Actions cache), so the code and the data both match.

Pure computation: standard library only, no network, no file other than the store files of `Store`. Every number
follows the definitions of docs/guides/delivery-metrics.md (DEFINITIONS_VERSION).
"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

FORMAT = 1
DEFINITIONS_VERSION = "2026-11-18"
STORE_EVENTS = "events.ndjson.gz"
STORE_STATE = "state.json"
RED = ("failure", "timeout", "error")
COMMIT_KINDS = ("agent", "ci_bot", "release_please", "dependabot", "bot", "human")
QUEUE_BRANCH = re.compile(r"^gh-readonly-queue/[^/]+/pr-(\d+)-")


# ── time helpers ────────────────────────────────────────────────────────────────────────────────────────────────


def ts(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def hours(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if a is None or b is None:
        return None
    return round(max((b - a).total_seconds(), 0.0) / 3600.0, 3)


def percentile(values: Iterable[float], q: float) -> Optional[float]:
    """Nearest-rank percentile (q in 0..100); None for no value."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    k = max(1, math.ceil(q / 100.0 * len(xs)))
    return round(xs[min(k, len(xs)) - 1], 3)


def iso_week(d: datetime) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ── the event store ─────────────────────────────────────────────────────────────────────────────────────────────


class Store:
    def __init__(self, events: Optional[dict] = None, state: Optional[dict] = None):
        self.events: dict[str, dict] = events or {}
        self.state: dict = state or {"repos": {}}
        self.touched_days: set[str] = set()

    @classmethod
    def load(cls, folder: Path) -> "Store":
        events: dict[str, dict] = {}
        p = folder / STORE_EVENTS
        if p.exists():
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        e = json.loads(line)
                        events[e["id"]] = e
        sp = folder / STORE_STATE
        state = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {"repos": {}}
        return cls(events, state)

    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        with gzip.open(folder / STORE_EVENTS, "wt", encoding="utf-8") as fh:
            for e in sorted(self.events.values(), key=lambda e: (e["time"] or "", e["id"])):
                fh.write(json.dumps(e, sort_keys=True, separators=(",", ":")) + "\n")
        (folder / STORE_STATE).write_text(json.dumps(self.state, indent=1, sort_keys=True), encoding="utf-8")

    # attributes read by a later, separate request: a re-read of the list keeps them
    KEEP = {
        "vcs.change": ("aa.docs_only",),
        "aa.vcs.commit": ("aa.files", "aa.files_truncated", "aa.additions", "aa.deletions"),
    }

    def put(self, e: dict) -> bool:
        old = self.events.get(e["id"])
        if old is not None:
            for k in self.KEEP.get(e["event"], ()):
                if old["attributes"].get(k) is not None and e["attributes"].get(k) is None:
                    e["attributes"][k] = old["attributes"][k]
        if old == e:
            return False
        self.events[e["id"]] = e
        if e.get("time"):
            self.touched_days.add(e["time"][:10])
        # an event that moved to another day is rewritten out of its old day's file too (AA-1135: the day files
        # rebuild exactly the store, see store_fingerprint)
        if old is not None and old.get("time") and old["time"][:10] != (e.get("time") or "")[:10]:
            self.touched_days.add(old["time"][:10])
        return True

    def repo_state(self, repo: str) -> dict:
        return self.state.setdefault("repos", {}).setdefault(repo, {})

    def of(self, kind: str, repo: Optional[str] = None) -> list[dict]:
        """The events of one kind (and repo), oldest first: (time, id) order, so every computation sees the same
        order whether the store was loaded from the cache or rebuilt from the day files (AA-1135 parity)."""
        return sorted(
            (
                e
                for e in self.events.values()
                if e["event"] == kind and (repo is None or e["attributes"]["vcs.repository.name"] == repo)
            ),
            key=lambda e: (e.get("time") or "", e["id"]),
        )

    def prune(self, keep_from: datetime) -> int:
        cut = iso(keep_from)
        old = [k for k, e in self.events.items() if (e.get("time") or "") < cut]
        for k in old:
            del self.events[k]
        return len(old)

    def day_files(self, days: Iterable[str]) -> dict[str, list[dict]]:
        want = set(days)
        out: dict[str, list[dict]] = defaultdict(list)
        for e in self.events.values():
            d = (e.get("time") or "")[:10]
            if d in want:
                out[d].append(e)
        return {d: sorted(v, key=lambda e: (e["time"], e["id"])) for d, v in out.items()}




def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]



# ── metrics ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _attr(e: dict, k: str) -> Any:
    return e["attributes"].get(k)


def latest_attempt(jobs: list[dict]) -> list[dict]:
    attempt = max([_attr(j, "aa.run_attempt") or 1 for j in jobs] or [1])
    return [j for j in jobs if (_attr(j, "aa.run_attempt") or 1) == attempt]


def run_outcome(app: dict, run: dict, jobs: Optional[list[dict]]) -> str:
    """success | verify_failed | deploy_failed | not_deployed | cancelled | running | unclassified.

    `jobs` None = not read yet (unclassified); [] = read, the run had no job."""
    result = _attr(run, "cicd.pipeline.result")
    if _attr(run, "cicd.pipeline.run.state") != "finalizing" or result is None:
        return "running"
    if result in ("cancellation", "skip") and not jobs:
        return "cancelled"
    if jobs is None:
        return "unclassified"
    latest = latest_attempt(jobs)
    dep = re.compile(app["deploy_job"])
    ver = re.compile(app["verify_job"]) if app.get("verify_job") else None
    deploy_jobs = [j for j in latest if dep.search(_attr(j, "cicd.pipeline.task.name") or "")]
    verify_jobs = [
        j
        for j in latest
        if ver
        and ver.search(_attr(j, "cicd.pipeline.task.name") or "")
        and _attr(j, "cicd.pipeline.task.run.result") not in (None, "skip")
    ]
    if any(_attr(j, "cicd.pipeline.task.run.result") in RED for j in deploy_jobs):
        return "deploy_failed"
    if not any(_attr(j, "cicd.pipeline.task.run.result") == "success" for j in deploy_jobs):
        return "cancelled" if result == "cancellation" else "not_deployed"
    if any(_attr(j, "cicd.pipeline.task.run.result") in RED for j in verify_jobs):
        return "verify_failed"
    return "success"


def group_outcome(outcomes: list[str]) -> str:
    """One app deploy from its runs: the worst run decides (a failed deploy job, then a red verify)."""
    return next((o for o in ("deploy_failed", "verify_failed", "unclassified") if o in outcomes), "success")


def deploy_groups(cfg: dict, app: dict, runs: list[dict], jobs_by_run: dict, deployments: list[dict]) -> list[dict]:
    """Deploy runs of the app -> app deploys (consecutive runs of one commit grouped), oldest first."""
    rows = []
    for r in runs:
        a = r["attributes"]
        if (
            a["aa.workflow.file"] not in app["deploy_workflows"]
            or a["vcs.ref.head.name"] != app["default_branch"]
            or a["aa.trigger"] not in cfg["deploy_triggers"]
        ):
            continue
        jobs = jobs_by_run.get(a["cicd.pipeline.run.id"])
        outcome = run_outcome(app, r, jobs)
        if outcome in ("running", "cancelled", "not_deployed"):
            continue
        dep = re.compile(app["deploy_job"])
        ver = re.compile(app["verify_job"]) if app.get("verify_job") else None
        latest = latest_attempt(jobs or [])
        dj = [
            ts(_attr(j, "end_time"))
            for j in latest
            if dep.search(_attr(j, "cicd.pipeline.task.name") or "") and _attr(j, "end_time")
        ]
        vj = [
            ts(_attr(j, "end_time"))
            for j in latest
            if ver
            and ver.search(_attr(j, "cicd.pipeline.task.name") or "")
            and _attr(j, "end_time")
            and _attr(j, "cicd.pipeline.task.run.result") not in (None, "skip")
        ]
        start, end = ts(a["start_time"]), ts(a["end_time"])
        deployed = max(dj) if dj else end
        rows.append(
            {
                "sha": a["vcs.ref.head.revision"],
                "start": start,
                "end": end,
                "deployed": deployed,
                "verified": (max(vj) if vj else deployed) if outcome == "success" else None,
                "verify_failed_at": max(vj) if vj and outcome == "verify_failed" else None,
                "outcome": outcome,
                "run": {
                    "id": a["cicd.pipeline.run.id"],
                    "url": a["cicd.pipeline.run.url.full"],
                    "workflow": a["cicd.pipeline.name"],
                    "outcome": outcome,
                    "attempt": a["aa.run_attempt"],
                },
            }
        )
    rows.sort(key=lambda x: x["start"] or datetime.min.replace(tzinfo=timezone.utc))
    groups: list[dict] = []
    for row in rows:
        g = groups[-1] if groups else None
        # runs of one commit that overlap (the services of one push) are one deploy; a later run of the same commit
        # (a retry, a manual redeploy) is its own deploy, marked `redeploy` below
        if g and g["sha"] == row["sha"] and row["start"] and g["end"] and row["start"] <= g["end"]:
            g["runs"].append(row["run"])
            g["start"] = min(g["start"], row["start"]) if g["start"] and row["start"] else g["start"] or row["start"]
            for k in ("end", "deployed"):
                g[k] = max(x for x in (g[k], row[k]) if x) if (g[k] or row[k]) else None
            g["verify_failed_at"] = g["verify_failed_at"] or row["verify_failed_at"]
            g["outcomes"].append(row["outcome"])
            g["verified_parts"].append(row["verified"])
        else:
            groups.append(
                {
                    "sha": row["sha"],
                    "start": row["start"],
                    "end": row["end"],
                    "deployed": row["deployed"],
                    "verify_failed_at": row["verify_failed_at"],
                    "runs": [row["run"]],
                    "outcomes": [row["outcome"]],
                    "verified_parts": [row["verified"]],
                }
            )
    for i, g in enumerate(groups):
        g["outcome"] = group_outcome(g["outcomes"])
        g["redeploy"] = i > 0 and groups[i - 1]["sha"] == g["sha"]
        g["verified"] = (
            max(v for v in g["verified_parts"] if v) if g["outcome"] == "success" and any(g["verified_parts"]) else None
        )
        g["deployments"] = sum(
            1
            for d in deployments
            if _attr(d, "vcs.ref.head.revision") == g["sha"]
            and g["start"]
            and g["end"]
            and g["start"] - timedelta(minutes=2) <= (ts(_attr(d, "aa.created_at")) or g["start"]) <= g["end"]
        )
        g["failed"], g["rework"] = [], []
        if g["outcome"] == "verify_failed":
            g["failed"].append("verify_red")
        if g["outcome"] == "deploy_failed":
            g["failed"].append("deploy_job_failed")
    return groups


def pr_of_branch(by_head: dict, branch: Optional[str], at: Optional[datetime]) -> Optional[int]:
    """The merged PR whose head branch is `branch` and that was open at `at`."""
    for opened, merged_at, number in by_head.get(branch or "", ()):
        if opened and merged_at and at and opened <= at <= merged_at:
            return number
    return None


def attach_changes(
    cfg: dict, app: dict, groups: list[dict], prs: list[dict], runs: list[dict], since: Optional[datetime] = None
) -> list[dict]:
    """Per merged pull request: its lead-time points, the deploy that first shipped it and the first green one.

    A pull request merged before `since` (the start of the complete run history) is not matched to a deploy: the
    deploys between its merge and `since` are not in the store, so the first stored deploy would claim it wrongly."""
    merged = [p for p in prs if _attr(p, "aa.merged_at") and _attr(p, "vcs.ref.base.name") == app["default_branch"]]
    merge_time = {
        _attr(p, "aa.merge_commit"): ts(_attr(p, "aa.merged_at")) for p in merged if _attr(p, "aa.merge_commit")
    }
    for g in groups:
        g["merge_time"] = merge_time.get(g["sha"]) or g["start"]
    # rollbacks: a deploy of a commit that was live earlier while another commit is live
    live, seen, live_group = None, set(), None
    for g in groups:
        if g["outcome"] == "unclassified":
            continue
        if live is not None and g["sha"] != live and g["sha"] in seen:
            g["rework"].append("rollback")
            g["rollback"] = True
            if live_group is not None and "rolled_back" not in live_group["failed"]:
                live_group["failed"].append("rolled_back")
                live_group.setdefault("repaired_by", []).append(g)
        if live is not None:
            seen.add(live)
        live, live_group = g["sha"], g
    ci = set(app["ci_workflows"])
    # GitHub empties a run's pull_requests list once the branch is gone: fall back to the head branch of a merged PR
    by_head: dict[str, list[tuple]] = defaultdict(list)
    for p in merged:
        by_head[_attr(p, "vcs.ref.head.name") or ""].append(
            (ts(_attr(p, "aa.created_at")), ts(_attr(p, "aa.merged_at")), _attr(p, "vcs.change.id"))
        )
    green: dict[int, datetime] = {}
    queued: dict[int, datetime] = {}
    queue_branches: dict[int, set] = defaultdict(set)
    for r in runs:
        a = r["attributes"]
        m = QUEUE_BRANCH.match(a["vcs.ref.head.name"] or "")
        if m and a["aa.trigger"] == "merge_group":
            n = int(m.group(1))
            t = ts(a["aa.created_at"])
            if t and (n not in queued or t < queued[n]):
                queued[n] = t
            queue_branches[n].add(a["vcs.ref.head.name"])
        if (
            a["aa.workflow.file"] in ci
            and a["aa.trigger"] in ("pull_request", "pull_request_target")
            and a["cicd.pipeline.result"] == "success"
        ):
            t = ts(a["end_time"])
            n = a["vcs.change.id"] or pr_of_branch(by_head, a["vcs.ref.head.name"], ts(a["aa.created_at"]))
            if n and t and (n not in green or t < green[n]):
                green[n] = t
    ordered = [g for g in groups if g["outcome"] != "unclassified" and not g.get("rollback")]
    changes = []
    for p in merged:
        a = p["attributes"]
        n, m_at = a["vcs.change.id"], ts(a["aa.merged_at"])
        known = since is None or m_at >= since
        shipped = next((g for g in ordered if g["merge_time"] and g["merge_time"] >= m_at), None) if known else None
        good = (
            next(
                (g for g in ordered if g["outcome"] == "success" and g["merge_time"] and g["merge_time"] >= m_at), None
            )
            if known
            else None
        )
        excluded = "bot" if a["aa.author.kind"] == "bot" else ("docs_only" if a["aa.docs_only"] else None)
        ch = {
            "number": n,
            "title": a["vcs.change.title"],
            "url": a["vcs.change.url.full"],
            "kind": a["aa.author.kind"],
            "login": a.get("aa.author.login"),
            "type": a["aa.change_type"],
            "revert_of": a["aa.revert_of"],
            "references": a["aa.references"] or [],
            "excluded": excluded,
            "opened": ts(a["aa.created_at"]),
            "first_green": green.get(n),
            "queued": queued.get(n),
            "merged": m_at,
            "deployed": good["deployed"] if good else None,
            "verified": good["verified"] if good else None,
            "shipped_in": shipped,
            "green_in": good,
            "queue_attempts": len(queue_branches.get(n, ())),
        }
        changes.append(ch)
        if shipped is not None:
            shipped.setdefault("changes", []).append(ch)
    by_number = {c["number"]: c for c in changes}
    window = timedelta(hours=cfg["rework_hours"])
    for c in changes:
        target = None
        reason = None
        if c["type"] == "revert" or c["revert_of"]:
            target, reason = by_number.get(c["revert_of"]), "reverted"
        elif c["type"] == "fix":
            cands = [
                by_number[r]
                for r in c["references"]
                if r in by_number
                and by_number[r]["merged"]
                and by_number[r]["merged"] < c["merged"]
                and by_number[r]["shipped_in"] is not None
            ]
            cands = [x for x in cands if c["merged"] - (x["shipped_in"]["deployed"] or x["merged"]) <= window]
            target, reason = (max(cands, key=lambda x: x["merged"]), "hotfix") if cands else (None, None)
        if target is None or target["shipped_in"] is None:
            continue
        g = target["shipped_in"]
        when = g["deployed"] or g["end"]
        if reason == "reverted" and when and c["merged"] - when > window:
            continue
        if reason not in g["failed"]:
            g["failed"].append(reason)
        g.setdefault("repaired_by", []).append(c)
        if c["shipped_in"] is not None and reason not in c["shipped_in"]["rework"]:
            c["shipped_in"]["rework"].append(reason)
    return changes


def recovery(groups: list[dict]) -> None:
    """Recovery time of each failed deploy: to the first green deploy that repairs it."""
    for i, g in enumerate(groups):
        if not g["failed"]:
            continue
        failed_at = g["verify_failed_at"] or g["end"] or g["deployed"]
        g["failed_at"] = failed_at
        cands = []
        if set(g["failed"]) & {"verify_red", "deploy_job_failed"}:
            nxt = next((x for x in groups[i + 1 :] if x["outcome"] == "success"), None)
            if nxt:
                cands.append(nxt["verified"] or nxt["end"])
        for rep in g.get("repaired_by", []):
            if isinstance(rep, dict) and "outcomes" in rep:  # a rollback deploy
                if rep["outcome"] == "success":
                    cands.append(rep["verified"] or rep["end"])
            elif isinstance(rep, dict) and rep.get("green_in") is not None:
                cands.append(rep["green_in"]["verified"] or rep["green_in"]["end"])
        cands = [c for c in cands if c and failed_at and c >= failed_at]
        g["recovered_at"] = min(cands) if cands else None
        g["recovery_h"] = hours(failed_at, g["recovered_at"]) if g["recovered_at"] else None


SEGMENTS = (
    ("to_first_green", "opened", "first_green"),
    ("to_queue", "first_green", "queued"),
    ("to_merge", "queued", "merged"),
    ("to_deploy", "merged", "deployed"),
    ("to_verify", "deployed", "verified"),
)
POINTS = ("opened", "first_green", "queued", "merged", "deployed", "verified")


def segments_of(c: dict) -> dict[str, float]:
    """Hours per segment; a missing point folds its segment into the next one that has both ends."""
    out: dict[str, float] = {}
    prev = c["opened"]
    for name, _a, b in SEGMENTS:
        end = c[b]
        if end is None or prev is None:
            continue
        out[name] = hours(prev, end) or 0.0
        prev = end
    return out


def in_range(t: Optional[datetime], a: datetime, b: datetime) -> bool:
    return t is not None and a <= t < b


def dora_block(groups: list[dict], changes: list[dict], a: datetime, b: datetime) -> dict:
    # a redeploy of the commit already live is not a change: left out of the counts (it can still be a recovery)
    reached = [g for g in groups if g["outcome"] != "unclassified" and not g["redeploy"] and in_range(g["start"], a, b)]
    green = [g for g in reached if g["outcome"] == "success"]
    failed = [g for g in reached if g["failed"]]
    rework = [g for g in reached if g["rework"]]
    recovered = [g["recovery_h"] for g in failed if g.get("recovery_h") is not None]
    lt = [c for c in changes if not c["excluded"] and c["verified"] and in_range(c["merged"], a, b)]
    weeks = max((b - a).total_seconds() / (7 * 86400), 1e-9)
    reasons: dict[str, int] = defaultdict(int)
    for g in failed:
        for r in g["failed"]:
            reasons[r] += 1
    return {
        "deployment_frequency": {
            "value": round(len(green) / weeks, 2),
            "unit": "per_week",
            "n": len(green),
            "attempts": len(reached),
        },
        "lead_time": {
            "p50_h": percentile([hours(c["opened"], c["verified"]) for c in lt], 50),
            "p90_h": percentile([hours(c["opened"], c["verified"]) for c in lt], 90),
            "n": len(lt),
        },
        "change_failure_rate": {
            "value": round(len(failed) / len(reached), 4) if reached else None,
            "n": len(reached),
            "failed": len(failed),
            "by_reason": dict(sorted(reasons.items())),
        },
        "recovery_time": {
            "p50_h": percentile(recovered, 50),
            "p90_h": percentile(recovered, 90),
            "n": len(recovered),
            "open": sum(1 for g in failed if g.get("recovery_h") is None),
        },
        "rework_rate": {
            "value": round(len(rework) / len(reached), 4) if reached else None,
            "n": len(reached),
            "rework": len(rework),
        },
    }


def lead_segments(changes: list[dict], a: datetime, b: datetime) -> list[dict]:
    lt = [c for c in changes if not c["excluded"] and c["verified"] and in_range(c["merged"], a, b)]
    out = []
    for name, _x, _y in SEGMENTS:
        vals = [s[name] for s in (segments_of(c) for c in lt) if name in s]
        out.append({"segment": name, "p50_h": percentile(vals, 50), "p90_h": percentile(vals, 90),
                    "p95_h": percentile(vals, 95), "n": len(vals)})
    return out


def run_minutes(run: dict, jobs: Optional[list[dict]]) -> tuple[int, bool]:
    """Actions minutes of a run (each job rounded up to the minute, every attempt) and whether its jobs were read; a
    run whose jobs are not read yet (or that had none) counts its wall-clock time."""
    if jobs:
        return sum(math.ceil((_attr(j, "aa.duration_s") or 0) / 60) for j in jobs), True
    return math.ceil((_attr(run, "aa.duration_s") or 0) / 60), jobs is not None


def ci_block(
    cfg: dict, app: dict, runs: list[dict], jobs_by_run: dict, a: datetime, b: datetime, now: datetime
) -> dict:
    done = [r for r in runs if _attr(r, "cicd.pipeline.result") and in_range(ts(_attr(r, "end_time")), a, b)]
    by_wf: dict[str, list[dict]] = defaultdict(list)
    for r in done:
        by_wf[_attr(r, "cicd.pipeline.name")].append(r)
    workflows = []
    total_min, covered = 0, 0
    for name, rs in by_wf.items():
        judged = [r for r in rs if _attr(r, "cicd.pipeline.result") not in ("cancellation", "skip")]
        fails = [r for r in judged if _attr(r, "cicd.pipeline.result") in RED]
        durs = [_attr(r, "aa.duration_s") for r in judged if _attr(r, "aa.duration_s") is not None]
        mins = 0
        for r in rs:
            m, known = run_minutes(r, jobs_by_run.get(_attr(r, "cicd.pipeline.run.id")))
            mins += m
            covered += known
        total_min += mins
        last = max(rs, key=lambda r: _attr(r, "end_time") or "")
        workflows.append(
            {
                "name": name,
                "file": _attr(rs[0], "aa.workflow.file"),
                "runs": len(judged),
                "failures": len(fails),
                "failure_rate": round(len(fails) / len(judged), 4) if judged else None,
                "p50_s": percentile(durs, 50),
                "p95_s": percentile(durs, 95),
                "reruns": sum(1 for r in rs if (_attr(r, "aa.run_attempt") or 1) > 1),
                "minutes": mins,
                "kind": "deploy"
                if _attr(rs[0], "aa.workflow.file") in app["deploy_workflows"]
                else ("ci" if _attr(rs[0], "aa.workflow.file") in app["ci_workflows"] else "other"),
                "last_url": _attr(last, "cicd.pipeline.run.url.full"),
                "last_result": _attr(last, "cicd.pipeline.result"),
            }
        )
    workflows.sort(key=lambda w: (-w["runs"], w["name"]))
    slowest = max(
        (w for w in workflows if w["kind"] in ("ci", "deploy") and w["p95_s"] is not None),
        key=lambda w: w["p95_s"],
        default=None,
    )
    # main: the CI and deploy workflows on the default branch
    watch = set(app["ci_workflows"]) | set(app["deploy_workflows"])
    main_runs = sorted(
        [
            r
            for r in runs
            if _attr(r, "aa.workflow.file") in watch
            and _attr(r, "vcs.ref.head.name") == app["default_branch"]
            and _attr(r, "aa.trigger") in cfg["main_triggers"]
            and _attr(r, "cicd.pipeline.result") not in (None, "cancellation", "skip")
        ],
        key=lambda r: _attr(r, "end_time") or "",
    )
    state, red_since, red_url = "unknown", None, None
    per_wf: dict[str, list[dict]] = defaultdict(list)
    for r in main_runs:
        per_wf[_attr(r, "aa.workflow.file")].append(r)
    if per_wf:
        state = "green"
        for rs in per_wf.values():
            if _attr(rs[-1], "cicd.pipeline.result") in RED:
                state = "red"
                streak = rs[-1]
                for r in reversed(rs):
                    if _attr(r, "cicd.pipeline.result") not in RED:
                        break
                    streak = r
                t = ts(_attr(streak, "start_time"))
                if red_since is None or (t and t < red_since):
                    red_since, red_url = t, _attr(rs[-1], "cicd.pipeline.run.url.full")
    in_win = [r for r in main_runs if in_range(ts(_attr(r, "end_time")), a, b)]
    main_fail = [r for r in in_win if _attr(r, "cicd.pipeline.result") in RED]
    return {
        "workflows": workflows[: cfg["list_cap"]],
        "slowest": {"name": slowest["name"], "p95_s": slowest["p95_s"], "n": slowest["runs"]} if slowest else None,
        "main": {
            "state": state,
            "red_since": iso(red_since),
            "red_hours": hours(red_since, now) if red_since else None,
            "url": red_url,
            "failure_rate": round(len(main_fail) / len(in_win), 4) if in_win else None,
            "n": len(in_win),
        },
        "reruns": sum(w["reruns"] for w in workflows),
        "minutes": {"total": total_min, "coverage": round(covered / len(done), 4) if done else None, "n": len(done)},
    }


def flaky_block(cfg: dict, runs: list[dict], jobs_by_run: dict, a: datetime, b: datetime) -> list[dict]:
    """Jobs that failed and then passed on the same commit (a re-run attempt, or a later run of that workflow)."""
    flips: dict[tuple, dict] = {}
    seq: dict[tuple, list[tuple]] = defaultdict(list)
    for r in runs:
        for j in jobs_by_run.get(_attr(r, "cicd.pipeline.run.id")) or []:
            res = _attr(j, "cicd.pipeline.task.run.result")
            if res not in ("success",) + RED:
                continue
            key = (
                _attr(r, "cicd.pipeline.name"),
                _attr(j, "cicd.pipeline.task.name"),
                _attr(r, "vcs.ref.head.revision"),
            )
            seq[key].append((_attr(j, "end_time") or "", res, _attr(j, "cicd.pipeline.task.run.url.full")))
    for (wf, job, _sha), items in seq.items():
        items.sort()
        for (_t1, r1, _u1), (t2, r2, u2) in zip(items, items[1:], strict=False):
            if r1 in RED and r2 == "success" and in_range(ts(t2), a, b):
                f = flips.setdefault((wf, job), {"workflow": wf, "job": job, "flips": 0, "last_at": None, "url": None})
                f["flips"] += 1
                if (f["last_at"] or "") < t2:
                    f["last_at"], f["url"] = t2, u2
    return sorted(flips.values(), key=lambda f: (-f["flips"], f["workflow"], f["job"]))[: cfg["list_cap"]]


def queue_block(changes: list[dict], a: datetime, b: datetime) -> dict:
    qs = [c for c in changes if c["queued"] and in_range(c["merged"], a, b)]
    waits = [hours(c["queued"], c["merged"]) for c in qs]
    return {
        "wait_p50_h": percentile(waits, 50),
        "wait_p95_h": percentile(waits, 95),
        "n": len(qs),
        "ejections": sum(max(c["queue_attempts"] - 1, 0) for c in qs),
    }


# ── AA-1125: merge queue entries ─────────────────────────────────────────────────────────────────────────────────


def open_entry(events: Optional[list]) -> bool:
    """True when the last queue event of a timeline is an `added` with no exit after it (still in the queue)."""
    state = False
    for k, _w, _a in events or ():
        if k == "added":
            state = True
        elif k in ("removed", "merged", "closed"):
            state = False
    return state


def queue_entries(timeline: dict) -> list[dict]:
    """Each stay of one pull request in the merge queue, oldest first.

    outcome: merged (the queue removed it and merged it at once), ejected (the queue removed it without a merge),
    removed (a person or the agent took it out, or the pull request was closed), queued (still in the queue).
    `new_commit`: a commit or a force push between the previous exit and this entry."""
    events = _attr(timeline, "aa.queue.events") or []
    out: list[dict] = []
    cur: Optional[dict] = None
    pushed = False
    for i, (k, w, actor) in enumerate(events):
        if k in ("commit", "force_push"):
            pushed = True
        elif k == "added":
            if cur is None:
                cur = {"enqueued": ts(w), "new_commit": pushed and bool(out), "enqueued_by": actor}
                pushed = False
        elif k == "removed" and cur is not None:
            # the queue's own removal comes with the merge (same second); a close at the same time is a person's act
            nxt = next((e for e in events[i + 1 :] if e[0] not in ("commit", "force_push")), None)
            outcome = "removed"
            if actor == "queue":
                outcome = "ejected"
                if nxt and nxt[0] == "merged":
                    outcome = "merged"
                elif nxt and nxt[0] == "closed" and (ts(nxt[1]) or ts(w)) - ts(w) <= timedelta(minutes=1):
                    outcome = "removed"
            out.append({**cur, "left": ts(w), "outcome": outcome, "removed_by": actor})
            cur = None
        elif k in ("merged", "closed") and cur is not None:
            out.append({**cur, "left": ts(w), "outcome": "merged" if k == "merged" else "removed", "removed_by": actor})
            cur = None
    if cur is not None:
        out.append({**cur, "left": None, "outcome": "queued", "removed_by": None})
    return out


def _assign_runs(entries: list[dict], runs: list[dict]) -> list[list[dict]]:
    """The merge_group runs of each queue entry of one pull request: a run belongs to the newest entry that started
    before it (one minute of clock slack), so a run is never counted twice."""
    out: list[list[dict]] = [[] for _ in entries]
    for r in runs:
        t = ts(_attr(r, "aa.created_at"))
        if t is None:
            continue
        idx = None
        for i, e in enumerate(entries):
            if e["enqueued"] and e["enqueued"] - timedelta(minutes=1) <= t:
                idx = i
        if idx is not None and (entries[idx]["left"] is None or t <= entries[idx]["left"] + timedelta(minutes=1)):
            out[idx].append(r)
    return out


def _rerun_green(run: dict, jobs: Optional[list[dict]]) -> bool:
    """A merge_group run whose newest attempt passed after a job of an earlier attempt failed."""
    if _attr(run, "cicd.pipeline.result") != "success" or (_attr(run, "aa.run_attempt") or 1) < 2:
        return False
    newest = _attr(run, "aa.run_attempt") or 1
    return any(
        (_attr(j, "aa.run_attempt") or 1) < newest and _attr(j, "cicd.pipeline.task.run.result") in RED
        for j in jobs or ()
    )


def classify_entries(
    cfg: dict, app: dict, timelines: list[dict], runs: list[dict], jobs_by_run: dict, prs: list[dict]
) -> list[dict]:
    """Every queue entry of the app with its merge_group runs, ejection reason and flaky signals."""
    org = cfg["org"]
    by_pr: dict[int, list[dict]] = defaultdict(list)
    for r in runs:
        a = r["attributes"]
        m = QUEUE_BRANCH.match(a["vcs.ref.head.name"] or "")
        if m and a["aa.trigger"] == "merge_group":
            by_pr[int(m.group(1))].append(r)
    titles = {_attr(p, "vcs.change.id"): _attr(p, "vcs.change.title") for p in prs}
    out: list[dict] = []
    for tl in timelines:
        n = _attr(tl, "vcs.change.id")
        entries = queue_entries(tl)
        assigned = _assign_runs(entries, by_pr.get(n, []))
        for i, e in enumerate(entries):
            rs = assigned[i]
            red = [r for r in rs if _attr(r, "cicd.pipeline.result") in ("failure", "error")]
            timeouts = [r for r in rs if _attr(r, "cicd.pipeline.result") == "timeout"]
            cancelled = [r for r in rs if _attr(r, "cicd.pipeline.result") == "cancellation"]
            reason = None
            if e["outcome"] == "ejected":
                reason = (
                    "checks_failed"
                    if red
                    else "checks_timeout"
                    if timeouts
                    else "checks_cancelled"
                    if cancelled
                    else "no_checks"
                    if not rs
                    else "other"
                )
            elif e["outcome"] == "removed":
                reason = "removed"
            mins = sum(run_minutes(r, jobs_by_run.get(_attr(r, "cicd.pipeline.run.id")))[0] for r in rs)
            reruns = [r for r in rs if (_attr(r, "aa.run_attempt") or 1) > 1]
            first_bad = min(red + timeouts, key=lambda r: _attr(r, "aa.created_at") or "", default=None)
            e.update(
                {
                    "pr": n,
                    "title": titles.get(n),
                    "url": f"https://github.com/{org}/{app['repo']}/pull/{n}",
                    "attempt": i + 1,
                    "attempts": len(entries),
                    "reason": reason,
                    "failed_workflows": sorted({_attr(r, "cicd.pipeline.name") for r in red + timeouts}),
                    "runs": len(rs),
                    "rebuilds": max(len({_attr(r, "vcs.ref.head.name") for r in rs}) - 1, 0),
                    "reruns": len(reruns),
                    "minutes": mins,
                    "run_url": _attr(first_bad, "cicd.pipeline.run.url.full")
                    if first_bad
                    else (_attr(rs[0], "cicd.pipeline.run.url.full") if rs else None),
                    "flaky": None,
                }
            )
            if any(_rerun_green(r, jobs_by_run.get(_attr(r, "cicd.pipeline.run.id"))) for r in rs):
                e["flaky"] = "rerun_green"
        # an ejection on red checks, then the same pull request back in the queue with no new commit and merged
        for prev, nxt in zip(entries, entries[1:], strict=False):
            if (
                prev["outcome"] == "ejected"
                and prev["reason"] in ("checks_failed", "checks_timeout")
                and not nxt["new_commit"]
                and nxt["outcome"] == "merged"
            ):
                prev["flaky"] = "reentry_green"
        out.extend(entries)
    return out


def entry_row(e: dict) -> dict:
    return {
        "pr": e["pr"],
        "title": e["title"],
        "url": e["url"],
        "enqueued_at": iso(e["enqueued"]),
        "left_at": iso(e["left"]),
        "outcome": e["outcome"],
        "reason": e["reason"],
        "failed_workflows": e["failed_workflows"],
        "wait_h": hours(e["enqueued"], e["left"]) if e["left"] else None,
        "attempt": e["attempt"],
        "attempts": e["attempts"],
        "runs": e["runs"],
        "rebuilds": e["rebuilds"],
        "reruns": e["reruns"],
        "minutes": e["minutes"],
        "flaky": e["flaky"],
        "new_commit": e["new_commit"],
        "run_url": e["run_url"],
    }


def recent_in_window(e: dict, a: datetime, b: datetime) -> bool:
    """A merge-queue entry of the window [a, b) in the recent list: it left the queue inside the window, or it is still
    queued and entered the queue before the window end (it was in the queue at the end of the range)."""
    if e["left"]:
        return in_range(e["left"], a, b)
    return e["outcome"] == "queued" and (e["enqueued"] is None or e["enqueued"] < b)


def wait_bins(cfg: dict) -> list[float]:
    """The upper edges (hours) of the merge-queue wait histogram (apps.json queue_wait_bins_h)."""
    return sorted(float(x) for x in cfg.get("queue_wait_bins_h") or (0.25, 0.5, 1, 2, 4, 8, 24))


def wait_histogram(waits: Iterable[Optional[float]], edges: list[float]) -> list[dict]:
    """Counts of `waits` per bin [lo, hi) of hours; the last bin has no upper edge (hi null)."""
    lows = [0.0, *edges]
    rows = [{"lo_h": lo, "hi_h": hi, "n": 0} for lo, hi in zip(lows, [*edges, None], strict=True)]
    for w in waits:
        if w is None:
            continue
        idx = next((i for i, hi in enumerate(edges) if w < hi), len(edges))
        rows[idx]["n"] += 1
    return rows


def queue_weekly(done: list[dict], a: datetime, b: datetime) -> list[dict]:
    """Per ISO week touching [a, b): the entries that left the queue in the window that week, by outcome, and the
    ejection rate (ejected over merged + ejected; null without either)."""
    if b <= a:
        return []
    out: dict[str, dict] = {}
    d = week_start(a.date())
    while d <= (b - timedelta(seconds=1)).date():
        ws = datetime.combine(d, datetime.min.time(), timezone.utc)
        out[iso_week(ws)] = {"week": iso_week(ws), "start": d.isoformat(), "merged": 0, "ejected": 0, "removed": 0,
                             "ejection_rate": None}
        d += timedelta(weeks=1)
    for e in done:
        row = out.get(iso_week(e["left"]))
        if row is not None and e["outcome"] in ("merged", "ejected", "removed"):
            row[e["outcome"]] += 1
    for row in out.values():
        judged = row["merged"] + row["ejected"]
        row["ejection_rate"] = round(row["ejected"] / judged, 4) if judged else None
    return list(out.values())


def queue_summary(
    cfg: dict, app: dict, store: Store, runs: list[dict], jobs_by_run: dict, prs: list[dict], a: datetime, b: datetime
) -> dict:
    """The merge-queue section of one app over the headline window [a, b) (entries that left the queue in it)."""
    repo = app["repo"]
    entries = classify_entries(cfg, app, store.of("aa.merge_queue.timeline", repo), runs, jobs_by_run, prs)
    done = [e for e in entries if e["left"] and in_range(e["left"], a, b)]
    merged = [e for e in done if e["outcome"] == "merged"]
    ejected = [e for e in done if e["outcome"] == "ejected"]
    judged = merged + ejected
    flaky = [e for e in judged if e["flaky"]]
    first_in: dict[int, datetime] = {}
    for e in entries:
        if e["enqueued"] and (e["pr"] not in first_in or e["enqueued"] < first_in[e["pr"]]):
            first_in[e["pr"]] = e["enqueued"]
    to_merge = [hours(first_in.get(e["pr"]), e["left"]) for e in merged]
    reasons: dict[str, dict] = {}
    for e in sorted(ejected + [e for e in done if e["outcome"] == "removed"], key=lambda e: e["left"]):
        r = reasons.setdefault(e["reason"], {"reason": e["reason"], "n": 0, "workflows": {}, "last_pr": None,
                                             "last_url": None, "last_at": None})
        r["n"] += 1
        for w in e["failed_workflows"]:
            r["workflows"][w] = r["workflows"].get(w, 0) + 1
        r["last_pr"], r["last_url"], r["last_at"] = e["pr"], e["run_url"] or e["url"], iso(e["left"])
    # merge_group CI: the runs of the queue branches that finished in the window
    qruns = [
        r
        for r in runs
        if _attr(r, "aa.trigger") == "merge_group"
        and _attr(r, "cicd.pipeline.result")
        and in_range(ts(_attr(r, "end_time")), a, b)
    ]
    qjudged = [r for r in qruns if _attr(r, "cicd.pipeline.result") not in ("cancellation", "skip")]
    qred = [r for r in qjudged if _attr(r, "cicd.pipeline.result") in RED]
    durs = [_attr(r, "aa.duration_s") for r in qjudged if _attr(r, "aa.duration_s") is not None]
    by_wf: dict[str, dict] = {}
    for r in qruns:
        w = by_wf.setdefault(_attr(r, "cicd.pipeline.name"), {"name": _attr(r, "cicd.pipeline.name"), "runs": 0,
                                                               "failures": 0, "minutes": 0, "reruns": 0})
        w["runs"] += 1
        w["failures"] += _attr(r, "cicd.pipeline.result") in RED
        w["minutes"] += run_minutes(r, jobs_by_run.get(_attr(r, "cicd.pipeline.run.id")))[0]
        w["reruns"] += (_attr(r, "aa.run_attempt") or 1) > 1
    days = _days(a.date(), (b - timedelta(seconds=1)).date()) if b > a else []
    per_day = {d.isoformat(): {"date": d.isoformat(), "merged": 0, "ejected": 0, "removed": 0} for d in days}
    for e in done:
        row = per_day.get(e["left"].date().isoformat())
        if row is not None:
            row[e["outcome"]] += 1
    st = store.repo_state(repo)
    return {
        "window": {"start": iso(a), "end": iso(b)},
        # AA-1135: the wait of each merged entry in bins of hours, and the outcomes and ejection rate per ISO week
        "wait_histogram": wait_histogram([hours(e["enqueued"], e["left"]) for e in merged], wait_bins(cfg)),
        "weekly": queue_weekly(done, a, b),
        "complete_since": st.get("runs_complete_since"),
        "entries": len(done),
        "prs": len({e["pr"] for e in done}),
        "merged": len(merged),
        "ejected": len(ejected),
        "removed": sum(1 for e in done if e["outcome"] == "removed"),
        "in_queue": sum(1 for e in entries if e["outcome"] == "queued"),
        "wait": {
            "p50_h": percentile([hours(e["enqueued"], e["left"]) for e in merged], 50),
            "p95_h": percentile([hours(e["enqueued"], e["left"]) for e in merged], 95),
            "n": len(merged),
        },
        "to_merge": {"p50_h": percentile(to_merge, 50), "p95_h": percentile(to_merge, 95), "n": len(to_merge)},
        "ejection_rate": {"value": round(len(ejected) / len(judged), 4) if judged else None, "n": len(judged),
                          "ejected": len(ejected)},
        "flaky": {
            "value": round(len(flaky) / len(judged), 4) if judged else None,
            "n": len(judged),
            "flaky": len(flaky),
            "rerun_green": sum(1 for e in flaky if e["flaky"] == "rerun_green"),
            "reentry_green": sum(1 for e in flaky if e["flaky"] == "reentry_green"),
        },
        "reasons": sorted(reasons.values(), key=lambda r: (-r["n"], r["reason"] or "")),
        "ci": {
            "runs": len(qjudged),
            "failures": len(qred),
            "failure_rate": round(len(qred) / len(qjudged), 4) if qjudged else None,
            "p50_s": percentile(durs, 50),
            "p95_s": percentile(durs, 95),
            "reruns": sum(1 for r in qruns if (_attr(r, "aa.run_attempt") or 1) > 1),
            "minutes": sum(w["minutes"] for w in by_wf.values()),
            "workflows": sorted(by_wf.values(), key=lambda w: (-w["minutes"], w["name"]))[: cfg["list_cap"]],
        },
        "throughput": list(per_day.values()),
        # S284: bounded at both ends BEFORE the list cap: entries that left after a past range's end took every place
        "recent": [
            entry_row(e)
            for e in sorted(entries, key=lambda e: e["left"] or b + timedelta(days=1), reverse=True)
            if recent_in_window(e, a, b)
        ][: cfg["list_cap"]],
        "flaky_entries": [entry_row(e) for e in sorted(flaky, key=lambda e: e["left"], reverse=True)][
            : cfg["list_cap"]
        ],
    }


# ── AA-1125: git activity ────────────────────────────────────────────────────────────────────────────────────────


def _top_dir(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "(root)"


def _child(path: str) -> str:
    """The drill-down level under a top-level directory: its sub-directory, or the file itself."""
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else path


def branch_prefix(ref: Optional[str]) -> str:
    """feat / fix / docs / chore / bot / release-please / dependabot / ... from a pull request's head branch."""
    ref = ref or ""
    if ref.startswith("release-please"):
        return "release-please"
    return ref.split("/", 1)[0].lower() if "/" in ref else "other"


def churn_block(cfg: dict, commits: list[dict], prs: list[dict], app: dict, a: datetime, b: datetime) -> dict:
    inside = [c for c in commits if in_range(ts(c["time"]), a, b)]
    read = [c for c in inside if _attr(c, "aa.files") is not None]
    excl = cfg.get("churn_exclude") or []
    dirs: dict[str, dict] = {}
    children: dict[str, dict[str, dict]] = defaultdict(dict)
    files: dict[str, dict] = {}

    def add(row: dict, adds: int, dels: int, sha: str) -> None:
        row["additions"] += adds
        row["deletions"] += dels
        row["churn"] += adds + dels
        row["_shas"].add(sha)

    for c in read:
        sha = _attr(c, "vcs.ref.head.revision")
        kind = _attr(c, "aa.author.kind") if _attr(c, "aa.author.kind") in COMMIT_KINDS else "human"
        for path, adds, dels in _attr(c, "aa.files") or []:
            if any(fnmatch.fnmatch(path, pat) for pat in excl):
                continue
            new = {"additions": 0, "deletions": 0, "churn": 0, "_shas": set()}
            d = dirs.setdefault(_top_dir(path), {"path": _top_dir(path), **new, "_files": set(), "_kinds": {}})
            add(d, adds, dels, sha)
            d["_files"].add(path)
            d["_kinds"].setdefault(kind, set()).add(sha)
            ch = children[d["path"]].setdefault(_child(path), {"path": _child(path), **new, "_shas": set(),
                                                                "last_at": None})
            add(ch, adds, dels, sha)
            ch["last_at"] = max(ch["last_at"] or "", c["time"] or "") or None
            f = files.setdefault(path, {"path": path, **new, "_shas": set(), "last_at": None})
            add(f, adds, dels, sha)
            f["last_at"] = max(f["last_at"] or "", c["time"] or "") or None

    def out(row: dict) -> dict:
        r = {k: v for k, v in row.items() if not k.startswith("_")}
        r["commits"] = len(row["_shas"])
        if "_files" in row:
            r["files"] = len(row["_files"])
        if "_kinds" in row:   # commits per author kind: who changes this directory
            r["by_kind"] = {k: len(v) for k, v in sorted(row["_kinds"].items())}
        return r

    def top(rows: Iterable[dict], k: int) -> list[dict]:
        return [out(r) for r in sorted(rows, key=lambda r: (-r["churn"], -len(r["_shas"]), r["path"]))[:k]]

    merged = [
        p
        for p in prs
        if _attr(p, "vcs.ref.base.name") == app["default_branch"] and in_range(ts(_attr(p, "aa.merged_at")), a, b)
    ]
    prefixes: dict[str, int] = defaultdict(int)
    for p in merged:
        prefixes[branch_prefix(_attr(p, "vcs.ref.head.name"))] += 1
    kinds = {k: 0 for k in COMMIT_KINDS}
    for c in inside:
        kinds[_attr(c, "aa.author.kind") if _attr(c, "aa.author.kind") in kinds else "human"] += 1
    dir_rows = top(dirs.values(), len(dirs))
    for d in dir_rows:
        d["children"] = top(children[d["path"]].values(), cfg["top_children"])
    return {
        "start": iso(a),
        "end": iso(b),
        "commits": len(inside),
        "files_read": len(read),
        "additions": sum(_attr(c, "aa.additions") or 0 for c in read),
        "deletions": sum(_attr(c, "aa.deletions") or 0 for c in read),
        "by_kind": kinds,
        "merges": len(merged),
        "merges_by_prefix": dict(sorted(prefixes.items(), key=lambda kv: (-kv[1], kv[0]))),
        "dirs": dir_rows,
        "files": top(files.values(), cfg["top_files"]),
    }


def git_summary(cfg: dict, app: dict, store: Store, prs: list[dict], now: datetime) -> dict:
    repo, org = app["repo"], cfg["org"]
    st = store.repo_state(repo)
    start = now - timedelta(days=cfg["git_days"])
    commits = sorted(
        (c for c in store.of("aa.vcs.commit", repo) if _attr(c, "vcs.ref.head.name") == app["default_branch"]),
        key=lambda c: (c["time"] or "", c["id"]),
    )
    recent = [c for c in commits if in_range(ts(c["time"]), start, now + timedelta(seconds=1))]
    per_day = {d.isoformat(): 0 for d in _days(start.date(), now.date())}
    for c in recent:
        k = (c["time"] or "")[:10]
        if k in per_day:
            per_day[k] += 1
    graph = []
    for c in reversed(commits[-cfg["graph_commits"] :]):
        a = c["attributes"]
        graph.append(
            {
                "sha": a["vcs.ref.head.revision"],
                "parents": a.get("aa.parents") or [],
                "title": a.get("aa.commit.title"),
                "pr": a.get("vcs.change.id"),
                "url": f"https://github.com/{org}/{repo}/commit/{a['vcs.ref.head.revision']}",
                "pr_url": f"https://github.com/{org}/{repo}/pull/{a['vcs.change.id']}" if a.get("vcs.change.id")
                else None,
                "kind": a.get("aa.author.kind"),
                "at": c["time"],
                "additions": a.get("aa.additions"),
                "deletions": a.get("aa.deletions"),
                "files": len(a["aa.files"]) if a.get("aa.files") is not None else None,
            }
        )
    read = sum(1 for c in recent if _attr(c, "aa.files") is not None)
    return {
        "branch": app["default_branch"],
        "days": cfg["git_days"],
        "complete_since": st.get("commits_complete_since"),
        "commits": len(recent),
        "files_coverage": round(read / len(recent), 4) if recent else None,
        "daily": [{"date": d, "n": n} for d, n in per_day.items()],
        "windows": {
            f"d{w}": churn_block(cfg, recent, prs, app, now - timedelta(days=w), now + timedelta(seconds=1))
            for w in cfg["git_windows"]
        },
        "graph": graph,
    }


def group_row(g: dict) -> dict:
    return {
        "sha": g["sha"],
        "started_at": iso(g["start"]),
        "deployed_at": iso(g["deployed"]),
        "finished_at": iso(g["end"]),
        "verified_at": iso(g["verified"]),
        "outcome": g["outcome"],
        "failed": list(g["failed"]),
        "rework": list(g["rework"]),
        "redeploy": g["redeploy"],
        "failed_at": iso(g.get("failed_at")),
        "recovered_at": iso(g.get("recovered_at")),
        "recovery_h": g.get("recovery_h"),
        "deployment_records": g["deployments"],
        "runs": g["runs"],
        "changes": [c["number"] for c in g.get("changes", [])][:50],
    }


def change_row(c: dict) -> dict:
    return {
        "number": c["number"],
        "title": c["title"],
        "url": c["url"],
        "kind": c["kind"],
        "login": c["login"],
        "excluded": c["excluded"],
        "merged_at": iso(c["merged"]),
        "lead_time_h": hours(c["opened"], c["verified"]) if c["verified"] else None,
        "segments": segments_of(c) if c["verified"] else {},
    }


def app_context(cfg: dict, app: dict, store: Store, now: datetime) -> dict:
    """Everything of one app that does not depend on the window: its runs and jobs, merged pull requests, deploys
    (grouped, failed and recovered) and changes (lead-time points). Every window of the app is cut from this."""
    repo = app["repo"]
    runs = store.of("cicd.pipeline.run", repo)
    jobs_by_run: dict[Any, list[dict]] = {}
    for m in store.of("aa.jobs.read", repo):
        jobs_by_run.setdefault(_attr(m, "cicd.pipeline.run.id"), [])
    for j in store.of("cicd.pipeline.task.run", repo):
        jobs_by_run.setdefault(_attr(j, "cicd.pipeline.run.id"), []).append(j)
    prs = store.of("vcs.change", repo)
    deps = [d for d in store.of("deployment", repo) if _attr(d, "deployment.environment.name") == cfg["environment"]]
    groups = deploy_groups(cfg, app, runs, jobs_by_run, deps)
    st = store.repo_state(repo)
    # the run history is complete from this day on; a window that starts earlier is measured from it (n says how much)
    cs = ts(f"{st['runs_complete_since']}T00:00:00Z") if st.get("runs_complete_since") else now
    changes = attach_changes(cfg, app, groups, prs, runs, since=cs)
    recovery(groups)
    commits = sorted(
        (c for c in store.of("aa.vcs.commit", repo) if _attr(c, "vcs.ref.head.name") == app["default_branch"]),
        key=lambda c: (c["time"] or "", c["id"]),
    )
    return {"store": store, "runs": runs, "jobs_by_run": jobs_by_run, "prs": prs, "groups": groups,
            "changes": changes, "cs": cs, "state": st, "commits": commits, "now": now}


def _window(ctx: dict, req_a: datetime, b: datetime) -> tuple[datetime, dict]:
    """The measured start of a window that asks to start at `req_a` (never before the complete history) and its
    `window` block."""
    now, cs = ctx["now"], ctx["cs"]
    a = min(max(req_a, cs), now)
    return a, {
        "start": iso(a),
        "end": iso(b),
        "days": round((b - req_a).total_seconds() / 86400, 1),
        "requested_start": iso(req_a),
        "covered_days": round(max((b - a).total_seconds(), 0.0) / 86400, 1),
    }


def changes_block(cfg: dict, changes: list[dict], a: datetime, b: datetime) -> dict:
    recent_changes = [c for c in changes if in_range(c["merged"], a, b)]
    return {
        "merged": len(recent_changes),
        "by_kind": {k: sum(1 for c in recent_changes if c["kind"] == k) for k in ("agent", "human", "bot")},
        "docs_only": sum(1 for c in recent_changes if c["excluded"] == "docs_only"),
        "awaiting_deploy": sum(1 for c in recent_changes if not c["excluded"] and not c["verified"]),
        "bot": [
            change_row(c)
            for c in sorted(recent_changes, key=lambda c: c["merged"], reverse=True)
            if c["excluded"] == "bot"
        ][: cfg["list_cap"]],
        "slowest": [
            change_row(c)
            for c in sorted(
                (c for c in recent_changes if not c["excluded"] and c["verified"]),
                key=lambda c: -(hours(c["opened"], c["verified"]) or 0),
            )
        ][:10],
    }


def git_range(cfg: dict, app: dict, ctx: dict, a: datetime, b: datetime) -> dict:
    """AA-1135: the git activity of one window (churn by directory and file, author kinds, merges by prefix) and its
    commits a day; the Git map tab of a chosen range."""
    st = ctx["state"]
    inside = [c for c in ctx["commits"] if in_range(ts(c["time"]), a, b)]
    per_day = {d.isoformat(): 0 for d in _days(a.date(), (b - timedelta(seconds=1)).date())} if b > a else {}
    for c in inside:
        k = (c["time"] or "")[:10]
        if k in per_day:
            per_day[k] += 1
    read = sum(1 for c in inside if _attr(c, "aa.files") is not None)
    return {
        **churn_block(cfg, inside, ctx["prs"], app, a, b),
        "complete_since": st.get("commits_complete_since"),
        "files_coverage": round(read / len(inside), 4) if inside else None,
        "daily": [{"date": d, "n": n} for d, n in per_day.items()],
    }


def range_block(cfg: dict, app: dict, ctx: dict, req_a: datetime, b: datetime) -> dict:
    """AA-1135: every windowed number of one app over [req_a, b) (clipped to the complete history): the same blocks
    as the 28-day headline, plus the deploys of the window, the merge queue and the git activity. The presets of the
    summary (`ranges`) and the CIS custom range both come from here."""
    a, window = _window(ctx, req_a, b)
    runs, jobs_by_run, groups, changes = ctx["runs"], ctx["jobs_by_run"], ctx["groups"], ctx["changes"]
    in_win = [g for g in groups if in_range(g["start"], a, b)]
    return {
        "window": window,
        "dora": dora_block(groups, changes, a, b),
        "lead_time_segments": lead_segments(changes, a, b),
        "ci": ci_block(cfg, app, runs, jobs_by_run, a, b, ctx["now"]),
        "merge_queue": queue_block(changes, a, b),
        "flaky": flaky_block(cfg, runs, jobs_by_run, a, b),
        "verify": verify_block(groups, a, b),
        "deploys": [group_row(g) for g in reversed(in_win)][: cfg["list_cap"]],
        "failed_deploys": [group_row(g) for g in reversed(in_win) if g["failed"]][: cfg["list_cap"]],
        "changes": changes_block(cfg, changes, a, b),
        "queue": queue_summary(cfg, app, ctx["store"], runs, jobs_by_run, ctx["prs"], a, b)
        if app.get("merge_queue")
        else None,
        "git": git_range(cfg, app, ctx, req_a, b),
    }


def range_presets(cfg: dict) -> list[int]:
    return [int(d) for d in cfg.get("range_presets") or (7, 28, 90)]


def app_summary(cfg: dict, app: dict, store: Store, now: datetime) -> dict:
    repo = app["repo"]
    ctx = app_context(cfg, app, store, now)
    runs, jobs_by_run, prs, groups, changes, cs = (ctx[k] for k in ("runs", "jobs_by_run", "prs", "groups", "changes",
                                                                    "cs"))
    st = ctx["state"]

    def clip(x: datetime) -> datetime:
        return min(max(x, cs), now)

    req_a, b = now - timedelta(days=cfg["headline_days"]), now
    a = clip(req_a)
    trend_from = datetime.combine(
        week_start(now.date()) - timedelta(weeks=cfg["trend_weeks"] - 1), datetime.min.time(), timezone.utc
    )
    weeks = []
    for i in range(cfg["trend_weeks"]):
        ws = trend_from + timedelta(weeks=i)
        we = ws + timedelta(weeks=1)
        d = dora_block(groups, changes, clip(ws), min(we, now))
        c = ci_block(cfg, app, runs, jobs_by_run, clip(ws), min(we, now), now)
        weeks.append(
            {
                "week": iso_week(ws),
                "start": ws.date().isoformat(),
                "end": (we - timedelta(days=1)).date().isoformat(),
                "partial": we > now,
                "complete": ws >= cs,
                **_flat(d),
                "main_failure_rate": c["main"]["failure_rate"],
                "main_n": c["main"]["n"],
                "ci_p95_s": c["slowest"]["p95_s"] if c["slowest"] else None,
                "minutes": c["minutes"]["total"],
            }
        )
    months = []
    first = now.date().replace(day=1)
    for i in reversed(range(cfg["trend_months"])):
        y, m = first.year, first.month - i
        while m < 1:
            y, m = y - 1, m + 12
        ms = datetime(y, m, 1, tzinfo=timezone.utc)
        me = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=timezone.utc)
        d = dora_block(groups, changes, clip(ms), min(me, now))
        c = ci_block(cfg, app, runs, jobs_by_run, clip(ms), min(me, now), now)
        months.append(
            {
                "month": f"{y:04d}-{m:02d}",
                "start": ms.date().isoformat(),
                "end": (me - timedelta(days=1)).date().isoformat(),
                "partial": me > now,
                "complete": ms >= cs,
                **_flat(d),
                "lead_time": d["lead_time"],
                "main_failure_rate": c["main"]["failure_rate"],
                "main_n": c["main"]["n"],
                "verify": verify_block(groups, clip(ms), min(me, now)),
                "failed_deploys": [group_row(g) for g in groups if g["failed"] and in_range(g["start"], ms, me)],
            }
        )
    in_trend = [g for g in groups if in_range(g["start"], trend_from, now)]
    return {
        "id": app["id"],
        "name": app["name"],
        "repo": repo,
        "environment": cfg["environment"],
        "window": {
            "start": iso(a),
            "end": iso(b),
            "days": cfg["headline_days"],
            "requested_start": iso(req_a),
            "covered_days": round((b - a).total_seconds() / 86400, 1),
        },
        "complete_since": st.get("runs_complete_since"),
        "dora": dora_block(groups, changes, a, b),
        "lead_time_segments": lead_segments(changes, a, b),
        "ci": ci_block(cfg, app, runs, jobs_by_run, a, b, now),
        "merge_queue": queue_block(changes, a, b),
        "flaky": flaky_block(cfg, runs, jobs_by_run, a, b),
        "verify": verify_block(groups, a, b),
        "deploys": [group_row(g) for g in reversed(in_trend)][: cfg["list_cap"]],
        "failed_deploys": [group_row(g) for g in reversed(in_trend) if g["failed"]][: cfg["list_cap"]],
        "changes": changes_block(cfg, changes, a, b),
        "trend": {"weeks": weeks, "months": months},
        "queue": queue_summary(cfg, app, store, runs, jobs_by_run, prs, a, b) if app.get("merge_queue") else None,
        "git": git_summary(cfg, app, store, prs, now),
        # AA-1135: the presets of the range picker, each the same blocks over the last N days
        "ranges": {f"d{n}": range_block(cfg, app, ctx, now - timedelta(days=n), now) for n in range_presets(cfg)},
    }


def verify_block(groups: list[dict], a: datetime, b: datetime) -> dict:
    gs = [g for g in groups if g["outcome"] in ("success", "verify_failed") and in_range(g["start"], a, b)]
    red = [g for g in gs if g["outcome"] == "verify_failed"]
    last = max(red, key=lambda g: g["start"], default=None)
    return {
        "pass_rate": round(1 - len(red) / len(gs), 4) if gs else None,
        "n": len(gs),
        "red": len(red),
        "last_red_at": iso(last["verify_failed_at"] or last["end"]) if last else None,
        "last_red_url": last["runs"][-1]["url"] if last else None,
    }


def _flat(d: dict) -> dict:
    return {
        "deploys": d["deployment_frequency"]["n"],
        "deploy_attempts": d["deployment_frequency"]["attempts"],
        "lead_time_p50_h": d["lead_time"]["p50_h"],
        "lead_time_n": d["lead_time"]["n"],
        "cfr": d["change_failure_rate"]["value"],
        "failed": d["change_failure_rate"]["failed"],
        "recovery_p50_h": d["recovery_time"]["p50_h"],
        "recovery_n": d["recovery_time"]["n"],
        "rework_rate": d["rework_rate"]["value"],
        "rework": d["rework_rate"]["rework"],
    }


def summarize(
    cfg: dict, store: Store, now: datetime, source: Optional[dict] = None, collection: Optional[dict] = None
) -> dict:
    return {
        "format": FORMAT,
        "generated_at": iso(now),
        "environment": cfg["environment"],
        "source": source or {},
        "definitions": {
            "version": DEFINITIONS_VERSION,
            "headline_days": cfg["headline_days"],
            "trend_weeks": cfg["trend_weeks"],
            "trend_months": cfg["trend_months"],
            "rework_hours": cfg["rework_hours"],
            "segments": [s[0] for s in SEGMENTS],
            "guide": "docs/guides/delivery-metrics.md",
            "git_days": cfg["git_days"],
            "git_windows": cfg["git_windows"],
            "commit_kinds": list(COMMIT_KINDS),
            "queue_outcomes": ["merged", "ejected", "removed", "queued"],
            # AA-1135: the range picker's presets, the stored history a custom range may ask for, and this engine
            "range_presets": range_presets(cfg),
            "store_days": cfg["store_days"],
            "store_from": store_start(cfg, now).isoformat(),
            "engine_sha256": engine_sha256(),
            "store_fingerprint": store_fingerprint(store.events.values(), store_cut(cfg, now)),
        },
        "collection": collection or {},
        "apps": [app_summary(cfg, app, store, now) for app in cfg["apps"]],
        # AA-1135: the collection parameters this summary was computed with (apps.json, public); the CIS custom range
        # recomputes with exactly these
        "config": cfg,
    }


# ── AA-1135: a custom range from the published day files ───────────────────────────────────────────────────────


def engine_sha256() -> str:
    """sha256 of this file: the collector publishes it, the CIS copy compares its own before computing a range."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def store_start(cfg: dict, now: datetime) -> date:
    """The first day the event store keeps (the collector prunes older events at the end of each run)."""
    return now.date() - timedelta(days=cfg["store_days"])


def store_cut(cfg: dict, now: datetime) -> datetime:
    """The first instant the event store keeps (midnight UTC of store_start)."""
    return datetime.combine(store_start(cfg, now), datetime.min.time(), timezone.utc)


def store_days(cfg: dict, now: datetime) -> list[str]:
    """Every day (YYYY-MM-DD) of the store of a summary computed at `now`, oldest first."""
    return [d.isoformat() for d in _days(store_start(cfg, now), now.date())]


def day_keys(cfg: dict, now: datetime) -> list[str]:
    """The day files (relative to the delivery prefix) that hold the store of a summary computed at `now`."""
    return [f"events/{d[:4]}/{d[5:7]}/{d[8:10]}.ndjson" for d in store_days(cfg, now)]


class StoreMismatch(ValueError):
    """The day files do not rebuild the store the summary was computed from (a failed upload, a lost cache)."""


def store_fingerprint(events: Iterable[dict], keep_from: datetime) -> dict[str, dict]:
    """Per repo, the events kept from `keep_from` on: their count and a sha256 of their canonical JSON lines in
    (time, id) order (the format of the day files). The summary publishes it; custom_range compares the store it
    rebuilt from the day files and refuses a difference instead of serving numbers that disagree with a preset."""
    cut = iso(keep_from) or ""
    by_repo: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for e in events:
        t = e.get("time") or ""
        if t < cut:
            continue
        repo = str((e.get("attributes") or {}).get("vcs.repository.name") or "")
        by_repo[repo].append((t, e["id"], json.dumps(e, sort_keys=True, separators=(",", ":"))))
    out: dict[str, dict] = {}
    for repo in sorted(by_repo):
        h = hashlib.sha256()
        for _t, _id, line in sorted(by_repo[repo]):
            h.update(line.encode("utf-8"))
            h.update(b"\n")
        out[repo] = {"events": len(by_repo[repo]), "sha256": h.hexdigest()}
    return out


def load_events(files: Iterable[Optional[str]]) -> dict[str, dict]:
    """The events of the day files (NDJSON text each, None for a missing day), one per id. An event that moved to a
    later day (a run that finished, a pull request updated) keeps an older copy in its first day's file: the copy
    with the newest time wins, as in the store."""
    events: dict[str, dict] = {}
    for text in files:
        for line in (text or "").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if not isinstance(e, dict) or not isinstance(e.get("id"), str) or "attributes" not in e:
                continue
            old = events.get(e["id"])
            if old is None or (e.get("time") or "") >= (old.get("time") or ""):
                events[e["id"]] = e
    return events


def state_from_summary(summary: dict) -> dict:
    """The store state a range needs (per repo: from which day the run and commit history is complete), as the
    summary publishes it."""
    repos: dict[str, dict] = {}
    for a in summary.get("apps") or []:
        git = a.get("git") if isinstance(a.get("git"), dict) else {}
        repos[a["repo"]] = {"runs_complete_since": a.get("complete_since"),
                            "commits_complete_since": git.get("complete_since")}
    return {"repos": repos}


def custom_range(summary: dict, events: dict[str, dict], a: datetime, b: datetime) -> dict:
    """Every app's `range_block` over [a, b) from the published day files, with the config and the time of the
    summary they belong to: the same numbers a preset of that window gives (tests: the parity test)."""
    cfg, now = summary["config"], ts(summary["generated_at"])
    if now is None:
        raise ValueError("summary without generated_at")
    store = Store(events, state_from_summary(summary))
    cut = store_cut(cfg, now)
    store.prune(cut)
    want = (summary.get("definitions") or {}).get("store_fingerprint")
    got = store_fingerprint(store.events.values(), cut)
    if want != got:
        off = sorted(r for r in set(want or {}) | set(got) if (want or {}).get(r) != got.get(r))
        raise StoreMismatch(f"the day files do not rebuild the summary's event store (repos: {', '.join(off) or '-'})")
    apps = []
    for app in cfg["apps"]:
        ctx = app_context(cfg, app, store, now)
        apps.append({"id": app["id"], "range": range_block(cfg, app, ctx, a, b)})
    return {"generated_at": summary["generated_at"], "engine_sha256": engine_sha256(), "start": iso(a),
            "end": iso(b), "apps": apps}
