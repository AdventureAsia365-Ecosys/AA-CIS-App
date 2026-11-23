"""AA-1170 (S287, compliance readiness page, phase 1) — Platform › Compliance (/admin/platform/compliance): the evidence
the collectors publish, the register of schema compliance (CIS 466) and the Evidenced % of each framework.

Data (CIS 466): compliance.requirement (every requirement of the in-scope frameworks, identifiers and our own labels),
compliance.control + compliance.requirement_mapping (the common controls and what they cover; loaded from the private
register by scripts/compliance/load_register.py, never committed), compliance.evidence (append-only, one row per
published check result) and compliance.score_snapshot (one row per UTC day and framework).

Evidence, phase 1 (tech lead S287 Q7, the Delivery page pattern of AA-1108): the root repo workflow "Compliance
evidence" (AA-Ecosys .github/workflows/compliance-evidence.yml, tools/compliance/collect.py) runs daily on master and
uploads ONE file next to the docs reader bundle with the existing Docs sync role:

  <prefix>/compliance/evidence.json   format 1: source, collector, collector_version (git SHA), run_ref,
                                      generated_at, checks[] = {check_id, status, summary, detail, link}

<prefix> is the docs reader prefix (api/services/docs_reader.py), so the CIS task role reads it under the same
read-only grant and nothing new is provisioned. This module ingests it into compliance.evidence (collected_at =
generated_at; the unique key (source, check_id, collected_at) makes a second read of the same file add nothing). The
file is read at most every docs_reader.CACHE_TTL_S per process when the page loads, or at once (at most every
docs_reader.MIN_REFRESH_S) on the Refresh action (compliance.write). Readiness tracking, not immutable evidence: the
Object Lock store is phase 2 (Q8).

Defence in depth (the root repo is public, and a published file could have been written by something else): only the
known keys pass, every text and detail value is cleaned of a 12-digit run standing alone (an account ID) and of an
``arn:`` string, links must be https://github.com/ pages, detail is at most 8 KiB of JSON (the table's CHECK) and a
row the table would refuse is dropped and logged, never half-written.

Score: api/services/compliance_score.py with the settings in force (area compliance, api/services/compliance_settings
.py). The overview stores today's score per framework (compliance.score_snapshot, upsert) so the trend and the
"scoring settings changed on" date come from history.

ComplianceMissing -> 404 (a control id), the published-file states are reported in the answer (`ingest`), never as an
error of the page: the score is computed from what the table already holds.

AA-1174 (S288, CIS 471). The frameworks come from compliance.framework (kind standard | law, scored, display name):
only the scored ones get a percentage and a snapshot; the law obligations are a checklist
(api/services/compliance_obligations.py). Two more evidence sources are collected by the API itself, ONLY on the
Refresh action (POST, compliance.write; never on a GET, so a read-only "view as" session never writes evidence):
``cis_db`` (api/services/compliance_db.py: the retention runs of the job runner) and ``crm_ops``
(api/services/compliance_crm.py: consent capture and campaign sends to opted-in contacts, counts from the partner CRM
with the staff member's own token; a 401 / 403 / no Cognito session records only the ingest state, never an evidence
row, so a viewer without CRM access cannot turn the check red for everyone). A source is collected at most every
LIVE_MIN_INTERVAL_S, whatever the number of processes and presses, so compliance.evidence grows by a few rows a day.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import structlog

from api.services import compliance_score as cs
from api.services import compliance_settings as keys
from api.services import docs_reader
from shared import settings

logger = structlog.get_logger()

SUPPORTED_FORMAT = 1
OBJECT_NAME = "compliance/evidence.json"
MAX_BUNDLE_BYTES = 1024 * 1024        # technical-constant: refuse an absurd file (a day's bundle is a few kB)
MAX_DETAIL_BYTES = 8192               # technical-constant: compliance.evidence CHECK evidence_detail_check (CIS 466)
MAX_CHECKS = 200                      # technical-constant: refuse a file with more rows than the register could use
TREND_DAYS = 90                       # technical-constant: the trend chart's window (the page, not the score)
EVIDENCE_HISTORY = 20                 # technical-constant: evidence rows per check in the control drawer
# A live source (cis_db, crm_ops) is collected at most every 10 minutes: the table is append-only; the validity windows
# that grade the rows are settings, in hours.
LIVE_MIN_INTERVAL_S = 600   # technical-constant: write-rate bound of an append-only table
LIVE_SOURCES = ("cis_db", "crm_ops")
SCHEDULED_SOURCES = frozenset({"root_daily"})   # the heartbeat applies to these only (a live source has no schedule)
STATUSES = ("pass", "fail", "error", "stale", "manual")
_SOURCE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_CHECK_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
_ARN = re.compile(r"arn:aws[a-z-]*:[^\s\"']*")
_GITHUB = "https://github.com/"
REDACTED = "[redacted]"


class ComplianceMissing(Exception):
    """No such control."""


_cache: dict[str, Any] = {"at": 0.0, "state": None}
_lock = asyncio.Lock()
_live_lock = asyncio.Lock()   # one live collection at a time per process (the interval check reads the table)


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["state"] = None


# ── the published file ──────────────────────────────────────────────────────────────────────────────────────────────


def _clean(value: Any, hits: list[int]) -> Any:
    if isinstance(value, str):
        out, n1 = _ACCOUNT_ID.subn(REDACTED, value)
        out, n2 = _ARN.subn(REDACTED, out)
        hits[0] += n1 + n2
        return out
    if isinstance(value, list):
        return [_clean(v, hits) for v in value]
    if isinstance(value, dict):
        return {str(_clean(str(k), hits)): _clean(v, hits) for k, v in value.items()}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def parse_bundle(raw: bytes, now: datetime) -> tuple[list[dict], list[str]]:
    """The evidence rows of a published file and the reasons rows were dropped. Raises ValueError on a file that is
    not format 1 (nothing of it is ingested)."""
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("format") != SUPPORTED_FORMAT:
        raise ValueError(f"the evidence file is not format {SUPPORTED_FORMAT}")
    source = data.get("source")
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise ValueError("the evidence file has no valid source")
    collected = _parse_time(data.get("generated_at"))
    if collected is None or collected > now + timedelta(minutes=10):
        raise ValueError("the evidence file has no valid generated_at")
    checks = data.get("checks")
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise ValueError("the evidence file has no valid checks list")
    hits = [0]
    collector = _clean(str(data.get("collector") or source), hits)[:120]
    version = data.get("collector_version")
    version = version if isinstance(version, str) and _SHA.fullmatch(version) else None
    run_ref = data.get("run_ref")
    run_ref = _clean(run_ref, hits)[:120] if isinstance(run_ref, str) and run_ref else None
    rows, dropped, seen = [], [], set()
    for c in checks:
        cid = c.get("check_id") if isinstance(c, dict) else None
        if not isinstance(cid, str) or not _CHECK_ID.fullmatch(cid) or cid in seen:
            dropped.append(f"bad or repeated check_id: {str(cid)[:64]!r}")
            continue
        status = c.get("status")
        if status not in STATUSES:
            dropped.append(f"{cid}: unknown status")
            continue
        detail = c.get("detail") if isinstance(c.get("detail"), dict) else {}
        detail = _clean(detail, hits)
        if len(json.dumps(detail).encode()) > MAX_DETAIL_BYTES:
            dropped.append(f"{cid}: detail over {MAX_DETAIL_BYTES} bytes")
            continue
        summary = c.get("summary")
        summary = _clean(summary, hits)[:300] if isinstance(summary, str) and summary else None
        link = c.get("link")
        link = link if isinstance(link, str) and link.startswith(_GITHUB) and len(link) <= 500 else None
        seen.add(cid)
        rows.append({"source": source, "check_id": cid, "status": status, "collected_at": collected,
                     "collector": collector or source, "collector_version": version, "run_ref": run_ref,
                     "summary": summary, "detail": detail, "link": link})
    if hits[0]:
        logger.error("compliance_evidence_redacted", count=hits[0], source=source)
    return rows, dropped


def _read_published(client_factory: Callable[[], Any]) -> Optional[bytes]:
    key = f"{docs_reader.s3_prefix()}/{OBJECT_NAME}"
    return docs_reader._read_bytes(client_factory(), docs_reader._bucket(), key, MAX_BUNDLE_BYTES)


SQL_INSERT_EVIDENCE = """
INSERT INTO compliance.evidence
    (source, check_id, status, collected_at, valid_until, collector, collector_version, run_ref, summary, detail, link)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
