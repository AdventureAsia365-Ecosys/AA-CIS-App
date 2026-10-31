"""AA-642 (S0) — DataForSEO has a cap on every path.

- fetch_all stops the remaining sub-calls after a BudgetExceeded or a fatal DFSCallError (401 / 402 / 403) and returns
  the partial result with ``last_stop``; a non-fatal failure of one sub-call is still skipped and the rest run.
- process_seo loads a run budget whenever it buys (the caller's job kind, else the dfs global row), never caches or
  persists a stopped result, and raises the budget alert on a budget stop.
- The two S1 callers pass the s1_rewrite kind; the S1 prefetch refuses to build an unbudgeted client; every
  DataForSEOClient in app code is built with a budget, except the free balance reads.
Mock-only: no DataForSEO call, no DB.
"""
import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.seo_intelligence import dataforseo_client as dfs_mod
from services.seo_intelligence import handler as seo_handler
from services.seo_intelligence import s1_prefetch
from services.seo_intelligence.dataforseo_client import DataForSEOClient
from shared.cost_guard import RunBudget

ROOT = Path(__file__).resolve().parents[2]
SEED = "Quiet Valley Ridge Walk"


def _serp_body(cost=0.002):
    return {"status_code": 20000, "cost": cost, "tasks": [{"status_code": 20000, "result": [{"items": [
        {"type": "people_also_ask", "items": [{"title": "Is the ridge walk steep?"}]},
        {"type": "related_searches", "items": ["ridge walk guide"]}]}]}]}


def _ideas_body(cost=0.09):
    return {"status_code": 20000, "cost": cost, "tasks": [{"status_code": 20000, "result": [
        {"keyword": "quiet valley ridge walk", "search_volume": 90}]}]}


class _FakeHttp:
    """httpx.AsyncClient stand-in: one scripted reply per POST, keyed by the endpoint path."""

    def __init__(self, replies: dict):
        self.replies = replies
        self.posts: list[str] = []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, auth=None, json=None):
        path = url.split("/v3/", 1)[1]
        self.posts.append(path)
        status, body = self.replies[path]
        request = httpx.Request("POST", url)
        return httpx.Response(status, json=body, request=request)


SERP = "serp/google/organic/live/advanced"
IDEAS = "keywords_data/google_ads/keywords_for_keywords/live"
VOLUME = "keywords_data/google_ads/search_volume/live"


@pytest.fixture
def http(monkeypatch):
    fake = _FakeHttp({})
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", fake)
    monkeypatch.setattr(dfs_mod, "record_dfs_call_sync", MagicMock())
    return fake


def _client(per_run=1.0):
    budget = RunBudget(provider="dfs", scope="job:s1_rewrite", per_run_usd=per_run, per_day_usd=None)
    return DataForSEOClient(login="x", password="y", tour_id="t1", budget=budget)


# ── fetch_all ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_budget_stop_skips_the_remaining_sub_calls_and_returns_the_partial_result(http):
    http.replies = {SERP: (200, _serp_body())}
    client = _client(per_run=0.05)            # the SERP (est. 0.002) fits, the ideas task (est. 0.09) does not
    out = await client.fetch_all(SEED, extra_seeds=["ridge walk"])
    assert http.posts == [SERP]                                 # the ideas task was never sent
    assert client.last_stop["reason"] == "budget" and client.last_stop["at"] == "ideas_multi"
    assert out["people_also_ask"] == ["Is the ridge walk steep?"]       # what was bought is kept
    assert out["keyword_ideas"] == [] and out["keywords"]["top_keywords"] == [SEED]
    assert client.budget.run_spent == pytest.approx(0.002)


@pytest.mark.asyncio
async def test_a_budget_stop_before_the_first_call_sends_nothing(http):
    client = _client(per_run=0.001)
    out = await client.fetch_all(SEED, extra_seeds=[])
    assert http.posts == []
    assert client.last_stop["reason"] == "budget" and client.last_stop["at"] == "serp"
    assert out["keywords"]["top_keywords"] == [SEED]


