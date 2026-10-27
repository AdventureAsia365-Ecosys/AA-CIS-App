"""AA-990 (S253) — one ranged-heading rule for every day reader.

Live case: Sri Lanka b45db4c8 ("Maldives and Sri Lanka on a budget", writer v2, 16 days) was published with days
1-12 and 16. The source groups days 12-15 ("Day 12-15: Beach leisure"); the writer v2 wrote one object with
"day": "12-15", serialized as "Day 12-15 — Beach leisure". validate's dash normaliser then rewrote it to
"Day 12 — 15 — Beach leisure" (Day 12, title "15 — Beach leisure"), every canonical reader saw day 12 alone, and the
day check did not fire because the source splitter also keyed the range by day 12 only.

The rule now (day_parse.day_range): a heading "Day A-B" (hyphen, en dash or "to"; "Day" or "Days") covers every day
A..B when A < B <= A + 14; otherwise it is the single day A. Source markers, written headings, the dash normaliser,
the day gap check, the day oracle, places/validity, places/days, the writer v2 serializer and the T2 day markers all
apply it. Pure: no AWS, DB or model call. Sources are trimmed, shape-faithful stand-ins.
"""
import pytest

from services.content_generation import graph as g
from services.content_generation.day_parse import (
    canonical_blocks, day_range, output_days, source_day_markers,
)
from services.content_generation.itinerary_utils import (
    generated_day_word_counts, parse_canonical_itinerary_days, serialize_itinerary_days,
)

CODE = "ITINERARY_DAY_COUNT_MISMATCH"

_PLACES = ["Colombo", "Negombo", "Sigiriya", "Dambulla", "Kandy", "Nuwara Eliya", "Ella", "Yala", "Mirissa",
           "Galle", "Bentota"]


def _source(range_line: str = "Day 12-15: Beach leisure in Bentota") -> str:
    lines = ["Day 1: Arrival in Male", "Transfer to the guesthouse on Maafushi.",
             "Day 2: Maafushi", "Snorkelling trip to the sandbank.",
             "Day 3: Male to Colombo", "Fly to Colombo and overnight there."]
    for day in range(4, 12):
        lines += [f"Day {day}: {_PLACES[day - 3]}", f"Drive to {_PLACES[day - 3]} and stay overnight."]
    lines += [range_line, "Free days on the beach at Bentota; no guided activities.",
              "Day 16: Departure", "Transfer to Colombo airport."]
    return "\n".join(lines)


SRI_LANKA = {"name": "Maldives and Sri Lanka on a budget", "duration": "16 days", "itineraries": _source()}


def _page(days, ranged=None, heading="Day {a}-{b}") -> str:
    """A written page with one block per day in `days`, plus one ranged block for `ranged` = (a, b)."""
    blocks = {d: f"Day {d} — Stop {d}\nThe group travels on day {d} and stays overnight." for d in days}
    if ranged:
        a, b = ranged
        blocks[a] = (heading.format(a=a, b=b) + " — Beach leisure in Bentota\n"
                     "Free days on the beach at Bentota with no guided activities.")
    return "\n\n".join(blocks[d] for d in sorted(blocks))


# ── 1. the rule ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("first,last,expected", [
    (12, 15, (12, 15)),
    (12, None, (12, 12)),
    (15, 12, (15, 15)),        # backwards: a single day, never a fold
    (12, 12, (12, 12)),
    (1, 15, (1, 15)),          # 14-day span: still a range
    (1, 16, (1, 1)),           # over two weeks: a single day
])
def test_day_range(first, last, expected):
    assert day_range(first, last) == expected


# ── 2. the source splitter ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line,expected", [
    ("Day 12-15: Beach leisure in Bentota", (12, 15)),
    ("Day 12–15 — Beach leisure", (12, 15)),
    ("Days 12 to 15: Beach leisure", (12, 15)),
    ("DAYS 12 TO 15 - BENTOTA", (12, 15)),
    ('"Day 12-15 / Beach', (12, 15)),
    ("Day 15-12: Beach leisure", (15, 15)),            # invalid range: the single first day
    ("Day 3 to 5 hours drive to Kandy", (3, 3)),       # a unit after the number: not a range
    ("Day 1 to Kathmandu", (1, 1)),
])
def test_source_markers_read_ranges(line, expected):
    assert [(a, b) for a, b, _i in source_day_markers(line)] == [expected]