ON CONFLICT (source, check_id, collected_at) DO NOTHING
"""


async def ingest(pool: Any, rows: list[dict], validity_hours: Optional[int]) -> int:
    """Insert the rows (one transaction); returns how many were new. valid_until = collected_at + the validity window
    of the source in force at ingest (the score re-applies the current window at read time)."""
    if not rows:
        return 0
    new = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for r in rows:
                until = r["collected_at"] + timedelta(hours=validity_hours) if validity_hours else None
                status = await conn.execute(
                    SQL_INSERT_EVIDENCE, r["source"], r["check_id"], r["status"], r["collected_at"], until,
                    r["collector"], r["collector_version"], r["run_ref"], r["summary"], json.dumps(r["detail"]),
                    r["link"])
                new += status.endswith(" 1")
    return new


async def sync(pool: Any, refresh: bool = False, client_factory: Optional[Callable[[], Any]] = None,
               now: Optional[datetime] = None, authorization: Optional[str] = None, collect_live: bool = False) -> dict:
    """Read the published file (cached per process) and ingest it. Never raises for storage or format: the answer
    says what happened (state ok | missing | unavailable | invalid), the page shows it. ``collect_live`` (the Refresh
    action only) also collects the cis_db and crm_ops sources (``live`` in the answer) on every call, cache hit or not:
    LIVE_MIN_INTERVAL_S bounds that write rate, so a Refresh pressed right after a page load still collects.
    ``authorization`` is the staff member's relay token for the CRM. The cached state never carries ``live``."""
    state = await _sync_published(pool, refresh, client_factory, now)
    if not collect_live:
        return state
    async with _live_lock:
        live = await collect_live_sources(pool, authorization, now or datetime.now(timezone.utc))
    return {**state, "live": live}