@pytest.mark.parametrize("status", [401, 402, 403])
@pytest.mark.asyncio
async def test_a_fatal_http_error_stops_the_run(http, status):
    http.replies = {SERP: (status, {"status_code": 40200, "status_message": "Payment required."})}
    client = _client()
    await client.fetch_all(SEED, extra_seeds=["ridge walk"])
    assert http.posts == [SERP]                                 # no ideas task after an auth / payment failure
    assert client.last_stop["reason"] == "fatal" and client.last_stop["at"] == "serp"


@pytest.mark.asyncio
async def test_a_fatal_status_in_a_200_body_stops_the_run(http):
    http.replies = {SERP: (200, {"status_code": 40200, "cost": 0, "status_message": "Payment required.",
                                 "tasks": [{"status_code": 40200, "status_message": "Payment required."}]})}
    client = _client()
    await client.fetch_all(SEED, extra_seeds=[])
    assert http.posts == [SERP] and client.last_stop["reason"] == "fatal"


@pytest.mark.asyncio
async def test_a_non_fatal_failure_skips_only_that_sub_call(http):
    http.replies = {SERP: (500, {"status_code": 50000}), IDEAS: (200, _ideas_body())}
    client = _client()
    out = await client.fetch_all(SEED, extra_seeds=["ridge walk"])
    assert http.posts == [SERP, IDEAS] and client.last_stop is None
    assert out["people_also_ask"] == []                         # the SERP failed; the ideas task still ran


@pytest.mark.asyncio
async def test_single_seed_path_a_fatal_volume_call_stops_serp_and_ideas(http):
    http.replies = {VOLUME: (402, {"status_code": 40200})}
    client = _client()
    out = await client.fetch_all(SEED)
    assert http.posts == [VOLUME] and client.last_stop["at"] == "keywords"
    assert out["keyword_ideas"] == []


@pytest.mark.asyncio
async def test_a_run_with_no_failure_has_no_stop(http):
    http.replies = {SERP: (200, _serp_body()), IDEAS: (200, _ideas_body())}
    client = _client()
    await client.fetch_all(SEED, extra_seeds=["ridge walk"])
    assert http.posts == [SERP, IDEAS] and client.last_stop is None
    assert client.budget.run_spent == pytest.approx(0.092)


# ── process_seo ─────────────────────────────────────────────────────────────────────────────────────────────────

class _Cache:
    def __init__(self):
        self.set = AsyncMock()

    async def get(self, key):
        return None


async def _process(fake_client, **kw):
    repo = MagicMock()
    repo.insert = AsyncMock(return_value="seo-id")
    cache = _Cache()
    loads = AsyncMock(return_value=RunBudget(provider="dfs", scope="job:x", per_run_usd=0.5, per_day_usd=None))
    alert = AsyncMock()
    with patch.object(seo_handler, "DataForSEOClient", return_value=fake_client) as ctor, \
         patch.object(seo_handler, "SeoContextRepository", return_value=repo), \
         patch.object(seo_handler, "asyncpg") as asyncpg_mock, \
         patch.object(seo_handler, "get_database_url", return_value="postgres://stub"), \
         patch.object(seo_handler.cost_guard, "load_run_budget", new=loads), \
         patch.object(seo_handler.cost_guard, "maybe_alert", new=alert):
        asyncpg_mock.connect = AsyncMock(return_value=AsyncMock())
        out = await seo_handler.process_seo(tour_id="t1", destination=SEED, seed=SEED, tenant_id=None,
                                            seo_mode="dataforseo", cache=cache, **kw)
    return out, ctor, loads, alert, cache, repo


def _fake_client(stop=None):
    c = MagicMock()
    c.fetch_all = AsyncMock(return_value={"keywords": {"top_keywords": [SEED]}, "people_also_ask": [],
                                          "keyword_ideas": []})
    c.last_stop = stop
    return c


@pytest.mark.asyncio
async def test_process_seo_buys_under_the_callers_job_kind_budget():
    out, ctor, loads, _, cache, repo = await _process(_fake_client(), budget_kind="s1_rewrite")
    assert loads.await_args.args[1:] == ("dfs", "s1_rewrite")
    assert ctor.call_args.kwargs["budget"] is not None                  # the client is built with the budget
    assert out["status"] == "fetched" and cache.set.await_count == 1 and repo.insert.await_count == 1


@pytest.mark.asyncio
async def test_process_seo_without_a_kind_uses_the_global_dfs_row():
    _, _, loads, _, _, _ = await _process(_fake_client())
    assert loads.await_args.args[1:] == ("dfs", "global")


