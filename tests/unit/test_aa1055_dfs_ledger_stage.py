"""AA-1055 (S274) — every DataForSEO ledger row names its stage at the call site, and research is priced at the
endpoint it buys through.

Ledger (extends the AA-1058 guard, tests/unit/test_aa1058_cost_attribution.py, which resolves a missing stage when the
row is written):
- the free balance read writes stage `dfs_balance` whoever asks: a client with its own stage, inside a bound job kind
  (the research job reads the balance before it buys), and on a failed read;
- a research run's store-hit row carries the run's stage, not the bound job kind;
- AST guard over api / services / shared / scripts: every `record_dfs_call*` call passes `stage=`, every
  `DataForSEOClient(...)` passes `stage=` unless it is only the free `DataForSEOClient(...).fetch_balance()`.

Prices (one table, shared.cost_guard):
- the Standard queue's measured unit prices are below the live ones, and the labels research checks the run budget
  with are the queue module's labels;
- the estimate and the USD-cap selection use the prices of the run's mode; an unknown mode raises.
Mock-only: no provider call, no DB.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from services.seo_intelligence import dataforseo_client as dfs_mod
from services.seo_intelligence.dataforseo_client import BALANCE_STAGE, DataForSEOClient
from shared.llm_client import capacity

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("api", "services", "shared", "scripts")
LEDGER_WRITERS = {"record_dfs_call", "record_dfs_call_sync", "record_dfs_call_with_pool"}
# The writer module itself: its sync wrapper forwards **kwargs (the stage is resolved there, AA-1058).
WRITER_MODULE = "shared/dfs_client/call_log.py"


# ── the balance read ──────────────────────────────────────────────────────────────────────────────────────────────

class _Http:
    def __init__(self, status=200, body=None):
        self.status, self.body = status, body

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, auth=None):
        return httpx.Response(self.status, json=self.body, request=httpx.Request("GET", url))


_MONEY = {"status_code": 20000, "cost": 0, "tasks": [{"status_code": 20000,
                                                       "result": [{"money": {"balance": 7.5}}]}]}


@pytest.fixture
def rows(monkeypatch):
    m = MagicMock()
    monkeypatch.setattr(dfs_mod, "record_dfs_call_sync", m)
    return m


@pytest.mark.asyncio
async def test_balance_read_is_logged_as_dfs_balance_even_inside_a_research_job(monkeypatch, rows):
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", _Http(body=_MONEY))
    with capacity.bind_kind("segment_research"):
        money = await DataForSEOClient(login="x", password="y", stage="a3_search_demand").fetch_balance()
    assert money["balance"] == 7.5
    row = rows.call_args.kwargs
    assert row["endpoint"] == "appendix_user_data" and row["stage"] == BALANCE_STAGE == "dfs_balance"


@pytest.mark.asyncio
async def test_failed_balance_read_is_logged_as_dfs_balance_too(monkeypatch, rows):
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", _Http(status=500, body={}))
    with pytest.raises(httpx.HTTPStatusError):
        await DataForSEOClient(login="x", password="y").fetch_balance()
    row = rows.call_args.kwargs
    assert row["status"] == "error" and row["stage"] == "dfs_balance"


@pytest.mark.asyncio
async def test_a_paid_call_keeps_the_client_stage(monkeypatch, rows):
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", _Http(status=500, body={}))
    with pytest.raises(Exception):
        await DataForSEOClient(login="x", password="y", stage="a3_search_demand")._request(
            "task_get", "https://api.dataforseo.com/v3/serp/google/organic/task_get/advanced/t1")
    assert rows.call_args.kwargs["stage"] == "a3_search_demand"


# ── the research run's store-hit row ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_research_store_hit_row_carries_the_run_stage(monkeypatch):
    from services.acp_contract import segment_research_batch as rb

    logged = []

    async def fake_log(pool, **kw):
        logged.append(kw)
    monkeypatch.setattr(rb, "record_dfs_call_with_pool", fake_log)

    class _Conn:
        async def fetch(self, sql, *args):
            return [{"keyword": "zarvok lake", "search_volume": 40}]

        async def executemany(self, *a):
            return None

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

    client = DataForSEOClient(login="x", password="y", stage=rb.RESEARCH_STAGE)
    place = rb._Place("Zarvok Lake", ["hike"], ["US"], keywords=["zarvok lake"])
    with capacity.bind_kind("segment_research"):
        await rb._buy_volumes([place], [(2840, "United States", "en")], client, _Pool(), rb._RunGuard())
    assert logged and logged[0]["cache_hit"] is True and logged[0]["stage"] == "a3_search_demand"


# ── the guard ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _py_files():
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path, path.relative_to(ROOT).as_posix()


def _name(func) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _has_stage(call: ast.Call) -> bool:
    return any(k.arg == "stage" for k in call.keywords)


def _balance_only(tree) -> set[int]:
    """ids of the `DataForSEOClient(...)` calls used only as `DataForSEOClient(...).fetch_balance()`."""
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "fetch_balance"
                and isinstance(node.func.value, ast.Call) and _name(node.func.value.func) == "DataForSEOClient"):
            out.add(id(node.func.value))
    return out


def test_every_dfs_ledger_write_and_buying_client_names_its_stage():
    missing = []
    for path, rel in _py_files():
        src = path.read_text(encoding="utf-8")
        if "record_dfs_call" not in src and "DataForSEOClient(" not in src:
            continue
        tree = ast.parse(src)
        balance = _balance_only(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _name(node.func)
            if name in LEDGER_WRITERS and rel != WRITER_MODULE and not _has_stage(node):
                missing.append(f"{rel}:{node.lineno} {name}")
            if name == "DataForSEOClient" and id(node) not in balance and not _has_stage(node):
                missing.append(f"{rel}:{node.lineno} DataForSEOClient")
    assert not missing, f"DataForSEO ledger rows without a stage at the call site: {missing}"


def test_the_guard_sees_a_writer_without_stage():
    tree = ast.parse("await record_dfs_call_with_pool(pool, endpoint='x')\n"
                     "c = DataForSEOClient(budget=b)\n"
                     "m = await DataForSEOClient().fetch_balance()\n")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    balance = _balance_only(tree)
    flagged = [_name(n.func) for n in calls
               if (_name(n.func) in LEDGER_WRITERS or (_name(n.func) == "DataForSEOClient" and id(n) not in balance))
               and not _has_stage(n)]
    assert sorted(flagged) == ["DataForSEOClient", "record_dfs_call_with_pool"]


# ── one price table ───────────────────────────────────────────────────────────────────────────────────────────────

def test_queue_prices_are_the_measured_ones_and_below_live():
    from services.seo_intelligence import dfs_queue as q
    from shared.cost_guard import DFS_CALL_ESTIMATE_USD as E
    from shared.cost_guard import DFS_RESEARCH_ENDPOINTS, dfs_research_prices

    assert DFS_RESEARCH_ENDPOINTS["standard"] == (q.VOLUME_ENDPOINT, q.SERP_ENDPOINT)
    assert dfs_research_prices("standard") == (0.06, 0.0006)
    assert dfs_research_prices("live") == (E["search_volume_bulk"], E["serp_advanced"])
    assert E["search_volume_bulk_queue"] < E["search_volume_bulk"]
    assert E["serp_advanced_queue"] < E["serp_advanced"]
    with pytest.raises(ValueError):
        dfs_research_prices("priority")


def _places(n: int, markets=("US", "UK", "AU")):
    from services.acp_contract import segment_research_batch as rb
    return [rb._Place(f"p{i}", [], list(markets), serp=True) for i in range(n)]


@pytest.mark.parametrize("queue,volume,serp", [("live", 0.09, 0.002), ("standard", 0.06, 0.0006)])
def test_estimate_prices_the_run_at_its_endpoint(queue, volume, serp):
    from services.acp_contract import segment_research_batch as rb

    est = rb.estimate_plan(_places(1203), queue=queue)
    assert est["queue"] == queue and est["unit_usd"] == {"volume_task": volume, "serp": serp}
    assert est["dfs_usd_cap"] == round(est["volume_tasks_cap"] * volume + est["serp_calls_cap"] * serp, 4)
    assert est["dfs_usd"] == round(est["volume_tasks"] * volume + est["serp_calls"] * serp, 4)
    assert rb.estimate_plan([], queue=queue)["unit_usd"] == {"volume_task": volume, "serp": serp}


def test_queue_estimate_of_the_first_queued_run_matches_its_bill():
    """09/11/2026: 12 volume tasks and 1,203 SERPs; the live-priced estimate said 3.486 USD, the ledger 1.41 USD."""
    from services.acp_contract import segment_research_batch as rb

    live = 12 * rb._prices("live")[0] + 1203 * rb._prices("live")[1]
    queued = 12 * rb._prices("standard")[0] + 1203 * rb._prices("standard")[1]
    assert round(live, 3) == 3.486
    assert 1.3 <= queued <= 1.5


def test_usd_cap_selects_more_places_through_the_queue():
    from services.acp_contract import segment_research_batch as rb

    places = _places(3000)
    live = rb.select_within_usd(places, 3.0, queue="live")
    queued = rb.select_within_usd(places, 3.0, queue="standard")
    assert len(queued) > 2 * len(live) > 0
    assert rb.estimate_plan(queued, queue="standard")["dfs_usd_cap"] <= 3.0
    assert rb.estimate_plan(places[:len(queued) + 1], queue="standard")["dfs_usd_cap"] > 3.0
    with pytest.raises(ValueError):
        rb.estimate_plan(places[:3], queue="priority")


def test_admin_route_pins_the_resolved_queue_mode_into_the_job_payload(monkeypatch):
    from api.routers import admin_segment_research as route
    from services.seo_intelligence import dfs_queue as q

    monkeypatch.setenv(q.QUEUE_ENV, "standard")
    scope = route.ResearchScope(markets=["US"])
    assert route._kwargs(scope)["dfs_queue"] == "standard"
    assert route._kwargs(route.ResearchScope(markets=["US"], dfs_queue="live"))["dfs_queue"] == "live"
    monkeypatch.delenv(q.QUEUE_ENV)
    assert route._kwargs(scope)["dfs_queue"] == "live"
