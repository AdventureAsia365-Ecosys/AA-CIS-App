"""AA-1055 (stale guard) — the research stale share per targeted market (services/acp_contract/research_coverage), its
data coverage rows, the daily ops check `research_stale_share`, and the `reatomized` research scope (a re-atomize's new
places wait for an approved run). No database, no DataForSEO / LLM / Jev call. Invented place names only."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import api.core.ops_metrics_sampler as sampler
import api.services.data_coverage.build as b
from api.services.data_coverage import registry as reg
from api.services.data_coverage import sources as src
from services.acp_contract import research_coverage as rc
from services.acp_contract import segment_research as sr
from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP
from shared import ops_checks

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


# ── markets ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_targeted_markets_resolve_like_the_slate():
    assert rc.targeted_markets([{"countries": ["UK", "AU"]}, '{"countries": ["US", "DE"]}']) == ["US", "UK", "AU", "DE"]
    # nothing usable (no config row, empty, an unmapped country, broken JSON) = the Slate's default, US
    assert rc.targeted_markets([None]) == ["US"]
    assert rc.targeted_markets([{"countries": ["XX"]}, "{not json"]) == ["US"]
    assert rc.targeted_markets([]) == []


def test_target_markets_never_records_an_unmapped_request():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"target_market": {"countries": ["XX", "UK"]}}])
    with patch("shared.dfs_client.unmapped_market.record_unmapped_market_request", AsyncMock()) as rec:
        assert asyncio.run(rc.target_markets(pool)) == ["UK"]
    rec.assert_not_awaited()
    sql = pool.fetch.await_args.args[0]
    assert "is_active" in sql and "cancelled_at IS NULL" in sql and "LEFT JOIN shared.tenant_seo_config" in sql
    assert "INSERT" not in sql.upper()


# ── one definition of stale ──────────────────────────────────────────────────────────────────────────────────────────
def test_stale_tiers_is_the_plan_rule():
    got = {("US", "volume"), ("UK", "volume")}
    assert sr.stale_tiers(got, ["US", "UK", "AU"], ("volume", "serp")) == (["AU"], True)
    assert sr.stale_tiers(got | {("US", "serp")}, ["US"], ("volume", "serp")) == ([], False)
    assert sr.stale_tiers(set(), ["UK"], ("serp",)) == ([], True)   # the SERP is US whatever market is asked


def test_the_serp_counts_against_the_us_share_only():
    volume_only = {("US", "volume"), ("UK", "volume")}
    assert rc.is_stale(volume_only, "US") and not rc.is_stale(volume_only, "UK")
    assert not rc.is_stale(volume_only | {("US", "serp")}, "US")
    assert rc.is_stale(set(), "AU")


def test_summarize_counts_the_stale_share_per_market():
    fresh = {
        "Zarvok Lake": {("US", "volume"), ("US", "serp"), ("UK", "volume")},
        "Melnith Gorge": {("US", "volume"), ("UK", "volume")},       # US SERP missing
        "Quellan Falls": {("US", "volume"), ("US", "serp")},          # never researched for the UK
        "Torvan Pass": set(),                                         # never researched
        "Ilvar Bay": {("US", "volume"), ("US", "serp"), ("UK", "volume")},
    }
    out = rc.summarize(fresh, fresh, ["US", "UK"], reatomized={"Torvan Pass", "Ilvar Bay"})
    assert out["places_in_scope"] == 5 and out["limit"] == rc.STALE_SHARE_LIMIT
    us, uk = out["by_market"]["US"], out["by_market"]["UK"]
    assert (us["places_stale"], us["stale_share"], us["above_limit"]) == (2, 0.4, True)
    assert (uk["places_stale"], uk["stale_share"], uk["above_limit"]) == (2, 0.4, True)
    # Ilvar Bay was re-atomized but is fresh everywhere: nothing to buy, so it does not wait
    assert out["waiting_after_reatomize"] == 1


def test_summarize_at_the_limit_is_not_above_it_and_empty_scope_is_zero():
    fresh = {f"P{i}": {("UK", "volume")} for i in range(4)} | {"P4": set()}
    out = rc.summarize(fresh, fresh, ["UK"])
    assert out["by_market"]["UK"] == {"places_in_scope": 5, "places_stale": 1, "stale_share": 0.2,
                                      "above_limit": False}
    assert rc.summarize([], {}, ["US"])["by_market"]["US"]["stale_share"] == 0.0


# ── the reatomized scope ─────────────────────────────────────────────────────────────────────────────────────────────
def _row(seg, place, touched, types=("sightseeing",)):
    return {"segment_id": seg, "canonical_place": place, "canonical_action": "explore",
            "activity_types": list(types), "atoms_touched": touched}


def test_reatomized_places_are_atomized_after_their_newest_research_or_never_researched():
    rows = [_row("s1", "Zarvok Lake", T0 + timedelta(days=3)),
            _row("s2", "Zarvok Lake", T0 - timedelta(days=9)),          # the newest atom counts
            _row("s3", "Melnith Gorge", T0 - timedelta(days=2)),
            _row("s4", "Torvan Pass", T0 - timedelta(days=300))]
    newest = {"Zarvok Lake": T0, "Melnith Gorge": T0}
    assert sr.reatomized_places(rows, newest) == {"Zarvok Lake", "Torvan Pass"}


class _Conn:
    def __init__(self, scope, fresh=(), newest=None, tenants=None):
        self.scope, self.fresh, self.newest, self.tenants = scope, list(fresh), newest or {}, tenants
        self.sql: list[str] = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if "FROM acp_contract.atom_segment s" in sql:
            return self.scope
        if "max(researched_at)" in sql:
            return [{"canonical_place": p, "newest": t} for p, t in self.newest.items() if p in set(args[0])]
        if "FROM acp_contract.segment_research_log" in sql:
            return [{"canonical_place": p, "market": m, "tier": t} for p, m, t in self.fresh if p in set(args[0])]
        if "shared.tenants" in sql:
            return self.tenants or []
        return []


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def fetch(self, sql, *args):
        return await self.conn.fetch(sql, *args)


@pytest.mark.asyncio
async def test_places_in_scope_reatomized_keeps_only_the_new_places():
    conn = _Conn([_row("s1", "Zarvok Lake", T0 + timedelta(days=1)), _row("s2", "Melnith Gorge", T0),
                  _row("s3", "Torvan Pass", T0)], newest={"Zarvok Lake": T0, "Melnith Gorge": T0 + timedelta(days=1)})
    pool = _Pool(conn)
    every = await sr._places_in_scope(pool, None, None, None, cached_only=True)
    assert set(every) == {"Zarvok Lake", "Melnith Gorge", "Torvan Pass"}
    new = await sr._places_in_scope(pool, None, None, None, cached_only=True, reatomized=True)
    assert new == {"Zarvok Lake": ["explore"], "Torvan Pass": ["explore"]}
    named = await sr._places_in_scope(pool, ["Torvan Pass", "Melnith Gorge"], None, None, cached_only=True,
                                      reatomized=True)
    assert named == {"Torvan Pass": ["explore"]}


@pytest.mark.asyncio
async def test_a_reatomize_preview_selects_its_stale_places_and_buys_nothing():
    conn = _Conn([_row("s1", "Zarvok Lake", T0 + timedelta(days=1)), _row("s2", "Melnith Gorge", T0)],
                 fresh=[("Zarvok Lake", "US", "volume"), ("Zarvok Lake", "US", "serp")],
                 newest={"Zarvok Lake": T0, "Melnith Gorge": T0 + timedelta(days=2)})
    with patch.object(sr, "DataForSEOClient") as dfs, \
            patch("services.acp_contract.segment_research_batch.research_batch", AsyncMock()) as batch:
        out = await sr.run_segment_research({"countries": ["US", "UK"]}, _Pool(conn), dry_run=True, reatomized=True)
    # Zarvok Lake: atomized after its research, UK volume never bought -> selected; Melnith Gorge: not re-atomized
    assert out["reatomized"] is True and out["places_in_scope"] == 1 and out["places_selected"] == 1
    assert out["estimate"]["dfs_usd_cap"] > 0 and out["cost_usd"] == 0.0
    dfs.assert_not_called()
    batch.assert_not_awaited()


def test_the_route_and_the_job_carry_the_reatomized_scope():
    from api.routers.admin_segment_research import ResearchScope, _kwargs
    from services.jobs.segment_research_job import scope_kwargs

    scope = ResearchScope(markets=["uk"], reatomized=True, tour_ids=["t-1"])
    kw = _kwargs(scope)
    assert kw["reatomized"] is True and kw["tour_ids"] == ["t-1"]
    assert scope_kwargs({"markets": ["UK"], **kw})["reatomized"] is True
    assert ResearchScope(markets=["US"]).reatomized is False


# ── measure ──────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_measure_reads_scope_log_and_markets_without_a_paid_call():
    conn = _Conn([_row("s1", "Zarvok Lake", T0), _row("s2", "Melnith Gorge", T0 + timedelta(days=3)),
                  _row("s3", "", T0)],                                       # unnamed place: excluded by rule
                 fresh=[("Zarvok Lake", "UK", "volume"), ("Zarvok Lake", "US", "volume"),
                        ("Zarvok Lake", "US", "serp")],
                 newest={"Zarvok Lake": T0 + timedelta(days=1)},
                 tenants=[{"target_market": {"countries": ["UK"]}}, {"target_market": None}])
    with patch("services.acp_contract.atom_ranking.decide", AsyncMock()) as jev:
        out = await rc.measure(_Pool(conn))
    jev.assert_not_awaited()
    assert out["markets"] == ["US", "UK"]
    assert out["places_in_scope"] == 2
    assert out["by_market"]["UK"]["places_stale"] == 1 and out["by_market"]["US"]["places_stale"] == 1
    assert out["waiting_after_reatomize"] == 1   # Melnith Gorge; Zarvok Lake was atomized before its research
    assert out["fresh_for_days"] == 365
    assert not any("INSERT" in s.upper() or "UPDATE " in s.upper() for s in conn.sql)


@pytest.mark.asyncio
async def test_read_only_pool_opens_a_bounded_read_only_transaction_under_the_gate():
    events = []

    class _Tx:
        async def __aenter__(self):
            events.append("begin")

        async def __aexit__(self, *a):
            events.append("end")

    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_Tx())
    conn.execute = AsyncMock(side_effect=lambda sql: events.append(sql))
    conn.fetch = AsyncMock(return_value=[{"n": 1}])
    gate = asyncio.Semaphore(1)
    pool = rc.ReadOnlyPool(_Pool(conn), "4s", "1s", gate=gate)
    assert await pool.fetch("SELECT 1") == [{"n": 1}]
    conn.transaction.assert_called_once_with(readonly=True)
    assert events == ["begin", "SET LOCAL statement_timeout = '4s'", "SET LOCAL lock_timeout = '1s'", "end"]
    assert not gate.locked()


# ── data coverage ────────────────────────────────────────────────────────────────────────────────────────────────────
def _summary(us=(2, 10), uk=None):
    by = {"US": {"places_in_scope": us[1], "places_stale": us[0], "stale_share": us[0] / us[1],
                 "above_limit": us[0] / us[1] > rc.STALE_SHARE_LIMIT}}
    if uk:
        by["UK"] = {"places_in_scope": uk[1], "places_stale": uk[0], "stale_share": uk[0] / uk[1],
                    "above_limit": uk[0] / uk[1] > rc.STALE_SHARE_LIMIT}
    return {"markets": list(by), "places_in_scope": us[1], "limit": rc.STALE_SHARE_LIMIT, "by_market": by,
            "waiting_after_reatomize": 3}


def test_coverage_values_flatten_every_dfs_market():
    v = rc.coverage_values(_summary(us=(2, 10), uk=(5, 10)))
    assert v["in_scope"] == 10 and v["allowed"] == 2 and v["waiting_after_reatomize"] == 3
    assert v["targeted_US"] == 1 and v["stale_US"] == 2 and v["targeted_UK"] == 1 and v["stale_UK"] == 5
    assert v["targeted_DE"] == 0 and "stale_DE" not in v
    assert rc.coverage_values(_summary(us=(3, 11)))["allowed"] == 2      # floor(0.2 x 11)


def test_one_row_per_dfs_market_plus_the_waiting_row():
    ids = {r.id for r in reg.ROWS}
    assert {f"research.stale_share.{m.lower()}" for m in DFS_LOCATION_MAP} <= ids
    assert "research.waiting_after_reatomize" in ids
    assert reg.SOURCE_BY_KEY["research"].tier == "slow" and "research" in src.FUNCTIONS
    for r in reg.ROWS:
        if r.shown_if is not None:   # shown_if reads only declared sources too
            asked = set()
            r.shown_if(lambda s, n, asked=asked: asked.add(s) or 1)
            assert asked <= set(r.sources), r.id


def _research_source(values):
    return {"research": {"status": "ok", "reason": None, "timing_ms": 3, "tier": "slow", "values": values}}


def test_stale_share_row_is_a_gap_above_twenty_percent_and_ok_at_it():
    row = next(r for r in reg.ROWS if r.id == "research.stale_share.us")
    at = b.evaluate(row, _research_source(rc.coverage_values(_summary(us=(2, 10)))), {})
    above = b.evaluate(row, _research_source(rc.coverage_values(_summary(us=(3, 10)))), {})
    assert (at["status"], at["value"], at["denominator"], at["expected_gap"], at["pct"]) == ("ok", 2, 10, 2, 20.0)
    assert above["status"] == "gap" and above["kind"] == "backlog" and above["issue"]


def test_untargeted_markets_are_hidden_and_a_failed_source_shows_every_row_na():
    values = rc.coverage_values(_summary(us=(2, 10), uk=(1, 10)))
    shown = {r.id for r in reg.ROWS if r.id.startswith("research.") and b.shown(r, _research_source(values))}
    assert shown == {"research.stale_share.us", "research.stale_share.uk", "research.waiting_after_reatomize"}
    failed = {"research": {"status": "timeout", "reason": "timeout", "timing_ms": 1, "tier": "slow", "values": {}}}
    rows = [r for r in reg.ROWS if r.id.startswith("research.")]
    assert all(b.shown(r, failed) for r in rows)
    assert all(b.evaluate(r, failed, {})["status"] == "na" for r in rows)


@pytest.mark.asyncio
async def test_the_research_source_reads_measure_through_a_bounded_read_only_pool(monkeypatch):
    seen = {}

    async def fake_measure(pool):
        seen["pool"] = pool
        return _summary(us=(4, 10))

    monkeypatch.setattr(rc, "measure", fake_measure)
    ctx = src.Ctx(MagicMock())
    out = await src.research(ctx)
    assert isinstance(seen["pool"], rc.ReadOnlyPool) and seen["pool"]._gate is ctx.gate
    assert seen["pool"]._statement_timeout == src.CIS_STATEMENT_TIMEOUT
    assert out["stale_US"] == 4 and out["allowed"] == 2


@pytest.mark.asyncio
async def test_build_hides_untargeted_market_rows(monkeypatch):
    values = rc.coverage_values(_summary(us=(4, 10)))

    def make(key):
        async def fn(ctx):
            if key == "research":
                return values
            return {"live": 1}
        return fn
    for key in list(src.FUNCTIONS):
        monkeypatch.setitem(src.FUNCTIONS, key, make(key))
    out = await b.build(MagicMock(), None, None)
    rows = {r["id"]: r for g in out["groups"] for r in g["rows"]}
    assert rows["research.stale_share.us"]["status"] == "gap"
    assert "research.stale_share.uk" not in rows and "research.waiting_after_reatomize" in rows
    from api.schemas.data_coverage import DataCoverageResponse
    DataCoverageResponse.model_validate(out)


# ── the ops check ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_check_fires_above_the_limit_and_clears_below_it():
    found = ops_checks.state_findings({"research_stale": _summary(us=(2, 10), uk=(7, 10))})
    hot = [f for f in found if f.check == "research_stale_share"]
    assert len(hot) == 1 and "UK 70.0 %" in hot[0].summary and "US" not in hot[0].details["markets"]
    assert ops_checks.state_findings({"research_stale": _summary(us=(2, 10), uk=(2, 10))}) == []
    assert ops_checks.state_findings({}) == []
    chk = ops_checks.check_for("research_stale_share")
    assert chk.page == "/admin/data-coverage" and chk.bell_every == timedelta(hours=20)
    subject, text = ops_checks.render(hot[0], "dev")
    assert subject.isascii() and len(subject) <= 100 and "segment-research/preview" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("due", [True, False])
async def test_the_sampler_measures_only_when_due_and_the_checks_read_it(due, monkeypatch):
    measure = AsyncMock(return_value=_summary(us=(9, 10)))
    monkeypatch.setattr(rc, "measure", measure)
    seen = {}

    async def fake_checks(pool, client, out):
        seen["findings"] = ops_checks.state_findings(out)

    with patch.object(sampler, "_ops_checks_part", side_effect=fake_checks):
        out = await sampler.sample_once(MagicMock(), None, kinds={}, part_timeout_s=0.5, research_stale=due)
    assert measure.await_count == (1 if due else 0)
    assert ("research_stale" in out) is due
    assert any(f.check == "research_stale_share" for f in seen["findings"]) is due


@pytest.mark.asyncio
async def test_a_failed_measure_is_logged_in_the_tick_not_raised(monkeypatch):
    monkeypatch.setattr(rc, "measure", AsyncMock(side_effect=RuntimeError("down")))
    with patch.object(sampler, "_ops_checks_part", AsyncMock()):
        out = await sampler.sample_once(MagicMock(), None, kinds={}, part_timeout_s=0.5, research_stale=True)
    assert "research_stale_error" in out and "research_stale" not in out


@pytest.mark.asyncio
async def test_the_loop_measures_on_the_first_tick_then_once_per_interval():
    seen = []

    async def fake_sample(pool, client, **kw):
        seen.append(kw["research_stale"])
        return {"sampled_at": "x"}

    app = SimpleNamespace(state=SimpleNamespace(pool=MagicMock()))
    with patch.object(sampler, "sample_once", side_effect=fake_sample), \
            patch.object(sampler, "jev_credit_tick", AsyncMock()), \
            patch.object(sampler, "provider_credit_tick", AsyncMock()):
        task = asyncio.create_task(sampler.run_ops_metrics_sampler(
            app, 0.01, 0, client_factory=lambda: object(), jev_check_every_s=3600, recompute_sweep_every_s=3600,
            research_stale_every_s=0.2))
        started = time.monotonic()
        while len(seen) < 3 or time.monotonic() - started < 0.3:
            await asyncio.sleep(0.01)
        await sampler.stop_ops_metrics_sampler(task)
    assert seen[0] is True and seen[1] is False
    assert 2 <= seen.count(True) <= 4


def test_the_daily_schedule_and_budget():
    assert rc.CHECK_EVERY_S == 86400.0 and rc.CHECK_TIMEOUT_S <= sampler.PART_TIMEOUT_S
    assert src.RESEARCH_WAIT_S < b.TIER_DEADLINE_S["slow"]
