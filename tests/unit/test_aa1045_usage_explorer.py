"""AA-1045 — the usage explorer: dimensions (usage_dims), the hourly rollup builder (usage_rollup), the rollup job, the
explorer query (usage_explorer), the Calls drill-down filters (ai_gateway_calls) and the route policy. Pure: no database
(the SQL runs in tests/integration/test_aa1045_usage_rollup_db.py)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import ai_gateway_calls as calls
from api.services import usage_dims as ud
from api.services import usage_explorer as ue
from api.services import usage_rollup as ur


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# ── usage_dims ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_every_stored_dimension_has_an_answer_or_none_per_ledger():
    for s in ud.SOURCES:
        for d in ud.STORED_DIMS:
            e = ud.dim_sql(s, d)
            assert e is None or isinstance(e, str)
        assert ud.dim_sql(s, "status") is not None and ud.dim_sql(s, "app") is not None
    assert ud.dim_sql("llm", "error_class") == "error_class"
    assert ud.dim_sql("jev", "error_class") is None
    with pytest.raises(ValueError):
        ud.dim_sql("crm", "app")


def test_jev_status_is_the_calls_rule():
    from api.services.ai_gateway import JEV_STATUS_SQL
    assert ud.dim_sql("jev", "status") == JEV_STATUS_SQL


def test_fallback_rule_is_the_route_trace_rung():
    # answered rows only, on the route trace's fallback rung (imported, not copied)
    from shared.llm_client import route_watch
    assert ud.FALLBACK_SQL == f"({route_watch.OK} AND {route_watch.FALLBACK_RUNG})"
    for part in ("fallback_used", "attempt_no, 1) > 1", "route_position"):
        assert part in ud.FALLBACK_SQL


def test_product_matches_route_chains():
    from api.services.llm_route_chains import product_of
    for stage in ("t9_write", "a3_atomize", "s1_rewrite", "tp_compose", "supply_content_writer",
                  "writing_memory_mine", "segment_research", "f10_embed"):
        assert ud.product_of(stage) == product_of(stage)
    assert ud.product_of("crm_draft_reply") == "crm"
    assert ud.product_of("zz_unknown") == "other" and ud.product_of(None) == "other"
    sql = ud.product_sql("stage")
    assert sql.startswith("(CASE ") and "ELSE 'other' END)" in sql
    assert "LIKE 't9\\_%'" in sql and "= 'supply_content_writer'" in sql and "'crm\\_%'" in sql


def test_histogram_and_percentile():
    assert ud.HIST_BUCKETS == 20 and ud.hist_sql().count("count(*) FILTER") == 20
    h = [0] * 20
    assert ud.percentile_ms(h) is None
    h[0] = 100                     # every call under 100 ms
    assert 0 <= ud.percentile_ms(h) <= 100
    h = [0] * 20
    h[3] = 90                      # [500, 1000)
    h[5] = 10                      # [1500, 2000)
    assert 1500 <= ud.percentile_ms(h) <= 2000
    assert ud.percentile_ms(h, 0.5) < 1000
    h = [0] * 20
    h[19] = 5                      # past the last bound: its lower bound
    assert ud.percentile_ms(h) == ud.LATENCY_BOUNDS_MS[-1]
    assert ud.merge_hist([1] * 20, [2] * 20) == [3] * 20
    assert ud.merge_hist(None, [1, 2]) == [1, 2] + [0] * 18


# ── usage_rollup ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_insert_sql_reads_every_ledger_and_leaves_jev_llm_rows_out():
    sql = ur.insert_sql()
    for t in ("shared.llm_call_log", "shared.decision_log", "shared.dfs_call_log", "geo.geocode_call"):
        assert f"FROM {t}" in sql
    assert "coalesce(provider, '') <> 'typesafe'" in sql
    assert "date_trunc('hour', u.at, 'UTC')" in sql
    assert sql.count("UNION ALL") == 3
    assert "$1" in sql and "$2" in sql and "$3" not in sql


def test_hours_chunks_and_clamp():
    now = _utc(2026, 11, 4, 10, 25)
    assert ur.floor_hour(now) == _utc(2026, 11, 4, 10)
    assert ur.ceil_hour(now) == _utc(2026, 11, 4, 11) and ur.ceil_hour(_utc(2026, 11, 4, 10)) == _utc(2026, 11, 4, 10)
    parts = ur.chunks(_utc(2026, 11, 1), _utc(2026, 11, 3, 6))
    assert parts[0] == (_utc(2026, 11, 1), _utc(2026, 11, 2)) and parts[-1] == (_utc(2026, 11, 3), _utc(2026, 11, 3, 6))
    a, b = ur.clamp_range(_utc(2026, 11, 4, 3, 30), _utc(2026, 11, 4, 8, 10), now=now)
    assert (a, b) == (_utc(2026, 11, 4, 3), _utc(2026, 11, 4, 9))
    a, b = ur.clamp_range(_utc(2020, 1, 1), _utc(2030, 1, 1), now=now)     # raw window .. current hour's end
    assert b == _utc(2026, 11, 4, 11) and a > now - ur.RAW_WINDOW
    with pytest.raises(ValueError):
        ur.clamp_range(_utc(2020, 1, 1), _utc(2020, 2, 1), now=now)


def test_compare_is_exact():
    raw = {"llm": {"ledger_rows": 3, "requests": 3, "tokens_in": 30, "tokens_out": 60, "cost_usd": Decimal("0.123456")}}
    same = {"llm": {**raw["llm"]}}
    out = ur.compare(raw, same)
    assert out["ok"] and all(s["equal"] for s in out["sources"])
    off = {"llm": {**raw["llm"], "cost_usd": Decimal("0.123457")}}
    out = ur.compare(raw, off)
    assert not out["ok"] and out["sources"][0]["diff"] == {"cost_usd": "0.000001"}
    assert not ur.compare({"dfs": {"ledger_rows": 1, "requests": 1}}, {})["ok"]


# ── the job ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_job_registered_background_and_exempt():
    from services import jobs  # noqa: F401
    from services.jobs import ai_usage_rollup_job as job
    from shared.jobs import registry

    k = registry.get_kind(job.KIND)
    assert k is not None and k.concurrency == 1 and k.background and k.deploy_gate_exempt and not k.deploy_blocking


def test_job_plan_modes():
    from services.jobs import ai_usage_rollup_job as job
    from shared.jobs.registry import NonRetryable

    now = _utc(2026, 11, 4, 10, 25)
    mode, a, b = job.plan({"mode": "schedule"}, now)
    assert mode == "schedule" and b == _utc(2026, 11, 4, 11) and b - a == timedelta(hours=job.LATE_HOURS)
    mode, a, b = job.plan({"mode": "backfill"}, now)
    assert mode == "backfill" and b == _utc(2026, 11, 4, 11) and a >= now - ur.RAW_WINDOW
    mode, a, b = job.plan({"mode": "manual", "from": "2026-11-01T00:00:00+00:00", "to": "2026-11-02T00:00:00Z"}, now)
    assert (a, b) == (_utc(2026, 11, 1), _utc(2026, 11, 2))
    for bad in ({"mode": "zz"}, {"mode": "manual"}, {"mode": "manual", "from": "x", "to": "y"},
                {"mode": "manual", "from": "2020-01-01T00:00:00Z", "to": "2020-01-02T00:00:00Z"}):
        with pytest.raises(NonRetryable):
            job.plan(bad, now)


@pytest.mark.asyncio
async def test_enqueue_scheduled_backfill_once_and_quarter_hours(monkeypatch):
    from services.jobs import ai_usage_rollup_job as job

    now = _utc(2026, 11, 4, 10, 37)
    assert job.schedule_key(now) == "ai_usage_rollup:2026-11-04T10:30"
    enq = AsyncMock(side_effect=[("j-1", True), ("j-2", True)])
    monkeypatch.setattr(job, "enqueue", enq)
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=[True, False, False])     # table, backfill key, quarter key
    out = await job.enqueue_scheduled(pool, now=now)
    assert out == [{"job_id": "j-1", "mode": "backfill"}, {"job_id": "j-2", "mode": "schedule"}]
    assert enq.await_args_list[0].kwargs["idempotency_key"] == job.BACKFILL_KEY
    assert enq.await_args_list[1].kwargs["idempotency_key"] == "ai_usage_rollup:2026-11-04T10:30"
    # before CIS 372: nothing
    pool.fetchval = AsyncMock(return_value=False)
    enq.reset_mock()
    assert await job.enqueue_scheduled(pool, now=now) == [] and enq.await_count == 0
    # both keys exist: nothing
    pool.fetchval = AsyncMock(side_effect=[True, True, True])
    assert await job.enqueue_scheduled(pool, now=now) == [] and enq.await_count == 0


@pytest.mark.asyncio
async def test_sampler_part(monkeypatch):
    from api.core import ops_metrics_sampler as sampler
    from services.jobs import ai_usage_rollup_job as job

    monkeypatch.setattr(job, "enqueue_scheduled", AsyncMock(return_value=[{"job_id": "j", "mode": "schedule"}]))
    out: dict = {}
    await sampler._ai_usage_rollup_part(MagicMock(), {job.KIND: object()}, datetime.now(timezone.utc), out)
    assert out == {"ai_usage_rollup": [{"job_id": "j", "mode": "schedule"}]}
    out = {}
    await sampler._ai_usage_rollup_part(MagicMock(), {}, datetime.now(timezone.utc), out)
    assert out == {}
    monkeypatch.setenv(job.SWITCH_ENV, "off")
    await sampler._ai_usage_rollup_part(MagicMock(), {job.KIND: object()}, datetime.now(timezone.utc), out)
    assert out == {}


# ── the explorer query ─────────────────────────────────────────────────────────────────────────────────────────────

def test_parse_query_defaults_and_filters():
    q = ue.parse_query({})
    assert (q.range, q.granularity, q.group, q.sort, q.dir) == ("7d", "day", ("provider",), "cost_usd", "desc")
    q = ue.parse_query({"range": "custom", "from": "2026-10-01", "to": "2026-10-31", "granularity": "week",
                        "group": "provider,stage", "f_app": "cis,tripplanner", "f_tenant": "(none)",
                        "f_status": "ok", "sort": "stage", "dir": "asc", "per_unit": "1"})
    assert q.group == ("provider", "stage") and q.filters["app"] == ("cis", "tripplanner")
    assert q.filters["tenant"] == ("(none)",) and q.per_unit and q.sort == "stage"


@pytest.mark.parametrize("params", [
    {"range": "zz"}, {"range": "custom"}, {"range": "custom", "from": "2026-10-05", "to": "2026-10-01"},
    {"range": "custom", "from": "2024-01-01", "to": "2026-01-01"}, {"granularity": "minute"},
    {"group": "provider,stage,model"}, {"group": "colour"}, {"f_tenant": "not-a-uuid"},
    {"f_app": "bad;value"}, {"sort": "stage"}, {"dir": "up"}, {"f_model": ",".join(f"m{i}" for i in range(60))},
])
def test_parse_query_refuses(params):
    with pytest.raises(ValueError):
        ue.parse_query(params)


def test_resolve_range_in_ict():
    now = _utc(2026, 11, 4, 20, 10)          # 03:10 ICT on 5 Nov
    a, b = ue.resolve_range(ue.parse_query({"range": "today"}), now)
    assert a == _utc(2026, 11, 4, 17) and b == _utc(2026, 11, 4, 21)
    a, b = ue.resolve_range(ue.parse_query({"range": "this_month"}), now)
    assert a == _utc(2026, 10, 31, 17)
    a, b = ue.resolve_range(ue.parse_query({"range": "last_month"}), now)
    assert (a, b) == (_utc(2026, 9, 30, 17), _utc(2026, 10, 31, 17))
    a, b = ue.resolve_range(ue.parse_query({"range": "7d"}), now)
    assert b - a == timedelta(days=7)
    a, b = ue.resolve_range(ue.parse_query({"range": "custom", "from": "2026-10-01", "to": "2026-10-01"}), now)
    assert (a, b) == (_utc(2026, 9, 30, 17), _utc(2026, 10, 1, 17))
    q = ue.parse_query({"range": "custom", "from": "2026-08-01", "to": "2026-10-01", "granularity": "hour"})
    with pytest.raises(ValueError):
        ue.check_shape(q, *ue.resolve_range(q, now))


def test_aggregate_sql_filters_and_family_join():
    q = ue.parse_query({"group": "provider,time", "f_app": "cis,(none)", "granularity": "day"})
    sql, args = ue.aggregate_sql(q, _utc(2026, 11, 1), _utc(2026, 11, 2), by=q.group)
    assert "fam" not in sql and "coalesce((app)::text, '') = ANY($3::text[])" in sql
    assert args[2] == ["cis", ""] and "date_trunc('day', hour, 'Asia/Ho_Chi_Minh') AS d_time" in sql
    q = ue.parse_query({"group": "family", "f_family": "claude"})
    sql, _ = ue.aggregate_sql(q, _utc(2026, 11, 1), _utc(2026, 11, 2), by=q.group, hist=True)
    assert "LEFT JOIN fam ON fam_key = model" in sql and "unnest(latency_hist) WITH ORDINALITY" in sql
    sql, _ = ue.per_unit_sql(ue.parse_query({"group": "stage", "f_source": "llm"}), _utc(2026, 11, 1),
                             _utc(2026, 11, 2))
    assert "FROM shared.llm_call_log" in sql and "count(DISTINCT tour_id)" in sql and "'llm'" in sql


def test_buckets_and_finish():
    assert ue.bucket_label("2026-11-03T17:00:00+00:00", "day") == "2026-11-04"
    assert ue.bucket_label("2026-10-31T17:00:00+00:00", "month") == "2026-11"
    assert ue.bucket_label("2026-11-04T02:00:00+00:00", "hour") == "2026-11-04T09:00+07:00"
    assert ue.bucket_end("2026-10-31T17:00:00+00:00", "month") == "2026-12-01T00:00:00+07:00"
    assert ue.bucket_end("2026-11-01T17:00:00+00:00", "week") == "2026-11-09T00:00:00+07:00"
    acc = ue.new_acc()
    ue.add_into(acc, {"requests": 10, "failed_requests": 2, "answered_llm": 8, "fallback_requests": 2,
                      "cost_usd": Decimal("1.5"), "latency_ms_sum": 1000, "latency_ms_count": 4, "tokens_in": 5})
    m = ue.finish(acc, {"tours": 3, "tour_cost": Decimal("0.9"), "pieces": 0, "piece_cost": 0})
    assert m["error_rate"] == 0.2 and m["fallback_rate"] == 0.25 and m["avg_latency_ms"] == 250
    assert m["cost_per_tour"] == 0.3 and m["cost_per_piece"] is None and m["cost_usd"] == 1.5
    assert ue.finish(ue.new_acc())["error_rate"] is None


def test_bucket_span_clips_edge_buckets_to_the_range():
    # 7d at day granularity, now 14:20 ICT: the range is [10-28 15:00, 11-04 15:00) ICT, so the first and the last day
    # are cut; the drill-down must not reach outside the range (the Calls list would count rows the cell did not).
    now = datetime(2026, 11, 4, 7, 20, tzinfo=timezone.utc)
    since, until = ue.resolve_range(ue.parse_query({"range": "7d"}), now)
    first = ue.bucket_span("2026-10-27T17:00:00+00:00", "day", since, until)
    assert first == {"start": "2026-10-28T15:00:00+07:00", "end": "2026-10-29T00:00:00+07:00",
                     "label": "2026-10-28 15:00–2026-10-29", "partial": True}
    last = ue.bucket_span("2026-11-03T17:00:00+00:00", "day", since, until)
    assert (last["start"], last["end"], last["partial"]) == ("2026-11-04T00:00:00+07:00", "2026-11-04T15:00:00+07:00",
                                                             True)
    assert last["label"] == "2026-11-04–2026-11-04 15:00"
    mid = ue.bucket_span("2026-10-30T17:00:00+00:00", "day", since, until)
    assert mid == {"start": "2026-10-31T00:00:00+07:00", "end": "2026-11-01T00:00:00+07:00", "label": "2026-10-31",
                   "partial": False}
    # a month bucket of a 30d range starts at the range, not at the first of the month
    since, until = ue.resolve_range(ue.parse_query({"range": "30d"}), now)
    m = ue.bucket_span("2026-09-30T17:00:00+00:00", "month", since, until)
    assert m["start"] == since.astimezone(ue.TZ).isoformat() and m["end"] == "2026-11-01T00:00:00+07:00"
    assert m["partial"] is True
    # an hour bucket is never cut (ranges are whole hours)
    h = ue.bucket_span("2026-11-04T02:00:00+00:00", "hour", since, until)
    assert h["partial"] is False and h["label"] == "2026-11-04T09:00+07:00"


def test_sort_rows_puts_missing_last():
    rows = [{"dims": {"stage": "b"}, "cost_usd": 1.0, "p95_latency_ms": None},
            {"dims": {"stage": "a"}, "cost_usd": 3.0, "p95_latency_ms": 900},
            {"dims": {"stage": ""}, "cost_usd": 2.0, "p95_latency_ms": 100}]
    assert [r["cost_usd"] for r in ue.sort_rows(rows, "cost_usd", "desc")] == [3.0, 2.0, 1.0]
    assert [r["dims"]["stage"] for r in ue.sort_rows(rows, "stage", "asc")] == ["a", "b", ""]
    assert [r["p95_latency_ms"] for r in ue.sort_rows(rows, "p95_latency_ms", "asc")] == [100, 900, None]


def test_crm_merge_rules():
    assert ue.crm_eligible(ue.parse_query({"granularity": "hour"})) == "hour_granularity"
    assert ue.crm_eligible(ue.parse_query({"group": "tenant"})) is None      # its rows show no tenant
    assert ue.crm_eligible(ue.parse_query({"group": "stage", "f_account": "x"})) == "filter_account"
    assert ue.crm_eligible(ue.parse_query({"group": "stage,model", "f_source": "crm"})) is None
    w = ue.crm_windows(_utc(2026, 1, 1), _utc(2026, 7, 1))
    assert w[0][0] == date(2026, 1, 1) and all((b - a).days < ue.CRM_MAX_DAYS for a, b in w)
    assert w[-1][1] == date(2026, 7, 1)         # 07:00 ICT of 1 July is inside the window
    rows = ue.crm_rows([{"day": "2026-11-02", "stage": "crm_draft", "model": "m-one", "calls": 5, "failed_calls": 1,
                         "input_tokens": 100, "output_tokens": 50, "cost_usd": "0.25"},
                        {"day": "bad"}, "zz"], {"m-one": "fam-one"})
    assert [(r["status"], r["requests"]) for r in rows] == [("ok", 4), ("error", 1)]
    assert rows[0]["cost_usd"] == Decimal("0.25") and rows[1]["cost_usd"] == 0 and rows[0]["family"] == "fam-one"
    q = ue.parse_query({"f_status": "ok"})
    assert ue.crm_keep(rows[0], q) and not ue.crm_keep(rows[1], q)
    assert ue.crm_bucket(date(2026, 11, 4), "week") == "2026-11-01T17:00:00+00:00"
    assert ue.crm_bucket(date(2026, 11, 4), "month") == "2026-10-31T17:00:00+00:00"


def test_saved_view_check_and_month():
    name, q = ue.check_view("  Weekly cost ", {"range": "7d", "group": "provider", "f_app": "cis", "sort": ""})
    assert name == "Weekly cost" and q == {"range": "7d", "group": "provider", "f_app": "cis"}
    for bad in (("", {}), ("x" * 81, {}), ("ok", {"evil": "1"}), ("ok", {"range": "zz"}), ("ok", {"range": 3}),
                ("ok", [])):
        with pytest.raises(ValueError):
            ue.check_view(*bad)
    assert ue.month_range("2026-02", _utc(2026, 11, 4)) == (date(2026, 2, 1), date(2026, 3, 1))
    assert ue.month_range(None, _utc(2026, 1, 4)) == (date(2025, 12, 1), date(2026, 1, 1))
    with pytest.raises(ValueError):
        ue.month_range("2026-13", _utc(2026, 1, 4))


# ── the Calls drill-down ───────────────────────────────────────────────────────────────────────────────────────────

def test_calls_parse_drill_down_filters():
    t = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
    f = calls.parse_filters(since="2026-11-03T17:00:00+00:00", until="2026-11-04T17:00:00+00:00", tenant=t.upper(),
                            model="m-one", account="(none)", product="tenant_content")
    assert f.tenant == t and f.since == "2026-11-03T17:00:00+00:00"
    assert f.dims() == {"tenant": t, "model": "m-one", "account": "(none)", "product": "tenant_content"}
    for bad in ({"since": "2026-11-03T17:00:00+00:00"}, {"since": "2026-11-03", "until": "2026-11-04"},
                {"since": "2026-11-04T00:00:00+00:00", "until": "2026-11-03T00:00:00+00:00"},
                {"since": "2024-01-01T00:00:00+00:00", "until": "2026-01-01T00:00:00+00:00"},
                {"tenant": "nope"}, {"model": "bad;x"}):
        with pytest.raises(ValueError):
            calls.parse_filters(**bad)


def test_calls_sources_follow_the_dimensions():
    assert calls.sources_for(calls.parse_filters(error_class="credential")) == ("llm",)
    assert "crm" not in calls.sources_for(calls.parse_filters(tenant="6f9619ff-8b86-d011-b42d-00c04fc964ff"))
    assert calls.sources_for(calls.parse_filters(product="crm")) == ("llm", "jev", "dfs", "geocoder", "crm")
    assert "crm" not in calls.sources_for(calls.parse_filters(product="catalogue"))
    # (none) keeps every CIS ledger; the CRM is left out like the explorer leaves it out (it sends no error class)
    assert calls.sources_for(calls.parse_filters(error_class="(none)")) == ("llm", "jev", "dfs", "geocoder")


def test_calls_source_sql_uses_the_explorer_expressions():
    f = calls.parse_filters(since="2026-11-03T17:00:00+00:00", until="2026-11-04T17:00:00+00:00", model="m-one",
                            account="acc-x", product="catalogue")
    sql, args = calls.source_sql("dfs", f, since=_utc(2026, 11, 3, 17), until=_utc(2026, 11, 4, 17), cursor=None,
                                 limit=5)
    assert "created_at < $2" in sql
    assert "coalesce((endpoint)::text, '') = $" in sql and "coalesce(('dataforseo')::text, '') = $" in sql
    assert "m-one" in args and "acc-x" in args and "catalogue" in args
    f = calls.parse_filters(error_class="(none)")
    sql, args = calls.source_sql("jev", f, since=_utc(2026, 11, 3), until=_utc(2026, 11, 4), cursor=None, limit=5)
    assert "error_class" not in sql and "created_at <= $2" in sql


# ── the route policy ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role,method,route,allowed", [
    ("admin", "GET", "/admin/usage-explorer/query", True),
    ("finance", "GET", "/admin/usage-explorer/query", True),
    ("ai_ops", "GET", "/admin/usage-explorer/query", True),
    ("ai_ops", "POST", "/admin/usage-explorer/views", True),
    ("content", "GET", "/admin/usage-explorer/query", False),
    ("customer_success", "GET", "/admin/usage-explorer/query", False),
    ("support", "GET", "/admin/usage-explorer/invoices", False),
    ("viewer", "POST", "/admin/usage-explorer/views", False),
    ("finance", "POST", "/admin/usage-explorer/rollup/run", False),
    ("admin", "POST", "/admin/usage-explorer/rollup/run", True),
])
def test_route_policy(role, method, route, allowed):
    from api.core.staff_authz import denial_reason, principal_from_role
    assert (denial_reason(principal_from_role(role), method, route) is None) is allowed