async def _sync_published(pool: Any, refresh: bool, client_factory: Optional[Callable[[], Any]],
                          now: Optional[datetime]) -> dict:
    max_age = docs_reader.MIN_REFRESH_S if refresh else docs_reader.CACHE_TTL_S
    if _cache["state"] is not None and time.monotonic() - _cache["at"] < max_age:
        return _cache["state"]
    async with _lock:
        if _cache["state"] is not None and time.monotonic() - _cache["at"] < max_age:
            return _cache["state"]
        now = now or datetime.now(timezone.utc)
        state: dict[str, Any] = {"read_at": now.isoformat(), "object": OBJECT_NAME}
        try:
            raw = await asyncio.to_thread(_read_published, client_factory or docs_reader._s3_client)
        except Exception as err:   # storage
            logger.warning("compliance_evidence_read_failed", error=type(err).__name__)
            raw, state["state"] = None, "unavailable"
        if raw is None and "state" not in state:
            state["state"] = "missing"
        if raw is not None:
            try:
                rows, dropped = parse_bundle(raw, now)
            except ValueError as err:
                state.update(state="invalid", detail=str(err))
            else:
                source = rows[0]["source"] if rows else None
                hours = await _validity_hours(source) if source else None
                state.update(state="ok", rows=len(rows), new=await ingest(pool, rows, hours), dropped=dropped)
        _cache["state"], _cache["at"] = state, time.monotonic()
        return state


