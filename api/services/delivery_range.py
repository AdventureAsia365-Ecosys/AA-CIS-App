"""AA-1135 — the time range of Platform › Delivery: the collector's presets and a custom from / to.

Presets (7 / 28 / 90 days) are computed by the root collector every hour and published per app in summary.json
(`ranges.d7|d28|d90`); `preset_key` maps the page's `range` to one. A custom range (whole UTC days, from and to
included) is recomputed here from the collector's daily event files with a byte copy of its engine
(api/services/delivery_engine.py + delivery_engine.lock.json, synced by the root tools/delivery/sync_engine.py), so a
custom range and a preset of the same window give the same numbers:

  * refused (RangeRefused 422) when to is before from, longer than the setting cis.delivery.custom_range_max_days,
    starting before the stored history (definitions.store_from) or ending after the newest collection;
  * paused (409) while the published engine sha256 differs from this copy's (a collector change not synced yet) or
    the summary predates AA-1135 (no store_from); refused (409 out_of_step) when the day files do not rebuild the
    store the summary was computed from (its definitions.store_fingerprint; the child exits OUT_OF_STEP);
  * computed in a child process (api/services/delivery_range_worker.py: the events take about 250 MB while computing,
    given back to the system when the child exits), one computation at a time;
  * cached per (summary generated_at, from, to) in this process: the newest cis.delivery.custom_range_cache_entries
    ranges, dropped as soon as a newer summary is published.

Cold path (S284 live check: the first custom range after a summary rebuild took 29 s and API Gateway answered 504):
  * the child starts from `_BOOT`, which registers an empty `api` package before importing the worker, so it never runs
    api/__init__.py (the whole FastAPI app: about 2,700 modules and 200 MB before the first day file);
  * the store work that does not depend on the window (day file reads, the fingerprint check, every app's context) is
    done once per summary and saved as a snapshot in a private temporary folder of this process (`_snapshot_path`);
    a custom range of a prepared summary only cuts its window (no S3 read);
  * `warm` prepares it in the background as soon as a summary that offers custom ranges is loaded for a preset (the
    page's first load), so a custom range picked afterwards answers in about a second; a summary whose day files are
    out of step is remembered and refused at once.

The merge-queue `recent` list of every range is bounded to its window by the engine before its list cap (S284 live
check: a custom range ending before the newest collection listed later entries); `clip_recent` keeps the same bound on
the API side as a guard.

The child's answer passes the same account-ID mask as the summary (delivery_metrics._mask): pull request titles are
free text. Read only: S3 GETs of the docs reader prefix, no table, no GitHub token.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

from api.services import delivery_engine as engine
from api.services.delivery_range_worker import OUT_OF_STEP
from services.reports import delivery_settings as ds
from shared import settings

logger = structlog.get_logger()

PRESETS = ("7d", "28d", "90d")
CUSTOM = "custom"
WORKER_TIMEOUT_S = 180  # technical-constant: wall-clock guard of one child computation (seconds on Dev data)
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
# the prepared store per summary (one file, the newest summary) and the summary whose day files were out of step
_snap: dict[str, Any] = {"dir": None, "refused": None, "task": None}
WARM = True  # tests switch the background preparation off; it is exercised by its own tests


def reset_cache() -> None:
    _cache.clear()
    _snap["refused"] = None


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


def clip_recent(queue: Any) -> Any:
    """The merge-queue section with its `recent` entries limited to the section's own window [start, end).

    The engine applies the same bound (delivery_engine.recent_in_window) before its list cap since the S284 live
    check, where a custom range that ends before the newest collection listed entries from after its last day; this
    is the API-side guard and removes nothing from a block of the current engine. Kept: an entry that left the queue
    inside the window, and one still queued that entered the queue before the window end (it was in the queue at the
    end of the range)."""
    if not isinstance(queue, dict) or not isinstance(queue.get("recent"), list):
        return queue
    w = queue.get("window") if isinstance(queue.get("window"), dict) else {}
    a, b = engine.ts(w.get("start")), engine.ts(w.get("end"))
    if a is None or b is None:
        return queue

    def inside(e: Any) -> bool:
        if not isinstance(e, dict):
            return False
        left = engine.ts(e.get("left_at"))
        if left is not None:
            return a <= left < b
        entered = engine.ts(e.get("enqueued_at"))
        return e.get("outcome") == "queued" and (entered is None or entered < b)

    return {**queue, "recent": [e for e in queue["recent"] if inside(e)]}


def _payload(bundle: dict, a: Optional[datetime], b: Optional[datetime], bucket: str, prefix: str,
             snapshot: Optional[Path] = None) -> dict:
    """The child's stdin; a and b None: prepare the summary's snapshot only."""
    apps = [{"repo": x.get("repo"), "complete_since": x.get("complete_since"),
             "git": {"complete_since": (x.get("git") or {}).get("complete_since")}}
            for x in bundle["apps"] if isinstance(x.get("repo"), str)]
    return {"bucket": bucket, "prefix": prefix, "start": engine.iso(a), "end": engine.iso(b),
            "snapshot": str(snapshot) if snapshot else None,
            "summary": {"generated_at": bundle["generated_at"], "config": bundle["config"],
                        "definitions": bundle["definitions"], "apps": apps}}


# The child's entry: an empty `api` package (its folder only) stands in for api/__init__.py, which imports the whole
# app; then the worker module and its few imports load as usual. argv[1] is the api folder.
_STUB = ("import sys, types\n"
         "pkg = types.ModuleType('api')\n"
         "pkg.__path__ = [sys.argv[1]]\n"
         "sys.modules['api'] = pkg\n"
         "from api.services import delivery_range_worker as worker\n")
_BOOT = _STUB + "sys.exit(worker.main())\n"


def _worker_argv() -> list[str]:
    return [sys.executable, "-c", _BOOT, str(_ROOT / "api")]


def _snapshot_path(generated_at: str) -> Path:
    """Where the prepared store of a summary lives: a folder of this process that only its user can read (mkdtemp,
    0700), one file per summary and engine; the files of older summaries are removed."""
    if _snap["dir"] is None or not Path(_snap["dir"]).is_dir():
        _snap["dir"] = tempfile.mkdtemp(prefix="aa-delivery-range-")
    name = _snapshot_name(generated_at)
    folder = Path(_snap["dir"])
    for old in folder.iterdir():
        if old.name != name:
            old.unlink(missing_ok=True)
    return folder / name


def prepared(generated_at: str) -> bool:
    """Whether the snapshot of this summary is ready (a custom range then reads no day file)."""
    return _snap["dir"] is not None and (Path(_snap["dir"]) / _snapshot_name(generated_at)).is_file()


def _snapshot_name(generated_at: str) -> str:
    return hashlib.sha256(f"{generated_at}|{engine.engine_sha256()}".encode()).hexdigest()[:24] + ".pickle"


async def _run_worker(payload: dict) -> dict:
    proc = await asyncio.create_subprocess_exec(*_worker_argv(), cwd=str(_ROOT), stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), WORKER_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise RangeFailed("The custom range took too long") from e
    if proc.returncode == OUT_OF_STEP:
        logger.warning("delivery_range_out_of_step", error=err.decode(errors="replace")[:300])
        raise _out_of_step()
    if proc.returncode != 0:
        logger.warning("delivery_range_worker_failed", rc=proc.returncode, error=err.decode(errors="replace")[:300])
        raise RangeFailed("The custom range could not be computed")
    try:
        return json.loads(out)
    except ValueError as e:
        raise RangeFailed("The custom range answer is not JSON") from e


def _out_of_step() -> RangeRefused:
    return RangeRefused(409, "out_of_step", "The collector's day files are out of step with its summary; custom ranges "
                        "come back after its next full upload. The presets work.")


async def _child(bundle: dict, a: Optional[datetime], b: Optional[datetime]) -> dict:
    """One child run for this summary (the caller holds _lock): the range [a, b), or the snapshot only when a and b
    are None. A summary found out of step is remembered, so the next request is refused without a child."""
    from api.services import docs_reader

    gen = bundle["generated_at"]
    if _snap["refused"] == gen:
        raise _out_of_step()
    prefix = f"{docs_reader.s3_prefix()}/delivery"
    payload = _payload(bundle, a, b, docs_reader._bucket(), prefix, _snapshot_path(gen))
    try:
        return await _run_worker(payload)
    except RangeRefused as err:
        if err.code == "out_of_step":
            _snap["refused"] = gen
        raise


def warm(bundle: dict) -> None:
    """Prepare the snapshot of `bundle` in the background when it offers custom ranges and none is ready (called when
    a summary is served for a preset). Never raises: a failure only means the first custom range prepares it."""
    if not WARM or not bounds(bundle)["custom"] or not isinstance(bundle.get("config"), dict):
        return
    gen = bundle["generated_at"]
    task = _snap["task"]
    if prepared(gen) or _snap["refused"] == gen or (task is not None and not task.done()):
        return
    try:
        _snap["task"] = asyncio.get_running_loop().create_task(_warm(bundle))
    except RuntimeError:  # no running loop: nothing to schedule on
        return


async def _warm(bundle: dict) -> None:
    gen = bundle["generated_at"]
    async with _lock:
        if prepared(gen) or _snap["refused"] == gen:
            return
        try:
            await _child(bundle, None, None)
            logger.info("delivery_range_prepared", generated_at=gen)
        except Exception as err:  # logged only: the first custom range prepares it again (or answers the refusal)
            logger.warning("delivery_range_prepare_failed", generated_at=gen, error=type(err).__name__)


async def compute(bundle: dict, start: Any, end: Any) -> dict:
    """{"start", "end", "from", "to", "apps": {app id: range block}, "cached"} of one custom range."""
    from api.services import delivery_metrics

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
        raw = await _child(bundle, a, b)
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
