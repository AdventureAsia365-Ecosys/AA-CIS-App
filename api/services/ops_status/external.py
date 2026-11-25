"""AA-1142 (S292, P2; S293 follow-up) — external synthetic checks: what a customer outside the VPC meets (DNS, TLS
and its certificate, the public health path), next to the internal checks of the 5-minute sampler (S285 review idea
11).

Producers (``acp_shared.ops_external_check.source``, CIS 492), each optional (``cis.ops.external_check_sources``):

- ``worker`` (S293): the CIS worker's monitoring job ``ops_external_check`` (services/jobs/ops_external_check_job.py,
  :mod:`api.services.ops_status.external_probe`) every ``cis.ops.external_check_interval_min`` minutes against
  ``cis.ops.external_check_targets``, from the worker's internet egress, written by :func:`store_run`. The S292 design
  relied on GitHub only, whose schedule ran 0 times on its first day (root crons start up to 10 hours late).
- ``github``: the root repo workflow "External checks" (AA-Ecosys ``.github/workflows/external-checks.yml``,
  ``tools/ops/external_checks.py``) uploads its newest run with the existing Docs sync role to
  ``<docs reader prefix>/ops/external-checks.json`` (format 1: checked_at, results[] {target, app, host, url, mode,
  dns_ok, tls_ok, cert_not_after, http_status, http_ok, latency_ms, error}); the 5-minute status sampler
  (:func:`ingest`, part ``external`` of :func:`api.services.ops_status.sampler.tick`) copies a run it has not seen yet.

Both go through :func:`store_run` with the state it derives (:func:`state_of`): down when DNS, TLS or the request
failed, degraded on an unexpected HTTP status or a certificate within ``cis.ops.status_cert_crit_days``, warn within
``cis.ops.status_cert_warn_days``, else ok. ``ON CONFLICT DO NOTHING``: a run is stored once.

Page. :func:`collect` gives one row per target and source: its newest result (the certificate level recomputed with
today's settings), the strip of the range and whether that row is stale (its newest result older than
``cis.ops.status_stale_factor`` x its source's interval). Each row is judged on its own age, not its source's: a target
removed from ``cis.ops.external_check_targets`` (or from the root repo's list) keeps its last result in the table, and
while the source keeps checking the other targets that old result must read as stopped, not as a live failure. The
feed is stale only when every source in use is stale. :func:`items` turns a target failing in a row that is not stale,
or a certificate at its critical days, into one SEV2 Needs-attention item per target, and a feed that stopped into
one SEV3 item.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from api.services.ops_status import store
from api.services.ops_status.ranges import TimeRange
from shared import ops_external_settings as xs
from shared import ops_status_thresholds as th

logger = structlog.get_logger()

OBJECT_NAME = "ops/external-checks.json"
SUPPORTED_FORMAT = 1
SCHEDULE_MINUTES = 15  # technical-constant: the GitHub workflow's cron (external-checks.yml), its interval
MAX_BYTES = 256 * 1024  # technical-constant: refuse an absurd results file (today about 3 KB)
MAX_RESULTS = 40  # technical-constant: the targets one run may report (today 8)
APPS = xs.APPS
SOURCES = xs.SOURCES
_TARGET = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")

INSERT_SQL = """
    INSERT INTO acp_shared.ops_external_check (source, checked_at, target, app, host, state, dns_ok, tls_ok,
                                               cert_not_after, http_status, latency_ms, error)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    ON CONFLICT DO NOTHING
"""
RANGE_SQL = """
    SELECT source, checked_at, target, app, host, state, dns_ok, tls_ok, cert_not_after, http_status, latency_ms, error
      FROM acp_shared.ops_external_check
     WHERE checked_at >= $1 AND checked_at < $2 AND source = ANY($3::text[])
     ORDER BY checked_at
"""
LATEST_SQL = """
    SELECT DISTINCT ON (source, target) source, checked_at, target, app, host, state, dns_ok, tls_ok, cert_not_after,
           http_status, latency_ms, error
      FROM acp_shared.ops_external_check
     WHERE checked_at >= $1 AND source = ANY($2::text[])
     ORDER BY source, target, checked_at DESC
