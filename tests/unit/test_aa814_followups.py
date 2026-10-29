"""AA-814 follow-ups (S258): Facts Entries for the T2 tour rewrite behind T2_FACTS, and the Facts 'Source:' line in
the leak / label checks. Mock-only: no AWS, no DB, no LLM."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.acp_content_writing import facts as writer_facts
from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates, reader_checklist
from services.acp_produce.tenant_pipeline import _t3_grounding_check
from services.content_generation import batch_prompt, t2_facts

TENANT = "692a3214-5cc2-46c6-a5ca-265389747269"
_TOUR = {
    "name": "Bhutan: Culinary Trail East to West", "country": "Bhutan", "duration": "10 Days",
    "summary": "A journey from Samdrup Jongkhar to Paro.", "highlights": ["Punakha Dzong"],
    "itineraries": "Day 1: Arrive in Samdrup Jongkhar.\nDay 2: Drive to Tashigang, visit Chorten Kora.",
}
_T2 = {"tour": _TOUR, "seo": {}, "few_shots": [], "subtitle_focus": "standard", "is_tenant_rewrite": True,
       "tenant_id": TENANT, "rewrite_language": "en-US", "brand_name": "Trail Pulse"}
_A1 = {"tour": _TOUR, "seo": {}, "few_shots": [], "subtitle_focus": "standard", "is_tenant_rewrite": False}


def _fact(fact_id, scope, title, body, country="Bhutan", provenance="Rate sheet"):
    return {"fact_id": fact_id, "scope": scope, "tenant_id": TENANT if scope == "tenant" else None, "kind": "price",
            "title": title, "body": body, "stated_on": None, "valid_until": None, "provenance": provenance,
            "country": country, "updated_at": None}


def _pool(rows=None, error=None):
    conn = AsyncMock()
    if error is not None:
        conn.fetch.side_effect = error
    else:
        conn.fetch.return_value = rows or []
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for env in ("T2_FACTS", "S1_PER_DAY_TARGETS", "S1_STRUCTURED_FACTS"):
        monkeypatch.delenv(env, raising=False)


# ---------------------------------------------------------------- T2_FACTS switch and read

@pytest.mark.parametrize("value,on", [(None, False), ("", False), ("off", False), ("r1", False), ("on", True),
                                      ("TRUE", True), ("1", True), ("yes", True)])
def test_t2_facts_is_off_unless_set_on(monkeypatch, value, on):
    if value is not None:
        monkeypatch.setenv("T2_FACTS", value)
    assert t2_facts.enabled() is on


@pytest.mark.asyncio
async def test_switch_off_reads_nothing_and_keeps_the_source():
    pool, conn = _pool([_fact("fact_p1", "platform", "Visa", "e-visa USD 40")])
    tour, diag = await t2_facts.with_facts(pool, _TOUR, TENANT)
    assert tour is _TOUR and diag == {"mode": "off"}
    conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_switch_on_puts_the_picked_facts_on_a_copy_of_the_source(monkeypatch):
    monkeypatch.setenv("T2_FACTS", "on")
    rows = [_fact("fact_p1", "platform", "Tour price", "From USD 3,100 per person"),
            _fact("fact_t1", "tenant", "Tour price", "From USD 2,950 per person"),
            _fact("fact_p2", "platform", "Best season", "March to May and September to November")]
    pool, conn = _pool(rows)
    tour, diag = await t2_facts.with_facts(pool, _TOUR, TENANT)
    assert "facts" not in _TOUR
    # the tenant's own price replaces the platform price on the same subject (facts.pick_facts)
    assert tour["facts"] == ("- Tour price: From USD 2,950 per person\n"
                             "- Best season: March to May and September to November")
    assert diag == {"mode": "on", "facts": 2, "tenant_facts": 1, "chars": len(tour["facts"])}
    assert conn.fetch.call_args.args[1:] == (uuid.UUID(TENANT), "Bhutan")


@pytest.mark.asyncio
async def test_a_tour_without_country_asks_for_any_country_facts_only(monkeypatch):
    monkeypatch.setenv("T2_FACTS", "on")
    pool, conn = _pool([])
    tour, diag = await t2_facts.with_facts(pool, {**_TOUR, "country": ""}, TENANT)
    # "" (not None): the writer query's "$2 IS NULL" branch would admit every country's facts
    assert conn.fetch.call_args.args[2] == ""
    assert "facts" not in tour and diag["facts"] == 0


@pytest.mark.asyncio
async def test_a_facts_read_error_rewrites_without_facts(monkeypatch):
    monkeypatch.setenv("T2_FACTS", "on")
    pool, _ = _pool(error=RuntimeError("pool closed"))
    tour, diag = await t2_facts.with_facts(pool, _TOUR, TENANT)
    assert tour is _TOUR and diag["mode"] == "on" and "pool closed" in diag["error"]


def test_fact_lines_carry_no_id_or_source_label():
    text = t2_facts.facts_lines([_fact("fact_5e51d64df5", "tenant", " Tour  price ", "From USD 2\nper person"),
                                 _fact("fact_p2", "platform", "", "Dry season")])
    assert text == "- Tour price: From USD 2 per person\n- Dry season"
    assert "fact_" not in text and "Source" not in text


# ---------------------------------------------------------------- T2 writer prompt

_WITH_FACTS = {**_TOUR, "facts": "- Tour price: From USD 2,950 per person"}


def test_t2_prompt_gets_the_facts_block_with_the_tour_data():
    up = batch_prompt.build_s1_user_prompt({**_T2, "tour": _WITH_FACTS})
    assert "FACTS (checked facts for this trip's country" in up
    assert "- Tour price: From USD 2,950 per person" in up
    assert up.index("- Exclusions:") < up.index("FACTS (checked") < up.index("PER-DAY SOURCE LENGTH")
    assert "Copy every number, price and date exactly" in up and "never say where a fact comes from" in up
    assert "—" not in up.split("FACTS (checked", 1)[1].split("PER-DAY SOURCE LENGTH", 1)[0]


def test_t2_prompt_without_facts_is_byte_identical():
    base = batch_prompt.build_s1_user_prompt(dict(_T2))
    assert batch_prompt.build_s1_user_prompt({**_T2, "tour": {**_TOUR, "facts": ""}}) == base
    assert "FACTS (checked" not in base


def test_a1_prompt_never_gets_a_facts_block():
    base = batch_prompt.build_s1_user_prompt(dict(_A1))
    assert batch_prompt.build_s1_user_prompt({**_A1, "tour": _WITH_FACTS}) == base


def test_a_price_from_a_fact_is_supported_in_t3_grounding():
    generated = {"summary": "This ten-day trip costs from USD 2,950 per person."}
    without = _t3_grounding_check(generated, [str(v) for v in _TOUR.values() if v], background=False)
    with_facts = _t3_grounding_check(generated, [str(v) for v in _WITH_FACTS.values() if v], background=False)
    assert without and without[0]["novel_numbers"] == ["2950"]
    assert with_facts == []


# ---------------------------------------------------------------- the Facts 'Source:' line (T9 seed)

_FACTS_BLOCK = writer_facts.format_facts_block([
    _fact("fact_t1", "tenant", "Tour price", "From USD 2,950 per person.", provenance="Our 2027 rate sheet"),
    _fact("fact_p1", "platform", "Visa", "Visitors need a visa arranged through a licensed operator.",
          provenance="Tourism Council of Bhutan website, checked October 2026")])


@pytest.mark.parametrize("sentence", [
    "Source: Our 2027 rate sheet; agency fact",
    "Source: Tourism Council of Bhutan website; platform fact.",
    "- Source: Our 2027 rate sheet",
    "Prices start from USD 2,950 per person (agency fact).",
    "Visas go through a licensed operator, a platform fact worth knowing.",
])
def test_leak_check_catches_the_facts_source_label(sentence):
    assert h.LEAK_RE.search(sentence)


@pytest.mark.parametrize("sentence", [
    "The river is the source of the valley's rice.",
    "Their main source: the river, fed by glaciers.",
    "Ask the agency for the latest fact sheet.",
])
def test_leak_check_leaves_ordinary_prose(sentence):
    assert not h.LEAK_RE.search(sentence)


def test_autofix_deletes_a_copied_source_line():
    text, log = h.autofix("Prices start from USD 2,950 per person. Source: Our 2027 rate sheet; agency fact.\n\n"
                          "The camp is quiet.", h.HygieneContext(seed="Day 1 of the trip", scope="", trip_name="T"))
    assert "Source:" not in text and "agency fact" not in text
    assert "Prices start from USD 2,950 per person." in text and "The camp is quiet." in text
    assert any("source-notes wording" in line for line in log)


def test_find_issues_reports_a_kept_owner_tag_as_a_leak():
    issues = h.find_issues("Prices start from USD 2,950 per person (agency fact).",
                           h.HygieneContext(seed="Day 1 of the trip", scope="", trip_name="T"))
    assert any("agency fact" in v for v in issues["leak"])


def test_source_label_and_owner_tag_are_not_seed_words():
    forms = h.source_forms(_FACTS_BLOCK)
    assert {"rate", "sheet", "tourism", "council"} <= forms
    assert not {"source", "agency", "platform"} & forms


def test_unused_facts_never_offer_the_source_line():
    unused = quality_gates.unused_source_facts(_FACTS_BLOCK, "A short body about the camp.")
    assert unused and not any(u.startswith("Source") or "platform fact" in u for u in unused)


def test_reader_checklist_quotes_the_fact_without_its_source_line():
    assert reader_checklist.fact_entries(_FACTS_BLOCK) == [
        "Tour price: From USD 2,950 per person.",
        "Visa: Visitors need a visa arranged through a licensed operator."]
