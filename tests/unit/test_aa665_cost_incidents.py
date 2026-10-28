"""AA-665 — recorded cost incidents (api/services/cost_incidents.py) and how Cost Operations › DataForSEO reads them.

No DB: the pool is faked by SQL text; the SQL itself runs in tests/integration/test_aa665_cost_ops_sql.py.
"""
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from api.services import cost_incidents as ci
from api.services import cost_ops

UTC = timezone.utc


class _Pool:
    """fetch / fetchrow answer by SQL text; records every call."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.answers.get(sql, [])

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        rows = self.answers.get(sql, [])
        return rows[0] if rows else None


def _incident(**kw):
    base = dict(id="probe", provider="dfs", title="Probe", started_at=datetime(2025, 1, 10, 5, tzinfo=UTC),
                ended_at=datetime(2025, 1, 10, 11, tzinfo=UTC), trigger="t", scope="s", failure_point="f",
                cause="c", since_fixed=("x",), llm_stages=("a3_search_demand",), investigated="i")
    base.update(kw)
    return ci.CostIncident(**base)


# ── the records ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_records_are_well_formed():
    ids = [i.id for i in ci.INCIDENTS]
    assert len(ids) == len(set(ids))
    for i in ci.INCIDENTS:
        assert i.started_at.tzinfo is not None and i.ended_at.tzinfo is not None
        assert i.started_at < i.ended_at
        assert i.provider in {"dfs", "bedrock", "openai", "jev"}
        assert i.title and i.trigger and i.scope and i.failure_point and i.cause and i.since_fixed


def test_record_text_is_user_facing():
    """Shown in the admin UI: no issue IDs, no personal names (roles only)."""
    for i in ci.INCIDENTS:
        text = " ".join([i.title, i.trigger, i.scope, i.failure_point, i.cause, i.investigated, *i.since_fixed])
        assert not re.search(r"\bAA-\d+\b", text), i.id
        assert not re.search(r"Nghi[eệ]p|Th[uư]\b", text), i.id


def test_the_25_09_incident_is_recorded():
    (inc,) = ci.incidents_overlapping(datetime(2026, 9, 25, tzinfo=UTC), datetime(2026, 9, 26, tzinfo=UTC), "dfs")
    assert inc.id == "2026-09-25-dfs-research-sweep"
    assert inc.started_at == datetime(2026, 9, 25, 5, tzinfo=UTC) and inc.ended_at == datetime(2026, 9, 25, 11,
                                                                                                tzinfo=UTC)
    assert "a3_search_demand" in inc.llm_stages


def test_overlap_is_half_open_and_filters_provider():
    inc = _incident()
    pool = (inc, _incident(id="later", started_at=datetime(2025, 3, 1, tzinfo=UTC),
                           ended_at=datetime(2025, 3, 2, tzinfo=UTC)))
    d = datetime(2025, 1, 10, tzinfo=UTC)
    assert ci.incidents_overlapping(d, d + timedelta(days=1), incidents=pool) == [inc]
    # [since, until) touching the incident's end or start only = no overlap
    assert ci.incidents_overlapping(inc.ended_at, inc.ended_at + timedelta(hours=1), incidents=pool) == []
    assert ci.incidents_overlapping(inc.started_at - timedelta(hours=1), inc.started_at, incidents=pool) == []
    assert ci.incidents_overlapping(d, d + timedelta(days=1), "bedrock", incidents=pool) == []
    assert [i.id for i in ci.incidents_overlapping(d, datetime(2025, 4, 1, tzinfo=UTC), incidents=pool[::-1])] \
        == ["probe", "later"]


# ── the live numbers ─────────────────────────────────────────────────────────────────────────────────────────────────

def _ep(endpoint, calls, cost, first, last, kw=0, linked=0):
    return {"endpoint": endpoint, "calls": calls, "live_calls": calls, "cost_usd": cost, "keywords": kw,
            "linked_calls": linked, "first_paid_at": first, "last_paid_at": last}


@pytest.mark.asyncio
async def test_incident_view_reads_numbers_from_the_logs():
    inc = _incident()
    t = inc.started_at
    pool = _Pool({
        ci.INCIDENT_DFS_SQL: [
            _ep("search_volume_bulk", 678, 39.24, t + timedelta(minutes=5), t + timedelta(hours=3, minutes=51),
                kw=3051),
            _ep("keywords_for_keywords", 100, 9.0, t + timedelta(minutes=6), t + timedelta(hours=3)),
            _ep("appendix_user_data", 1, 0.0, None, None),
        ],
        ci.INCIDENT_LLM_SQL: [{"stage": "a3_search_demand", "calls": 7900, "cost_usd": 3.25}],
    })
    v = await ci.incident_view(pool, inc)
    assert v["dfs"]["calls"] == 779 and v["dfs"]["cost_usd"] == pytest.approx(48.24)
    assert v["dfs"]["first_paid_at"] == (t + timedelta(minutes=5)).isoformat()
    assert v["dfs"]["last_paid_at"] == (t + timedelta(hours=3, minutes=51)).isoformat()
    assert [e["endpoint"] for e in v["dfs"]["endpoints"]][0] == "search_volume_bulk"
    assert v["llm"] == {"source": "raw", "calls": 7900, "cost_usd": 3.25,
                        "stages": [{"stage": "a3_search_demand", "calls": 7900, "cost_usd": 3.25}]}
    assert v["trigger"] == "t" and v["since_fixed"] == ["x"] and v["started_at"] == t.isoformat()
    dfs_call, llm_call = pool.calls
    assert dfs_call[1] == (inc.started_at, inc.ended_at)
    assert llm_call[1] == (inc.started_at, inc.ended_at, ["a3_search_demand"])


@pytest.mark.asyncio
async def test_incident_view_falls_back_to_the_daily_rollup():
    inc = _incident()
    pool = _Pool({ci.INCIDENT_LLM_DAILY_SQL: [{"stage": "a3_search_demand", "calls": 8000, "cost_usd": 3.3}]})
    v = await ci.incident_view(pool, inc)
    assert v["llm"]["source"] == "daily" and v["llm"]["calls"] == 8000
    assert pool.calls[-1][1] == (date(2025, 1, 10), date(2025, 1, 10), ["a3_search_demand"])
    assert v["dfs"] == {"calls": 0, "live_calls": 0, "cost_usd": 0, "linked_calls": 0, "first_paid_at": None,
                        "last_paid_at": None, "endpoints": []}


@pytest.mark.asyncio
async def test_incident_view_without_llm_stages_reads_no_llm():
    v = await ci.incident_view(_Pool({}), _incident(llm_stages=()))
    assert v["llm"] == {"source": None, "calls": 0, "cost_usd": 0, "stages": []}


# ── Cost Operations › DataForSEO › Runs ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dfs_runs_carries_the_overlapping_incidents(monkeypatch):
    inc = _incident()
    monkeypatch.setattr(cost_ops, "incidents_overlapping",
                        lambda since, until, provider=None: ci.incidents_overlapping(since, until, provider, (inc,)))
    d = datetime(2025, 1, 10, tzinfo=UTC)
    out = await cost_ops.dfs_runs(_Pool({}), d, d + timedelta(days=1))
    (v,) = out["incidents"]
    assert v["id"] == "probe" and v["title"] == "Probe"
    out = await cost_ops.dfs_runs(_Pool({}), d + timedelta(days=1), d + timedelta(days=2))
    assert out["incidents"] == []


@pytest.mark.asyncio
async def test_infer_unlinked_passes_incident_windows_and_names_them(monkeypatch):
    inc = _incident()
    monkeypatch.setattr(cost_ops, "incidents_overlapping",
                        lambda since, until, provider=None: ci.incidents_overlapping(since, until, provider, (inc,)))
    d = datetime(2025, 1, 10, tzinfo=UTC)
    pool = _Pool({cost_ops.DFS_INFER_SQL: [
        {"job_id": None, "attribution": "incident", "candidate_kinds": [], "incident_id": "probe", "calls": 9,
         "live_calls": 9, "dfs_cost_usd": 4.0, "first_call_at": d, "last_call_at": d},
        {"job_id": None, "attribution": "none", "candidate_kinds": [], "incident_id": None, "calls": 1,
         "live_calls": 1, "dfs_cost_usd": 0.1, "first_call_at": d, "last_call_at": d},
    ]})
    out = await cost_ops.dfs_runs(pool, d, d + timedelta(days=1), infer_unlinked=True)
    incident, none = out["inferred"]
    assert incident["attribution"] == "incident" and incident["incident_id"] == "probe"
    assert incident["incident_title"] == "Probe" and incident["job"] is None
    assert none["incident_id"] is None and none["incident_title"] is None
    (args,) = [a for sql, a in pool.calls if sql == cost_ops.DFS_INFER_SQL]
    assert args == (d, d + timedelta(days=1), ["probe"], [inc.started_at], [inc.ended_at])


@pytest.mark.asyncio
async def test_infer_unlinked_without_incidents_passes_empty_arrays(monkeypatch):
    monkeypatch.setattr(cost_ops, "incidents_overlapping", lambda since, until, provider=None: [])
    d = datetime(2025, 1, 10, tzinfo=UTC)
    jid = uuid.uuid4()
    pool = _Pool({cost_ops.DFS_INFER_SQL: [
        {"job_id": jid, "attribution": "window", "candidate_kinds": [], "incident_id": None, "calls": 2,
         "live_calls": 2, "dfs_cost_usd": 1.0, "first_call_at": d, "last_call_at": d}]})
    out = await cost_ops.dfs_runs(pool, d, d + timedelta(days=1), infer_unlinked=True)
    assert out["incidents"] == [] and out["inferred"][0]["job_id"] == str(jid)
    (args,) = [a for sql, a in pool.calls if sql == cost_ops.DFS_INFER_SQL]
    assert args[2:] == ([], [], [])