@pytest.mark.asyncio
async def test_process_seo_uses_a_budget_it_is_given_without_loading_one():
    given = RunBudget(provider="dfs", scope="job:y", per_run_usd=0.2, per_day_usd=None)
    _, ctor, loads, _, _, _ = await _process(_fake_client(), budget=given)
    loads.assert_not_awaited()
    assert ctor.call_args.kwargs["budget"] is given


@pytest.mark.asyncio
async def test_a_budget_stop_is_neither_cached_nor_persisted_and_alerts():
    stop = {"reason": "budget", "error": "dfs per-run budget $0.50 reached", "at": "ideas_multi"}
    out, _, _, alert, cache, repo = await _process(_fake_client(stop), budget_kind="s1_rewrite")
    assert out["status"] == "budget_stopped" and out["stopped"] == stop
    assert out["data"]["keywords"]["top_keywords"] == [SEED]            # S1 still gets the partial data
    cache.set.assert_not_awaited()
    repo.insert.assert_not_awaited()
    assert "DataForSEO budget stopped" in alert.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_a_fatal_stop_is_neither_cached_nor_persisted_and_does_not_raise_the_budget_alert():
    stop = {"reason": "fatal", "error": "DataForSEO status 40200: Payment required.", "at": "serp"}
    out, _, _, alert, cache, repo = await _process(_fake_client(stop), budget_kind="s1_rewrite")
    assert out["status"] == "dfs_stopped"
    cache.set.assert_not_awaited()
    repo.insert.assert_not_awaited()
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_budget_means_no_dataforseo_call():
    fake = _fake_client()
    with patch.object(seo_handler, "DataForSEOClient", return_value=fake), \
         patch.object(seo_handler, "asyncpg") as asyncpg_mock, \
         patch.object(seo_handler, "get_database_url", return_value="postgres://stub"), \
         patch.object(seo_handler.cost_guard, "load_run_budget", new=AsyncMock(side_effect=OSError("db down"))):
        asyncpg_mock.connect = AsyncMock(return_value=AsyncMock())
        with pytest.raises(OSError):
            await seo_handler.process_seo(tour_id="t1", destination=SEED, seed=SEED, seo_mode="dataforseo",
                                          cache=_Cache())
    fake.fetch_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_and_cache_hits_never_load_a_budget():
    loads = AsyncMock()
    with patch.object(seo_handler.cost_guard, "load_run_budget", new=loads):
        out = await seo_handler.process_seo(tour_id="t1", destination=SEED, seo_mode="disabled")
    assert out["status"] == "disabled"
    loads.assert_not_awaited()


# ── callers and construction sites ──────────────────────────────────────────────────────────────────────────────

def test_both_s1_callers_pass_the_s1_rewrite_budget_kind():
    assert seo_handler.S1_REWRITE_BUDGET_KIND == "s1_rewrite"
    for path in ("api/routers/admin_pipeline.py", "services/content_generation/s1_batch.py"):
        text = (ROOT / path).read_text()
        calls = text.count("await process_seo(")
        assert calls == 1 and text.count("budget_kind=S1_REWRITE_BUDGET_KIND") == calls, path


@pytest.mark.asyncio
async def test_the_s1_prefetch_refuses_to_build_an_unbudgeted_client():
    with pytest.raises(ValueError, match="run budget"):
        await s1_prefetch.prefetch(MagicMock(), [], tenant_id="t", location_code=2840, language_code="en")


def test_every_dataforseo_client_in_app_code_has_a_budget_except_free_balance_reads():
    out = subprocess.run(["git", "grep", "-n", "DataForSEOClient(", "--", "api", "services", "shared"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    sites = [line for line in out.splitlines() if not re.search(r"class DataForSEOClient|^\S+:\d+:\s*#", line)]
    assert sites, "no construction site found"
    for line in sites:
        assert "budget=" in line or "DataForSEOClient().fetch_balance()" in line, line


def test_the_dead_dataforseo_code_is_gone():
    assert not (ROOT / "services/acp_produce/dataforseo.py").exists()
    assert not hasattr(seo_handler, "lambda_handler")
    from services.acp_produce.models import SERPProfile   # kept: Brief.serp uses it
    assert SERPProfile