SQL_SOURCE_LAST = "SELECT max(collected_at) AS last FROM compliance.evidence WHERE source = $1"


async def _collect_one(pool: Any, source: str, authorization: Optional[str], now: datetime) -> dict:
    from api.services import compliance_crm, compliance_db   # noqa: PLC0415 - optional sources, imported on use

    async with pool.acquire() as conn:
        last = await conn.fetchval(SQL_SOURCE_LAST, source)
    if last is not None and (now - last).total_seconds() < LIVE_MIN_INTERVAL_S:
        return {"state": "recent", "last": last.isoformat()}
    if source == "cis_db":
        rows, info = await compliance_db.collect(pool, now), {}
    else:
        days = int(await settings.aget(keys.CAMPAIGN_CONSENT_WINDOW_DAYS))
        rows, info = await compliance_crm.collect(authorization, days, now)
    if not rows:
        return {"state": "skipped", **info}
    new = await ingest(pool, rows, await _validity_hours(source))
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {"state": "ok", "rows": len(rows), "new": new, "by_status": by_status, **info}


async def collect_live_sources(pool: Any, authorization: Optional[str], now: datetime) -> dict:
    """Collect the API's own sources once (Refresh only); one failing source never stops the other."""
    out: dict[str, dict] = {}
    for source in LIVE_SOURCES:
        try:
            out[source] = await _collect_one(pool, source, authorization, now)
        except Exception as err:   # noqa: BLE001 - reported in the answer, the page still loads
            logger.warning("compliance_live_collect_failed", source=source, error=type(err).__name__)
            out[source] = {"state": "error", "detail": type(err).__name__}
    return out


# ── settings ────────────────────────────────────────────────────────────────────────────────────────────────────────


async def _validity_hours(source: str) -> Optional[int]:
    key = keys.VALIDITY_HOURS.get(source)
    return int(await settings.aget(key)) if key else None


async def score_settings() -> cs.ScoreSettings:
    validity = {src: int(await settings.aget(key)) for src, key in keys.VALIDITY_HOURS.items()}
    limits = {cid: (fact, float(await settings.aget(key)), kind) for cid, (fact, key, kind) in keys.FACT_LIMITS.items()}
    return cs.ScoreSettings(validity_hours=validity, heartbeat_hours=int(await settings.aget(keys.HEARTBEAT_HOURS)),
                            partial_weight=float(await settings.aget(keys.PARTIAL_WEIGHT)), fact_limits=limits,
                            heartbeat_sources=SCHEDULED_SOURCES)


async def reference_scores(scored: Optional[list[str]] = None) -> dict:
    value = await settings.aget(keys.REFERENCE_SCORES)
    allowed = set(scored if scored is not None else cs.DEFAULT_SCORED)
    return {k: float(v) for k, v in (value or {}).items() if k in allowed and isinstance(v, (int, float))}


# ── the register ────────────────────────────────────────────────────────────────────────────────────────────────────

SQL_REQUIREMENTS = ("SELECT framework, ref, section, title, applicable, sort_order, counsel_review, applies_as, "
                    "source_url, doc_url FROM compliance.requirement ORDER BY framework, sort_order")
SQL_FRAMEWORKS = ("SELECT framework, kind, title, jurisdiction, citation, source_url, in_force_on, applies_from, "
                  "scored, sort_order, active FROM compliance.framework ORDER BY sort_order, framework")
SQL_CONTROLS = ("SELECT control_id, title, intent, domain, owner_role, frequency, test_type, checks, scope, active, "
                "updated_at FROM compliance.control ORDER BY control_id")
