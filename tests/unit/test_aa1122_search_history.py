"""AA-1122 items 1-2 — the monthly volume history (search_history) and the close-variant demand groups.

No DB, no DataForSEO: invented keywords and fake answers only."""
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.acp_contract import segment_research_batch as rb
from services.seo_intelligence import search_history as sh
from services.seo_intelligence.dataforseo_client import DataForSEOClient


def _row(kw, months, vol=None):
    return {"keyword": kw, "search_volume": vol,
            "monthly_searches": [{"year": y, "month": m, "search_volume": v} for y, m, v in months]}


def _answer(rows):
    return {"status_code": 20000, "tasks": [{"status_code": 20000, "result": rows}]}


YEAR = [(2026, m, 100 * m) for m in range(1, 13)]   # Jan 100 ... Dec 1200


def test_series_of_sorts_dedupes_and_drops_bad_entries():
    row = {"monthly_searches": [{"year": 2026, "month": 3, "search_volume": 30},
                                {"year": 2026, "month": 1, "search_volume": 10},
                                {"year": 2026, "month": 3, "search_volume": 99},    # repeat: first kept
                                {"year": 2026, "month": 13, "search_volume": 5},    # no such month
                                {"year": "x", "month": 2},
                                {"year": 2026, "month": 2, "search_volume": -4},    # bad number -> None
                                "junk"]}
    assert sh.series_of(row) == [(date(2026, 1, 1), 10), (date(2026, 2, 1), None), (date(2026, 3, 1), 30)]
    assert sh.series_of({}) == []


def test_monthly_from_answer_limits_to_asked_keywords():
    data = _answer([_row("zz amber falls", YEAR), _row("zz other ridge", YEAR), {"keyword": "zz none"}])
    got = sh.monthly_from_answer(data, ["zz amber falls", "zz none"])
    assert list(got) == ["zz amber falls"] and len(got["zz amber falls"]) == 12
    assert sh.monthly_from_answer({"tasks": []}) == {}


def test_buffer_pops_per_location_and_keyword():
    buf = sh.MonthlyBuffer()
    buf.keep(2840, "search_volume_bulk", {"a": [(date(2026, 1, 1), 1)], "b": [(date(2026, 1, 1), 2)]})
    buf.keep(2826, "search_volume_bulk", {"a": [(date(2026, 1, 1), 3)]})
    assert buf.pop(2840, ["a", "zz"]) == {"a": ("search_volume_bulk", [(date(2026, 1, 1), 1)])}
    assert len(buf) == 2 and buf.pop(None) == {}
    assert list(buf.pop(2840)) == ["b"]


def test_rows_for_skips_keywords_the_table_refuses():
    rows = sh.rows_for("US", {"x" * 201: ("s", [(date(2026, 1, 1), 1)]), "ok": ("s", [(date(2026, 1, 1), 2)])})
    assert rows == [("ok", "US", date(2026, 1, 1), 2, "s")]


@pytest.mark.asyncio
async def test_write_never_raises_and_returns_rows_offered():
    conn = MagicMock(spec=["executemany"])      # a bare connection (no acquire)
    conn.executemany = AsyncMock()
    assert await sh.write(conn, "US", {"k": ("s", [(date(2026, 1, 1), 5)])}) == 1
    sql, rows = conn.executemany.await_args.args
    assert "ON CONFLICT (keyword, market, month) DO NOTHING" in sql and rows == [("k", "US", date(2026, 1, 1), 5, "s")]
    conn.executemany = AsyncMock(side_effect=RuntimeError("store down"))
    assert await sh.write(conn, "US", {"k": ("s", [(date(2026, 1, 1), 5)])}) == 0
    assert await sh.write(conn, "US", {}) == 0


def test_seasonality_peak_and_no_yoy_with_one_year():
    s = sh.seasonality({"zz amber falls": [(date(2026, m, 1), v) for _y, m, v in YEAR],
                        "zz amber falls trek": [(date(2026, 7, 1), 5000)]})
    assert s.peak_month == 7 and s.peak_volume == 5000     # MAX across the place's keywords, never a sum
    assert s.yoy_pct is None and len(s.months) == 12


