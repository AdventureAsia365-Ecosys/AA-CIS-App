"""AA-1142 (S292, P2) — external synthetic checks: what a customer outside the VPC meets (DNS, TLS and its certificate,
the public health path), next to the internal checks of the 5-minute sampler (S285 review idea 11).

Producer. The root repo workflow "External checks" (AA-Ecosys ``.github/workflows/external-checks.yml``,
``tools/ops/external_checks.py``) runs every 15 minutes on GitHub Actions (public repo: 0 USD; no Route 53 health check,
no new AWS service) and uploads its newest run, next to the delivery summary, with the existing Docs sync role:

  <docs reader prefix>/ops/external-checks.json   format 1: checked_at, results[] {target, app, host, url, mode,
                                                  dns_ok, tls_ok, cert_not_after, http_status, http_ok, latency_ms,
                                                  error}

Copy. The 5-minute status sampler (:func:`ingest`, part ``external`` of :func:`api.services.ops_status.sampler.tick`)
reads that object (one S3 GET, the CIS task role's docs reader grant) and inserts a run it has not seen yet into
``acp_shared.ops_external_check`` (CIS 485; ``ON CONFLICT (target, checked_at) DO NOTHING``), with the state it derives
(:func:`state_of`): down when DNS, TLS or the request failed, degraded on an unexpected HTTP status or a certificate
within ``cis.ops.status_cert_crit_days``, warn within ``cis.ops.status_cert_warn_days``, else ok.

Page. :func:`collect` gives one row per target: its newest result (the certificate level recomputed with today's
settings), the strip of the range (the same cells as the internal uptime strip, :func:`store.strip`) and the age of
the feed, stale after ``cis.ops.status_stale_factor`` x the 15-minute schedule. :func:`items` turns a failing target or
a certificate at its critical days into a SEV2 Needs-attention item and a feed that stopped into a SEV3 one.
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
from shared import ops_status_thresholds as th

logger = structlog.get_logger()

OBJECT_NAME = "ops/external-checks.json"
SUPPORTED_FORMAT = 1
SCHEDULE_MINUTES = 15  # technical-constant: the workflow's cron (external-checks.yml), the feed's refresh time
MAX_BYTES = 256 * 1024  # technical-constant: refuse an absurd results file (today about 3 KB)
MAX_RESULTS = 40  # technical-constant: the targets one run may report (today 8)
APPS = ("cis", "tripplanner", "booking", "crm", "shared")
_TARGET = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")

INSERT_SQL = """
    INSERT INTO acp_shared.ops_external_check (checked_at, target, app, host, state, dns_ok, tls_ok, cert_not_after,
                                               http_status, latency_ms, error)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    ON CONFLICT (target, checked_at) DO NOTHING
"""
RANGE_SQL = """
    SELECT checked_at, target, app, host, state, dns_ok, tls_ok, cert_not_after, http_status, latency_ms, error
      FROM acp_shared.ops_external_check
     WHERE checked_at >= $1 AND checked_at < $2
     ORDER BY checked_at
"""
LATEST_SQL = """
    SELECT DISTINCT ON (target) checked_at, target, app, host, state, dns_ok, tls_ok, cert_not_after, http_status,
           latency_ms, error
      FROM acp_shared.ops_external_check
     WHERE checked_at >= $1
     ORDER BY target, checked_at DESC
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


async def ingest(pool: Any, out: dict, loader: Any = None) -> None:
    """Sampler part ``external``: copy the newest published run into the table (once per run). Raises on a storage
    error (the tick names the part)."""
    raw = await asyncio.to_thread(loader or _load_bytes)
    if raw is None:
        out["external"] = "not_published"
        return
    try:
        checked_at, results = parse(json.loads(raw))
    except (ValueError, FeedError) as e:
        out["external"] = f"unreadable: {str(e)[:120]}"
        return
    limits = th.cert_days()
    rows = [(checked_at, r["target"], r["app"], r["host"], state_of(r, checked_at, limits), r["dns_ok"], r["tls_ok"],
             r["cert_not_after"], r["http_status"], r["latency_ms"], r["error"]) for r in results]
    async with pool.acquire() as conn:
        await conn.executemany(INSERT_SQL, rows)
    out["external"] = {"checked_at": checked_at.isoformat(), "results": len(rows)}


def stale_after(factor: float) -> timedelta:
    return timedelta(minutes=SCHEDULE_MINUTES * factor)


