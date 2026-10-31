"""AA-804 (UI part 3) — the Calls tab API (GET /admin/ai-gateway/calls, api/services/ai_gateway_calls.py), the tour
trace (GET /admin/ai-gateway/trace?tour_id=, api/services/ai_gateway.py trace_tour) and, AA-1015, the TripPlanner
per-stage caps on the Apps board card (api/services/platform_apps.py tp_spend_today).

No database: the builders are pure and the readers run on a fake pool; the SQL runs on PostgreSQL in
tests/integration/test_aa804_ai_gateway_sql.py. The CRM is a fake crm.ops.v1 reader. Names are invented test values.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from api.services import ai_gateway as gw
from api.services import ai_gateway_calls as calls
from api.services import platform_apps
from tests.unit.test_aa804_ai_gateway_api import FakePool, _request

NOW = datetime(2026, 3, 14, 15, 30, tzinfo=timezone.utc)
JOB = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"
TOUR = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
CALL = "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"


# ── filters and cursor ─────────────────────────────────────────────────────────────────────────────────────────────

def test_parse_filters_normalises_and_refuses_bad_values():
    f = calls.parse_filters(job_id=f" {JOB.upper()} ", app="cis", provider="bedrock", status="refused",
                            stage="invented_stage", source="llm", window="7d")
    assert f.job_id == JOB and f.window == "7d" and f.provider == "bedrock"
    assert calls.parse_filters(app=" ", window="").window == calls.DEFAULT_WINDOW
    for kw in ({"job_id": "nope"}, {"tour_id": "x"}, {"app": "CIS!"}, {"provider": "acme"}, {"status": "maybe"},
               {"stage": "Bad Stage"}, {"source": "fax"}, {"window": "2y"}):
        with pytest.raises(ValueError):
            calls.parse_filters(**kw)


def test_cursor_round_trip_and_refusals():
    at = NOW - timedelta(minutes=3)
    cur = calls.make_cursor(at, f"llm:{CALL}")
    assert calls.parse_cursor(cur) == (at, f"llm:{CALL}")
    assert calls.parse_cursor(None) is None and calls.parse_cursor("") is None
    for bad in ("garbage", f"{at.isoformat()}", f"2026-03-14T10:00:00~llm:{CALL}",  # naive time
                f"{at.isoformat()}~fax:1", f"{at.isoformat()}~llm:'; drop"):
        with pytest.raises(ValueError):
            calls.parse_cursor(bad)


@pytest.mark.parametrize("kw,expected", [
    ({}, ("llm", "jev", "dfs", "geocoder", "crm")),
    ({"source": "dfs"}, ("dfs",)),
    ({"status": "truncated"}, ("llm",)),
    ({"status": "refused"}, ("llm", "dfs", "crm")),
    ({"provider": "jev"}, ("jev",)),
    ({"provider": "bedrock"}, ("llm", "crm")),
    ({"provider": "geocoder"}, ("geocoder",)),
    ({"app": "crm"}, ("crm",)),
    ({"app": "tripplanner"}, ("llm", "dfs")),
    ({"app": "cis"}, ("llm", "jev", "dfs", "geocoder")),
    ({"stage": "invented_stage"}, ("llm", "jev", "dfs")),
    ({"stage": "crm_draft"}, ("llm", "jev", "dfs", "crm")),
    ({"job_id": JOB}, ("llm", "jev", "dfs", "geocoder")),
    ({"tour_id": TOUR}, ("llm", "jev", "dfs", "geocoder")),
])
def test_sources_for_leaves_out_ledgers_that_cannot_match(kw, expected):
    assert calls.sources_for(calls.parse_filters(**kw)) == expected


# ── SQL ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _sql(source, cursor=None, tour_jobs=None, **kw):
    sql, args = calls.source_sql(source, calls.parse_filters(**kw), since=NOW - timedelta(hours=24), until=NOW,
                                 cursor=cursor, limit=101, tour_jobs=tour_jobs)
    return " ".join(sql.split()), args


def test_llm_sql_keyset_filters_and_jev_rows_left_out():
    cur = (NOW - timedelta(minutes=5), f"dfs:{CALL}")
    sql, args = _sql("llm", cursor=cur, app="tripplanner", status="refused", stage="invented_stage",
                     provider="bedrock")
    assert "FROM shared.llm_call_log" in sql and "coalesce(provider, '') <> 'typesafe'" in sql
    assert "created_at <= $3 AND (created_at < $3 OR ('llm:' || id::text) COLLATE \"C\" < $4)" in sql
    assert "coalesce(app, quality_signal ->> 'app', 'cis') = $5" in sql
    assert "coalesce(status, 'ok') = $6" in sql and "stage = $7" in sql
    assert "provider LIKE 'bedrock%'" in sql and sql.endswith("ORDER BY created_at DESC, key DESC LIMIT $8")
    assert args == [NOW - timedelta(hours=24), NOW, *cur, "tripplanner", "refused", "invented_stage", 101]
    sql, args = _sql("llm", provider="anthropic")
    assert "provider = $3" in sql and args[2] == "anthropic"


def test_tour_filter_reads_the_tour_column_and_its_jobs():
    sql, args = _sql("llm", tour_id=TOUR, tour_jobs=[JOB])
    assert "(tour_id = $4::uuid OR job_id = ANY($3::uuid[]))" in sql and args[2:4] == [[JOB], TOUR]
    sql, args = _sql("jev", tour_id=TOUR, tour_jobs=[])
    assert "job_id = ANY($3::uuid[])" in sql and "tour_id" not in sql.split("FROM")[1] and args[2] == []


def test_derived_status_of_the_ledgers_without_a_status_column():
    sql, _ = _sql("jev", status="error")
    assert "FROM shared.decision_log" in sql
    assert "CASE WHEN error IS NOT NULL OR zone = 'error' THEN 'error' ELSE 'ok' END = $3" in sql
    sql, _ = _sql("geocoder", status="error", job_id=JOB)
    assert "FROM geo.geocode_call" in sql and "called_at >= $1" in sql and "job_id = $3::uuid" in sql
    assert "ORDER BY called_at DESC" in sql


# ── rows and merge ─────────────────────────────────────────────────────────────────────────────────────────────────

def _llm_row(at, key, status="ok", cost=0.01):
    return {"key": key, "tenant_id": None, "at": at, "stage": "invented_stage", "role": "writer", "model": "model-a",
            "provider": "anthropic", "account": None, "connection_key": "invented-direct", "job_id": None,
            "tour_id": None, "tokens_in": 0, "tokens_out": 0, "cache_read_tokens": None, "cache_write_tokens": None,
            "cost_usd": cost, "stop_reason": None, "fallback_used": False, "pricing_mode": None, "status": status,
            "error_class": "credential" if status == "refused" else None, "latency_ms": 12, "attempt_no": 1,
            "app": "cis", "shadow": False}


def test_a_refused_llm_attempt_is_a_row_with_its_class_and_connection():
    r = calls.cis_row("llm", _llm_row(NOW, f"llm:{CALL}", status="refused", cost=0.0))
    assert r["id"] == f"llm:{CALL}" and r["outcome"] == "refused" and r["status"] == "refused"
    assert r["error_class"] == "credential" and r["account"] == "invented-direct" and r["cost_usd"] == 0.0


def test_crm_row_maps_the_contract_fields_and_drops_bad_rows():
    row = {"call_id": CALL, "tenant_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d", "at": NOW.isoformat(), "app": "crm",
           "stage": "crm_draft", "role": "writer", "provider": "bedrock", "model": "model-b", "connection_key": None,
           "status": "refused", "refusal_code": "budget", "error_class": None, "input_tokens": 0,
           "output_tokens": 0, "cost_usd": "0.000000", "latency_ms": 4, "is_shadow": False, "is_experiment": False,
           "job_id": "5e4f3a2b-1c0d-4e9f-8a7b-6c5d4e3f2a1b"}
    r = calls.crm_row(row)
    assert r["id"] == f"crm:{CALL}" and r["app"] == "crm" and r["status"] == "refused"
    assert r["error_class"] == "budget" and r["cost_usd"] == 0.0 and r["job_id"] is None
    assert r["detail"]["crm_job_id"] == row["job_id"]
    assert calls.crm_row({"call_id": "x", "at": NOW.isoformat()}) is None


def test_crm_query_maps_the_cis_cursor_onto_the_crm_keyset():
    f = calls.parse_filters(stage="crm_draft", status="ok", window="30d")
    since = NOW - timedelta(days=30)
    q = calls.crm_query(f, since=since, until=NOW, cursor=None, limit=101)
    assert q == {"from": "2026-02-12", "to": "2026-03-14", "limit": 101, "stage": "crm_draft", "status": "ok"}
    at = NOW - timedelta(minutes=1)
    assert calls.crm_query(f, since=since, until=NOW, cursor=(at, f"crm:{CALL}"), limit=5)["cursor"] == \
        f"{at.isoformat()}~{CALL}"
    # a cursor on another source at time T keeps every CRM row of T ("crm:" sorts before every other source)
    assert calls.crm_query(f, since=since, until=NOW, cursor=(at, "llm:abc"), limit=5)["cursor"].endswith(
        "~ffffffff-ffff-ffff-ffff-ffffffffffff")


def test_merge_page_orders_by_time_then_id_and_sets_the_cursor():
    t = NOW - timedelta(minutes=1)
    pages = {"llm": [calls.cis_row("llm", _llm_row(t, "llm:b")), calls.cis_row("llm", _llm_row(t, "llm:a"))],
             "crm": [{"id": "crm:z", "_at": t, "at": t.isoformat()}],
             "dfs": [{"id": "dfs:q", "_at": NOW, "at": NOW.isoformat()}]}
    rows, nxt = calls.merge_page(pages, 3)
    assert [r["id"] for r in rows] == ["dfs:q", "llm:b", "llm:a"]
    assert nxt == calls.make_cursor(t, "llm:a") and all("_at" not in r for r in rows)
    rows, nxt = calls.merge_page(pages, 10)
    assert [r["id"] for r in rows][-1] == "crm:z" and nxt is None


# ── list_calls ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _calls_pool(llm_rows=None, fail=()):
    return FakePool({
        "FROM shared.llm_call_log": llm_rows if llm_rows is not None else [
            _llm_row(NOW - timedelta(minutes=2), f"llm:{CALL}", status="refused", cost=0.0)],
        "FROM shared.decision_log": [],
        "FROM shared.dfs_call_log": [],
        "FROM geo.geocode_call": [],
        "FROM shared.job": [{"id": JOB, "kind": "invented_kind", "status": "succeeded", "created_at": NOW,
                             "started_at": NOW, "finished_at": NOW, "cost_usd": 0.0, "total_n": 1}],
    }, fail=fail)


def _crm_ok(rows):
    seen = []

    async def read(params):
        seen.append(params)
        return {"from": params["from"], "to": params["to"], "rows": rows, "next_cursor": None}, {}
    return read, seen


def test_list_calls_merges_cis_and_crm_rows():
    crm_rows = [{"call_id": CALL, "tenant_id": None, "at": (NOW - timedelta(minutes=1)).isoformat(),
                 "stage": "crm_draft", "role": "writer", "provider": "bedrock", "model": "m", "status": "ok",
                 "input_tokens": 1, "output_tokens": 2, "cost_usd": "0.010000", "latency_ms": 5, "is_shadow": False,
                 "is_experiment": False}]
    read, seen = _crm_ok(crm_rows)
    out = asyncio.run(calls.list_calls(_calls_pool(), calls.parse_filters(), crm_calls=read, now=NOW))
    assert [r["source"] for r in out["rows"]] == ["crm", "llm"]
    assert out["rows"][1]["status"] == "refused" and out["crm"] == {"status": "ok", "rows": 1}
    assert out["next_cursor"] is None and out["errors"] == [] and seen[0]["limit"] == calls.DEFAULT_LIMIT + 1
    assert out["window"]["key"] == "24h" and out["sources"] == list(calls.SOURCES)


def test_list_calls_falls_back_to_the_crm_aggregate():
    async def refused(params):
        return None, {"status": "unavailable", "reason": "cognito_session_required"}

    async def usage(start, end):
        assert (start, end) == (date(2026, 3, 13), date(2026, 3, 15))
        return {"status": "ok", "calls": 7, "cost_usd": 0.42, "by_model": [{"model": "m", "calls": 7,
                                                                             "cost_usd": 0.42}]}

    out = asyncio.run(calls.list_calls(_calls_pool(), calls.parse_filters(), crm_calls=refused, crm_usage=usage,
                                       now=NOW))
    assert [r["source"] for r in out["rows"]] == ["llm"]
    assert out["crm"]["status"] == "unavailable" and out["crm"]["aggregate"]["calls"] == 7


def test_list_calls_skips_the_crm_when_no_crm_row_can_match_and_contains_a_failed_ledger():
    read, seen = _crm_ok([])
    out = asyncio.run(calls.list_calls(_calls_pool(fail=("FROM shared.dfs_call_log",)),
                                       calls.parse_filters(job_id=JOB), crm_calls=read, now=NOW))
    assert seen == [] and out["crm"] == {"status": "not_asked", "reason": "filtered_out"}
    assert [e["source"] for e in out["errors"]] == ["dfs"] and len(out["rows"]) == 1


def test_list_calls_pages_with_a_cursor():
    rows = [_llm_row(NOW - timedelta(minutes=i), f"llm:{i:032x}") for i in range(1, 4)]
    pool = _calls_pool(llm_rows=rows)
    out = asyncio.run(calls.list_calls(pool, calls.parse_filters(source="llm"), limit=2, now=NOW))
    assert len(out["rows"]) == 2 and out["next_cursor"] == calls.make_cursor(rows[1]["at"], rows[1]["key"])
    # the source read asked for limit + 1 rows and no cursor on the first page
    assert pool.ran("FROM shared.llm_call_log")[0][-1] == 3


def test_list_calls_with_a_tour_reads_the_tour_jobs_first():
    pool = _calls_pool()
    asyncio.run(calls.list_calls(pool, calls.parse_filters(tour_id=TOUR), now=NOW))
    assert pool.ran("FROM shared.job") == [(TOUR, calls.MAX_TOUR_JOBS)]
    llm_args = pool.ran("FROM shared.llm_call_log")[0]
    assert [JOB] in llm_args and TOUR in llm_args


# ── tour trace ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_trace_tour_reads_every_key_of_the_tour():
    t0 = NOW - timedelta(hours=1)
    llm = dict(_llm_row(t0, "llm:x"), total_n=1, total_cost=0.01, tour_id=TOUR)
    dfs = {"at": t0 + timedelta(minutes=1), "endpoint": "invented_endpoint", "keyword": "kw", "location_code": 1,
           "keyword_count": 2, "cost_usd": 0.05, "cache_hit": False, "fetched_live": True, "tour_id": TOUR,
           "job_id": JOB, "status": "ok", "latency_ms": 800, "error": None, "stage": None, "total_n": 1,
           "total_cost": 0.05}
    pool = FakePool({
        "FROM shared.job": [{"id": JOB, "kind": "invented_kind", "status": "succeeded", "created_at": t0,
                             "started_at": t0, "finished_at": NOW, "cost_usd": 0.06, "total_n": 3}],
        "FROM shared.llm_call_log": [llm], "FROM shared.dfs_call_log": [dfs], "FROM geo.geocode_call": [],
        "FROM shared.decision_log": [],
    })
    out = asyncio.run(gw.trace_tour(pool, TOUR))
    assert out["kind"] == "tour" and out["job"] is None and out["tour"]["id"] == TOUR
    assert out["tour"]["jobs_total"] == 3 and len(out["tour"]["jobs"]) == 1 and "jobs_truncated" in out["notes"]
    assert [e["source"] for e in out["events"]] == ["llm", "dfs"] and out["totals"]["cost_usd"] == 0.06
    assert out["events"][1]["outcome"] == "live" and out["events"][1]["latency_ms"] == 800
    assert pool.ran("FROM shared.llm_call_log") == [(TOUR, [JOB], gw.TRACE_LIMIT)]
    assert pool.ran("FROM shared.decision_log") == [([JOB], gw.TRACE_LIMIT)]
    sql = " ".join(gw.TOUR_LLM_SQL.split())
    assert "JOIN acp_shared.content_piece p ON p.piece_id = l.content_piece_id" in sql
    assert "coalesce(provider, '') <> 'typesafe'" in sql


def test_trace_tour_with_unreadable_jobs_still_reads_the_tour_columns():
    pool = FakePool({"FROM shared.llm_call_log": [], "FROM shared.dfs_call_log": [], "FROM geo.geocode_call": [],
                     "FROM shared.decision_log": []}, fail=("FROM shared.job",))
    out = asyncio.run(gw.trace_tour(pool, TOUR))
    assert "jobs_unreadable" in out["notes"] and out["tour"]["jobs"] == []
    assert pool.ran("FROM shared.llm_call_log") == [(TOUR, [], gw.TRACE_LIMIT)]


# ── router ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _call_route(pool, **kw):
    from api.routers import admin_ai_gateway as router_mod

    args = {k: None for k in ("job_id", "tour_id", "app", "provider", "status", "stage", "source", "window",
                              "cursor")}
    args.update(limit=calls.DEFAULT_LIMIT, **kw)
    req = _request(pool)
    req.scope = {"state": {}}
    req.headers = {}
    return asyncio.run(router_mod.get_calls(req, **args))


def test_calls_route_answers_422_on_a_bad_filter_or_cursor():
    for kw in ({"status": "maybe"}, {"cursor": "garbage"}, {"job_id": "nope"}):
        with pytest.raises(HTTPException) as e:
            _call_route(FakePool({}), **kw)
        assert e.value.status_code == 422


def test_calls_route_without_a_cognito_session_marks_the_crm_unavailable():
    out = _call_route(_calls_pool(), source=None)
    assert out["crm"]["status"] == "unavailable" and out["crm"]["reason"] == "cognito_session_required"
    assert out["rows"][0]["status"] == "refused"


def test_trace_route_takes_exactly_one_of_job_or_tour():
    from api.routers import admin_ai_gateway as router_mod

    for kw in ({"job_id": None, "tour_id": None}, {"job_id": JOB, "tour_id": TOUR},
               {"job_id": None, "tour_id": "not-a-uuid"}):
        with pytest.raises(HTTPException) as e:
            asyncio.run(router_mod.get_trace(_request(FakePool({})), **kw))
        assert e.value.status_code == 422
    pool = FakePool({"FROM shared.job": [], "FROM shared.llm_call_log": [], "FROM shared.dfs_call_log": [],
                     "FROM geo.geocode_call": [], "FROM shared.decision_log": []})
    out = asyncio.run(router_mod.get_trace(_request(pool), job_id=None, tour_id=TOUR.upper()))
    assert out["kind"] == "tour" and out["tour"]["id"] == TOUR and out["events"] == []


# ── AA-1015: TripPlanner stage caps on the Apps board card ─────────────────────────────────────────────────────────

def test_tp_spend_today_without_stage_caps_shows_the_combined_cap():
    s = platform_apps.tp_spend_today([{"stage": "tp_compose", "calls": 3, "cost_usd": 4.1}])
    assert s["cap_usd"] == 5.0 and s["pct_of_cap"] == 82.0 and s["level"] == "warn"
    assert s["near_cap"] is True and s["near_cap_pct"] == 80 and s["stage_caps_set"] is False
    assert [(x["stage"], x["cap_usd"], x["cost_usd"]) for x in s["stage_caps"]] == [
        ("tp_compose", None, 4.1), ("tp_search_embed", None, 0.0)]
    assert platform_apps.tp_spend_today([])["near_cap"] is False


def test_tp_spend_today_with_a_stage_cap_flags_that_stage():
    s = platform_apps.tp_spend_today([{"stage": "tp_search_embed", "calls": 9, "cost_usd": 0.9},
                                      {"stage": "tp_compose", "calls": 1, "cost_usd": 0.1}],
                                     stage_caps={"tp_compose": 4.0, "tp_search_embed": 1.0})
    by = {x["stage"]: x for x in s["stage_caps"]}
    assert by["tp_search_embed"]["near_cap"] is True and by["tp_search_embed"]["pct_of_cap"] == 90.0
    assert by["tp_search_embed"]["level"] == "warn" and by["tp_compose"]["near_cap"] is False
    assert s["near_cap"] is True and s["stage_caps_set"] is True and s["level"] == "ok"


def test_the_mirrored_stage_caps_match_the_online_stages():
    from api.core import app_registry

    assert set(app_registry.TP_STAGE_CAPS_USD) == set(app_registry.TP_ONLINE_STAGES)
    assert 0 < app_registry.TP_NEAR_CAP_RATIO < 1