def test_source_split_keeps_one_block_but_counts_every_day():
    from services.content_generation.prompts import parse_source_day_word_counts

    parsed = parse_source_day_word_counts(SRI_LANKA["itineraries"], "16 days")
    assert 12 in parsed["day_word_counts"] and 13 not in parsed["day_word_counts"]   # one block, keyed by day 12
    assert parsed["day_text"][12].startswith("Beach leisure in Bentota")
    covered = {d for a, b, _i in parsed["markers"] for d in range(a, b + 1)}
    assert covered == set(range(1, 17))


# ── 3. the written-itinerary parser ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("heading", ["Day {a}-{b}", "Day {a}–{b}", "Days {a}-{b}", "Day {a} to {b}",
                                     "Days {a} – {b}"])
def test_canonical_parser_expands_a_ranged_heading(heading):
    text = _page(list(range(1, 12)) + [16], ranged=(12, 15), heading=heading)
    days = parse_canonical_itinerary_days(text)
    assert sorted(days) == list(range(1, 17))
    assert days[12]["title"] == days[15]["title"] == "Beach leisure in Bentota"
    assert days[13]["body"] == days[12]["body"] and days[13]["range"] == (12, 15)
    assert "Beach" not in days[11]["body"]               # never folded into the day before
    assert "range" not in days[11]
    assert sorted(generated_day_word_counts(text)) == list(range(1, 17))
    assert [n for n, _t, _f in output_days(text)] == list(range(1, 17))


def test_range_at_the_end_of_the_tour():
    text = _page(range(1, 14), ranged=(14, 16))
    assert sorted(parse_canonical_itinerary_days(text)) == list(range(1, 17))
    blocks = canonical_blocks(text)
    assert (blocks[-1].first, blocks[-1].last, blocks[-1].ranged) == (14, 16, True)


def test_invalid_and_overlapping_ranges():
    backwards = parse_canonical_itinerary_days("Day 14 — A\nx\n\nDay 15-12 — B\ny")
    assert sorted(backwards) == [14, 15] and "range" not in backwards[15]
    too_long = parse_canonical_itinerary_days("Day 1-30 — A\nx")
    assert sorted(too_long) == [1]
    # an overlap: the later block wins its day, as catalog._tour_day_text (DISTINCT ON … ord DESC)
    overlap = parse_canonical_itinerary_days("Day 3-5 — Trek\ntrek text\n\nDay 4 — Rest\nrest text")
    assert sorted(overlap) == [3, 4, 5]
    assert overlap[4]["title"] == "Rest" and overlap[5]["title"] == "Trek"


def test_serialize_writes_a_ranged_block_back_once():
    text = _page(list(range(1, 12)) + [16], ranged=(12, 15))
    out = serialize_itinerary_days(parse_canonical_itinerary_days(text))
    assert out.count("Beach leisure in Bentota") == 1 and "Day 12-15 — Beach leisure in Bentota" in out
    assert parse_canonical_itinerary_days(out) == parse_canonical_itinerary_days(text)
    overlap = "Day 3-5 — Trek\ntrek text\n\nDay 4 — Rest\nrest text"
    assert parse_canonical_itinerary_days(serialize_itinerary_days(parse_canonical_itinerary_days(overlap))) \
        == parse_canonical_itinerary_days(overlap)


def test_the_flag_fix_day_nudge_leaves_a_ranged_block_alone():
    """A one-day nudge would rewrite "Day 12-15" as day 12 alone; ranged days are skipped (no model call)."""
    from unittest.mock import MagicMock, patch
    from services.content_generation import flag_fix_node as ff

    text = _page(list(range(1, 12)) + [16], ranged=(12, 15))
    nudged = []

    def _nudge(_client, _src, title, body, _target, **_kw):
        nudged.append(title)
        return title, body, MagicMock(cost_usd=0.0, model_used="m")

    with patch.object(ff, "LLMClient"), patch.object(ff, "nudge_itinerary_day", side_effect=_nudge), \
         patch.object(ff, "record_call_sync"), patch.object(ff, "ITINERARY_CLAMP_MIN", 100.0):
        ff._repair_still_compressed_days({"tour": SRI_LANKA}, text, nudge_budget=20)
    assert nudged and "Beach leisure in Bentota" not in nudged