def test_seasonality_yoy_full_years_and_same_month():
    two = {"k": [(date(2025, m, 1), 100) for m in range(1, 13)] + [(date(2026, m, 1), 150) for m in range(1, 13)]}
    assert sh.seasonality(two).yoy_pct == 50.0
    thirteen = {"k": [(date(2025, 12, 1), 200)] + [(date(2026, m, 1), 100) for m in range(1, 13)]}
    assert sh.seasonality(thirteen).yoy_pct == -50.0      # Dec 2026 against Dec 2025
    assert sh.seasonality({}).peak_month is None
    assert sh.seasonality({"k": [(date(2026, 1, 1), 0)]}).peak_month is None


def test_client_keeps_the_months_of_a_volume_answer():
    client = DataForSEOClient(login="zz", password="zz")
    client._precheck = MagicMock()
    client._request = AsyncMock(return_value=(_answer([_row("zz amber falls", YEAR, vol=650)]), 5))
    client._log_live = MagicMock()
    import asyncio
    out = asyncio.run(client.fetch_volumes_bulk(["zz amber falls", "zz none"], 2840, "en"))
    assert out == {"zz amber falls": 650, "zz none": None}
    got = client.monthly.pop(2840, ["zz amber falls", "zz none"])
    assert got["zz amber falls"][0] == "search_volume_bulk" and len(got["zz amber falls"][1]) == 12


@pytest.mark.asyncio
async def test_live_purchase_hands_the_months_to_the_store():
    client = MagicMock()
    client.monthly = sh.MonthlyBuffer()

    async def fetch(kws, loc, lang, raise_on_error=False):
        client.monthly.keep(loc, "search_volume_bulk", {"zz amber falls": [(date(2026, 1, 1), 40)]})
        return {"zz amber falls": 40}
    client.fetch_volumes_bulk = fetch
    cache = MagicMock()
    cache.volumes = AsyncMock(return_value={})
    cache.put_volumes = AsyncMock()
    out = await rb._purchase_volumes(["zz amber falls"], 2840, "en", client, None, cache)
    assert out == {"zz amber falls": 40}
    args, kwargs = cache.put_volumes.await_args
    assert args == ("US", {"zz amber falls": 40})
    assert kwargs["history"] == {"zz amber falls": ("search_volume_bulk", [(date(2026, 1, 1), 40)])}


def test_queue_history_labels_the_queue_endpoint():
    from services.seo_intelligence.dfs_queue import VOLUME_ENDPOINT, queue_history
    got = queue_history(_answer([_row("zz amber falls", YEAR)]), ["zz amber falls"])
    assert got["zz amber falls"][0] == VOLUME_ENDPOINT


# ── item 2: close variants ───────────────────────────────────────────────────────────────────────────────────────────
def test_demand_groups_join_identical_volumes_only():
    per = {"zz tiger cliff hike": {"US": 2400, "UK": 590}, "zz tigers cliff hike": {"US": 2400, "UK": 590},
           "zz tiger cliff": {"US": 2400, "UK": 880}, "zz cliff cafe": {"US": 0}}
    groups = rb.demand_groups(["zz tiger cliff", "zz tiger cliff hike", "zz tigers cliff hike", "zz cliff cafe",
                               "zz unknown"], per)
    assert groups == [["zz tiger cliff"], ["zz tiger cliff hike", "zz tigers cliff hike"], ["zz cliff cafe"],
                      ["zz unknown"]]


def test_best_keyword_prefers_a_fresh_serp_inside_the_group_never_the_volume():
    place = rb._Place("Zz Tiger Cliff", [], [], keywords=["zz tigers cliff hike", "zz tiger cliff hike"])
    per = {"zz tigers cliff hike": {"US": 2400}, "zz tiger cliff hike": {"US": 2400}}
    best = {"zz tigers cliff hike": 2400, "zz tiger cliff hike": 2400}
    assert rb.best_keyword(place, best, per_market=per) == "zz tigers cliff hike"            # map order
    assert rb.best_keyword(place, best, serp_fresh={"zz tiger cliff hike"},
                           per_market=per) == "zz tiger cliff hike"                          # SERP presence


def test_best_keyword_group_with_more_demand_wins_and_none_without_volume():
    place = rb._Place("Zz Amber", [], [], keywords=["zz amber", "zz amber falls", "zz amber falls walk"])
    per = {"zz amber": {"US": 50}, "zz amber falls": {"US": 900}, "zz amber falls walk": {"US": 900}}
    best = {k: v["US"] for k, v in per.items()}
    assert rb.best_keyword(place, best, per_market=per) == "zz amber falls"
    assert rb.best_keyword(place, {}, per_market={}) is None
    assert rb.best_keyword(rb._Place("x", [], []), best) is None
