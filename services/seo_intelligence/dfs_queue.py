"""services/seo_intelligence/dfs_queue.py — AA-1055: the DataForSEO Standard queue for search volume and SERPs.

Every DataForSEO call in this repo used the ``/live`` endpoints: the answer comes back in the same HTTP call, at the
highest price. Research is batch work nobody waits on, so it can use the Standard queue instead: ``task_post`` sends up
to 100 tasks (paid at post), ``tasks_ready`` lists the finished ones and ``task_get`` returns a result in the same
shape as the live answer (both free). The audit (root docs/audits/2026-11-08-S273-session/dfs-cost-audit/design.vi.md
section 6) expects about 0.0006 USD per SERP instead of 0.002 and about 2/3 of the live volume price: not verified,
``scripts/aa1055_dfs_queue_trial.py`` reads the real cost of one task of each kind from DataForSEO's answer.

Which endpoint research uses is a setting: ``DFS_RESEARCH_QUEUE`` (env, ``live`` by default, ``standard`` to queue),
overridden per job by the ``dfs_queue`` payload field. ``live`` keeps the code path of today unchanged.

Everything goes through ``DataForSEOClient``: its admin switch, the shared circuit, the run budget (checked per task
before the post, charged with the post's real cost) and ``shared.dfs_call_log`` (one row per post, endpoint labels
``search_volume_bulk_queue`` / ``serp_advanced_queue``, with the cost DataForSEO reports; the free polls write no row).
A task that does not finish within ``timeout_s`` or answers an error raises ``DFSCallError`` for that task only: the
caller stores nothing for it (AA-647), exactly as for a failed live call.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field

import structlog

from services.seo_intelligence.dataforseo_client import (
    DATAFORSEO_BASE,
    DFS_OK,
    DataForSEOClient,
    DFSCallError,
    _dfs_status_error,
    as_dfs_error,
)
from shared.dfs_client.call_log import extract_cost

logger = structlog.get_logger()

QUEUE_ENV = "DFS_RESEARCH_QUEUE"
MODES = ("live", "standard")

TASK_CREATED = 20100
# task_get of a task still being worked on: 40601 "Task Handed", 40602 "Task In Queue". Not a failure: wait.
NOT_READY = (40601, 40602)
POST_MAX_TASKS = 100
VOLUME_ENDPOINT = "search_volume_bulk_queue"
SERP_ENDPOINT = "serp_advanced_queue"

_VOLUME = {
    "post": f"{DATAFORSEO_BASE}/keywords_data/google_ads/search_volume/task_post",
    "ready": f"{DATAFORSEO_BASE}/keywords_data/google_ads/search_volume/tasks_ready",
    "get": f"{DATAFORSEO_BASE}/keywords_data/google_ads/search_volume/task_get/{{id}}",
}
_SERP = {
    "post": f"{DATAFORSEO_BASE}/serp/google/organic/task_post",
    "ready": f"{DATAFORSEO_BASE}/serp/google/organic/tasks_ready",
    "get": f"{DATAFORSEO_BASE}/serp/google/organic/task_get/advanced/{{id}}",
}


def research_queue_mode(value: str | None = None) -> str:
    """``live`` | ``standard``: the job payload's value when given, else the env setting, else ``live``. An unknown
    value is refused (a typo must not silently fall back to the expensive endpoint or to an untested one)."""
    raw = value if value is not None else os.environ.get(QUEUE_ENV, "")
    mode = (raw or "live").strip().lower()
    if mode not in MODES:
        raise ValueError(f"{QUEUE_ENV} / dfs_queue must be one of {MODES}, got {raw!r}")
    return mode


@dataclass
class PostedTask:
    """One task the queue accepted: DataForSEO's id, the cost it reported for it and the caller's tag."""
    task_id: str
    tag: str
    cost_usd: float | None
    posted_at: float = field(default_factory=time.monotonic)


def parse_post(data: dict) -> list[PostedTask]:
    """The accepted tasks of a ``task_post`` answer, in order. A task not created (its own status is not 20100) is
    left out: the caller sees its tag missing and treats it as failed."""
    out: list[PostedTask] = []
    for task in (data or {}).get("tasks") or []:
        if not isinstance(task, dict) or task.get("status_code") != TASK_CREATED or not task.get("id"):
            continue
        tag = ((task.get("data") or {}).get("tag")) or ""
        cost = task.get("cost")
        out.append(PostedTask(task_id=str(task["id"]), tag=str(tag),
                              cost_usd=float(cost) if isinstance(cost, (int, float)) else None))
    return out


def parse_ready(data: dict) -> set[str]:
    """Task ids of a ``tasks_ready`` answer (``tasks[*].result[*].id``)."""
    ids: set[str] = set()
    for task in (data or {}).get("tasks") or []:
        for row in (task or {}).get("result") or []:
            if isinstance(row, dict) and row.get("id"):
                ids.add(str(row["id"]))
    return ids


def task_get_state(data: dict) -> str:
    """``ready`` | ``pending`` | ``error`` for one ``task_get`` answer."""
    tasks = (data or {}).get("tasks") or []
    code = tasks[0].get("status_code") if tasks and isinstance(tasks[0], dict) else None
    if code == DFS_OK:
        return "ready"
    if code in NOT_READY:
        return "pending"
    return "error"


def parse_volumes(data: dict, keywords: list[str]) -> dict[str, int | None]:
    """keyword -> volume for every asked keyword (None = DataForSEO has no row for it), same rule as the live
    ``fetch_volumes_bulk``."""
    out: dict[str, int | None] = {kw: None for kw in keywords}
    try:
        results = data["tasks"][0]["result"] or []
    except (KeyError, IndexError, TypeError):
        results = []
    for row in results:
        if isinstance(row, dict) and row.get("keyword") in out:
            out[row["keyword"]] = row.get("search_volume")
    return out


class StandardQueue:
    """Posts tasks, waits for them and returns the raw results. One instance per run (it keeps the pending ids)."""

    def __init__(self, client: DataForSEOClient, *, poll_s: float = 20.0, timeout_s: float = 3600.0,
                 direct_after_s: float = 600.0, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        self.client = client
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        # A task not listed by tasks_ready after this long is asked for directly (tasks_ready lists at most 1,000
        # tasks of the whole account; a direct task_get is free too).
        self.direct_after_s = direct_after_s
        self._sleep = sleep
        self._clock = clock

    async def _post(self, kind: dict, endpoint: str, payload: list[dict], *, keyword_count: int,
                    location_code: int | None, keyword: str | None = None) -> list[PostedTask]:
        c = self.client
        c._precheck(endpoint, location_code=location_code, keyword_count=keyword_count, keyword=keyword,
                    tasks=len(payload))
        try:
            data, ms = await c._request(endpoint, kind["post"], payload=payload, timeout=60,
                                        location_code=location_code, keyword_count=keyword_count, keyword=keyword)
        except Exception as e:
            raise as_dfs_error(e) from e
        c._log_live(endpoint, data, keyword=keyword, location_code=location_code, keyword_count=keyword_count,
                    latency_ms=ms, task_ok=(TASK_CREATED,), meta={"queue": "standard", "tasks": len(payload)})
        status_err = _dfs_status_error(data, (TASK_CREATED,))
        posted = parse_post(data)
        if status_err is not None and status_err.fatal:
            raise status_err
        if not posted:
            raise status_err or DFSCallError("DataForSEO queue accepted no task")
        logger.info("dfs_queue_posted", endpoint=endpoint, tasks=len(posted), cost_usd=extract_cost(data))
        return posted

    async def _get(self, kind: dict, task_id: str) -> dict | None:
        """The finished answer, None while the task is still queued; DFSCallError on an error answer."""
        try:
            data, _ms = await self.client._request("task_get", kind["get"].format(id=task_id))
        except Exception as e:
            raise as_dfs_error(e) from e
        state = task_get_state(data)
        if state == "pending":
            return None
        if state == "error":
            raise _dfs_status_error(data) or DFSCallError(f"DataForSEO task {task_id} returned no result")
        return data

    async def _ready(self, kind: dict) -> set[str] | None:
        try:
            data, _ms = await self.client._request("tasks_ready", kind["ready"])
        except Exception as e:  # noqa: BLE001 — a failed poll is retried; a direct task_get covers it
            err = as_dfs_error(e)
            if err.fatal:
                raise err from e
            logger.warning("dfs_queue_ready_failed", error=str(err)[:200])
            return None
        return parse_ready(data)

    async def wait(self, kind: dict, posted: list[PostedTask]) -> dict[str, dict | DFSCallError]:
        """tag -> the finished raw answer, or the DFSCallError of that task (a timeout is one too)."""
        pending = {t.task_id: t for t in posted}
        out: dict[str, dict | DFSCallError] = {}
        started = self._clock()
        while pending:
            ready = await self._ready(kind)
            now = self._clock()
            for task_id, t in list(pending.items()):
                asked_direct = now - t.posted_at >= self.direct_after_s
                if ready is not None and task_id not in ready and not asked_direct:
                    continue
                try:
                    data = await self._get(kind, task_id)
                except DFSCallError as err:
                    if err.fatal:
                        raise
                    out[t.tag] = err
                    pending.pop(task_id)
                    continue
                if data is not None:
                    out[t.tag] = data
                    pending.pop(task_id)
            if not pending:
                break
            if self._clock() - started >= self.timeout_s:
                for t in pending.values():
                    out[t.tag] = DFSCallError(f"DataForSEO task {t.task_id} not finished after {self.timeout_s:.0f} s")
                logger.warning("dfs_queue_timeout", pending=len(pending))
                break
            await self._sleep(self.poll_s)
        return out

    # ── the two task kinds research buys ─────────────────────────────────────────────────────────────────────────
    async def volumes(self, keywords: list[str], location_code: int, language_code: str) -> dict[str, int | None]:
        """Search volume of up to 1,000 keywords in one market, as ONE queued task. Raises DFSCallError on failure
        (the caller splits the chunk, as for a failed live task)."""
        if not keywords:
            return {}
        chunk = keywords[:1000]
        tag = uuid.uuid4().hex[:16]
        posted = await self._post(_VOLUME, VOLUME_ENDPOINT, [{
            "keywords": chunk, "location_code": location_code, "language_code": language_code, "tag": tag,
        }], keyword_count=len(chunk), location_code=location_code)
        got = (await self.wait(_VOLUME, posted)).get(tag)
        if got is None:
            raise DFSCallError("DataForSEO queue lost the volume task")
        if isinstance(got, DFSCallError):
            raise got
        return parse_volumes(got, chunk)

    async def serps(self, asks: list[tuple[str, int, str]]) -> dict[int, dict | DFSCallError]:
        """One SERP (organic, advanced) per (keyword, location_code, language_code), posted 100 per request.
        Returns index of ``asks`` -> the raw answer (same shape as the live advanced SERP) or that task's error."""
        out: dict[int, dict | DFSCallError] = {}
        for start in range(0, len(asks), POST_MAX_TASKS):
            part = list(enumerate(asks))[start:start + POST_MAX_TASKS]
            tags = {f"{i}-{uuid.uuid4().hex[:8]}": i for i, _ in part}
            payload = [{"keyword": kw, "location_code": loc, "language_code": lang, "tag": tag}
                       for tag, (_i, (kw, loc, lang)) in zip(tags, part)]
            try:
                posted = await self._post(_SERP, SERP_ENDPOINT, payload, keyword_count=len(payload),
                                          location_code=payload[0]["location_code"])
            except DFSCallError as err:
                if err.fatal:
                    raise
                for i in tags.values():
                    out[i] = err
                continue
            answers = await self.wait(_SERP, posted)
            for tag, i in tags.items():
                out[i] = answers.get(tag, DFSCallError("DataForSEO queue did not accept the SERP task"))
        return out


__all__ = ["StandardQueue", "research_queue_mode", "QUEUE_ENV", "VOLUME_ENDPOINT", "SERP_ENDPOINT",
           "parse_post", "parse_ready", "parse_volumes", "task_get_state"]