SQL_MAPPINGS = "SELECT control_id, framework, ref, coverage, note FROM compliance.requirement_mapping"
SQL_LATEST = """
SELECT DISTINCT ON (check_id) check_id, source, status, collected_at, valid_until, collector, collector_version,
       run_ref, summary, detail, link
  FROM compliance.evidence
 ORDER BY check_id, collected_at DESC, evidence_id DESC
"""
SQL_LAST_SEEN = "SELECT source, max(collected_at) AS last_seen, count(*) AS n FROM compliance.evidence GROUP BY source"
SQL_UPSERT_SNAPSHOT = """
INSERT INTO compliance.score_snapshot
    (snapshot_date, framework, n_requirements, n_met, n_partial, n_gap, n_stale, n_unmapped, strict_pct,
     weighted_pct, reference_pct, settings, computed_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, now())
ON CONFLICT (snapshot_date, framework) DO UPDATE
   SET n_requirements = EXCLUDED.n_requirements, n_met = EXCLUDED.n_met, n_partial = EXCLUDED.n_partial,
       n_gap = EXCLUDED.n_gap, n_stale = EXCLUDED.n_stale, n_unmapped = EXCLUDED.n_unmapped,
       strict_pct = EXCLUDED.strict_pct, weighted_pct = EXCLUDED.weighted_pct,
       reference_pct = EXCLUDED.reference_pct, settings = EXCLUDED.settings, computed_at = now()
"""
SQL_TREND = """
SELECT snapshot_date, framework, strict_pct, weighted_pct, reference_pct, n_stale, settings
  FROM compliance.score_snapshot
 WHERE snapshot_date >= $1
 ORDER BY snapshot_date, framework
"""
SQL_HISTORY = """
SELECT check_id, source, status, collected_at, valid_until, collector, collector_version, run_ref, summary, detail, link
  FROM (SELECT e.*, row_number() OVER (PARTITION BY check_id ORDER BY collected_at DESC, evidence_id DESC) AS rn
          FROM compliance.evidence e WHERE check_id = ANY($1::text[])) h
 WHERE rn <= $2
 ORDER BY check_id, collected_at DESC
"""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _row(r: Any) -> dict:
    out = dict(r)
    for k in ("checks", "detail", "settings"):
        if k in out:
            out[k] = _json(out[k])
    return out


async def _state(conn: Any) -> dict:
    return {
        "frameworks": [_row(r) for r in await conn.fetch(SQL_FRAMEWORKS)],
        "requirements": [_row(r) for r in await conn.fetch(SQL_REQUIREMENTS)],
        "controls": [_row(r) for r in await conn.fetch(SQL_CONTROLS)],
        "mappings": [_row(r) for r in await conn.fetch(SQL_MAPPINGS)],
        "latest": {r["check_id"]: _row(r) for r in await conn.fetch(SQL_LATEST)},
        "sources": {r["source"]: {"last_seen": r["last_seen"], "n": r["n"]} for r in await conn.fetch(SQL_LAST_SEEN)},
    }


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def _check_json(st: cs.CheckState) -> dict:
    return {"check_id": st.check_id, "state": st.state, "reason": st.reason, "source": st.source,
            "collected_at": _iso(st.collected_at), "expires_at": _iso(st.expires_at)}


def scored_frameworks(data: dict) -> list[str]:
    """The frameworks that get a percentage, in display order (compliance.framework scored and active)."""
    return [f["framework"] for f in data["frameworks"] if f["scored"] and f["active"]]


def framework_info(data: dict) -> list[dict]:
    """Every active framework with its display name and kind, for the page (names come from the table, not the UI)."""
    return [{"framework": f["framework"], "title": f["title"], "kind": f["kind"], "scored": f["scored"],
             "jurisdiction": f.get("jurisdiction")} for f in data["frameworks"] if f["active"]]


async def _evaluate(conn: Any, now: datetime) -> tuple[dict, dict, cs.ScoreSettings]:
    data = await _state(conn)
    s = await score_settings()
    last_seen = {src: v["last_seen"] for src, v in data["sources"].items()}
    result = cs.evaluate(data["requirements"], data["controls"], data["mappings"], data["latest"], last_seen, s, now,
                         scored=scored_frameworks(data))
    return data, result, s