"""


class FeedError(ValueError):
    """The results file is not one this code reads."""


def _time(v: Any) -> Optional[datetime]:
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(timezone.utc) if t.tzinfo else None


def _int(v: Any, lo: int, hi: int) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = int(v)
    return v if lo <= v <= hi else None


def _bool(v: Any) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def parse(doc: Any) -> tuple[datetime, list[dict]]:
    """(checked_at, results) of one published run, every field checked; FeedError on a file this code does not read.
    A result with a bad target or host is dropped (logged), never stored. Pure."""
    if not isinstance(doc, dict) or doc.get("format") != SUPPORTED_FORMAT:
        raise FeedError("unsupported format")
    checked_at = _time(doc.get("checked_at"))
    if checked_at is None:
        raise FeedError("checked_at missing")
    results = doc.get("results")
    if not isinstance(results, list) or len(results) > MAX_RESULTS:
        raise FeedError("results missing or too many")
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        target, host = r.get("target"), r.get("host")
        if not (isinstance(target, str) and _TARGET.match(target) and isinstance(host, str) and _HOST.match(host)):
            logger.warning("ops_status_external_bad_result", target=str(target)[:60])
            continue
        err = r.get("error")
        out.append({"target": target, "app": r.get("app") if r.get("app") in APPS else None, "host": host,
                    "mode": r.get("mode") if r.get("mode") in ("health", "tls") else "health",
                    "dns_ok": _bool(r.get("dns_ok")), "tls_ok": _bool(r.get("tls_ok")),
                    "cert_not_after": _time(r.get("cert_not_after")),
                    "http_status": _int(r.get("http_status"), 100, 599), "http_ok": _bool(r.get("http_ok")),
                    "latency_ms": _int(r.get("latency_ms"), 0, 600000),
                    "error": err[:300] if isinstance(err, str) and err else None})
    return checked_at, out


def cert_days_left(not_after: Optional[datetime], now: datetime) -> Optional[float]:
    if not_after is None:
        return None
    return round((not_after - now).total_seconds() / 86400.0, 1)


def cert_level(days: Optional[float], limits: dict) -> str:
    """ok | warn | crit | none (no certificate read). Pure."""
    if days is None:
        return "none"
    if days <= limits["crit"]:
        return "crit"
    return "warn" if days <= limits["warn"] else "ok"


def state_of(r: dict, now: datetime, limits: dict) -> str:
    """The state of one result: down (DNS, TLS or the request failed), degraded (an unexpected HTTP status, or the
    certificate at its critical days), warn (certificate at its warn days), ok. Pure.

    A name that resolved but gave no TLS handshake (tls_ok None: the connection timed out or was refused) is down in
    both modes: a TLS-only target has no HTTP status to fall back on, so it must never read as ok."""
    if r.get("dns_ok") is False or r.get("tls_ok") is False:
        return "down"
    if r.get("dns_ok") is True and r.get("tls_ok") is not True:
        return "down"
    if r.get("mode") != "tls" and r.get("http_status") is None:
        return "down"
    level = cert_level(cert_days_left(r.get("cert_not_after"), now), limits)
    if r.get("http_ok") is False or level == "crit":
        return "degraded"
    if r.get("dns_ok") is None and r.get("tls_ok") is None:
        return "unknown"
    return "warn" if level == "warn" else "ok"


def _load_bytes() -> Optional[bytes]:
    from api.services import docs_reader

    key = f"{docs_reader.s3_prefix()}/{OBJECT_NAME}"
    return docs_reader._read_bytes(docs_reader._s3_client(), docs_reader._bucket(), key, MAX_BYTES)


async def store_run(pool: Any, source: str, checked_at: datetime, results: list[dict]) -> int:
    """Store one run of one source (rows of the :func:`parse` shape) with the state of each result; a run already
    stored writes nothing. Returns the rows offered."""
    limits = th.cert_days()
    rows = [(source, checked_at, r["target"], r["app"], r["host"], state_of(r, checked_at, limits), r["dns_ok"],
             r["tls_ok"], r["cert_not_after"], r["http_status"], r["latency_ms"], r["error"]) for r in results]
    if rows:
        async with pool.acquire() as conn:
            await conn.executemany(INSERT_SQL, rows)
    return len(rows)


async def ingest(pool: Any, out: dict, loader: Any = None) -> None:
    """Sampler part ``external``: copy the newest published GitHub run into the table (once per run), unless the
    source github is left out of ``cis.ops.external_check_sources``. Raises on a storage error (the tick names the
    part)."""
    if "github" not in xs.sources():
        out["external"] = "github_off"
        return
    raw = await asyncio.to_thread(loader or _load_bytes)
    if raw is None:
        out["external"] = "not_published"
        return
    try:
        checked_at, results = parse(json.loads(raw))
    except (ValueError, FeedError) as e:
        out["external"] = f"unreadable: {str(e)[:120]}"
        return
    n = await store_run(pool, "github", checked_at, results)
    out["external"] = {"checked_at": checked_at.isoformat(), "results": n}


def interval_minutes(source: str) -> int:
    """A source's own interval: the worker's setting, the GitHub workflow's cron."""
    return xs.interval_min() if source == "worker" else SCHEDULE_MINUTES


