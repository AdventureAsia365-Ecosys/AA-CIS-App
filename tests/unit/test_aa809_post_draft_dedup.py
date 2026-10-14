"""AA-809 — a drafted place card that names an existing card links to it instead of inserting a
duplicate (Dev trial 14/10/2026: 'National Institute for Zorig Chusum' vs a legacy 3-part-key card,
'Kuang Si Waterfall' vs 'Kuang Si Falls')."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services.acp_shared import place_cards as pc
from tests.unit.test_aa772_place_card_cost import FakeDB, FakeLLM, _atom, _ensure

FACTS = [{"text": "It teaches the thirteen traditional arts.", "kind": "what_it_is"}]


def _card(card_id, key, name, country, status="auto_checked", locality=None, region=None, facts=FACTS):
    return {"card_id": card_id, "place_key": key, "display_name": name, "locality": locality, "region": region,
            "country": country, "status": status, "cost_usd": 0.01, "facts": list(facts)}


def _draft(name, country, locality=None, aliases=()):
    return {"display_name": name, "locality": locality or "", "region": "", "country": country,
            "aliases": list(aliases), "facts": [{"text": "A drafted fact.", "kind": "history"}]}


# ── match_key (the synonym table) ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [("Kuang Si Waterfall", "Kuang Si Falls"), ("Kuang Si Waterfalls", "Kuang Si Falls"),
                                 ("Pangong Tso", "Pangong Lake"), ("Lake Pangong", "Pangong Tso"),
                                 ("Membar Tsho", "Membar Lake"), ("Nam Co", "Lake Nam")])
def test_match_key_folds_feature_synonyms(a, b):
    assert pc.match_key(a) == pc.match_key(b)


@pytest.mark.parametrize("a,b", [("Kuang Si Butterfly Park", "Kuang Si Falls"), ("Co Loa Citadel", "Loa Citadel"),
                                 ("Punakha Dzong", "Punakha")])
def test_match_key_keeps_different_places_apart(a, b):
    assert pc.match_key(a) != pc.match_key(b)


def test_match_key_never_changes_place_key():
    assert pc.place_key("Kuang Si Waterfall", "Laos") == "kuang si waterfall|laos"


# ── build path (ensure_place_cards → draft → post-draft resolver) ────────────────────────────────

def test_zorig_identical_name_links_to_the_legacy_3_part_key_card():
    legacy = _card("c-zorig", "national institute for zorig chusum|thimphu|bhutan",
                   "National Institute for Zorig Chusum", "Bhutan", locality="Thimphu")
    db = FakeDB([_atom("a1", "Zorig Chusum painting school", "Bhutan")], cards=[legacy])
    llm = FakeLLM(card=_draft("National Institute for Zorig Chusum", "Bhutan", "Thimphu", ["Zorig Chusum School"]))
    with patch.object(pc.logger, "info") as log:
        out = _ensure(db, llm, ["a1"])
    assert len(llm.drafts) == 1                          # the itinerary name did not match: one paid draft
    assert list(db.cards) == ["c-zorig"]                 # no duplicate inserted
    assert db.links["a1"] == {"card_id": "c-zorig", "miss_reason": None}
    assert out["linked_after_draft"] == 1 and out["linked_new"] == 0 and out["built"] == 1
    assert db.cards["c-zorig"]["facts"] == FACTS        # existing facts never overwritten
    assert db.cards["c-zorig"]["cost_usd"] == pytest.approx(0.014)
    assert db.aliases[("national institute for zorig chusum", "bhutan")] == "c-zorig"
    assert db.aliases[("zorig chusum painting school", "bhutan")] == "c-zorig"
    hit = [c for c in log.call_args_list if c.args[0] == "place_card_post_draft_match"]
    assert hit and hit[0].kwargs["method"] == "core_key"


def test_kuang_si_waterfall_links_to_kuang_si_falls():
    falls = _card("c-falls", "kuang si falls|laos", "Kuang Si Falls", "Laos", locality="Luang Prabang")
    db = FakeDB([_atom("a1", "Kuang Si Waterfall", "Laos")], cards=[falls])
    db.misses[("kuang si waterfall", "laos")] = {"reason": "error", "rule_version": 0, "prompt_sha": "x",
                                                 "attempts": 1, "retry_after": None}
    llm = FakeLLM(card=_draft("Kuang Si Waterfall", "Laos", "Luang Prabang"))
    with patch.object(pc.logger, "info") as log:
        out = _ensure(db, llm, ["a1"])
    assert list(db.cards) == ["c-falls"] and db.links["a1"]["card_id"] == "c-falls"
    assert out["linked_after_draft"] == 1
    assert db.aliases[("kuang si waterfall", "laos")] == "c-falls"
    assert ("kuang si waterfall", "laos") not in db.misses   # the miss is cleared
    methods = [c.kwargs["method"] for c in log.call_args_list if c.args[0] == "place_card_post_draft_match"]
    assert methods == ["feature_synonym"]


def test_a_draft_alias_hits_the_alias_table():
    card = _card("c-tn", "paro taktsang|bhutan", "Paro Taktsang", "Bhutan")
    db = FakeDB([_atom("a1", "Taktsang hike", "Bhutan")], cards=[card],
                aliases={("tigers nest", "bhutan"): "c-tn"})
    llm = FakeLLM(card=_draft("Taktsang Palphug Monastery", "Bhutan", "Paro", ["Tiger's Nest"]))
    out = _ensure(db, llm, ["a1"])
    assert list(db.cards) == ["c-tn"] and out["linked_after_draft"] == 1


def test_a_draft_alias_that_is_only_part_of_the_name_never_matches():
    """'Punakha' as an alias of 'Punakha Dzong' must not link the dzong to the town card."""
    town = _card("c-town", "punakha|bhutan", "Punakha", "Bhutan")
    db = FakeDB([_atom("a1", "Punakha Dzong", "Bhutan")], cards=[town], aliases={("punakha", "bhutan"): "c-town"})
    out = _ensure(db, FakeLLM(card=_draft("Punakha Dzong", "Bhutan", "Punakha", ["Punakha"])), ["a1"])
    assert out["linked_new"] == 1 and out["linked_after_draft"] == 0
    assert db.links["a1"]["card_id"] != "c-town" and len(db.cards) == 2


def test_city_palace_jaipur_and_udaipur_stay_two_cards():
    jaipur = _card("c-jai", "city palace|jaipur|india", "City Palace", "India", locality="Jaipur")
    db = FakeDB([_atom("a1", "City Palace", "India", evidence="Visit the palace by the lake.")], cards=[jaipur])
    out = _ensure(db, FakeLLM(card=_draft("City Palace", "India", "Udaipur")), ["a1"])
    assert out["linked_new"] == 1 and out["linked_after_draft"] == 0 and len(db.cards) == 2
    new = db.cards[db.links["a1"]["card_id"]]
    assert new["place_key"] == "city palace|udaipur|india" and db.cards["c-jai"]["facts"] == FACTS


def test_a_generic_name_in_the_same_locality_links():
    jaipur = _card("c-jai", "city palace|jaipur|india", "City Palace", "India", locality="Jaipur")
    db = FakeDB([_atom("a1", "City Palace", "India", evidence="Visit the palace.")], cards=[jaipur])
    out = _ensure(db, FakeLLM(card=_draft("City Palace", "India", "Jaipur")), ["a1"])
    assert out["linked_after_draft"] == 1 and list(db.cards) == ["c-jai"]


def test_a_rejected_card_is_never_matched():
    rejected = _card("c-rej", "kuang si falls|laos", "Kuang Si Falls", "Laos", status="rejected")
    db = FakeDB([_atom("a1", "Kuang Si Waterfall", "Laos")], cards=[rejected],
                aliases={("kuang si waterfall", "laos"): "c-rej"})
    out = _ensure(db, FakeLLM(card=_draft("Kuang Si Waterfall", "Laos")), ["a1"])
    assert out["linked_after_draft"] == 0 and out["linked_new"] == 1
    assert db.links["a1"]["card_id"] not in (None, "c-rej")
    assert db.cards["c-rej"]["status"] == "rejected" and db.cards["c-rej"]["cost_usd"] == 0.01


def test_a_different_place_sharing_a_word_is_not_matched():
    falls = _card("c-falls", "kuang si falls|laos", "Kuang Si Falls", "Laos")
    db = FakeDB([_atom("a1", "Kuang Si Butterfly Park", "Laos")], cards=[falls])
    out = _ensure(db, FakeLLM(card=_draft("Kuang Si Butterfly Park", "Laos", "Luang Prabang",
                                          ["Butterfly Park Luang Prabang"])), ["a1"])
    assert out["linked_new"] == 1 and out["linked_after_draft"] == 0 and len(db.cards) == 2


def test_another_country_is_never_matched():
    falls = _card("c-falls", "kuang si falls|laos", "Kuang Si Falls", "Laos")
    db = FakeDB([_atom("a1", "Kuang Si Falls", "Thailand")], cards=[falls])
    out = _ensure(db, FakeLLM(card=_draft("Kuang Si Falls", "Thailand")), ["a1"])
    assert out["linked_new"] == 1 and len(db.cards) == 2


def test_two_matching_cards_are_ambiguous_and_insert_as_before():
    a = _card("c-a", "kuang si falls|laos", "Kuang Si Falls", "Laos")
    b = _card("c-b", "kuang si falls|luang prabang|laos", "Kuang Si Falls", "Laos")
    db = FakeDB([], cards=[a, b])
    match = asyncio.run(pc.find_card_for_draft(db, "kuang si waterfall|laos", "Kuang Si Waterfall", [],
                                               None, None, "Laos"))
    assert match is None


def test_empty_counters_show_linked_after_draft():
    assert pc._empty_counters(0)["linked_after_draft"] == 0
    assert asyncio.run(pc.ensure_place_cards([], AsyncMock()))["linked_after_draft"] == 0