def _heartbeat(data: dict, s: cs.ScoreSettings, now: datetime) -> list[dict]:
    out = []
    scheduled = s.heartbeat_sources if s.heartbeat_sources is not None else frozenset(data["sources"])
    for src in sorted(set(data["sources"]) | set(s.validity_hours), key=lambda x: (x not in scheduled, x)):
        seen = data["sources"].get(src, {}).get("last_seen")
        age = round((now - seen).total_seconds() / 3600, 1) if seen else None
        limit = s.heartbeat_hours if src in scheduled else s.validity_hours.get(src, s.heartbeat_hours)
        out.append({"source": src, "last_seen": _iso(seen), "age_hours": age, "heartbeat_hours": limit,
                    "scheduled": src in scheduled, "ok": seen is not None and age is not None and age <= limit,
                    "evidence_rows": data["sources"].get(src, {}).get("n", 0)})
    return out


async def overview(pool: Any, now: Optional[datetime] = None, ingest_state: Optional[dict] = None,
                   write: bool = True) -> dict:
    """The Overview tab: Evidenced % per scored framework (strict, weighted, reference), today's snapshot stored, the
    trend, stale / failing evidence, the collectors' heartbeat and the settings in force. ``write`` False (a read-only
    "view as" session) skips the snapshot upsert."""
    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        data, result, s = await _evaluate(conn, now)
        scored = scored_frameworks(data)
        refs = await reference_scores(scored)
        today = now.date()
        if write:
            async with conn.transaction():
                for fw, sc in result["frameworks"].items():
                    await conn.execute(SQL_UPSERT_SNAPSHOT, today, fw, sc["n_requirements"], sc["n_met"],
                                       sc["n_partial"], sc["n_gap"], sc["n_stale"], sc["n_unmapped"], sc["strict_pct"],
                                       sc["weighted_pct"], refs.get(fw), json.dumps(s.as_json(), sort_keys=True))
        trend_rows = [_row(r) for r in await conn.fetch(SQL_TREND, today - timedelta(days=TREND_DAYS))
                      if r["framework"] in scored]
    titles = {}
    for c in data["controls"]:
        for cid in cs.required_checks(c):
            titles.setdefault(cid, []).append(c["control_id"])
    attention = [dict(_check_json(st), controls=sorted(titles.get(cid, [])))
                 for cid, st in result["checks"].items() if st.state in ("stale", "fail", "error")]
    states = list(result["controls"].values())
    first = [r for r in trend_rows if scored and r["framework"] == scored[0]]
    kinds = {f["framework"]: f for f in framework_info(data)}
    laws = [f for f in data["frameworks"] if f["active"] and f["kind"] == "law"]
    law_ids = {f["framework"] for f in laws}
    obligations = [r for r in data["requirements"] if r["applicable"] and r["framework"] in law_ids]
    return {
        "environment": "dev",
        "generated_at": now.isoformat(),
        "frameworks": [dict(framework=fw, reference_pct=refs.get(fw), title=kinds.get(fw, {}).get("title", fw),
                            kind=kinds.get(fw, {}).get("kind", "standard"), **result["frameworks"][fw])
                       for fw in scored],
        "framework_info": list(kinds.values()),
        "controls": {st: states.count(st) for st in cs.CONTROL_STATES} | {"total": len(states)},
        "checks": {st: sum(1 for c in result["checks"].values() if c.state == st) for st in cs.CHECK_STATES},
        "attention": sorted(attention, key=lambda a: (a["state"], a["check_id"])),
        "heartbeat": _heartbeat(data, s, now),
        "trend": [{"date": _iso(r["snapshot_date"]), "framework": r["framework"],
                   "strict_pct": float(r["strict_pct"]), "weighted_pct": float(r["weighted_pct"]),
                   "reference_pct": float(r["reference_pct"]) if r["reference_pct"] is not None else None}
                  for r in trend_rows],
        "settings": s.as_json(),
        "settings_changed_on": cs.settings_changed_on(first),
        "register": {"requirements": len(data["requirements"]), "controls": len(data["controls"]),
                     "mappings": len(data["mappings"]), "laws": len(laws), "obligations": len(obligations)},
        "ingest": ingest_state,
    }


