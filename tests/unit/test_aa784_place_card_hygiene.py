"""AA-784 (S253) — place-card identity and fact hygiene from the blind 20-card spot check of the live build
(GPT-6 Luna × Haiku 4.5; root repo docs/audits/2026-10-26-S252-place-card-model-eval/spot-check):

- region is a first-level administrative division or empty — 4/7 bad cases were geographic / trekking areas;
- locality is the settlement the place is IN or empty — 2/7 were the nearest town of a remote point;
- the check's locality_ok / region_ok clear a wrong field instead of rejecting the card;
- present-tense status / administrative-role and authorship facts are never kept (C02, C18, C20);
- rebuild_place_cards re-drafts named cards in place (the 11 flagged cards), via the place_cards_build job.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.acp_shared import place_cards as pc
from services.acp_shared.atom_seed import _location_lines, writer_facts, writer_identity
from shared.jobs.registry import NonRetryable
from shared.llm_client.models import LLMResponse
from tests.unit.test_aa772_place_card_cost import FakeDB, _atom, _ensure

# ── the 7 bad locality / region cases of the spot check (packet.json, verbatim identity) ────────────────────────────

SPOT_CHECK_BAD = {
    "C04": ({"display_name": "Thado Koshi", "locality": "Lukla", "region": "Khumbu", "country": "Nepal"},
            ("Thado Koshigaon",)),
    "C08": ({"display_name": "Old Goa", "locality": "Goa Velha", "region": "Goa", "country": "India"}, ("Old Goa",)),
    "C11": ({"display_name": "Bulgan Province", "locality": "", "region": "Northern Mongolia", "country": "Mongolia"},
            ("Bulgan province",)),
    "C13": ({"display_name": "Barafu Camp", "locality": "Moshi", "region": "Kilimanjaro", "country": "Tanzania"},
            ("Barafu hut", "Barafu")),
    "C14": ({"display_name": "Malé", "locality": "Malé", "region": "North Malé Atoll", "country": "Maldives"},
            ("Colombo International Airport to Male",)),
    "C15": ({"display_name": "Thien Mu Pagoda", "locality": "Hue", "region": "Central Vietnam", "country": "Vietnam"},
            ("Thien Mu Pagoda",)),
    "C17": ({"display_name": "Kuaishan Giant Trees Trail", "locality": "Wufeng Township", "region": "Hsinchu County",
             "country": "Taiwan"}, ("The Kuaishan Big Trees Trail",)),
}
# (locality, region) after the deterministic rules. C08 (a neighbouring village) and C17 (the wrong side of a county
# line) are not detectable without a gazetteer: the prompts + the check's locality_ok / region_ok + a rebuild fix them.
SPOT_CHECK_EXPECTED = {
    "C04": ("", ""), "C08": ("Goa Velha", "Goa"), "C11": ("", ""), "C13": ("", "Kilimanjaro"), "C14": ("", ""),
    "C15": ("Hue", ""), "C17": ("Wufeng Township", "Hsinchu County"),
}


@pytest.mark.parametrize("cid", sorted(SPOT_CHECK_BAD))
def test_spot_check_bad_identities(cid):
    card, names = SPOT_CHECK_BAD[cid]
    card = dict(card)
    pc.clean_identity(card, place_names=names, country=card["country"])
    assert (card["locality"], card["region"]) == SPOT_CHECK_EXPECTED[cid]


def test_clean_identity_reports_what_it_cleared():
    card = dict(SPOT_CHECK_BAD["C04"][0])
    out = pc.clean_identity(card, place_names=("Thado Koshigaon",), country="Nepal")
    assert out == [{"field": "region", "reason": "trek_area", "value": "Khumbu"},
                   {"field": "locality", "reason": "village", "value": "Lukla"}]


# ── region ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("region,name,country,why", [
    ("Northern Mongolia", "Bulgan Province", "Mongolia", "is_place"),     # the place IS a first-level division
    ("Northern Mongolia", "Erdene Zuu", "Mongolia", "geographic"),
    ("Central Vietnam", "Thien Mu Pagoda", "Vietnam", "geographic"),
    ("Khumbu", "Thado Koshi", "Nepal", "trek_area"),
    ("North Malé Atoll", "Malé", "Maldives", "geographic"),
    ("Everest Region", "Namche Bazaar", "Nepal", "trek_area"),
    ("Annapurna Circuit", "Manang", "Nepal", "trek_area"),
    ("Annapurna Region", "Ghorepani", "Nepal", "trek_area"),
    ("Kathmandu Valley", "Boudhanath", "Nepal", "geographic"),
    ("Mekong Delta", "Cai Rang Floating Market", "Vietnam", "geographic"),
    ("Central Highlands", "Pleiku", "Vietnam", "geographic"),
    ("South India", "Meenakshi Temple", "India", "geographic"),
    ("North East India", "Kaziranga", "India", "geographic"),
    ("Vietnam", "Hoi An", "Vietnam", "country"),
    ("Viet Nam", "Hoi An", "Vietnam", "country"),
    ("Hue", "Hue", "Vietnam", "is_place"),
])
def test_region_that_is_not_a_first_level_division_is_cleared(region, name, country, why):
    assert pc.region_problem(region, name=name, country=country) == why


@pytest.mark.parametrize("region,name,country", [
    ("South Gyeongsang", "Namhae Island", "South Korea"),
    ("Central Province", "Nuwara Eliya Post Office", "Sri Lanka"),
    ("West Bengal", "Darjeeling", "India"),
    ("Gandaki Province", "Bahundanda", "Nepal"),
    ("Koshi Province", "Thado Koshi", "Nepal"),
    ("Tibet Autonomous Region", "Jokhang", "China"),
    ("Tibet", "Pabongka Monastery", "China"),          # an alias of China in shared.countries, still a region
    ("Central Visayas", "Olango Island", "Philippines"),
    ("Northern Province", "Jaffna Fort", "Sri Lanka"),
    ("Western Australia", "Broome", "Australia"),
    ("Inner Mongolia", "Hulunbuir", "China"),
    ("Rift Valley Province", "Lake Nakuru", "Kenya"),
    ("Kaafu Atoll", "Maafushi", "Maldives"),
    ("Gilgit-Baltistan", "Askole", "Pakistan"),
    ("Arkhangai", "Khöshöö Tsaidam", "Mongolia"),
    ("Khövsgöl Province", "Khövsgöl Lake", "Mongolia"),  # the lake is named like its province
    ("Bulgan Province", "Bulgan", "Mongolia"),          # the aimag centre town
    ("Ladakh", "Pangong Lake", "India"),
    ("Kilimanjaro", "Barafu Camp", "Tanzania"),
    ("Hsinchu County", "Kuaishan Giant Trees Trail", "Taiwan"),
    ("Ladakh, Gilgit-Baltistan, Xinjiang", "Karakoram", "India"),   # a range across borders: left alone
    ("", "Zenkov Cathedral", "Kazakhstan"),
])
def test_first_level_divisions_are_kept(region, name, country):
    assert pc.region_problem(region, name=name, country=country) is None


# ── locality ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("locality,name,region,country,names,why", [
    ("Moshi", "Barafu Camp", "Kilimanjaro", "Tanzania", ("Barafu hut",), "remote_place"),
    ("Lukla", "Thado Koshi", "", "Nepal", ("Thado Koshigaon",), "village"),
    ("Malé", "Malé", "", "Maldives", (), "is_place"),
    ("Pleiku", "Pleiku", "Gia Lai", "Vietnam", (), "is_place"),
    ("Khatgal", "Khövsgöl Lake", "Khövsgöl Province", "Mongolia", (), "remote_place"),
    ("Manang", "Thorong La", "Gandaki Province", "Nepal", (), "remote_place"),
    ("Arusha", "Kilimanjaro National Park", "Kilimanjaro", "Tanzania", (), "remote_place"),
    ("Lukla", "Mount Everest", "Koshi Province", "Nepal", (), "remote_place"),
    ("Leh", "Khardung La", "Ladakh", "India", (), "remote_place"),
    ("Hanoi", "Ban Lac", "Hanoi", "Vietnam", (), "same_as_region"),
    ("Siem Reap", "Tonle Sap (Tonle Sap Lake)", "", "Cambodia", (), "remote_place"),
    ("Pokhara", "Dhampus", "Gandaki Province", "Nepal", ("Dhampus village",), "village"),
])
def test_locality_that_is_not_where_the_place_is_is_cleared(locality, name, region, country, names, why):
    assert pc.locality_problem(locality, name=name, region=region, country=country, place_names=names) == why


@pytest.mark.parametrize("locality,name,region,country", [
    ("Nuwara Eliya", "Nuwara Eliya Post Office", "Central Province", "Sri Lanka"),
    ("Hue", "Thien Mu Pagoda", "", "Vietnam"),
    ("Namche Bazaar", "Sagarmatha National Park Visitor Centre", "Koshi Province", "Nepal"),  # a building
    ("Udaipur", "Lake Palace", "Rajasthan", "India"),
    ("Udaipur", "City Palace", "Rajasthan", "India"),                  # generic: the key needs the locality
    ("Lapu-Lapu City", "Olango Island", "Cebu", "Philippines"),
    ("Chuo City", "Tsukuda Island", "Tokyo", "Japan"),
    ("Mumbai", "Goregaon", "Maharashtra", "India"),                    # '-gaon' is a village only on a Nepal card
    ("Almaty", "Zenkov Cathedral", "", "Kazakhstan"),
    ("Luang Prabang", "Phousi Hill", "Luang Prabang Province", "Laos"),
    ("Khashaat", "Khöshöö Tsaidam", "Arkhangai", "Mongolia"),
])
def test_settlement_localities_are_kept(locality, name, region, country):
    assert pc.locality_problem(locality, name=name, region=region, country=country) is None


# ── unverifiable facts ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,country,why", [
    ("Pleiku is a city in Gia Lai Province and the province’s capital.", "Vietnam", "admin_role"),       # C02
    ("The building continues to serve as a post office.", "Sri Lanka", "current_status"),                # C18
    ("It was designed by architect Andrei Zenkov and built from 1904 to 1907.", "Kazakhstan", "authorship"),  # C20
    ("The town of Bulgan is the provincial capital.", "Mongolia", "admin_role"),
    ("The fort currently houses a museum.", "India", "current_status"),
    ("The monastery is still active.", "Bhutan", "current_status"),
    ("Today it is part of Da Nang.", "Vietnam", "current_status"),
    ("The old palace now serves as the governor's residence.", "India", "current_status"),
    ("It functions as the district court.", "India", "current_status"),
    ("The town remains the seat of the district administration.", "Nepal", "admin_role"),
    ("The bridge was the work of French engineers.", "Vietnam", "authorship"),
    ("Its architect was Edwin Lutyens.", "India", "authorship"),
    # review S253: a sentence naming the country is still about a division when a division word is in it
    ("Pleiku is the capital of Gia Lai Province in Vietnam.", "Vietnam", "admin_role"),
    ("Pleiku is the provincial capital of Gia Lai, Vietnam.", "Vietnam", "admin_role"),
    ("Pleiku is the capital of Vietnam's Gia Lai Province.", "Vietnam", "admin_role"),
    ("Kandy is the capital of Central Province, Sri Lanka.", "Sri Lanka", "admin_role"),
    ("Kandy is the capital of the Central Province of Sri Lanka.", "Sri Lanka", "admin_role"),
    ("It is the largest city and capital of Bulgan Province, Mongolia.", "Mongolia", "admin_role"),
    ("Lhasa is the capital of Tibet.", "China", "admin_role"),           # an alias of China, not its name
])
def test_unverifiable_claims(text, country, why):
    assert pc.unverifiable_claim(text, country) == why


@pytest.mark.parametrize("text,country", [
    ("It served as the capital of Portuguese India during the period of Portuguese rule.", "India"),   # C08, past
    ("Malé is the capital and most populous city of the Maldives.", "Maldives"),                        # C14, national
    ("Ulaanbaatar is the capital of Mongolia.", "Mongolia"),
    ("Hanoi is the capital of Viet Nam.", "Vietnam"),                                                   # ISO name
    ("Vientiane is the capital and largest city of Laos.", "Laos"),
    ("Hue is the former imperial capital of Vietnam.", "Vietnam"),
    ("Luang Prabang was the royal capital of the Kingdom of Laos.", "Laos"),
    ("It is a historic post office building in Nuwara Eliya.", "Sri Lanka"),                           # C18 fact 0
    ("It is said to date from the 7th century.", "China"),
    ("The temple is located in the capital, Hanoi.", "Vietnam"),
    ("The cathedral survived the 1911 earthquake that struck Almaty.", "Kazakhstan"),                   # C20 fact 3
    ("Its architecture blends Khmer and Lao styles.", "Laos"),
    ("It was built by King Anawrahta in the 11th century.", "Myanmar"),
])
def test_background_facts_are_kept(text, country):
    assert pc.unverifiable_claim(text, country) is None


def test_writer_seed_drops_unverifiable_facts_unless_an_admin_verified_the_card():
    facts = [{"text": "Pleiku is a city in Gia Lai Province and the province’s capital.", "kind": "what_it_is"},
             {"text": "North Vietnamese forces took Pleiku in March 1975.", "kind": "history"}]
    card = {"display_name": "Pleiku", "country": "Vietnam", "status": "auto_checked", "facts": json.dumps(facts)}
    assert writer_facts(card) == ["North Vietnamese forces took Pleiku in March 1975."]
    assert writer_facts(dict(card, status="verified")) == [f["text"] for f in facts]


# ── identity rules at seed time (cards built before AA-784) ──────────────────────────────────────────────────────

SEED_LOCATION = {
    "C04": "Location: Thado Koshi, Nepal (the itinerary calls it Thado Koshigaon)",
    "C13": "Location: Barafu Camp, Kilimanjaro, Tanzania (the itinerary calls it Barafu hut)",
    "C14": "Location: Malé, Maldives (the itinerary calls it Colombo International Airport to Male)",
    "C15": "Location: Thien Mu Pagoda, Hue, Vietnam",
}


@pytest.mark.parametrize("cid", sorted(SEED_LOCATION))
def test_writer_seed_location_applies_the_identity_rules_to_stored_cards(cid):
    stored, names = SPOT_CHECK_BAD[cid]
    card = dict(stored, status="auto_checked")
    line = _location_lines(names[0], card["country"], card)[0]
    assert line == SEED_LOCATION[cid] + " (do not place it in any other town, region or country)"
    assert card == dict(stored, status="auto_checked")                  # the stored card is not changed


def test_writer_seed_location_keeps_a_verified_card_as_stored():
    stored, _ = SPOT_CHECK_BAD["C13"]
    assert writer_identity(dict(stored, status="verified")) == ("Moshi", "Kilimanjaro")
    assert writer_identity(dict(stored, status="auto_checked")) == ("", "Kilimanjaro")


# ── a rejected / held card is never duplicated under a new key (C07) ──────────────────────────────────────────────

C07_STORED = {"card_id": "c07", "place_key": "china nepal friendship bridge|nepal and china",
              "display_name": "China–Nepal Friendship Bridge", "locality": "Kodari / Zhangmu", "region": None,
              "country": "Nepal and China", "status": "rejected", "cost_usd": 0.0, "facts": []}


@pytest.mark.parametrize("country,locality,region", [
    ("Nepal", "Kodari", "Bagmati Province"),
    ("China", "Zhangmu", "Tibet Autonomous Region"),
])
def test_a_held_compound_country_card_is_not_replaced_or_duplicated_by_a_single_country_draft(
        country, locality, region):
    db = FakeDB([_atom("a1", "China-Nepal Friendship Bridge", country)], cards=[C07_STORED])
    draft = {"display_name": "China–Nepal Friendship Bridge", "locality": locality, "region": region,
             "country": country, "naming_note": "", "aliases": [],
             "facts": [{"text": "The bridge crosses the Bhote Koshi river.", "kind": "what_it_is"}]}
    out = _ensure(db, ScriptedLLM(draft), ["a1"])
    assert out["linked_new"] == 0 and out["missed"] == 1
    assert list(db.cards) == ["c07"] and db.cards["c07"]["status"] == "rejected"
    assert db.cards["c07"]["country"] == "Nepal and China"                 # not replaced
    assert db.links["a1"] == {"card_id": None, "miss_reason": "identity_rejected"}


@pytest.mark.parametrize("stored_country,draft_country", [
    ("Nepal and China", "India"),        # a compound that does not name the draft's country
    ("Nepal", "Nepal"),                  # a single-country rejected card under a legacy key: AA-772 rule (redraft)
])
def test_a_rejected_card_blocks_only_a_draft_its_compound_country_names(stored_country, draft_country):
    stored = dict(C07_STORED, country=stored_country, place_key="china nepal friendship bridge|kodari|nepal")
    db = FakeDB([_atom("a1", "China-Nepal Friendship Bridge", draft_country)], cards=[stored])
    draft = {"display_name": "China–Nepal Friendship Bridge", "locality": "", "region": "", "country": draft_country,
             "naming_note": "", "aliases": [], "facts": []}
    out = _ensure(db, ScriptedLLM(draft), ["a1"])
    assert out["linked_new"] == 1 and len(db.cards) == 2 and db.cards["c07"]["status"] == "rejected"


# ── prompts + check parsing ─────────────────────────────────────────────────────────────────────────────────────

def test_prompts_carry_the_rules():
    assert "first-level administrative division" in pc._CARD_SYSTEM and "Khumbu" in pc._CARD_SYSTEM
    assert "Never the nearest town" in pc._CARD_SYSTEM and "CURRENT status" in pc._CARD_SYSTEM
    assert '"locality_ok"' in pc._CHECK_SYSTEM and '"region_ok"' in pc._CHECK_SYSTEM
    assert "CURRENT status" in pc._CHECK_SYSTEM and "designed by" in pc._CHECK_SYSTEM


@pytest.mark.parametrize("body,placement", [
    ({"identity_ok": True, "verdicts": []}, {"locality_ok": True, "region_ok": True}),       # an older reply shape
    ({"identity_ok": True, "locality_ok": False, "region_ok": "false", "verdicts": []},
     {"locality_ok": False, "region_ok": False}),
    ({"identity_ok": True, "locality_ok": True, "region_ok": None, "verdicts": []},
     {"locality_ok": True, "region_ok": True}),
])
def test_check_reads_locality_and_region_flags(body, placement):
    client = MagicMock()
    client.generate.return_value = LLMResponse(content=json.dumps(body), model_used="haiku", provider="claude",
                                               cost_usd=0.001)
    with patch.object(pc, "LLMClient", return_value=client):
        out = pc.check_card({"display_name": "X", "country": "Nepal", "facts": []}, {"place": "X"})
    assert out[4] == placement


# ── build path: rules before the check, check flags, fact filter ───────────────────────────────────────────────

class ScriptedLLM:
    """Draft = `card`; check = `check` (+ one 'supported' per fact when verdicts are not given)."""
    def __init__(self, card, check=None):
        self.card, self.check = card, dict(check or {"identity_ok": True})
        self.check_prompts: list[str] = []

    def generate(self, req):
        if req.stage == "a3_place_card":
            body = self.card
        else:
            self.check_prompts.append(req.user_prompt)
            body = dict(self.check)
            body.setdefault("verdicts", ["supported"] * len(self.card.get("facts") or []))
        return LLMResponse(content=json.dumps(body), model_used="m", provider="p", cost_usd=0.002)


BARAFU = {"display_name": "Barafu Camp", "locality": "Moshi", "region": "Kilimanjaro", "country": "Tanzania",
          "naming_note": "", "aliases": [],
          "facts": [{"text": "Barafu Camp is a high camp on Mount Kilimanjaro.", "kind": "what_it_is"},
                    {"text": "The camp currently serves as the last stop before the summit.", "kind": "setting"},
                    {"text": "Its name means ice in Swahili.", "kind": "naming"}]}


def test_build_clears_the_nearest_town_before_the_check_and_drops_status_facts():
    db = FakeDB([_atom("a1", "Barafu hut", "Tanzania")])
    llm = ScriptedLLM(BARAFU)
    out = _ensure(db, llm, ["a1"])
    assert out["linked_new"] == 1
    card = db.cards[db.links["a1"]["card_id"]]
    assert card["locality"] is None and card["region"] == "Kilimanjaro"
    assert "locality: -" in llm.check_prompts[0]                       # the check saw the cleaned identity
    assert [f["text"] for f in card["facts"]] == [BARAFU["facts"][0]["text"], BARAFU["facts"][2]["text"]]


def test_check_flags_clear_a_field_instead_of_rejecting_the_card():
    draft = dict(BARAFU, display_name="Old Goa", locality="Goa Velha", region="Goa", country="India",
                 facts=[{"text": "It was the capital of Portuguese India.", "kind": "history"}])
    db = FakeDB([_atom("a1", "Old Goa", "India")])
    out = _ensure(db, ScriptedLLM(draft, {"identity_ok": True, "locality_ok": False}), ["a1"])
    assert out["linked_new"] == 1
    card = db.cards[db.links["a1"]["card_id"]]
    assert card["locality"] is None and card["region"] == "Goa" and len(card["facts"]) == 1


# ── rebuild (the 11 flagged cards) ───────────────────────────────────────────────────────────────────────────────

class RebuildConn:
    def __init__(self, cards, links, atoms):
        self.cards = {c["card_id"]: dict(c) for c in cards}
        self.links, self.atoms = links, atoms
        self.updates: list[tuple] = []

    async def fetch(self, sql, *args):
        if "FROM acp_shared.place_card WHERE card_id = ANY" in sql:
            return [self.cards[c] for c in args[0] if c in self.cards]
        if "FROM acp_shared.atom_place_card WHERE card_id" in sql:
            return [{"atom_id": a} for a, c in sorted(self.links.items()) if c == args[0]][:args[1]]
        if "v_active_tour_atoms" in sql:
            return [self.atoms[a] for a in args[0] if a in self.atoms]
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        assert "UPDATE acp_shared.place_card" in sql and "status = 'auto_checked'" in sql
        c = self.cards[args[0]]
        if c["status"] != "auto_checked":
            return None
        c.update(locality=args[1], region=args[2], naming_note=args[3], facts=json.loads(args[4]))
        c["cost_usd"] += args[5]
        return args[0]

    async def execute(self, sql, *args):
        assert "SET cost_usd = cost_usd + $2" in sql
        self.cards[args[0]]["cost_usd"] += args[1]

    def pool(self):
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=self)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire.return_value = ctx
        return pool


C13_ID = "b5f67f46-9400-46ca-9d44-662a8465940f"
C02_ID = "932247a2-c6e0-48e4-a04e-9d2ec934148c"
OLD_FACTS = [{"text": "Barafu Camp sits on the south-east route of Kilimanjaro.", "kind": "setting"}]


def _stored(card_id, name, country, locality, region, status="auto_checked", facts=OLD_FACTS):
    return {"card_id": card_id, "place_key": f"{pc.core_key(name)}|{pc.fold(country)}", "display_name": name,
            "locality": locality, "region": region, "country": country, "naming_note": None,
            "facts": json.dumps(facts), "status": status, "cost_usd": 0.01}


def _rebuild(conn, llm, ids):
    with patch.object(pc, "LLMClient", return_value=llm), patch.object(pc, "record_call_with_pool", new=AsyncMock()):
        return asyncio.run(pc.rebuild_place_cards(ids, conn.pool()))


def test_rebuild_rewrites_identity_and_facts_in_place():
    conn = RebuildConn([_stored(C13_ID, "Barafu Camp", "Tanzania", "Moshi", "Kilimanjaro")],
                       {"a1": C13_ID, "a2": C13_ID},
                       {"a1": _atom("a1", "Barafu hut", "Tanzania"), "a2": _atom("a2", "Barafu", "Tanzania")})
    draft = dict(BARAFU, display_name="Barafu Camp (Barafu Hut)")          # the stored name wins
    out = _rebuild(conn, ScriptedLLM(draft), [C13_ID])
    card = conn.cards[C13_ID]
    assert out["outcomes"] == {"rebuilt": 1} and out["cost_usd"] == pytest.approx(0.004)
    assert card["display_name"] == "Barafu Camp" and card["locality"] is None and card["region"] == "Kilimanjaro"
    assert [f["text"] for f in card["facts"]] == [BARAFU["facts"][0]["text"], BARAFU["facts"][2]["text"]]
    row = out["cards"][0]
    assert row["before"]["locality"] == "Moshi" and row["after"]["locality"] is None
    assert {"field": "locality", "reason": "remote_place", "value": "Moshi"} in row["cleared"]


def test_rebuild_keeps_old_facts_minus_unverifiable_when_the_new_check_keeps_none():
    old = [{"text": "Pleiku is a city in Gia Lai Province and the province’s capital.", "kind": "what_it_is"},
           {"text": "North Vietnamese forces took Pleiku in March 1975.", "kind": "history"}]
    conn = RebuildConn([_stored(C02_ID, "Pleiku", "Vietnam", "Pleiku", "Gia Lai", facts=old)], {"a1": C02_ID},
                       {"a1": _atom("a1", "Pleiku", "Vietnam")})
    draft = {"display_name": "Pleiku", "locality": "", "region": "Gia Lai", "country": "Vietnam",
             "facts": [{"text": "A new fact.", "kind": "history"}]}
    out = _rebuild(conn, ScriptedLLM(draft, {"identity_ok": True, "verdicts": ["unsure"]}), [C02_ID])
    assert out["cards"][0]["facts_from"] == "old_filtered"
    assert [f["text"] for f in conn.cards[C02_ID]["facts"]] == [old[1]["text"]]


def test_rebuild_leaves_the_card_when_the_draft_moves_country_or_the_check_rejects():
    conn = RebuildConn([_stored(C13_ID, "Barafu Camp", "Tanzania", "Moshi", "Kilimanjaro")], {"a1": C13_ID},
                       {"a1": _atom("a1", "Barafu hut", "Tanzania")})
    out = _rebuild(conn, ScriptedLLM(dict(BARAFU, country="Kenya")), [C13_ID])
    assert out["outcomes"] == {"country_changed": 1} and conn.cards[C13_ID]["locality"] == "Moshi"
    out = _rebuild(conn, ScriptedLLM(BARAFU, {"identity_ok": False}), [C13_ID])
    assert out["outcomes"] == {"identity_rejected": 1} and conn.cards[C13_ID]["locality"] == "Moshi"
    assert conn.cards[C13_ID]["cost_usd"] == pytest.approx(0.01 + 0.002 + 0.004)   # money spent is recorded


def test_rebuild_skips_reviewed_generic_unlinked_and_unknown_cards():
    verified = _stored("c-v", "Barafu Camp", "Tanzania", "Moshi", "Kilimanjaro", status="verified")
    rejected = _stored("c-r", "Friendship Bridge", "Nepal", "Kodari", "Bagmati Province", status="rejected")
    generic = _stored("c-g", "City Palace", "India", "Udaipur", "Rajasthan")
    lonely = _stored("c-l", "Thien Mu Pagoda", "Vietnam", "Hue", "Central Vietnam")
    conn = RebuildConn([verified, rejected, generic, lonely], {}, {})
    llm = ScriptedLLM(BARAFU)
    out = _rebuild(conn, llm, ["c-v", "c-r", "c-g", "c-l", "c-missing"])
    assert [c["outcome"] for c in out["cards"]] == ["skipped_verified", "skipped_rejected", "skipped_generic",
                                                    "no_atoms", "not_found"]
    assert llm.check_prompts == [] and out["cost_usd"] == 0


def test_rebuild_is_capped():
    with pytest.raises(ValueError):
        _rebuild(RebuildConn([], {}, {}), ScriptedLLM(BARAFU), [f"c{i}" for i in range(pc.REBUILD_MAX_CARDS + 1)])


# ── job + admin entry ────────────────────────────────────────────────────────────────────────────────────────────

def test_job_card_ids_mode_rebuilds_and_validates():
    from services.jobs import place_cards_job as job
    ctx = MagicMock()
    ctx.payload = {"card_ids": [C13_ID, C02_ID]}
    result = {"requested": 2, "outcomes": {"rebuilt": 2}, "cost_usd": 0.01, "cards": []}
    with patch.object(job, "rebuild_place_cards", new=AsyncMock(return_value=result)) as rb, \
            patch.object(job, "ensure_place_cards", new=AsyncMock()) as ens:
        out = asyncio.run(job.run(ctx))
    assert rb.await_args.args[0] == [C13_ID, C02_ID] and out["mode"] == "rebuild" and out["outcomes"] == {"rebuilt": 2}
    ens.assert_not_awaited()
    for bad in ([], "c1", [""], [1], ["c1"], [C13_ID] * (pc.REBUILD_MAX_CARDS + 1)):
        ctx.payload = {"card_ids": bad}
        with pytest.raises(NonRetryable):
            asyncio.run(job.run(ctx))
    ctx.payload = {}
    with pytest.raises(NonRetryable):
        asyncio.run(job.run(ctx))


def test_admin_rebuild_enqueues_one_attempt_job():
    from api.routers import admin_content_quality as router
    from shared.jobs import queue as job_queue
    req = MagicMock()
    body = router.PlaceCardRebuild(card_ids=[C13_ID.upper(), C13_ID, f" {C02_ID} "])
    with patch.object(router, "verify_admin_secret"), \
            patch.object(job_queue, "enqueue", new=AsyncMock(return_value=("job-1", True))) as enq:
        out = asyncio.run(router.rebuild_place_cards(body, req, x_admin_secret="s", x_admin_user_id="u1"))
    assert out == {"job_id": "job-1", "created": True, "card_ids": [C13_ID, C02_ID]}
    assert enq.await_args.args[1:] == ("place_cards_build", {"card_ids": [C13_ID, C02_ID]})
    assert enq.await_args.kwargs == {"max_attempts": 1, "created_by": "u1"}
    with patch.object(router, "verify_admin_secret"), pytest.raises(HTTPException) as e:
        asyncio.run(router.rebuild_place_cards(router.PlaceCardRebuild(card_ids=["C13"]), req, x_admin_secret="s"))
    assert e.value.status_code == 422


def test_admin_rebuild_route_has_a_staff_policy():
    from api.core.staff_authz import POLICY, QUALITY_WRITE
    assert POLICY[("POST", "/admin/content-quality/place-cards/rebuild")] == QUALITY_WRITE
