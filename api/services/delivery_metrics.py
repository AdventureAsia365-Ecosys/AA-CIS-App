"""AA-1108 — the CI/CD and DORA numbers per app behind the admin page Platform › Delivery (/admin/platform/delivery)
and the reports DEV-01 (monthly delivery and DORA) and DEV-03 (weekly engineering flow).

The root repo workflow "Delivery metrics" (AA-Ecosys .github/workflows/delivery-metrics.yml, tools/delivery/
delivery_metrics.py) reads the six public repos' GitHub data every hour (workflow runs, jobs, merged pull requests,
GitHub Deployments of the environment dev), keeps the normalised raw events and uploads one summary next to the docs
reader bundle:

  <prefix>/delivery/summary.json     format 1: generated_at, environment, source, definitions, collection, apps[]

<prefix> is the docs reader prefix (DOCS_READER_S3_PREFIX, default "docs-reader"; api/services/docs_reader.py), so the
CIS task role reads it under the same read-only grant and nothing new is provisioned. Read only: no table, no write
path, no GitHub token here. The summary is cached in process like the docs manifest (docs_reader.CACHE_TTL_S;
?refresh=1 re-reads storage at most every docs_reader.MIN_REFRESH_S) and refused above docs_reader.MAX_MANIFEST_BYTES.

Colours: every metric is graded green / amber / red with the settings of area "delivery" (CIS 433,
services/reports/delivery_settings.py; tech lead S282: the DORA "high" band to start, editable on Platform ›
Settings), and "few" when its sample size n is under cis.delivery.min_sample. The definitions of every number are in
the root guide docs/guides/delivery-metrics.md.

Defence in depth: only the known top-level keys pass, apps need a string id, and any 12-digit run left in a string is
masked again (the root repos are public; a bundle carrying one was written by something else).

DeliveryMissing -> 404 (nothing published yet), DeliveryUnavailable -> 503 (storage or format).
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import structlog

from api.services import docs_reader
from services.reports import delivery_settings as ds
from shared import settings

logger = structlog.get_logger()

SUPPORTED_FORMAT = 1
OBJECT_NAME = "delivery/summary.json"
_TOP_KEYS = ("generated_at", "environment", "definitions", "collection")
METRICS = ("deployment_frequency", "lead_time", "change_failure_rate", "recovery_time", "rework_rate", "ci_p95",
           "main")
GREEN, AMBER, RED, FEW, NONE = "green", "amber", "red", "few", "none"


class DeliveryMissing(Exception):
    """No delivery summary published yet (root repo workflow Delivery metrics)."""


class DeliveryUnavailable(Exception):
    """Storage failed or the summary is not in the expected shape."""


_cache: dict[str, Any] = {"value": None, "at": 0.0}
_lock = asyncio.Lock()


def reset_cache() -> None:
    _cache["value"] = None
    _cache["at"] = 0.0


def _mask(value: Any, hits: list[int]) -> Any:
    if isinstance(value, str):
        masked, n = docs_reader._ACCOUNT_ID.subn(docs_reader.ACCOUNT_ID_MARKER, value)
        hits[0] += n
        return masked
    if isinstance(value, list):
        return [_mask(v, hits) for v in value]
    if isinstance(value, dict):
        return {str(k): _mask(v, hits) for k, v in value.items()}
    return value


def _build(client_factory: Callable[[], Any]) -> dict:
    key = f"{docs_reader.s3_prefix()}/{OBJECT_NAME}"
    try:
        raw = docs_reader._read_bytes(client_factory(), docs_reader._bucket(), key, docs_reader.MAX_MANIFEST_BYTES)
    except Exception as err:
        logger.warning("delivery_summary_read_failed", key=key, error=type(err).__name__)
        raise DeliveryUnavailable("The delivery summary could not be read from storage") from err
    if raw is None:
        raise DeliveryMissing("No delivery summary has been published yet (root repo workflow Delivery metrics)")
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise DeliveryUnavailable("The stored delivery summary is not JSON") from err
    if not isinstance(data, dict) or data.get("format") != SUPPORTED_FORMAT or not isinstance(data.get("apps"), list):
        raise DeliveryUnavailable(f"The stored delivery summary is not format {SUPPORTED_FORMAT}")
    apps = [a for a in data["apps"] if isinstance(a, dict) and isinstance(a.get("id"), str) and a["id"]]
    if len(apps) != len(data["apps"]):
        logger.warning("delivery_summary_apps_dropped", dropped=len(data["apps"]) - len(apps))
    out: dict[str, Any] = {k: data.get(k) for k in _TOP_KEYS}
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    out["source"] = {k: source.get(k) for k in ("repo", "sha", "run_id")}
    out["apps"] = apps
    hits = [0]
    out = _mask(out, hits)
    if hits[0]:
        logger.error("delivery_summary_account_id_masked", count=hits[0])
    out["format"] = SUPPORTED_FORMAT
    out["loaded_at"] = datetime.now(timezone.utc).isoformat()
    return out


async def load(refresh: bool = False, client_factory: Optional[Callable[[], Any]] = None) -> dict:
    """The published summary (cached). `refresh` re-reads storage when the cached copy is older than MIN_REFRESH_S."""
    now = time.monotonic()
    max_age = docs_reader.MIN_REFRESH_S if refresh else docs_reader.CACHE_TTL_S
    value, at = _cache["value"], _cache["at"]
    if value is not None and now - at < max_age:
        return value
    async with _lock:
        value, at = _cache["value"], _cache["at"]
        if value is not None and time.monotonic() - at < max_age:
            return value
        value = await asyncio.to_thread(_build, client_factory or docs_reader._s3_client)
        _cache["value"], _cache["at"] = value, time.monotonic()
        return value


# ── colours ─────────────────────────────────────────────────────────────────────────────────────────────────────


async def thresholds() -> dict:
    """The values in force of the delivery settings, as the page and DEV-01 use them."""
    out: dict[str, Any] = {}
    for metric, (green, amber, higher) in ds.BANDS.items():
        out[metric] = {"green": await settings.aget(green), "amber": await settings.aget(amber),
                       "higher_is_better": higher, "keys": [green, amber]}
    out["main_red_hours"] = await settings.aget(ds.MAIN_RED_HOURS)
    out["min_sample"] = await settings.aget(ds.MIN_SAMPLE)
    return out


def band(value: Optional[float], n: Optional[int], band_: dict, min_sample: float) -> str:
    """green | amber | red for `value` against a green / amber pair; few when n < min_sample; none without a value."""
    if value is None:
        return NONE
    if n is not None and n < min_sample:
        return FEW
    g, a = float(band_["green"]), float(band_["amber"])
    if band_["higher_is_better"]:
        return GREEN if value >= g else AMBER if value >= a else RED
    return GREEN if value <= g else AMBER if value <= a else RED


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def metric_values(app: dict) -> dict[str, tuple[Optional[float], Optional[int]]]:
    """(value in the unit of its settings, n) per graded metric of one app summary."""
    d = app.get("dora") if isinstance(app.get("dora"), dict) else {}
    ci = app.get("ci") if isinstance(app.get("ci"), dict) else {}

    def part(name: str) -> dict:
        p = d.get(name)
        return p if isinstance(p, dict) else {}

    cfr, rw = _num(part("change_failure_rate").get("value")), _num(part("rework_rate").get("value"))
    slow = ci.get("slowest") if isinstance(ci.get("slowest"), dict) else {}
    p95 = _num(slow.get("p95_s"))
    return {
        "deployment_frequency": (_num(part("deployment_frequency").get("value")),
                                 part("deployment_frequency").get("attempts")),
        "lead_time": (_num(part("lead_time").get("p50_h")), part("lead_time").get("n")),
        "change_failure_rate": (None if cfr is None else round(cfr * 100, 2), part("change_failure_rate").get("n")),
        "recovery_time": (_num(part("recovery_time").get("p50_h")), part("recovery_time").get("n")),
        "rework_rate": (None if rw is None else round(rw * 100, 2), part("rework_rate").get("n")),
        "ci_p95": (None if p95 is None else round(p95 / 60, 1), slow.get("n")),
    }


def main_state(app: dict, red_hours: float) -> str:
    """green when the newest CI and deploy runs on the default branch passed; red when one has failed for longer
    than `red_hours`, amber when for less; none when no run was seen."""
    main = (app.get("ci") or {}).get("main") if isinstance(app.get("ci"), dict) else None
    if not isinstance(main, dict) or main.get("state") not in ("green", "red"):
        return NONE
    if main["state"] == "green":
        return GREEN
    h = _num(main.get("red_hours"))
    return RED if h is None or h > float(red_hours) else AMBER


def grade(app: dict, th: dict) -> dict[str, str]:
    """Colour of each metric of one app summary."""
    vals = metric_values(app)
    out = {m: band(v, n, th[m], float(th["min_sample"])) for m, (v, n) in vals.items()}
    out["main"] = main_state(app, th["main_red_hours"])
    return out


async def view(refresh: bool = False) -> dict:
    """The summary with each app's colours and the thresholds in force (what GET /admin/platform/delivery serves)."""
    bundle = await load(refresh=refresh)
    th = await thresholds()
    apps = [{**a, "status": grade(a, th)} for a in bundle["apps"]]
    return {**bundle, "apps": apps, "thresholds": th, "cache_ttl_s": docs_reader.CACHE_TTL_S}