def stale_after(factor: float, minutes: int = SCHEDULE_MINUTES) -> timedelta:
    return timedelta(minutes=minutes * factor)


def _src(r: dict) -> str:
    return r.get("source") or "github"


def source_summary(latest: list[dict], intervals: dict[str, int], now: datetime, factor: float) -> list[dict]:
    """Per source in use: its newest run, its interval and whether it is stale (no run, or older than factor x its
    interval). Pure."""
    out = []
    for src, minutes in intervals.items():
        times = [r["checked_at"] for r in latest if _src(r) == src]
        newest = max(times) if times else None
        out.append({"source": src, "newest": newest.isoformat() if newest else None, "interval_minutes": minutes,
                    "stale": newest is None or now - newest > stale_after(factor, minutes)})
    return out


def target_rows(in_range: list[dict], latest: list[dict], rng: TimeRange, now: datetime, limits: dict,
                slo_pct: float, stale_windows: Optional[dict[str, timedelta]] = None) -> list[dict]:
    """One row per target and source: newest result (certificate level with today's limits), the strip of the range,
    whether the row is stale: its own newest result older than its source's window (``stale_windows``, source ->
    factor x interval). Judged per row, so a target no longer checked (removed from the list, skipped as invalid)
    goes grey while its source keeps reporting the others. A source without a window is never stale. Pure."""
    by_key: dict[tuple[str, str], list[dict]] = {}
    for r in in_range:
        by_key.setdefault((_src(r), r["target"]), []).append({"sampled_at": r["checked_at"], "state": r["state"]})
    order = {s: i for i, s in enumerate(SOURCES)}
    rows = []
    for r in sorted(latest, key=lambda x: (x.get("app") or "", x["target"], order.get(_src(x), 9))):
        src = _src(r)
        days = cert_days_left(r.get("cert_not_after"), now)
        window = (stale_windows or {}).get(src)
        rows.append({"target": r["target"], "source": src,
                     "stale": window is not None and now - r["checked_at"] > window,
                     "app": r.get("app") or "shared", "host": r["host"],
                     "state": r["state"], "checked_at": r["checked_at"].isoformat(),
                     "http_status": r.get("http_status"), "latency_ms": r.get("latency_ms"),
                     "dns_ok": r.get("dns_ok"), "tls_ok": r.get("tls_ok"), "error": r.get("error"),
                     "cert_not_after": r["cert_not_after"].isoformat() if r.get("cert_not_after") else None,
                     "cert_days_left": days, "cert_level": cert_level(days, limits),
                     **store.strip(by_key.get((src, r["target"]), []), rng, slo_pct)})
    return rows


async def collect(pool: Any, rng: TimeRange, now: datetime) -> dict:
    """The external section: rows per target and source, each source's newest run, and whether the feed is stale
    (every source in use stale)."""
    from api.services.ops_status import sections

    enabled = list(xs.sources())
    if not enabled:
        return sections.not_measured("external_checks_off")
    since = now - timedelta(days=th.history_days())
    in_range, latest = await asyncio.gather(pool.fetch(RANGE_SQL, rng.start, rng.end, enabled),
                                            pool.fetch(LATEST_SQL, since, enabled))
    latest = [dict(r) for r in latest]
    if not latest:
        return sections.not_measured("no_external_results")
    intervals = {s: interval_minutes(s) for s in enabled}
    factor = th.stale_factor()
    srcs = source_summary(latest, intervals, now, factor)
    stale_by = {s["source"]: s["stale"] for s in srcs}
    newest = max(r["checked_at"] for r in latest)
    refresh = min(intervals.values())
    windows = {s: stale_after(factor, m) for s, m in intervals.items()}
    rows = target_rows([dict(r) for r in in_range], latest, rng, now, th.cert_days(), th.uptime_slo_pct(), windows)
    return sections.ok({"rows": rows, "newest": newest.isoformat(), "schedule_minutes": refresh, "sources": srcs,
                        "stale": all(stale_by.values()), "cert_days": th.cert_days()},
                       refresh * 60, as_of=newest)


