"""AA-774 (S235) — visiting/practical place-card facts stay out of the writer seed; the sibling gate
still counts every checked fact; the drafter asks only for what_it_is/history/setting/naming."""
import asyncio
import json

import pytest

from services.acp_content_writing import claim_rules
from services.acp_shared import atom_seed as seed_mod
from services.acp_shared import place_cards as pc
from services.acp_shared.atom_seed import (RICH_CARD_FACTS, THIN_FACTS, WRITER_FACT_KINDS, compose_route_moment,
                                           compose_seed, moment_fact_count, writer_facts)

pytestmark = pytest.mark.real_atom_seed

PERMIT = "Visitors need a special permit to enter the area."
ACCESS = "Access requires a multi-hour horse or reindeer ride."
CARD = {"card_id": "c1", "display_name": "Tsaatan camps", "locality": None, "region": "Khövsgöl Province",
        "country": "Mongolia", "naming_note": None, "status": "auto_checked",
        "facts": [{"text": "The Tsaatan are reindeer herders of the taiga.", "kind": "what_it_is"},
                  {"text": "Their camps move with the seasons.", "kind": "setting"},
                  {"text": PERMIT, "kind": "practical"},
                  {"text": ACCESS, "kind": "visiting"},
                  {"text": "They are also called the Dukha.", "kind": "naming"},
                  {"text": "Herding here goes back centuries."}]}  # no kind → what_it_is


def test_writer_kinds_are_the_four():
    assert WRITER_FACT_KINDS == {"what_it_is", "history", "setting", "naming"}
    assert WRITER_FACT_KINDS < set(pc.FACT_KINDS)  # old kinds stay valid on the card


def test_compose_seed_renders_only_writer_kinds_and_keeps_kindless_fact():
    seed = compose_seed("Tsaatan camps — visit", "Ride to the Tsaatan camp.", None, None, [],
                        place="Tsaatan camps", card=CARD)
    assert PERMIT not in seed and ACCESS not in seed
    assert "reindeer herders" in seed and "called the Dukha" in seed and "Herding here goes back" in seed
    assert "Their camps move" in seed


def test_jsonb_string_facts_are_filtered_too():
    card = dict(CARD, facts=json.dumps(CARD["facts"]))
    assert writer_facts(card) == writer_facts(CARD) and len(writer_facts(card)) == 4


def test_sibling_gate_counts_every_checked_fact():
    """3 checked facts, only 1 a writer fact: still rich → no 'Other trips' line (S233 regression)."""
    card = dict(CARD, facts=[{"text": "A monastery founded in the 17th century.", "kind": "history"},
                             {"text": PERMIT, "kind": "practical"}, {"text": ACCESS, "kind": "visiting"}])
    assert len(writer_facts(card)) < RICH_CARD_FACTS <= len(seed_mod._card_facts(card))
    seed = compose_seed("Monastery — visit", "Visit the monastery.", None, None,
                        ["Walk around the monastery at dusk."], place="Monastery", card=card)
    assert "Other trips" not in seed and PERMIT not in seed


def test_siblings_still_added_when_the_card_is_truly_thin():
    card = dict(CARD, facts=[{"text": PERMIT, "kind": "practical"}])
    seed = compose_seed("Monastery — visit", "Visit the monastery.", None, None,
                        ["Walk around the monastery at dusk."], place="Monastery", card=card)
    assert "Other trips" in seed and PERMIT not in seed and "Place background" not in seed


def test_atom_seed_parts_skips_sibling_query_on_unfiltered_count():
    calls = []

    class Conn:
        async def fetchrow(self, sql, *a):
            if "v_active_tour_atoms ta" in sql and "atom_place_card" not in sql:
                return {"text": "Monastery — visit", "evidence": "Visit it.", "emotional_hook": None,
                        "season_note": None, "place": "Monastery", "itinerary_day": 2, "country": "Mongolia"}
            return dict(CARD, facts=json.dumps([{"text": "A 17th-century monastery.", "kind": "history"},
                                                {"text": PERMIT, "kind": "practical"},
                                                {"text": ACCESS, "kind": "visiting"}]))

        async def fetch(self, sql, *a):
            calls.append(sql)
            return []

    parts = asyncio.run(seed_mod.atom_seed_parts(Conn(), "a1", "t1"))
    assert parts is not None and calls == []


