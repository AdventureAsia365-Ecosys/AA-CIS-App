"""AA-1135 — the time range of Platform › Delivery: the collector's presets and a custom from / to.

Presets (7 / 28 / 90 days) are computed by the root collector every hour and published per app in summary.json
(`ranges.d7|d28|d90`); `preset_key` maps the page's `range` to one. A custom range (whole UTC days, from and to
included) is recomputed here from the collector's daily event files with a byte copy of its engine
(api/services/delivery_engine.py + delivery_engine.lock.json, synced by the root tools/delivery/sync_engine.py), so a
custom range and a preset of the same window give the same numbers:

  * refused (RangeRefused 422) when to is before from, longer than the setting cis.delivery.custom_range_max_days,
    starting before the stored history (definitions.store_from) or ending after the newest collection;
  * paused (409) while the published engine sha256 differs from this copy's (a collector change not synced yet) or
    the summary predates AA-1135 (no store_from);
  * computed in a child process (api/services/delivery_range_worker.py: the events take about 250 MB while computing,
    given back to the system when the child exits), one computation at a time;
  * cached per (summary generated_at, from, to) in this process: the newest cis.delivery.custom_range_cache_entries
    ranges, dropped as soon as a newer summary is published.

The child's answer passes the same account-ID mask as the summary (delivery_metrics._mask): pull request titles are
free text. Read only: S3 GETs of the docs reader prefix, no table, no GitHub token.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

from api.services import delivery_engine as engine
from services.reports import delivery_settings as ds
from shared import settings

logger = structlog.get_logger()

PRESETS = ("7d", "28d", "90d")
CUSTOM = "custom"
WORKER_TIMEOUT_S = 180  # technical-constant: wall-clock guard of one child computation (about 5 s on Dev data)
_ROOT = Path(__file__).resolve().parents[2]


class RangeRefused(Exception):
    """A range the page asked for that cannot be served; `status` is the HTTP status, `code` a stable reason."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(status, code, message)
        self.status, self.code, self.message = status, code, message

    def __str__(self) -> str:
        return self.message


class RangeFailed(Exception):
    """The child computation failed (storage, timeout, a malformed day file)."""


_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_lock = asyncio.Lock()


def reset_cache() -> None:
    _cache.clear()


def preset_key(range_: str) -> str:
    """"7d" -> "d7": the key of a preset in an app's `ranges`."""
    return f"d{range_[:-1]}"


def _day(v: Any, name: str) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError as err:
        raise RangeRefused(422, "bad_date", f"{name} must be a date (YYYY-MM-DD)") from err


def bounds(bundle: dict) -> dict:
    """What the page's date picker may offer: the first stored day, the newest collected day, the presets."""
    d = bundle.get("definitions") if isinstance(bundle.get("definitions"), dict) else {}
    gen = engine.ts(bundle.get("generated_at"))
    return {
        "store_from": d.get("store_from"),
        "last_day": gen.date().isoformat() if gen else None,
        "presets": [f"{n}d" for n in d.get("range_presets") or ()],
        "custom": bool(d.get("store_from")) and d.get("engine_sha256") == engine.engine_sha256(),
    }


def window(bundle: dict, start: Any, end: Any, max_days: int) -> tuple[datetime, datetime]:
    """[a, b) of a custom range of whole UTC days, or RangeRefused."""
    s, e = _day(start, "from"), _day(end, "to")
    d = bundle.get("definitions") if isinstance(bundle.get("definitions"), dict) else {}
    gen = engine.ts(bundle.get("generated_at"))
    if not d.get("store_from") or gen is None or not isinstance(bundle.get("config"), dict):
        raise RangeRefused(409, "not_published", "Custom ranges need a newer delivery collection; the presets work")
    if d.get("engine_sha256") != engine.engine_sha256():
        raise RangeRefused(409, "engine_mismatch", "Custom ranges are paused until the CIS copy of the delivery "
                           "engine matches the collector's; the presets work")
    if e < s:
        raise RangeRefused(422, "reversed", "to is before from")
    days = (e - s).days + 1
    if days > max_days:
        raise RangeRefused(422, "too_long", f"The range is {days} days; the longest custom range is {max_days} days")
    first = date.fromisoformat(d["store_from"])
    if s < first:
        raise RangeRefused(422, "before_store", f"The stored history starts on {first.isoformat()}")
    if e > gen.date():
        raise RangeRefused(422, "after_collection", f"The newest collection is from {gen.date().isoformat()}")
    a = datetime.combine(s, datetime.min.time(), timezone.utc)
    b = min(datetime.combine(e + timedelta(days=1), datetime.min.time(), timezone.utc), gen)
    return a, b