# ── 4. validate's dash normaliser ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Day 12-15 — Beach leisure", "Day 12-15 — Beach leisure"),
    ("Day 12–15 – Beach leisure", "Day 12-15 — Beach leisure"),
    ("Day 12 to 15 -- Beach leisure", "Day 12-15 — Beach leisure"),
    ("Day 3 -- Kandy", "Day 3 — Kandy"),
    ("Day 1 - 3 hours drive to Paro", "Day 1 — 3 hours drive to Paro"),   # not a range
    ("Day 12 — 15 km ride", "Day 12 — 15 km ride"),
    ("Day 15-12 — Beach", "Day 15 — 12 — Beach"),                         # invalid: the old single-day rewrite
    # AA-990 review: a number joined to a word is the title, never a range end
    ("Day 2 – 4-hour trek to Base Camp", "Day 2 — 4-hour trek to Base Camp"),
    ("Day 5 – 3-hour drive to Kandy", "Day 5 — 3-hour drive to Kandy"),
    ("Day 2 - 10-km walk", "Day 2 — 10-km walk"),
    ("Day 2 - 4 hour trek", "Day 2 — 4 hour trek"),
    # prose: a range mid-line is left alone; a single day mid-line keeps the AA-228 rewrite
    ("Day 1 — Hanoi\nThe walk from Day 3 to 5 — the hardest stretch — follows.",
     "Day 1 — Hanoi\nThe walk from Day 3 to 5 — the hardest stretch — follows."),
    ("Day 1 — Hanoi\nfrom Day 3 to 5 — the hardest stretch", "Day 1 — Hanoi\nfrom Day 3 to 5 — the hardest stretch"),
    ("Arrive. Day 2 -- Halong", "Arrive. Day 2 — Halong"),
    ("Day 1 -- Arrive\n\n Day 12 – 15 -- Beach", "Day 1 — Arrive\n\n Day 12-15 — Beach"),   # Haiku "||" shape
])
def test_dash_normaliser_keeps_ranges(raw, expected):
    assert g.normalise_day_dashes(raw) == expected


def test_validate_never_splits_prose_out_as_a_ranged_day():
    page = ("Day 1 — Hanoi\nArrive in Hanoi.\n\nDay 2 – 4-hour trek to Base Camp\nThe trek starts early, and the "
            "walk from Day 3 to 5 — the hardest stretch — climbs to the pass.\n\nDay 3 — Pass\nx")
    state = {"tour": {"name": "t", "duration": "3 days",
                      "itineraries": "Day 1: Hanoi\nArrive.\nDay 2: Trek\nTrek.\nDay 3: Pass\nWalk."},
             "retry_count": 0,
             "generated": {"name": "Trek", "summary": "A trek.", "highlights": ["a", "b", "c", "d"],
                           "itineraries": page}}
    it = g.validate_node(state)["generated"]["itineraries"]
    assert "Day 2 — 4-hour trek to Base Camp" in it
    assert "walk from Day 3 to 5 — the hardest stretch" in it and "Day 3-5" not in it
    assert [b.first for b in canonical_blocks(it)] == [1, 2, 3]


def test_validate_keeps_the_ranged_heading_and_fires_no_day_code():
    page = _page(list(range(1, 12)) + [16], ranged=(12, 15))
    state = {"tour": SRI_LANKA, "retry_count": 0, "writer_version": "v2",
             "generated": {"name": "Maldives and Sri Lanka", "summary": "A budget trip.",
                           "highlights": ["a", "b", "c", "d"], "itineraries": page}}
    out = g.validate_node(state)
    assert "Day 12-15 — Beach leisure in Bentota" in out["generated"]["itineraries"]
    assert CODE not in out["failure_codes"]


# ── 5. the day checks (validate / final_day_guard, the day oracle) ─────────────────────────────────

def test_day_gap_counts_a_ranged_output_as_every_day():
    gap = g.itinerary_day_gap(SRI_LANKA, _page(list(range(1, 12)) + [16], ranged=(12, 15)))
    assert gap["missing"] == [] and gap["source_days"] == list(range(1, 17))


@pytest.mark.parametrize("page", [
    _page(list(range(1, 13)) + [16]),                                             # the range folded away
    _page(list(range(1, 12)) + [16]).replace("Day 16", "Day 12 — 15 — Beach\nx\n\nDay 16", 1),  # the damaged shape
])
def test_day_gap_flags_days_lost_from_a_source_range(page):
    assert g.itinerary_day_gap(SRI_LANKA, page)["missing"] == [13, 14, 15]


