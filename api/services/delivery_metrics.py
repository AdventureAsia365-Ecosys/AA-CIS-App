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

Defence in depth: only the known top-level keys pass, apps need a string id, and any 12-digit run standing alone in a
text string is masked again (the root repos are public; a bundle carrying one was written by something else). Hex
commit SHAs under `sha` keys and github.com links under `url` keys are identifiers, not text, and pass unchanged.

AA-1125 (S283): two more sections per app in the same summary, served by the same route with `section`:
  section=dora (default)  the AA-1108 view; the apps leave out `queue` and `git` (the page's first load stays small)
  section=queue           the merge-queue section of the apps with a GitHub merge queue (wait, outcomes, ejection
                          reasons, merge_group CI, flaky signals), coloured with the CIS 445 settings (queue wait p95,
                          ejection rate, flaky rate; cis.delivery.min_sample as for the DORA keys)
  section=git             the git activity of every app (commits a day over 90 days, author kinds, churn by directory
                          and file over 30 / 90 days, merges by branch prefix, the last commits for a graph); no colour

DeliveryMissing -> 404 (nothing published yet), DeliveryUnavailable -> 503 (storage or format).
"""
from __future__ import annotations

import asyncio
import json
import re
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
SECTIONS = ("dora", "queue", "git")
# AA-1125: the keys every section keeps from an app summary (the rest of the app belongs to its own section)
_APP_HEAD = ("id", "name", "repo", "environment", "window", "complete_since")
_HEAVY = ("queue", "git")


class DeliveryMissing(Exception):
    """No delivery summary published yet (root repo workflow Delivery metrics)."""


class DeliveryUnavailable(Exception):
    """Storage failed or the summary is not in the expected shape."""


_cache: dict[str, Any] = {"value": None, "at": 0.0}
_lock = asyncio.Lock()


def reset_cache() -> None:
    _cache["value"] = None
    _cache["at"] = 0.0


# A 12-digit run standing alone in text. Unlike docs_reader._ACCOUNT_ID (digit boundary only) a letter next to the run
# also stops it, so a run inside a commit SHA ("ab123456789012cd...", about 1.6 % of random SHAs) is not an account ID.
_ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_GITHUB = "https://github.com/"


def _passes(key: str, value: str) -> bool:
    """Identifiers that are never free text: a hex commit SHA under a sha key, a github.com link under a url key
    (run and job IDs reach 12 digits). Anything else under those keys is masked like text."""
    if key == "sha" or key.endswith("_sha"):
        return _SHA.fullmatch(value) is not None
    if key == "url" or key.endswith("_url"):
        return value.startswith(_GITHUB)
    return False


def _mask(value: Any, hits: list[int], key: str = "") -> Any:
    if isinstance(value, str):
        if _passes(key, value):
            return value
        masked, n = _ACCOUNT_ID.subn(docs_reader.ACCOUNT_ID_MARKER, value)
        hits[0] += n
        return masked
    if isinstance(value, list):
        return [_mask(v, hits, key) for v in value]
    if isinstance(value, dict):
        return {str(k): _mask(v, hits, str(k)) for k, v in value.items()}
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


# ── AA-1125: merge queue and git activity ──────────────────────────────────────────────────────────────────────


async def queue_thresholds() -> dict:
    """The values in force of the merge-queue settings (CIS 445) and the shared min_sample (CIS 433)."""
    out: dict[str, Any] = {}
    for metric, (green, amber, higher) in ds.QUEUE_BANDS.items():
        out[metric] = {"green": await settings.aget(green), "amber": await settings.aget(amber),
                       "higher_is_better": higher, "keys": [green, amber]}
    out["min_sample"] = await settings.aget(ds.MIN_SAMPLE)
    return out


def queue_values(queue: dict) -> dict[str, tuple[Optional[float], Optional[int]]]:
    """(value in the unit of its settings, n) per graded merge-queue metric of one app's `queue` section."""

    def part(name: str) -> dict:
        p = queue.get(name)
        return p if isinstance(p, dict) else {}

    ej, fl = _num(part("ejection_rate").get("value")), _num(part("flaky").get("value"))
    return {
        "queue_wait": (_num(part("wait").get("p95_h")), part("wait").get("n")),
        "ejection_rate": (None if ej is None else round(ej * 100, 2), part("ejection_rate").get("n")),
        "flaky_rate": (None if fl is None else round(fl * 100, 2), part("flaky").get("n")),
    }


def grade_queue(queue: dict, th: dict) -> dict[str, str]:
    """Colour of each merge-queue metric of one app."""
    return {m: band(v, n, th[m], float(th["min_sample"])) for m, (v, n) in queue_values(queue).items()}


def _head(app: dict) -> dict:
    return {k: app.get(k) for k in _APP_HEAD}


async def view(refresh: bool = False, section: str = "dora") -> dict:
    """The summary with each app's colours and the thresholds in force (what GET /admin/platform/delivery serves).

    `section` (AA-1125): dora (the AA-1108 view, without the queue and git sections), queue or git."""
    if section not in SECTIONS:
        raise ValueError(f"unknown section {section!r}")
    bundle = await load(refresh=refresh)
    base = {**bundle, "section": section, "cache_ttl_s": docs_reader.CACHE_TTL_S}
    if section == "queue":
        th = await queue_thresholds()
        apps = []
        for a in bundle["apps"]:
            q = a.get("queue")
            if isinstance(q, dict):
                apps.append({**_head(a), "queue": q, "queue_status": grade_queue(q, th)})
        return {**base, "apps": apps, "thresholds": th}
    if section == "git":
        apps = [{**_head(a), "git": a["git"]} for a in bundle["apps"] if isinstance(a.get("git"), dict)]
        return {**base, "apps": apps, "thresholds": {}}
    th = await thresholds()
    apps = [{**{k: v for k, v in a.items() if k not in _HEAVY}, "status": grade(a, th)} for a in bundle["apps"]]
    return {**base, "apps": apps, "thresholds": th}
