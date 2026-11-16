"""services/seo_intelligence/dfs_queue.py — AA-1055: the DataForSEO Standard queue for search volume and SERPs.

Every DataForSEO call in this repo used the ``/live`` endpoints: the answer comes back in the same HTTP call, at the
highest price. Research is batch work nobody waits on, so it can use the Standard queue instead: ``task_post`` sends up
to 100 tasks (paid at post), ``tasks_ready`` lists the finished ones and ``task_get`` returns a result in the same
shape as the live answer (both free). The audit (root docs/audits/2026-11-08-S273-session/dfs-cost-audit/design.vi.md
section 6) expects about 0.0006 USD per SERP instead of 0.002 and about 2/3 of the live volume price: not verified,
``scripts/aa1055/dfs_queue_trial.py`` reads the real cost of one task of each kind from DataForSEO's answer.

Which endpoint research uses is a setting: ``DFS_RESEARCH_QUEUE`` (env, ``live`` by default, ``standard`` to queue),
overridden per job by the ``dfs_queue`` payload field. ``live`` keeps the code path of today unchanged.

Everything goes through ``DataForSEOClient``: its admin switch, the shared circuit, the run budget (checked per task
before the post, charged with the post's real cost) and ``shared.dfs_call_log`` (one row per post, endpoint labels
``search_volume_bulk_queue`` / ``serp_advanced_queue``, with the cost DataForSEO reports; the free polls write no row).
A task that does not finish within ``timeout_s`` or answers an error raises ``QueuedTaskFailed`` (a ``DFSCallError``)
for that task only: the caller stores nothing for it (AA-647), exactly as for a failed live call, but never posts the
same keywords again in this run (the accepted task was already charged). A failed free poll (``tasks_ready`` or
``task_get`` transport error) is retried on the next poll, never counted as the task's failure. ``serps`` reports each
task through ``on_result`` as soon as it is collected, so the caller stores what was paid for before the whole wait
ends (a worker restart during a long wait keeps it).

AA-1120 — the queue goes through the research cache (``research_cache.ResearchCache``, required at construction:
``cache=None`` only for an adhoc trial that buys no research data). Before it posts, ``volumes`` / ``serps``
deduplicate the asks (one task per (keyword, location)), read the fresh store (hits are answered at once and logged as
0 USD ``cache_hit`` rows under this queue's endpoint label), then collect free the tasks an earlier run paid for within
``cis.seo.dfs_queue_recover_days`` and never collected (the post row's meta ``posted`` lists each task id with its
keywords); only what is left is posted. Every collected answer (bought or recovered) is written back to the store
before the caller sees it.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog

from services.seo_intelligence.research_cache import CachedSerp, ResearchCache, dedupe
from services.seo_intelligence.seed_builder import LOCATION_CODE_TO_MARKET
from services.seo_intelligence.search_history import monthly_from_answer, with_history
from services.seo_intelligence.dataforseo_client import (
    DATAFORSEO_BASE,
    DFS_OK,
    DataForSEOClient,
    DFSCallError,
    _dfs_status_error,
    as_dfs_error,
)
from shared.cost_guard import BudgetExceeded
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


class QueuedTaskFailed(DFSCallError):
    """A task the queue accepted (and charged at post) that then answered an error or did not finish in time. Posting
    its keywords again would pay twice: the caller marks them failed and leaves them to the next run. ``task_id`` is
    DataForSEO's id of the accepted task (a timed-out task may still finish in the queue)."""

    task_id: str | None = None

    @classmethod
    def of(cls, message: str, task_id: str | None, status_code: int | None = None) -> "QueuedTaskFailed":
        err = cls(message, False, status_code)
        err.task_id = task_id
        return err


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


