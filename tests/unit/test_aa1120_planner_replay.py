"""AA-1120 — the $0 planner replay script (scripts/aa1120/planner_replay.py): its pure planner on invented fixtures
(no DB, no DataForSEO). The 09/11-shaped fixture: two runs of one day buying the same places' keywords, the second
one paying again for pairs the first already stored, and SERP tasks shared by places with the same best keyword."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "aa1120" / "planner_replay.py"
spec = importlib.util.spec_from_file_location("aa1120_planner_replay", SCRIPT)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def _row(place, market, kw, rank, zone=None, mode=None):
    return {"place": place, "market": market, "keyword": kw, "rank": rank, "jev_zone": zone, "jev_mode": mode}


def test_buyable_mirrors_the_map_rules():
    rows = [_row("Zarvok Lake", "US", "zarvok lake", 0), _row("Zarvok Lake", "US", "zarvok lake kayak", 1),
            _row("Zarvok Lake", "US", "zarvok pier", 2, "reject", "enforce"),
            _row("Zarvok Lake", "US", "zarvok lilies", 3, "skipped", "rule:not_a_place"),
            _row("Zarvok Lake", "US", "zarvok lake ferry", 4, "reject", "shadow"),
            _row("Zarvok Lake", "US", "zarvok lake camp", 5), _row("Zarvok Lake", "US", "zarvok lake road", 6)]
    assert rp.buyable(rows) == {("Zarvok Lake", "US"): ["zarvok lake", "zarvok lake kayak", "zarvok lake ferry",
                                                       "zarvok lake camp"]}


def test_plan_after_dedupes_within_and_across_runs_and_skips_fresh_pairs():
    planned: set = set()
    first = {("Zarvok Lake", "US"): ["zarvok lake", "zarvok lake kayak"],
             ("Zarvok Pier", "US"): ["zarvok lake", "zarvok pier"],          # shares "zarvok lake"
             ("Melnith Gorge", "US"): ["melnith gorge"]}
    a = rp.plan_after(first, {("melnith gorge", "US")}, planned, serps=150, queue=True)
    assert a["keywords_asked"] == 5 and a["pairs_distinct"] == 4 and a["pairs_fresh_before"] == 1
    assert a["pairs_to_buy"] == 3 and a["volume_tasks"] == 1 and a["serp_requests"] == 2 and a["calls"] == 3
    # a second run of the day over the same places plans nothing for the pairs the first one planned
    b = rp.plan_after(first, {("melnith gorge", "US")}, planned, serps=0, queue=True)
    assert b["pairs_to_buy"] == 0 and b["volume_tasks"] == 0 and b["calls"] == 0
    live = rp.plan_after({("Quorra Pass", "UK"): ["quorra pass"]}, set(), planned, serps=3, queue=False)
    assert live["volume_tasks"] == 1 and live["serp_requests"] == 3


def test_before_of_and_the_reduction_on_a_replayed_day():
    ledger = [{"endpoint": "search_volume_bulk_queue", "cache_hit": False, "fetched_live": True, "n": 6, "usd": 0.36,
               "kws": 900},
              {"endpoint": "serp_advanced_queue", "cache_hit": False, "fetched_live": True, "n": 4, "usd": 0.21,
               "kws": 350},
              {"endpoint": "search_volume_bulk", "cache_hit": True, "fetched_live": False, "n": 1, "usd": 0.0,
               "kws": 40}]
    b = rp.before_of(ledger)
    assert b == {"rows": 11, "calls": 10, "usd": 0.57, "volume_tasks": 6, "volume_keywords_paid": 900,
                 "serp_requests": 4, "serp_tasks_paid": 350, "cache_hit_rows": 1}
    assert rp.reduction(10, 3) == 70.0 and rp.reduction(0, 0) == 0.0


def test_day_bounds_and_lines():
    start, end = rp.day_bounds("2026-11-09", 1)
    assert start == datetime(2026, 11, 9, tzinfo=timezone.utc) and (end - start).days == 1
    assert rp.line("total", calls=3, usd=0.1) == "RP total calls=3 usd=0.1"
