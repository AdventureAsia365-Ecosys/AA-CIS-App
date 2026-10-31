"""AA-884 — the Platform Apps aggregator (api/services/platform_apps.py) with fakes: no DB, no network."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from api.core import readiness
from api.services import platform_apps as pa

SHA = "a" * 40


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.sets = 0

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        self.sets += 1
        self.store[key] = value
        return True


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("APP_GIT_SHA", SHA)
    readiness.reset_cache()
    pa._task_revision.clear()
    pa._task_revision["value"] = 41
    monkeypatch.setattr("shared.jobs.worker.deploy_gate_status",
                        AsyncMock(return_value={"active": 0, "by_status": {}, "safe_to_deploy": True,
                                                "exempt_kinds": []}))
    yield
    readiness.reset_cache()


def _workers(*shas, alive=True):
    return {"workers": [{"worker_id": f"w{i}", "git_sha": s, "task_revision": 41, "running_jobs": 0,
                         "last_seen_at": datetime(2026, 10, 19, tzinfo=timezone.utc), "alive": alive}
                        for i, s in enumerate(shas)]}


def _pool(tp_rows=(), schema=readiness.REQUIRED_CIS_SCHEMA):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=list(tp_rows))
    pool.fetchval = AsyncMock(return_value=schema)
    return pool


def _booking_transport(health=(200, {"status": "ok", "git_sha": "b" * 40}),
                       ready=(200, {"status": "ok", "db": "ok", "schema_version": "bk-015"}), exc=None):
    def handler(request):
        if exc is not None:
            raise exc
        code, body = health if request.url.path.endswith("/health") else ready
        return httpx.Response(code, json=body)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_cis_parity_true_when_every_live_worker_runs_the_api_sha(monkeypatch):
    monkeypatch.setattr("shared.jobs.queue.worker_health", AsyncMock(return_value=_workers(SHA, SHA)))
    card = await pa._cis_card(_pool())
    assert card["parity"] is True and card["status"] == "ok"
    assert card["api"]["git_sha"] == SHA and card["api"]["commit_url"].endswith(f"/AA-CIS-App/commit/{SHA}")
    assert card["schema"]["version"] == str(readiness.REQUIRED_CIS_SCHEMA)
    assert card["deploy_gate"]["safe_to_deploy"] is True


@pytest.mark.asyncio
async def test_cis_parity_false_when_a_worker_differs(monkeypatch):
    monkeypatch.setattr("shared.jobs.queue.worker_health", AsyncMock(return_value=_workers(SHA, "c" * 40)))
    card = await pa._cis_card(_pool())
    assert card["parity"] is False and card["status"] == "degraded"
    assert any("another commit" in p for p in card["problems"])


@pytest.mark.asyncio
async def test_cis_no_live_worker_is_degraded(monkeypatch):
    monkeypatch.setattr("shared.jobs.queue.worker_health", AsyncMock(return_value=_workers(SHA, alive=False)))
    card = await pa._cis_card(_pool())
    assert card["workers"] == [] and card["parity"] is False and card["status"] == "degraded"


@pytest.mark.asyncio
async def test_cis_schema_behind_is_degraded(monkeypatch):
    monkeypatch.setattr("shared.jobs.queue.worker_health", AsyncMock(return_value=_workers(SHA)))
    card = await pa._cis_card(_pool(schema=readiness.REQUIRED_CIS_SCHEMA - 1))
    assert card["status"] == "degraded" and card["schema"]["status"] == "schema_behind"


@pytest.mark.asyncio
async def test_booking_ok_degraded_down_unknown():
    async with httpx.AsyncClient(transport=_booking_transport()) as c:
        card = await pa._booking_card(c)
    assert card["status"] == "ok" and card["git_sha"] == "b" * 40 and card["ready"]["schema_version"] == "bk-015"
    async with httpx.AsyncClient(transport=_booking_transport(ready=(503, {"status": "error"}))) as c:
        assert (await pa._booking_card(c))["status"] == "degraded"
    async with httpx.AsyncClient(transport=_booking_transport(health=(502, {}))) as c:
        assert (await pa._booking_card(c))["status"] == "down"
    async with httpx.AsyncClient(transport=_booking_transport(exc=httpx.ConnectTimeout("slow"))) as c:
        card = await pa._booking_card(c)
    assert card["status"] == "unknown" and card["error"] == "ConnectTimeout"


@pytest.mark.asyncio
async def test_tripplanner_spend_params_and_levels():
    pool = _pool(tp_rows=[{"stage": "tp_compose", "calls": 10, "cost_usd": 3.5},
                          {"stage": "tp_search_embed", "calls": 100, "cost_usd": 0.6}])
    card = await pa._tripplanner_card(pool)
    sql, stages, app = pool.fetch.await_args.args
    assert stages == ["tp_compose", "tp_search_embed"] and app == "tripplanner"
    assert "quality_signal ->> 'app'" in sql and "date_trunc('day', now() AT TIME ZONE 'UTC')" in sql
    spend = card["spend_today"]
    assert spend["cost_usd"] == 4.1 and spend["calls"] == 110 and spend["pct_of_cap"] == 82.0
    assert spend["level"] == "warn" and "default cap 5 USD" in spend["cap_label"]
    assert card["status"] == "unknown"
    assert card["ai"] == {"paused": False, "reason": None}   # AA-804: the spend was readable


@pytest.mark.asyncio
async def test_tripplanner_query_failure_is_contained():
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=OSError("db"))
    card = await pa._tripplanner_card(pool)
    assert card["spend_today"] is None and card["error"] == "OSError"
    # AA-804 / ADR 0009 d5: TripPlanner fails closed when spend is unreadable, so the card says AI is paused
    assert card["ai"] == {"paused": True, "reason": "spend_unreadable"}


@pytest.mark.asyncio
async def test_collect_apps_cache_hit_miss_and_refresh(monkeypatch):
    calls = []

    async def fake_build(pool, redis=None, traffic=None):
        calls.append(1)
        return {"apps": [{"id": "cis"}, {"id": "booking"}, {"id": "tripplanner"}], "generated_at": "t"}

    monkeypatch.setattr(pa, "_build", fake_build)
    redis = FakeRedis()
    first = await pa.collect_apps(None, redis)
    second = await pa.collect_apps(None, redis)
    assert first["_cached"] is False and second["_cached"] is True and len(calls) == 1
    assert json.loads(redis.store[pa.CACHE_KEY])["apps"][0]["id"] == "cis"
    third = await pa.collect_apps(None, redis, refresh=True)
    assert third["_cached"] is False and len(calls) == 2
    assert (await pa.collect_apps(None, None))["_cached"] is False and len(calls) == 3  # no Redis: always built
    one = await pa.collect_app(None, redis, "booking")
    assert one["id"] == "booking" and one["_cached"] is True
    assert await pa.collect_app(None, redis, "nope") is None