def posted_meta(payload: list[dict], posted: list[PostedTask]) -> list[dict]:
    """AA-1120 — what the post row's meta ``posted`` records of each accepted task: its id, its keywords and its
    location, so a later run can collect a task that was paid for and never collected (research_cache.pending_tasks).
    Pure."""
    by_tag = {str(p.get("tag") or ""): p for p in payload}
    out: list[dict] = []
    for t in posted:
        p = by_tag.get(t.tag) or {}
        kws = p.get("keywords") or ([p["keyword"]] if p.get("keyword") else [])
        out.append({"id": t.task_id, "keywords": list(kws), "location_code": p.get("location_code")})
    return out


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


def queue_history(data: dict, keywords: list[str]) -> dict:
    """AA-1122 — keyword -> (endpoint label, its 12 monthly volumes) of a queued volume answer (search_history), for
    the history the research cache writes with the volumes."""
    return {kw: (VOLUME_ENDPOINT, series) for kw, series in monthly_from_answer(data, keywords).items()}


class StandardQueue:
    """Posts tasks, waits for them and returns the raw results. One instance per run (it keeps the pending ids)."""

    def __init__(self, client: DataForSEOClient, *, cache: ResearchCache | None, poll_s: float = 20.0,
                 timeout_s: float = 3600.0, direct_after_s: float = 600.0, sleep=asyncio.sleep,
                 clock=time.monotonic) -> None:
        self.client = client
        # AA-1120: the research cache (read before a post, written after a collect); None only for an adhoc trial.
        self.cache = cache
        # The task ids this instance posted: never "recovered" by itself (they are being waited for).
        self._own_task_ids: set[str] = set()
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        # A task not listed by tasks_ready after this long is asked for directly (tasks_ready lists at most 1,000
        # tasks of the whole account; a direct task_get is free too).
        self.direct_after_s = direct_after_s
        self._sleep = sleep
        self._clock = clock
        # Posts are serialised (budget check -> paid post -> charge), so concurrent callers never pass the run
        # budget's check together; the free waits run in parallel.
        self._post_lock = asyncio.Lock()

    async def _post(self, kind: dict, endpoint: str, payload: list[dict], *, keyword_count: int,
                    location_code: int | None, keyword: str | None = None) -> list[PostedTask]:
        async with self._post_lock:
            return await self._post_locked(kind, endpoint, payload, keyword_count=keyword_count,
                                           location_code=location_code, keyword=keyword)

    async def _post_locked(self, kind: dict, endpoint: str, payload: list[dict], *, keyword_count: int,
                           location_code: int | None, keyword: str | None = None) -> list[PostedTask]:
        c = self.client
        c._precheck(endpoint, location_code=location_code, keyword_count=keyword_count, keyword=keyword,
                    tasks=len(payload))
        try:
            data, ms = await c._request(endpoint, kind["post"], payload=payload, timeout=60,
                                        location_code=location_code, keyword_count=keyword_count, keyword=keyword)
        except Exception as e:
            raise as_dfs_error(e) from e
        posted = parse_post(data)
        self._own_task_ids.update(t.task_id for t in posted)
        c._log_live(endpoint, data, keyword=keyword, location_code=location_code, keyword_count=keyword_count,
                    latency_ms=ms, task_ok=(TASK_CREATED,),
                    meta={"queue": "standard", "tasks": len(payload), "posted": posted_meta(payload, posted)})
        status_err = _dfs_status_error(data, (TASK_CREATED,))
        if status_err is not None and status_err.fatal:
            raise status_err
        if not posted:
            raise status_err or DFSCallError("DataForSEO queue accepted no task")
        logger.info("dfs_queue_posted", endpoint=endpoint, tasks=len(posted), cost_usd=extract_cost(data))
        return posted

    async def _get(self, kind: dict, task_id: str) -> dict | None:
        """The finished answer, None while the task is still queued or the free poll itself failed (asked again on
        the next poll); QueuedTaskFailed on an error answer of the task, a fatal DFSCallError on auth / payment."""
        try:
            data, _ms = await self.client._request("task_get", kind["get"].format(id=task_id))
        except Exception as e:  # noqa: BLE001 — a failed free poll is not the task's failure
            err = as_dfs_error(e)
            if err.fatal:
                raise err from e
            logger.warning("dfs_queue_get_failed", task_id=task_id, error=str(err)[:200])
            return None
        state = task_get_state(data)
        if state == "pending":
            return None
        if state == "error":
            err = _dfs_status_error(data)
            if err is not None and err.fatal:
                raise err
            raise QueuedTaskFailed.of(str(err) if err else f"DataForSEO task {task_id} returned no result",
                                      task_id, err.status_code if err else None)
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

    async def wait(self, kind: dict, posted: list[PostedTask],
                   on_result: Callable[[str, dict | DFSCallError], Awaitable[None]] | None = None,
                   ) -> dict[str, dict | DFSCallError]:
        """tag -> the finished raw answer, or the QueuedTaskFailed of that task (a timeout is one too).
        ``on_result(tag, answer_or_error)`` is awaited as each task is collected (before the next poll)."""
        pending = {t.task_id: t for t in posted}
        out: dict[str, dict | DFSCallError] = {}
        started = self._clock()

        async def _collect(tag: str, value: dict | DFSCallError) -> None:
            out[tag] = value
            if on_result is not None:
                await on_result(tag, value)

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
                    pending.pop(task_id)
                    await _collect(t.tag, err)
                    continue
                if data is not None:
                    pending.pop(task_id)
                    await _collect(t.tag, data)
            if not pending:
                break
            if self._clock() - started >= self.timeout_s:
                logger.warning("dfs_queue_timeout", pending=len(pending), task_ids=sorted(pending)[:20])
                for t in list(pending.values()):
                    await _collect(t.tag, QueuedTaskFailed.of(
                        f"DataForSEO task {t.task_id} not finished after {self.timeout_s:.0f} s", t.task_id))
                break
            await self._sleep(self.poll_s)
        return out

    # ── AA-1120: the research cache around every post ─────────────────────────────────────────────────────────────
    def _recovered_task(self, task_id: str) -> PostedTask:
        """A task an earlier run paid for: asked for directly on the first poll (it is long finished)."""
        return PostedTask(task_id=task_id, tag=f"r-{task_id}", cost_usd=0.0,
                          posted_at=self._clock() - self.direct_after_s)

    async def _recover(self, kind: dict, endpoint: str, location_code: int,
                       keywords: list[str]) -> list[tuple[list[str], dict]]:
        """(the task's keywords, its raw answer) for every task of ``endpoint`` an earlier run paid for within the
        recovery window, holding one of ``keywords`` (not fresh in the store) and never collected. Free (task_get).
        A task that answers an error (expired, unknown) is left out: its keywords are posted again."""
        if self.cache is None or not keywords:
            return []
        pending = await self.cache.pending_tasks(endpoint, location_code, keywords, exclude=self._own_task_ids)
        if not pending:
            return []
        tasks = [self._recovered_task(p.task_id) for p in pending]
        got = await self.wait(kind, tasks)
        out = [(p.keywords, got[t.tag]) for p, t in zip(pending, tasks) if isinstance(got.get(t.tag), dict)]
        logger.info("dfs_queue_recovered", endpoint=endpoint, asked=len(pending), collected=len(out))
        return out

    async def _cached_volumes(self, chunk: list[str], location_code: int) -> dict[str, int | None]:
        """The fresh store's answers for ``chunk``, then the answers of paid, uncollected tasks (written back)."""
        market = LOCATION_CODE_TO_MARKET.get(location_code)
        if self.cache is None or market is None:
            return {}
        out = await self.cache.volumes(chunk, market)
        await self.cache.log_hits(VOLUME_ENDPOINT, count=len(out), location_code=location_code,
                                  meta={"queue": "standard"})
        missing = [kw for kw in chunk if kw not in out]
        recovered: dict[str, int | None] = {}
        for task_keywords, data in await self._recover(_VOLUME, VOLUME_ENDPOINT, location_code, missing):
            answers = parse_volumes(data, task_keywords)
            await self.cache.put_volumes(market, answers, **with_history(queue_history(data, task_keywords)))
            recovered.update({kw: answers[kw] for kw in missing if kw in answers})
        await self.cache.log_hits(VOLUME_ENDPOINT, count=len(recovered), location_code=location_code,
                                  meta={"queue": "standard", "source": "recovered_task"})
        return {**out, **recovered}

    # ── the two task kinds research buys ─────────────────────────────────────────────────────────────────────────
    async def volumes(self, keywords: list[str], location_code: int, language_code: str) -> dict[str, int | None]:
        """Search volume of up to 1,000 keywords in one market. AA-1120: duplicates are asked once, the fresh store
        and paid-but-uncollected tasks answer first, only the rest is posted as ONE queued task, and its answer is
        written back to the store. Raises DFSCallError on failure (the caller splits the chunk, as for a failed live
        task), QueuedTaskFailed for a posted task that failed or timed out and research_cache.PaidNotStored when the
        store refused a paid answer (neither is split: the next run collects the paid task)."""
        chunk, _where = dedupe(kw for kw in keywords if kw)
        chunk = chunk[:1000]
        if not chunk:
            return {}
        out = await self._cached_volumes(chunk, location_code)
        missing = [kw for kw in chunk if kw not in out]
        if not missing:
            return {kw: out.get(kw) for kw in chunk}
        tag = uuid.uuid4().hex[:16]
        posted = await self._post(_VOLUME, VOLUME_ENDPOINT, [{
            "keywords": missing, "location_code": location_code, "language_code": language_code, "tag": tag,
        }], keyword_count=len(missing), location_code=location_code)
        got = (await self.wait(_VOLUME, posted)).get(tag)
        if got is None:
            raise QueuedTaskFailed.of("DataForSEO queue lost the volume task", posted[0].task_id)
        if isinstance(got, DFSCallError):
            raise got
        bought = parse_volumes(got, missing)
        market = LOCATION_CODE_TO_MARKET.get(location_code)
        if self.cache is not None and market is not None:
            await self.cache.put_volumes(market, bought, **with_history(queue_history(got, missing)))
        out.update(bought)
        return {kw: out.get(kw) for kw in chunk}

    async def _write_serp(self, ask: tuple[str, int, str], data: dict) -> dict | Exception:
        """Write one collected SERP back to the store (AA-1120); the answer, or the store error (the caller then keeps
        the place stale: nothing is logged researched that the store does not hold)."""
        market = LOCATION_CODE_TO_MARKET.get(ask[1])
        if self.cache is None or market is None:
            return data
        try:
            await self.cache.put_serp_answer(self.client, ask[0], market, data)
        except Exception as exc:  # noqa: BLE001 — one failed store must not stop collecting the others
            logger.warning("dfs_queue_serp_store_failed", keyword=ask[0], error=str(exc)[:200])
            return exc
        return data

    async def serps(self, asks: list[tuple[str, int, str]],
                    on_result: Callable[[int, dict | Exception | CachedSerp], Awaitable[None]] | None = None,
                    ) -> dict[int, dict | Exception | CachedSerp]:
        """One SERP (organic, advanced) per distinct (keyword, location_code, language_code), posted 100 per request.
        Returns index of ``asks`` -> the raw answer (same shape as the live advanced SERP, already written back to
        the store), a ``CachedSerp`` (fresh in the store: nothing bought) or that task's error. Asks repeated in
        ``asks`` share one task and get the same answer. ``on_result(index, value)`` is awaited once per index as
        soon as that index is settled (a store hit or a refused post at once, an accepted task when it is collected),
        so the caller can mark each place right away. A fatal error or budget stop before anything was posted is
        raised instead (no index is reported for the asks still to post)."""
        out: dict[int, dict | Exception | CachedSerp] = {}
        unique, where = dedupe(asks)

        async def _settle(u: int, value) -> None:
            if isinstance(value, dict):
                value = await self._write_serp(unique[u], value)
            for i in where[unique[u]]:
                out[i] = value
                if on_result is not None:
                    await on_result(i, value)

        todo = list(range(len(unique)))
        if self.cache is not None:
            todo = await self._serps_from_cache(unique, todo, _settle)
        all_posted, all_tags = await self._post_serps(unique, todo, _settle)

        async def _collected(tag: str, value: dict | DFSCallError) -> None:
            if tag in all_tags:
                await _settle(all_tags[tag], value)

        if all_posted:
            await self.wait(_SERP, all_posted, on_result=_collected)
        settled = {u for u in range(len(unique)) if where[unique[u]][0] in out}
        for u in all_tags.values():
            if u not in settled:
                await _settle(u, DFSCallError("DataForSEO queue did not accept the SERP task"))
        return out

    async def _post_serps(self, unique: list[tuple[str, int, str]], todo: list[int],
                          settle) -> tuple[list[PostedTask], dict[str, int]]:
        """Post the asks ``todo`` (indexes of ``unique``), 100 per request, all before the first poll: the queue works
        on them in parallel, so a run of thousands of places waits about as long as one request. A refused request
        settles its asks with the error; a budget stop or a fatal answer stops posting (raised when nothing was posted
        yet, else every ask still to post carries it and the tasks already paid for are still collected)."""
        all_posted: list[PostedTask] = []
        all_tags: dict[str, int] = {}
        for start in range(0, len(todo), POST_MAX_TASKS):
            part = todo[start:start + POST_MAX_TASKS]
            tags = {f"{u}-{uuid.uuid4().hex[:8]}": u for u in part}
            payload = [{"keyword": unique[u][0], "location_code": unique[u][1], "language_code": unique[u][2],
                        "tag": tag} for tag, u in tags.items()]
            try:
                posted = await self._post(_SERP, SERP_ENDPOINT, payload, keyword_count=len(payload),
                                          location_code=payload[0]["location_code"])
            except (DFSCallError, BudgetExceeded) as err:
                if isinstance(err, BudgetExceeded) or err.fatal:
                    if not all_posted:
                        raise
                    for rest in todo[start:]:
                        await settle(rest, err)
                    break
                for u in tags.values():
                    await settle(u, err)
                continue
            all_posted.extend(posted)
            all_tags.update(tags)
        return all_posted, all_tags

    async def _serps_from_cache(self, unique: list[tuple[str, int, str]], todo: list[int], settle) -> list[int]:
        """Settle the asks the fresh store or a paid, uncollected task answers; the asks still to post."""
        by_loc: dict[int, list[int]] = {}
        for u in todo:
            by_loc.setdefault(unique[u][1], []).append(u)
        left: list[int] = []
        for loc, us in by_loc.items():
            market = LOCATION_CODE_TO_MARKET.get(loc)
            if market is None:
                left.extend(us)
                continue
            hits = await self.cache.serps([unique[u][0] for u in us], market)
            for u in us:
                if unique[u][0] in hits:
                    await settle(u, hits[unique[u][0]])
            await self.cache.log_hits(SERP_ENDPOINT, count=sum(1 for u in us if unique[u][0] in hits),
                                      location_code=loc, meta={"queue": "standard"})
            missing = [u for u in us if unique[u][0] not in hits]
            recovered: set[int] = set()
            by_kw = {unique[u][0]: u for u in missing}
            for task_keywords, data in await self._recover(_SERP, SERP_ENDPOINT, loc, list(by_kw)):
                for kw in task_keywords:
                    if kw in by_kw and by_kw[kw] not in recovered:
                        recovered.add(by_kw[kw])
                        await settle(by_kw[kw], data)
            await self.cache.log_hits(SERP_ENDPOINT, count=len(recovered), location_code=loc,
                                      meta={"queue": "standard", "source": "recovered_task"})
            left.extend(u for u in missing if u not in recovered)
        return left


__all__ = ["StandardQueue", "QueuedTaskFailed", "research_queue_mode", "QUEUE_ENV", "VOLUME_ENDPOINT", "SERP_ENDPOINT",
           "parse_post", "parse_ready", "parse_volumes", "posted_meta", "task_get_state"]
