"""AA-1049 (S273, lane l17) — ONE written-day reader for the T2 day-count checks.

The S273 paid trial (T2_ORCHESTRATOR=linear on an audit tenant) stored a 12-day version that passed the orchestrator's
Master slot check and then got T2_DAY_COUNT_VS_MASTER ("0 day(s) for a 12-day Master") from the REPORT-mode final
contract. Cause: the slot check read days with ``day_parse.source_day_markers`` (any "Day N" line), the contract with
``day_parse.output_days`` (canonical "Day N — Title" only), and every stored T2 text is dash-fixed to "Day N: Title" /
"Day N · Title" (AA-778, ``t2_dashes.fix_t2_dashes``), on main as on the linear path. Both now call
``day_parse.written_day_numbers`` (so does the repair contract's days_kept).

1. Agreement: ``t2_orchestrator.day_slots``, ``final_contract._day_numbers`` and ``repair_contract.day_headings`` read
   the same days on every itinerary shape a writer or a code step leaves, and ``output_days`` agrees on the canonical
   ones; the dash fix never changes the days read.
2. Parity: over the full-chain T2 harness scenarios (the parity golden's), on the linear path and on main's, the
   contract records T2_DAY_COUNT_VS_MASTER exactly when the orchestrator's slot check fails on the stored text, and
   the linear path stores the itinerary in main's shape.

Invented tours and places only. No DB, no LLM, no network.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from services.content_generation import day_parse as dp
from services.content_generation import t2_orchestrator as o
from services.content_generation.t2_dashes import fix_t2_dashes
from services.quality import final_contract as fc
from services.quality import repair_contract as rc
from tests.unit.fixtures import aa1049_t2_harness as h

LINEAR = {"T2_ORCHESTRATOR": "linear"}

_BODY = "A slow morning by the canal in Brellin, then a walk to the old mill."

# (label, itinerary value, the days its headings cover in order)
SHAPES = [
    ("canonical", f"Day 1 — Arrive in Brellin\n{_BODY}\n\nDay 2 — Mill path\n{_BODY}\n\nDay 3 — Depart\n{_BODY}",
     [1, 2, 3]),
    ("t2_colon", f"Day 1: Arrive in Brellin\n{_BODY}\n\nDay 2: Mill path\n{_BODY}\n\nDay 3: Depart\n{_BODY}",
     [1, 2, 3]),
    ("t2_middot", f"Day 1 · Arrive: Brellin\n{_BODY}\n\nDay 2 · Mill path\n{_BODY}", [1, 2]),
    ("writer_json_list", [{"day": 1, "title": "Arrive in Brellin", "body": _BODY},
                          {"day": 2, "title": "Mill path", "body": _BODY},
                          {"day": 3, "title": "Depart", "body": _BODY}], [1, 2, 3]),
    ("normalize_list_description", [{"day": 1, "title": "Arrive", "description": _BODY},
                                    {"day": 2, "title": "Depart", "description": _BODY}], [1, 2]),
    ("markdown_heading", f"## Day 1 — Arrive in Brellin\n{_BODY}\n\n## Day 2 — Depart\n{_BODY}", [1, 2]),
    ("markdown_bold_colon", f"**Day 1:** Arrive in Brellin\n{_BODY}\n\n**Day 2:** Depart\n{_BODY}", [1, 2]),
    ("canonical_ranged", f"Day 1 — Arrive\n{_BODY}\n\nDay 2-4 — Lakeside days\n{_BODY}\n\nDay 5 — Depart\n{_BODY}",
     [1, 2, 3, 4, 5]),
    ("t2_colon_ranged", f"Day 1: Arrive\n{_BODY}\n\nDays 2–4: Lakeside days\n{_BODY}\n\nDay 5: Depart\n{_BODY}",
     [1, 2, 3, 4, 5]),
    ("duplicate_day", f"Day 1 — Arrive\n{_BODY}\n\nDay 2 — Mill\n{_BODY}\n\nDay 2 — Mill again\n{_BODY}", [1, 2, 2]),
    ("no_heading", "Three easy days around Brellin and its mill.", []),
    ("empty", "", []),
    ("none", None, []),
]


@pytest.mark.parametrize("label,value,want", SHAPES, ids=[s[0] for s in SHAPES])
def test_every_day_check_reads_the_same_days(label, value, want):
    assert dp.written_day_numbers(value) == want
    assert o.day_slots(value) == want                                   # the orchestrator's slot check
    assert fc._day_numbers(value) == sorted(set(want))                  # the T2 final contract
    assert rc.day_headings(rc._day_text(value)) == want                 # the repair contract's days_kept


@pytest.mark.parametrize("label,value,want", [s for s in SHAPES if s[0].startswith("canonical")
                                               or s[0] in ("writer_json_list", "normalize_list_description",
                                                           "duplicate_day")],
                         ids=lambda v: v if isinstance(v, str) and " " not in v else None)
def test_output_days_agrees_on_the_canonical_shapes(label, value, want):
    """``output_days`` stays S1's strict canonical reader (the S1 storage contract and its SQL twin); on every
    canonical text, and on a writer day list once serialized, it reads what the shared reader reads."""
    text = dp.written_itinerary_text(value)
    assert [n for n, _t, _f in dp.output_days(text)] == want == dp.written_day_numbers(value)


@pytest.mark.parametrize("label,value,want", [s for s in SHAPES if isinstance(s[1], str) and "—" in s[1]],
                         ids=lambda v: v if isinstance(v, str) and " " not in v else None)
def test_the_t2_dash_fix_never_changes_the_days_read(label, value, want):
    fixed, _log = fix_t2_dashes({"itineraries": value}, {"name": "Brellin Mill Walk", "itineraries": value})
    assert "—" not in fixed["itineraries"]
    assert dp.written_day_numbers(fixed["itineraries"]) == want


def test_a_title_with_its_own_colon_switches_the_marker_and_keeps_the_days():
    text = f"Day 1 — Arrive: Brellin\n{_BODY}\n\nDay 2 — Mill path\n{_BODY}"
    fixed, _ = fix_t2_dashes({"itineraries": text}, {"name": "x", "itineraries": text})
    assert fixed["itineraries"].startswith("Day 1 · Arrive: Brellin")
    assert dp.written_day_numbers(fixed["itineraries"]) == [1, 2]


def test_the_s273_trial_shape_twelve_dash_fixed_days_pass_the_contract():
    """The trial's case, with invented text: a 12-day Master in the canonical form and the T2 version dash-fixed to
    "Day N: Title". The slot check passes and the contract records no day finding (it recorded '0 day(s) for a
    12-day Master (missing 1..12)')."""
    master_itin = "\n\n".join(f"Day {n} — Stage {n} of the Velk trail\n{_BODY}" for n in range(1, 13))
    master = {"name": "Velk Trail in 12 Days", "summary": "Twelve days on the Velk trail.", "country": "Nowhere",
              "itineraries": master_itin}
    version, _ = fix_t2_dashes({"name": "Velk Trail", "summary": "Twelve days on foot.", "itineraries": master_itin},
                               master)
    assert version["itineraries"].startswith("Day 1: Stage 1")
    bundle = SimpleNamespace(slots=o.compose_slots(master))
    assert bundle.slots["day_numbers"] == list(range(1, 13))
    assert o.slot_findings(version, bundle) == []
    codes = [f["code"] for f in fc.check_t2(fc.T2Input(content=version, master=master), exit="done").findings]
    assert "T2_DAY_COUNT_VS_MASTER" not in codes


def test_the_contract_reads_a_writer_day_list_instead_of_its_str():
    """A non-string itinerary (the writer-JSON day list) was ``str()``-ed by the contract and read as 0 days."""
    master = {"name": "Brellin", "summary": "Two days.", "itineraries": "Day 1 — Arrive\nx\n\nDay 2 — Depart\ny"}
    content = {"name": "Brellin", "summary": "Two days.",
               "itineraries": [{"day": 1, "title": "Arrive", "body": "x"}, {"day": 2, "title": "Depart", "body": "y"}]}
    codes = [f["code"] for f in fc.check_t2(fc.T2Input(content=content, master=master), exit="done").findings]
    assert "T2_DAY_COUNT_VS_MASTER" not in codes


def test_a_dropped_day_is_still_found_in_the_dash_fixed_form():
    master = {"name": "Brellin", "summary": "Three days.",
              "itineraries": "Day 1 — Arrive\nx\n\nDay 2 — Mill\ny\n\nDay 3 — Depart\nz"}
    content = {"name": "Brellin", "summary": "Three days.", "itineraries": "Day 1: Arrive\nx\n\nDay 2: Mill\ny"}
    [f] = [f for f in fc.check_t2(fc.T2Input(content=content, master=master), exit="done").findings
           if f["code"] == "T2_DAY_COUNT_VS_MASTER"]
    assert f["detail"] == "2 day(s) for a 3-day Master (missing 3)"
    assert o.slot_findings(content, SimpleNamespace(slots=o.compose_slots(master))) == ["T2_DAY_COUNT_VS_MASTER"]


# ── 2. parity over the full-chain harness scenarios ────────────────────────────────────────────


def _json(v):
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _stored(trace: dict) -> tuple[dict, dict]:
    """(rewritten_content, final_contract record) of the version save in a harness trace."""
    [save] = [args for sql, args in trace["sql"] if "SET rewritten_content" in sql]
    args = [_json(a) for a in save]
    content = next(a for a in args if isinstance(a, dict) and "itineraries" in a)
    contract = next(a["final_contract"] for a in args if isinstance(a, dict) and a.get("final_contract"))
    return content, contract


_MARKER_FORM = re.compile(r"^Days?\s+\d+(?:\s*[-–]\s*\d+)?\s*(—|:|·)", re.MULTILINE)


def _marker_forms(itin) -> set[str]:
    return set(_MARKER_FORM.findall(itin)) if isinstance(itin, str) else {type(itin).__name__}


@pytest.fixture(scope="module")
def traces():
    return {mode: h.run_all(env=env) for mode, env in (("main", {}), ("linear", LINEAR))}


@pytest.mark.parametrize("name", sorted(h.SCENARIOS))
def test_contract_day_finding_iff_the_slot_check_fails(traces, name):
    """(a) For every golden scenario on both paths: the stored final contract records T2_DAY_COUNT_VS_MASTER exactly
    when the orchestrator's Master slot check fails on the stored itinerary, and check_t2 re-run on the stored text
    agrees with the stored record."""
    from services.jobs import t2_rewrite_job as job
    master = job.source_tour_dict({**h.MASTER_ROW, **(h.SCENARIOS[name].get("row") or {})})
    bundle = SimpleNamespace(slots=o.compose_slots(master))
    assert bundle.slots["day_numbers"] == [1, 2, 3]
    for mode in ("main", "linear"):
        content, contract = _stored(traces[mode][name])
        slot_broken = bool(o.slot_findings(content, bundle))
        stored_codes = {f["code"] for f in contract["findings"]}
        rerun = {f["code"] for f in fc.check_t2(fc.T2Input(content=content, master=master), exit="done").findings}
        assert ("T2_DAY_COUNT_VS_MASTER" in stored_codes) == slot_broken, (mode, name, contract["findings"])
        assert ("T2_DAY_COUNT_VS_MASTER" in rerun) == slot_broken, (mode, name)
        assert slot_broken == (name == "day_dropped"), (mode, name)


@pytest.mark.parametrize("name", sorted(h.SCENARIOS))
def test_linear_stores_the_itinerary_in_mains_shape(traces, name):
    main_itin = _stored(traces["main"][name])[0]["itineraries"]
    lin_itin = _stored(traces["linear"][name])[0]["itineraries"]
    assert isinstance(main_itin, str) and isinstance(lin_itin, str)
    assert _marker_forms(lin_itin) == _marker_forms(main_itin) == {":"}
    assert o.day_slots(lin_itin) == o.day_slots(main_itin)