@pytest.mark.parametrize("range_line", ["Day 12–15: Beach leisure in Bentota",
                                        "Days 12 to 15: Beach leisure in Bentota"])
def test_final_day_guard_passes_a_v2_draft_with_the_source_range(range_line):
    tour = {**SRI_LANKA, "itineraries": _source(range_line)}
    state = {"tour": tour, "generated": {"itineraries": _page(list(range(1, 12)) + [16], ranged=(12, 15))},
             "writer_version": "v2", "failure_codes": [], "feedback": "", "revalidate_ran": True,
             "revalidate_passed": True}
    out = g.final_day_guard(state)
    assert CODE not in (out.get("failure_codes") or []) and out["revalidate_passed"] is True


def test_final_day_guard_holds_a_v2_draft_that_folded_the_range():
    state = {"tour": SRI_LANKA, "generated": {"itineraries": _page(list(range(1, 13)) + [16])},
             "writer_version": "v2", "failure_codes": [], "feedback": "", "revalidate_ran": True,
             "revalidate_passed": True}
    out = g.final_day_guard(state)
    assert CODE in out["failure_codes"] and out["day_guard_missing"] == [13, 14, 15]
    assert out["revalidate_passed"] is False


# ── 5b. AA-990 review: only a trusted source range raises the (v2 hard) day check ─────────────────────

_HUE = ("Day 1: Arrive in Hanoi\nTransfer to the hotel.\nDay 2: Halong Bay\nCruise on the bay.\n"
        "Day 3 - 5 temples of Hue in one day, then fly home.\nVisit the citadel and the tombs.")


def test_numeric_source_title_is_not_a_range_for_the_day_check():
    tour = {"name": "Vietnam", "duration": "3 days", "itineraries": _HUE}
    page = "Day 1 — Hanoi\nx\n\nDay 2 — Halong Bay\ny\n\nDay 3 — Five temples of Hue\nz"
    gap = g.itinerary_day_gap(tour, page)
    assert gap["missing"] == [] and gap["source_days"] == [1, 2, 3]
    state = {"tour": tour, "generated": {"itineraries": page}, "writer_version": "v2", "failure_codes": [],
             "feedback": "", "revalidate_ran": True, "revalidate_passed": True}
    assert CODE not in (g.final_day_guard(state).get("failure_codes") or [])
    # no duration: the missing separator after "5" is enough
    assert g.itinerary_day_gap({**tour, "duration": ""}, page)["missing"] == []


def test_hyphenated_quantity_is_not_a_source_range():
    src = "Day 1: Arrive.\nDay 2 - 4-hour trek to camp.\nDay 3: Home."
    assert source_day_markers(src) == [(1, 1, 0), (2, 2, 1), (3, 3, 2)]
    assert source_day_markers("Day 1: a\nDay 2 – 10-km walk\nDay 3: b")[1] == (2, 2, 1)


@pytest.mark.parametrize("src,duration,expected", [
    ("Day 1: a\nDays 2-4: Beach\nDay 5: b", "5 days", {2: 4}),
    ("Day 1: a\nDay 2 - 4 – Beach\nDay 5: b", "", {2: 4}),
    ("Day 1: a\n**Days 2-4**\nDay 5: b", "", {2: 4}),
    ("Day 1: a\nDays 2-4 Beach\nDay 5: b", "", {}),                 # no separator after the range end
    ("Day 1: a\nDays 2-6: Beach\nDay 5: b", "", {2: 4}),            # cut at the next marker
    ("Day 1: a\nDay 2: b\nDays 3-6: Beach", "4 days", {3: 4}),      # cut at the duration
    ("Day 1: a\nDay 2: b\nDays 3-6: Beach", "3 days", {}),          # cut down to one day
    ("Day 1: a\nDay 2, 3 & 4: Beach\nDay 5: b", "", {2: 4}),        # a day list is explicit
])
def test_trusted_day_ranges(src, duration, expected):
    from services.content_generation.day_parse import trusted_day_ranges

    assert trusted_day_ranges(src, source_day_markers(src), duration) == expected