def _payload(bundle: dict, a: datetime, b: datetime, bucket: str, prefix: str) -> dict:
    apps = [{"repo": x.get("repo"), "complete_since": x.get("complete_since"),
             "git": {"complete_since": (x.get("git") or {}).get("complete_since")}}
            for x in bundle["apps"] if isinstance(x.get("repo"), str)]
    return {"bucket": bucket, "prefix": prefix, "start": engine.iso(a), "end": engine.iso(b),
            "summary": {"generated_at": bundle["generated_at"], "config": bundle["config"],
                        "definitions": bundle["definitions"], "apps": apps}}


def _worker_argv() -> list[str]:
    return [sys.executable, "-m", "api.services.delivery_range_worker"]


async def _run_worker(payload: dict) -> dict:
    proc = await asyncio.create_subprocess_exec(*_worker_argv(), cwd=str(_ROOT), stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), WORKER_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise RangeFailed("The custom range took too long") from e
    if proc.returncode != 0:
        logger.warning("delivery_range_worker_failed", rc=proc.returncode, error=err.decode(errors="replace")[:300])
        raise RangeFailed("The custom range could not be computed")
    try:
        return json.loads(out)
    except ValueError as e:
        raise RangeFailed("The custom range answer is not JSON") from e


async def compute(bundle: dict, start: Any, end: Any) -> dict:
    """{"start", "end", "from", "to", "apps": {app id: range block}, "cached"} of one custom range."""
    from api.services import delivery_metrics, docs_reader

    max_days = int(await settings.aget(ds.CUSTOM_RANGE_MAX_DAYS))
    a, b = window(bundle, start, end, max_days)
    s, e = _day(start, "from"), _day(end, "to")
    key = (bundle["generated_at"], s.isoformat(), e.isoformat())
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
        return {**hit, "cached": True}
    async with _lock:
        hit = _cache.get(key)
        if hit is not None:
            return {**hit, "cached": True}
        prefix = f"{docs_reader.s3_prefix()}/delivery"
        raw = await _run_worker(_payload(bundle, a, b, docs_reader._bucket(), prefix))
        if raw.get("engine_sha256") != engine.engine_sha256() or not isinstance(raw.get("apps"), list):
            raise RangeFailed("The custom range answer does not match this engine")
        hits = [0]
        apps = {x["id"]: delivery_metrics._mask(x["range"], hits) for x in raw["apps"]
                if isinstance(x, dict) and isinstance(x.get("id"), str) and isinstance(x.get("range"), dict)}
        if hits[0]:
            logger.error("delivery_range_account_id_masked", count=hits[0])
        value = {"start": engine.iso(a), "end": engine.iso(b), "from": s.isoformat(), "to": e.isoformat(),
                 "apps": apps}
        # a newer summary makes every cached range stale: keep only this summary's ranges, newest last
        for k in [k for k in _cache if k[0] != key[0]]:
            del _cache[k]
        _cache[key] = value
        cap = max(1, int(await settings.aget(ds.CUSTOM_RANGE_CACHE_ENTRIES)))
        while len(_cache) > cap:
            _cache.popitem(last=False)
        return {**value, "cached": False}


def cached_ranges() -> int:
    return len(_cache)


def asked(range_: str, start: Optional[str], end: Optional[str]) -> None:
    """Validate the shape of the query (the router's Literal already limits `range`)."""
    if range_ == CUSTOM and (not start or not end):
        raise RangeRefused(422, "missing_dates", "A custom range needs from and to")