def _mapped(data: dict) -> dict[str, list[dict]]:
    by_ctl: dict[str, list[dict]] = {}
    for m in data["mappings"]:
        by_ctl.setdefault(m["control_id"], []).append(m)
    return by_ctl


async def controls(pool: Any, now: Optional[datetime] = None) -> dict:
    """The Controls tab: every control with its state, its checks' states and how many requirements it covers per
    framework (about 40 rows at the start, one answer; the table filters on the client)."""
    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        data, result, _ = await _evaluate(conn, now)
    by_ctl = _mapped(data)
    rows = []
    for c in data["controls"]:
        frameworks: dict[str, int] = {}
        for m in by_ctl.get(c["control_id"], []):
            frameworks[m["framework"]] = frameworks.get(m["framework"], 0) + 1
        checks = [_check_json(result["checks"][i]) for i in cs.required_checks(c) if i in result["checks"]]
        last = max((ck["collected_at"] for ck in checks if ck["collected_at"]), default=None)
        rows.append({"control_id": c["control_id"], "title": c["title"], "domain": c["domain"],
                     "owner_role": c["owner_role"], "frequency": c["frequency"], "test_type": c["test_type"],
                     "scope": c["scope"], "active": c["active"],
                     "state": result["controls"].get(c["control_id"], "inactive"),
                     "frameworks": frameworks, "checks": checks, "last_evidence_at": last})
    info = framework_info(data)
    return {"generated_at": now.isoformat(), "controls": rows, "frameworks": [f["framework"] for f in info],
            "framework_info": info}


async def control_detail(pool: Any, control_id: str, now: Optional[datetime] = None) -> dict:
    """The control drawer: the control, the requirements it covers (with each requirement's state), its checks and
    their evidence history (newest first)."""
    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        data, result, _ = await _evaluate(conn, now)
        control = next((c for c in data["controls"] if c["control_id"] == control_id), None)
        if control is None:
            raise ComplianceMissing(f"No control {control_id}")
        ids = [c.get("check_id") if isinstance(c, dict) else c for c in control.get("checks") or []]
        history = [_row(r) for r in await conn.fetch(SQL_HISTORY, [i for i in ids if isinstance(i, str)],
                                                     EVIDENCE_HISTORY)]
    reqs = {(r["framework"], r["ref"]): r for r in data["requirements"]}
    covers = []
    for m in sorted(_mapped(data).get(control_id, []), key=lambda m: (m["framework"], m["ref"])):
        r = reqs.get((m["framework"], m["ref"]), {})
        covers.append({"framework": m["framework"], "ref": m["ref"], "coverage": m["coverage"], "note": m.get("note"),
                       "title": r.get("title"), "section": r.get("section"),
                       "state": result["requirements"].get((m["framework"], m["ref"]))})
    checks = []
    for c in control.get("checks") or []:
        cid = c.get("check_id") if isinstance(c, dict) else c
        if not isinstance(cid, str):
            continue
        st = result["checks"].get(cid)
        checks.append(dict(_check_json(st) if st else {"check_id": cid, "state": "missing"},
                           required=bool(c.get("required", True)) if isinstance(c, dict) else True,
                           history=[{k: _iso(v) for k, v in h.items() if k != "check_id"}
                                    for h in history if h["check_id"] == cid]))
    out = {k: _iso(v) for k, v in control.items()}
    out.update(state=result["controls"].get(control_id, "inactive"), requirements=covers, checks=checks)
    return out
