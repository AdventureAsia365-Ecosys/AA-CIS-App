"""AA-925 (Q-2) — the one duration / day-marker reader (services/content_generation/day_parse.py) and its two
wrappers (prompts._estimate_day_count, seo_meta_utils.tour_days). Property tables, no hypothesis."""
import pytest

from services.content_generation import day_parse as dp
from services.content_generation.prompts import _estimate_day_count, parse_source_day_word_counts
from services.content_generation.seo_meta_utils import tour_days

# raw duration → (day_count, exact_days, sub_day, ambiguous)
DURATIONS = [
    ("14 days/13 nights", 14, 14, False, False),
    ("13 Nights 14 days", 14, 14, False, False),
    ("14 DAYS", 14, 14, False, False),
    ("4 Days / 3 Nights", 4, 4, False, False),
    ("7D/6N", 7, 7, False, False),
    ("7D6N", 7, 7, False, False),
    ("2D1N", 2, 2, False, False),
    ("2 weeks", 14, 14, False, False),
    ("Twelve Days", 12, 12, False, False),
    ("08 nights", 9, 9, False, False),
    ("1 day", 1, 1, False, False),
    ("full day", 1, 1, False, False),
    ("Day trip", 1, 1, False, False),
    ("half day", 1, None, True, False),
    ("Half-Day", 1, None, True, False),
    ("3 hours", 1, None, True, False),
    ("Approx. 6 hours", 1, None, True, False),
    ("5 hrs", 1, None, True, False),
    ("10–12 days", 10, None, False, True),
    ("10-12 Days", 10, None, False, True),
    ("", None, None, False, False),
    (None, None, None, False, False),
    ("on request", None, None, False, False),
]


@pytest.mark.parametrize("raw,count,exact,sub_day,ambiguous", DURATIONS, ids=[str(d[0]) for d in DURATIONS])
def test_parse_duration(raw, count, exact, sub_day, ambiguous):
    d = dp.parse_duration(raw)
    assert (d.day_count, d.exact_days, d.sub_day, d.ambiguous) == (count, exact, sub_day, ambiguous)


@pytest.mark.parametrize("raw,count,exact,_s,_a", DURATIONS, ids=[str(d[0]) for d in DURATIONS])
def test_wrappers_read_the_same_parser(raw, count, exact, _s, _a):
    assert _estimate_day_count(raw) == (count or 0)
    assert tour_days(raw) == exact


def test_subday_duration_no_longer_splits_into_days():
    """The AA-729 leak: '3 hours' was read as 3 days by the first-integer estimate."""
    out = parse_source_day_word_counts("Ride the cable car up to the ridge and walk back down.", "3 hours")
    assert out["used_fallback"] is True and list(out["day_word_counts"]) == [1]


@pytest.mark.parametrize("text,expected", [
    ("Day 1: a\nDay 2: b", [(1, 1, 0), (2, 2, 1)]),
    ("Days 1–3: a\nDay 4: b", [(1, 3, 0), (4, 4, 1)]),
    ("Days 1-3: a\nDay 4: b", [(1, 3, 0), (4, 4, 1)]),
    ("Day 1 - 3 hours drive to Paro\nDay 2: b", [(1, 1, 0), (2, 2, 1)]),      # a unit, not a range
    ("Day 5 - 3: odd\nDay 6: b", [(5, 5, 0), (6, 6, 1)]),                     # backwards, not a range
    ("► Day 01: a\n• Day 2 b", [(1, 1, 0), (2, 2, 1)]),
    ("Day One: a\nDay Two: b", [(1, 1, 0), (2, 2, 1)]),
    ("Easy Day 82 km Tarmac Road\nDay 1: a", [(1, 1, 1)]),                    # mid-line number (AA-346)
    ("Day 0: arrive\nDay 1: a", [(1, 1, 1)]),                                 # day 0 is not a day
    ("no markers here", []),
])
def test_source_day_markers(text, expected):
    assert dp.source_day_markers(text) == expected


def test_parse_source_day_word_counts_keeps_its_split_and_adds_markers():
    out = parse_source_day_word_counts("Days 1–2: walk the valley\nDay 3: drive home", "3 days")
    assert out["used_fallback"] is False
    assert sorted(out["day_word_counts"]) == [1, 3]          # the split is unchanged: a range is one block
    assert out["markers"] == [(1, 2, 0), (3, 3, 1)]          # the markers carry the range


def test_output_days_keeps_order_and_duplicates():
    text = "Day 1 — Paro\nArrive. Walk.\n\nDay 2 — Hike\nHike up.\n\nDay 2 — Hike again\nAgain.\n\nDay 1 — Back\nBack."
    assert dp.output_days(text) == [(1, "Paro", "Arrive."), (2, "Hike", "Hike up."), (2, "Hike again", "Again."),
                                    (1, "Back", "Back.")]


def test_day_parse_is_a_leaf_module():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(dp))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {getattr(n, "module", None) or n.names[0].name for n in top}
    assert not names & {"prompts", "graph", "seo_meta_utils", "itinerary_utils"}, names
