"""AA-1170 (S287, compliance readiness page, phase 1) — the Evidenced % of each framework; pure (no DB, no I/O).

Tech lead S287 Q3: the headline number is **Evidenced %**: only fresh, passing evidence counts. Two numbers per
framework, never mixed: strict (met only) and weighted (met + partial x the partial weight, S286: 0.5); the S286 hand
scores are a reference line beside them (display only). Root advisory docs/audits/2026-11-21-S287-compliance-tracking/
README.md section 4 C.3.

Every value that moves the score is a setting (area compliance, CIS 466, api/services/compliance_settings.py), read by
the caller and handed in as :class:`ScoreSettings`; nothing here is a policy literal.

States, from the evidence up:

check        the newest evidence row of a check_id (any source):
               missing   no row
               error     its collector is silent past the heartbeat window, the row says error, or a fact the score
                         reads is absent or unreadable
               stale     older than the validity window of its source (the current setting, so shortening the window
                         in Settings lowers the score at once), or past the row's own valid_until; a row the collector
                         marked stale
               manual    a manual result waiting for review (not met in phase 1)
               pass/fail the collector's result; for the checks of FACT_LIMITS the result is recomputed here from the
                         published fact against the setting (a drill date older than the setting fails)
control      no required check -> no_evidence; every required check pass -> met; some pass -> partial; none pass and
             one stale -> stale; else gap
requirement  no mapping to an active control -> unmapped; a full-coverage control met -> met; a partial-coverage
             control met or any control partial -> partial; none of those and one stale -> stale; else gap
framework    over the applicable requirements: strict = met / n, weighted = (met + w x partial) / n, in percent with two
             decimals; n = 0 -> 0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Optional

CHECK_STATES = ("pass", "fail", "error", "stale", "manual", "missing")
CONTROL_STATES = ("met", "partial", "stale", "gap", "no_evidence")
REQUIREMENT_STATES = ("met", "partial", "stale", "gap", "unmapped")
FRAMEWORKS = ("iso27001:2022", "soc2:2017", "iso42001:2023", "owasp-top10:2021", "owasp-llm:2025", "eu-ai-act:2024")


@dataclass(frozen=True)
class ScoreSettings:
    """The settings in force (cis.compliance.*), read by the caller."""
    validity_hours: Mapping[str, int]                  # evidence source -> hours a result counts
    heartbeat_hours: int                               # a collector silent longer turns its checks to error
    partial_weight: float                              # weight of partial coverage in the weighted score
    # check_id -> (fact published in detail, limit, kind): kind "age_days" (an ISO date at most `limit` days old) or
    # "max" (a count at most `limit`)
    fact_limits: Mapping[str, tuple[str, float, str]] = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"validity_hours": dict(self.validity_hours), "heartbeat_hours": self.heartbeat_hours,
                "partial_weight": self.partial_weight,
                "fact_limits": {k: {"fact": f, "limit": lim, "kind": kind}
                                for k, (f, lim, kind) in sorted(self.fact_limits.items())}}


@dataclass(frozen=True)
class CheckState:
    check_id: str
    state: str
    reason: str
    source: Optional[str] = None
    collected_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


def _fact_result(detail: Any, fact: str, limit: float, kind: str, now: datetime) -> tuple[str, str]:
    value = detail.get(fact) if isinstance(detail, dict) else None
    if value is None:
        return "error", f"the collector did not publish '{fact}'"
    if kind == "age_days":
        try:
            day = date.fromisoformat(str(value)[:10])
        except ValueError:
            return "error", f"'{fact}' is not a date"
        age = (now.date() - day).days
        if age <= limit:
            return "pass", f"{age} days old (limit {limit:g})"
        return "fail", f"{age} days old, over the limit of {limit:g}"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "error", f"'{fact}' is not a number"
    if n <= limit:
        return "pass", f"{n:g} (limit {limit:g})"
    return "fail", f"{n:g}, over the limit of {limit:g}"


def check_state(check_id: str, row: Optional[Mapping[str, Any]], last_seen: Mapping[str, datetime],
                s: ScoreSettings, now: datetime) -> CheckState:
    """The state of one check from its newest evidence row (``row`` None: no evidence)."""
    if row is None:
        return CheckState(check_id, "missing", "no evidence yet")
    source = row["source"]
    collected = row["collected_at"]
    seen = last_seen.get(source, collected)
    if now - seen > timedelta(hours=s.heartbeat_hours):
        return CheckState(check_id, "error", f"collector {source} silent for more than {s.heartbeat_hours} h",
                          source, collected)
    hours = s.validity_hours.get(source)
    candidates = [row.get("valid_until")] + ([collected + timedelta(hours=hours)] if hours else [])
    ends = [c for c in candidates if c is not None]
    if not ends:
        return CheckState(check_id, "error", f"no validity window for source {source}", source, collected)
    expires = min(ends)
    base = dict(check_id=check_id, source=source, collected_at=collected, expires_at=expires)
    if now > expires:
        return CheckState(state="stale", reason="the evidence is past its validity window", **base)
    status = row["status"]
    if status in ("error", "stale", "manual", "fail"):
        return CheckState(state=status, reason=row.get("summary") or status, **base)
    limit = s.fact_limits.get(check_id)
    if limit is not None:
        fact, lim, kind = limit
        state, reason = _fact_result(row.get("detail"), fact, lim, kind, now)
        return CheckState(state=state, reason=reason, **base)
    return CheckState(state="pass", reason=row.get("summary") or "pass", **base)


def required_checks(control: Mapping[str, Any]) -> list[str]:
    out = []
    for c in control.get("checks") or []:
        if isinstance(c, dict) and isinstance(c.get("check_id"), str) and c.get("required", True):
            out.append(c["check_id"])
        elif isinstance(c, str):
            out.append(c)
    return out


def control_state(control: Mapping[str, Any], checks: Mapping[str, CheckState]) -> str:
    ids = required_checks(control)
    if not ids:
        return "no_evidence"
    states = [checks[i].state if i in checks else "missing" for i in ids]
    if all(st == "pass" for st in states):
        return "met"
    if any(st == "pass" for st in states):
        return "partial"
    if any(st == "stale" for st in states):
        return "stale"
    return "gap"


def requirement_state(mappings: Iterable[Mapping[str, Any]], controls: Mapping[str, str]) -> str:
    """``mappings`` of one requirement; ``controls`` = control_id -> state of the ACTIVE controls."""
    hits = [(m["coverage"], controls[m["control_id"]]) for m in mappings if m["control_id"] in controls]
    if not hits:
        return "unmapped"
    if any(cov == "full" and st == "met" for cov, st in hits):
        return "met"
    if any(st in ("met", "partial") for _, st in hits):
        return "partial"
    if any(st == "stale" for _, st in hits):
        return "stale"
    return "gap"


def framework_score(states: Iterable[str], partial_weight: float) -> dict:
    counts = {st: 0 for st in REQUIREMENT_STATES}
    for st in states:
        counts[st] += 1
    n = sum(counts.values())
    strict = round(100.0 * counts["met"] / n, 2) if n else 0.0
    weighted = round(100.0 * (counts["met"] + partial_weight * counts["partial"]) / n, 2) if n else 0.0
    return {"n_requirements": n, "n_met": counts["met"], "n_partial": counts["partial"], "n_gap": counts["gap"],
            "n_stale": counts["stale"], "n_unmapped": counts["unmapped"], "strict_pct": strict,
            "weighted_pct": weighted}


def evaluate(requirements: list[Mapping[str, Any]], controls: list[Mapping[str, Any]],
             mappings: list[Mapping[str, Any]], latest: Mapping[str, Mapping[str, Any]],
             last_seen: Mapping[str, datetime], s: ScoreSettings, now: datetime) -> dict:
    """Every state at once: ``checks`` (check_id -> CheckState), ``controls`` (control_id -> state of the active
    controls), ``requirements`` ((framework, ref) -> state of the applicable ones) and ``frameworks`` (framework ->
    score). ``latest`` = check_id -> newest evidence row; ``last_seen`` = source -> newest collected_at."""
    active = [c for c in controls if c.get("active", True)]
    wanted = {i for c in active for i in required_checks(c)} | set(latest)
    checks = {i: check_state(i, latest.get(i), last_seen, s, now) for i in sorted(wanted)}
    ctl = {c["control_id"]: control_state(c, checks) for c in active}
    by_req: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for m in mappings:
        by_req.setdefault((m["framework"], m["ref"]), []).append(m)
    reqs = {(r["framework"], r["ref"]): requirement_state(by_req.get((r["framework"], r["ref"]), []), ctl)
            for r in requirements if r.get("applicable", True)}
    frameworks = {}
    for fw in FRAMEWORKS:
        frameworks[fw] = framework_score((st for (f, _), st in reqs.items() if f == fw), s.partial_weight)
    return {"checks": checks, "controls": ctl, "requirements": reqs, "frameworks": frameworks}


def settings_changed_on(snapshots: list[Mapping[str, Any]]) -> Optional[str]:
    """The newest snapshot date whose settings differ from the snapshot before it (snapshots of one framework, any
    order); None when they never changed."""
    rows = sorted(snapshots, key=lambda r: r["snapshot_date"])
    changed = None
    for prev, cur in zip(rows, rows[1:]):
        if prev.get("settings") != cur.get("settings"):
            changed = cur["snapshot_date"]
    return changed.isoformat() if isinstance(changed, date) else changed