def test_route_moment_uses_writer_facts_for_richest_and_thin():
    rich_practical = dict(CARD, card_id="c2", facts=[{"text": PERMIT, "kind": "practical"},
                                                      {"text": ACCESS, "kind": "visiting"},
                                                      {"text": "Bring warm layers.", "kind": "practical"}])
    plain = dict(CARD, card_id="c1", facts=[{"text": "The lake is in northern Mongolia.", "kind": "setting"}])
    group = [{"text": "Lake — walk", "day": 3, "card": rich_practical, "place": "Lake", "country": "Mongolia"},
             {"text": "Lake — boat", "day": 4, "card": plain, "place": "Lake", "country": "Mongolia"}]
    block = compose_route_moment(group)
    assert "northern Mongolia" in block and PERMIT not in block and "warm layers" not in block
    n = moment_fact_count(block)
    assert n == 1 and n < THIN_FACTS and "Thin moment" in block


def test_single_route_moment_thin_tag_counts_writer_facts():
    block = compose_route_moment([{"text": "Camp — visit", "day": 5, "card": CARD, "place": "Camp",
                                   "country": "Mongolia"}])
    assert moment_fact_count(block) == 4 and "Thin moment" not in block


def test_claim_rules_round_trip_on_filtered_seed():
    seed = compose_seed("Tsaatan camps — visit", "Ride to the Tsaatan camp.", None, None, [],
                        place="Tsaatan camps", card=CARD)
    lines = claim_rules.number_source_lines(seed)
    p_lines = [ln.text for ln in lines if ln.id.startswith("P")]
    assert len(p_lines) == 4 and any("reindeer herders" in t for t in p_lines)
    assert not any(PERMIT in ln.text or ACCESS in ln.text for ln in lines)


# ---------------------------------------------------------------- drafter prompt

def test_drafter_prompt_asks_for_writer_kinds_only():
    prompt = pc._CARD_SYSTEM
    assert "visitors typically see" not in prompt and "practical or safety" not in prompt
    assert "kind is one of: what_it_is, history, setting, naming." in prompt
    assert "permits or access" in prompt


def test_prompt_sha_changed():
    assert pc.PROMPT_SHA != "6b2ef32fe2cb38fa"  # pre-AA-774 value: stored misses are retried


def test_old_kinds_still_validate_on_clean():
    out = pc._clean_facts([{"text": PERMIT, "kind": "practical"}, {"text": "x y", "kind": "bogus"}])
    assert out == [{"text": PERMIT, "kind": "practical"}, {"text": "x y", "kind": "what_it_is"}]


# ---------------------------------------------------------------- PROMPT_SHA bump never rebuilds cards

def test_prompt_sha_bump_redrafts_no_existing_card_and_stays_capped():
    """Synthesis correction (S235): the new PROMPT_SHA only re-opens stored MISSES; linked atoms and
    cards found by alias are never re-drafted, and a build stays under its max_places cap."""
    from tests.unit.test_aa772_place_card_cost import FakeDB, FakeLLM, _atom, _ensure

    old_card = {"card_id": "k1", "place_key": "khovsgol|mongolia", "display_name": "Khövsgöl Lake",
                "locality": None, "region": None, "country": "Mongolia", "status": "auto_checked",
                "cost_usd": 0.0, "facts": [{"text": PERMIT, "kind": "practical"}]}
    atoms = [_atom("linked", "Lake Khövsgöl"), _atom("by_alias", "Khovsgol Nuur")] + \
        [_atom(f"m{i}", f"Place Number {i}") for i in range(5)]
    db = FakeDB(atoms, cards=[old_card], aliases={("khovsgol nuur", "mongolia"): "k1"})
    db.links["linked"] = {"card_id": "k1", "miss_reason": None}
    for i in range(5):
        db.misses[(f"place number {i}", "mongolia")] = {
            "reason": "no_name", "rule_version": pc.RULE_VERSION, "prompt_sha": "6b2ef32fe2cb38fa",
            "retry_after": None, "attempts": 1}
    llm = FakeLLM()
    out = _ensure(db, llm, [a["atom_id"] for a in atoms], max_places=2)
    assert out["already_linked"] == 1 and out["linked_existing"] == 1
    assert out["drafted"] == 2 and out["over_cap"] == 3 and len(llm.drafts) == 2
    assert not any("Khövsgöl" in d or "Khovsgol" in d for d in llm.drafts)
    assert db.cards["k1"]["facts"] == [{"text": PERMIT, "kind": "practical"}]  # stored card untouched