def test_v1_t2_prompt_names_a_ranged_source_block():
    from services.content_generation.prompts import build_rewrite_prompt

    prompt = build_rewrite_prompt(SRI_LANKA, {})
    assert "\nDay 11: " in prompt and "\nDay 16: " in prompt
    assert '\nDays 12-15: 14 words (one source block: write ONE itinerary object with "day": "12-15"' in prompt
    targeted = build_rewrite_prompt(SRI_LANKA, {}, per_day_targets=True)
    assert "\nDays 12-15: 14 words (target ~" in targeted and '"day": "12-15"' in targeted
    # a source without a trusted range: no "Days" line, nothing about a range
    plain = build_rewrite_prompt({**SRI_LANKA, "itineraries": _HUE, "duration": "3 days"}, {})
    assert "\nDays " not in plain and "one source block" not in plain and "\nDay 3: " in plain


def test_t2_rewrite_of_a_ranged_master_page():
    """A tenant T2 rewrite reads the Master aa_itineraries as its source: "Day 12-15 —" there is a trusted range."""
    master = _page(list(range(1, 12)) + [16], ranged=(12, 15))
    tour = {"name": "Maldives and Sri Lanka", "duration": "16 days", "itineraries": master}
    assert g.itinerary_day_gap(tour, master)["missing"] == []
    folded = _page(list(range(1, 13)) + [16])
    assert g.itinerary_day_gap(tour, folded)["missing"] == [13, 14, 15]


def test_day_oracle_sees_the_ranged_days():
    from services.quality import day_oracle

    ok = {f["code"] for f in day_oracle.check(SRI_LANKA["itineraries"], "16 days",
                                              _page(list(range(1, 12)) + [16], ranged=(12, 15)))}
    assert not ok & {day_oracle.DAY_OUTPUT_GAP, day_oracle.DAY_COUNT_VS_DURATION, day_oracle.DAY_OUTPUT_EXTRA}
    folded = {f["code"] for f in day_oracle.check(SRI_LANKA["itineraries"], "16 days",
                                                  _page(list(range(1, 13)) + [16]))}
    assert day_oracle.DAY_OUTPUT_GAP in folded


# ── 6. the other readers ───────────────────────────────────────────────────────────────────────────

def test_places_validity_and_days_agree():
    from services.places import days as days_mod
    from services.places import validity as v

    text = _page(list(range(1, 14)), ranged=(14, 16), heading="Days {a} to {b}")
    got = dict(v.duration_findings("16 days", text))
    assert "ranged_heading" in got and "missing_day_numbers" not in got and "duration_text_vs_days" not in got
    assert v.duration_days("Flexible", text) == 16      # the v_tour fallback: the highest covered day
    assert sorted(v.day_blocks(text)) == list(range(1, 17))
    blocks = days_mod.parse_days(text)
    assert (blocks[-1].day_from, blocks[-1].day_to) == (14, 16)
    assert [(b.day_from, b.day_to) for b in days_mod.parse_days("Day 15-12 — Beach\nx")] == [(15, 15)]


def test_writer_v2_serializer_and_prompt():
    from services.content_generation import s1_writer_v2 as v2

    items = [{"day": 11, "title": "Galle", "body": "Walk the fort."},
             {"day": "12–15", "title": "Beach leisure in Bentota", "body": "Free days on the beach."},
             {"day": "Days 16 to 17", "title": "Departure", "body": "Transfer."},
             {"day": "19-18", "title": "Odd", "body": "x"}]
    out = v2.serialize_days(items)
    assert "Day 12-15 — Beach leisure in Bentota" in out and "Day 16-17 — Departure" in out
    assert "Day 19-18 — Odd" in out                       # an invalid range is left as written (day 19)
    plan = v2.day_plan(SRI_LANKA)
    assert [d["day"] for d in plan["days"]] == list(range(1, 17))
    sheet = v2.render_fact_sheet(SRI_LANKA, v2.build_fact_table(SRI_LANKA), plan)
    assert "Days 12–15 are one block in the source" in sheet
    assert '"day": "12-15"' in sheet and "never invent" in sheet


def test_t2_day_markers_cover_a_ranged_heading():
    from services.content_generation.t2_dashes import _CANONICAL_MARKER_RE, unify_day_markers

    text = "Day 11 — Galle\nx\n\nDay 12-15 — Beach leisure\ny"
    colon = _CANONICAL_MARKER_RE.sub(r"\1: ", text)
    assert "Day 12-15: Beach leisure" in colon and "—" not in colon
    assert "Day 12-15 · Beach: Bentota" in unify_day_markers(colon.replace("Beach leisure", "Beach: Bentota"))