def target_rows(in_range: list[dict], latest: list[dict], rng: TimeRange, now: datetime, limits: dict,
                slo_pct: float) -> list[dict]:
    """One row per target: newest result (certificate level with today's limits), the strip of the range. Pure."""
    by_target: dict[str, list[dict]] = {}
    for r in in_range:
        by_target.setdefault(r["target"], []).append({"sampled_at": r["checked_at"], "state": r["state"]})
    rows = []
    for r in sorted(latest, key=lambda x: (x.get("app") or "", x["target"])):
        days = cert_days_left(r.get("cert_not_after"), now)
        rows.append({"target": r["target"], "app": r.get("app") or "shared", "host": r["host"],
                     "state": r["state"], "checked_at": r["checked_at"].isoformat(),
                     "http_status": r.get("http_status"), "latency_ms": r.get("latency_ms"),
                     "dns_ok": r.get("dns_ok"), "tls_ok": r.get("tls_ok"), "error": r.get("error"),
                     "cert_not_after": r["cert_not_after"].isoformat() if r.get("cert_not_after") else None,
                     "cert_days_left": days, "cert_level": cert_level(days, limits),
                     **store.strip(by_target.get(r["target"], []), rng, slo_pct)})
    return rows


async def collect(pool: Any, rng: TimeRange, now: datetime) -> dict:
    """The external section: rows per target, the feed's newest run and whether it is stale."""
    from api.services.ops_status import sections

    since = now - timedelta(days=th.history_days())
    in_range, latest = await asyncio.gather(pool.fetch(RANGE_SQL, rng.start, rng.end), pool.fetch(LATEST_SQL, since))
    latest = [dict(r) for r in latest]
    if not latest:
        return sections.not_measured("no_external_results")
    newest = max(r["checked_at"] for r in latest)
    factor = th.stale_factor()
    rows = target_rows([dict(r) for r in in_range], latest, rng, now, th.cert_days(), th.uptime_slo_pct())
    return sections.ok({"rows": rows, "newest": newest.isoformat(), "schedule_minutes": SCHEDULE_MINUTES,
                        "stale": now - newest > stale_after(factor), "cert_days": th.cert_days()},
                       SCHEDULE_MINUTES * 60, as_of=newest)


def items(data: dict) -> list[tuple[str, str, str, dict]]:
    """(item id, severity, kind, row) for the Needs-attention list: a target down or degraded now (sev2), a certificate
    at its critical days (sev2), a feed that stopped (sev3, one item). A stale feed raises no target item (the newest
    result is old). Pure."""
    if data.get("stale"):
        return [("external-feed", "sev3", "stale", {"newest": data.get("newest")})]
    out = []
    for r in data.get("rows") or []:
        if r.get("cert_level") == "crit":
            out.append((f"external-cert-{r['target']}", "sev2", "cert", r))
        if r.get("state") in ("down", "degraded") and r.get("cert_level") != "crit":
            out.append((f"external-{r['target']}", "sev2", "fails", r))
    return out


def verdict_items(data: dict) -> list[dict]:
    """The Needs-attention rows of the external checks (``verdict._EXTRA_ITEMS``). Pure."""
    from api.services.ops_status.verdict import PAGES, RUNBOOK_INCIDENT, _item

    out = []
    for iid, sev, kind, r in items(data):
        if kind == "stale":
            out.append(_item(iid, sev, "shared", f"External checks have not reported since {r.get('newest')}",
                             "The root repo workflow External checks runs every 15 minutes on GitHub Actions",
                             since=r.get("newest"), source="external checks",
                             msg={"key": "external_stale", "since": r.get("newest")},
                             links=[("Settings", PAGES["settings"])]))
        elif kind == "cert":
            out.append(_item(iid, sev, r["app"], f"Certificate of {r['host']} ends in {r['cert_days_left']:g} days",
                             f"Ends {r.get('cert_not_after')}", since=r.get("checked_at"), source="external checks",
                             msg={"key": "external_cert", "app": r["app"], "host": r["host"],
                                  "days": r["cert_days_left"]},
                             links=[("Incident response", RUNBOOK_INCIDENT), ("Settings", PAGES["settings"])]))
        else:
            what = ("DNS" if r.get("dns_ok") is False else "TLS" if r.get("tls_ok") is False
                    else f"HTTP {r['http_status']}" if r.get("http_status") else "the request")
            out.append(_item(iid, sev, r["app"], f"{r['host']} fails from outside: {what}", r.get("error") or "",
                             since=r.get("checked_at"), source="external checks", detail_source=True,
                             msg={"key": "external_fails", "app": r["app"], "host": r["host"], "what": what},
                             links=[("App health", PAGES["apps"]), ("Incident response", RUNBOOK_INCIDENT)]))
    return out