def items(data: dict) -> list[tuple[str, str, str, dict]]:
    """(item id, severity, kind, row) for the Needs-attention list, at most one per target and kind: a certificate at
    its critical days (sev2), else a target down or degraded now in a row that is not stale (sev2; the row names
    every source whose fresh result sees it failing; a target no longer checked has only stale rows, so no item), and
    a feed that stopped (sev3, one item, nothing else: every newest result is old). Pure."""
    if data.get("stale"):
        return [("external-feed", "sev3", "stale", {"newest": data.get("newest")})]
    fresh: dict[str, list[dict]] = {}
    for r in data.get("rows") or []:
        if not r.get("stale"):
            fresh.setdefault(r["target"], []).append(r)
    out = []
    for target in sorted(fresh):
        rows = sorted(fresh[target], key=lambda r: r.get("checked_at") or "", reverse=True)
        cert = next((r for r in rows if r.get("cert_level") == "crit"), None)
        failing = [r for r in rows if r.get("state") in ("down", "degraded") and r.get("cert_level") != "crit"]
        if cert:
            out.append((f"external-cert-{target}", "sev2", "cert", cert))
        if failing:
            out.append((f"external-{target}", "sev2", "fails",
                        {**failing[0], "sources": [r.get("source") or "github" for r in failing]}))
    return out


def what_failed(r: dict) -> tuple[str, Optional[int]]:
    """(code, HTTP status) of a failing result: dns, tls, http or request. Pure."""
    if r.get("dns_ok") is False:
        return "dns", None
    if r.get("tls_ok") is False:
        return "tls", None
    if r.get("http_status"):
        return "http", r["http_status"]
    return "request", None


def verdict_items(data: dict) -> list[dict]:
    """The Needs-attention rows of the external checks (``verdict._EXTRA_ITEMS``). Pure."""
    from api.services.ops_status.verdict import PAGES, RUNBOOK_INCIDENT, _item

    out = []
    for iid, sev, kind, r in items(data):
        if kind == "stale":
            out.append(_item(iid, sev, "shared", f"External checks have not reported since {r.get('newest')}",
                             "No source in use (the CIS worker job, the root repo workflow on GitHub Actions) "
                             "reported within its interval",
                             since=r.get("newest"), source="external checks",
                             msg={"key": "external_stale", "since": r.get("newest")},
                             detail_msg={"key": "external_stale_detail"},
                             links=[("Settings", PAGES["settings"])]))
        elif kind == "cert":
            out.append(_item(iid, sev, r["app"], f"Certificate of {r['host']} ends in {r['cert_days_left']:g} days",
                             f"Ends {r.get('cert_not_after')}", since=r.get("checked_at"), source="external checks",
                             msg={"key": "external_cert", "app": r["app"], "host": r["host"],
                                  "days": r["cert_days_left"]},
                             links=[("Incident response", RUNBOOK_INCIDENT), ("Settings", PAGES["settings"])]))
        else:
            code, status = what_failed(r)
            what = {"dns": "DNS", "tls": "TLS", "http": f"HTTP {status}"}.get(code, "the request")
            sources = r.get("sources") or [r.get("source") or "github"]
            out.append(_item(iid, sev, r["app"], f"{r['host']} fails from outside: {what}", r.get("error") or "",
                             since=r.get("checked_at"), source="external checks", detail_source=True,
                             msg={"key": "external_fails", "app": r["app"], "host": r["host"], "what": what,
                                  "code": code, "status": status, "sources": sources},
                             links=[("App health", PAGES["apps"]), ("Incident response", RUNBOOK_INCIDENT)]))
    return out
