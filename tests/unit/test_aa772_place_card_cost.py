"""AA-772 (S234) — place-card cost cut: free rule skip, grouping by place, alias / core-name reuse,
negative cache, ≤1 draft per place per call, merge + duplicates, job cap, migration 221."""
import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.acp_shared import place_cards as pc
from shared.llm_client.models import LLMResponse
from shared.llm_client.role_config import SAFE_DEFAULTS

MIGRATION = Path(__file__).resolve().parents[2] / "api" / "migrations" / "221_s234_place_card_cache_and_stages.sql"


# ── pure rules ──────────────────────────────────────────────────────────────────────────────────

SKIPPED = ["standard ger camp", "ger camp", "local ger camp", "your hotel", "Your Hotel", "hotel", "city center",
           "riverside", "ruins of an ancient monastery", "a local nomadic family", "the hotel",
           "Chinggis Khaan International Airport", "Samdrup Jongkhar Border Gate", "Sod Camp",
           "Ulziit Khishig Ger Camp", "Khaiphaen Restaurant", "Paro Railway Station"]
KEPT = ["Paro", "Punakha Dzong", "Kuang Si Falls", "Lake Khövsgöl", "Mai Chau", "Craft Bazaar", "Chorten Kora",
        "Everest Base Camp", "Tiger's Nest", "Kaja Throm Farmers Market", "Nong Khouay", "Pha Din Pass",
        "Điện Biên Phủ"]


@pytest.mark.parametrize("place", SKIPPED)
def test_generic_and_logistics_places_are_skipped(place):
    assert pc.card_skip_reason(place) in ("unnamed", "logistics_place")


@pytest.mark.parametrize("place", KEPT)
def test_named_places_are_kept(place):
    assert pc.card_skip_reason(place) is None


@pytest.mark.parametrize("place,atype", [("Hua Hin Railway Station", "culture"), ("Dorasan Station", "culture"),
                                         ("Gulmarg hill station", "other"), ("Everest View Hotel", "trek"),
                                         ("Tsaatan reindeer herding camps", "culture"),
                                         ("Hua Hin Elephant Camp", "other")])
def test_logistics_noun_on_a_non_logistics_moment_keeps_its_card(place, atype):
    """Review fix: ranking keeps these rankable, so the card rule must not drop them."""
    assert pc.card_skip_reason(place, atype) is None


@pytest.mark.parametrize("atype", ["stay", "transit", "food", None])
def test_logistics_noun_on_a_logistics_moment_is_skipped(atype):
    assert pc.card_skip_reason("Paro Railway Station", atype) == "logistics_place"


def test_skip_reasons():
    assert pc.card_skip_reason("ruins of an ancient monastery") == "unnamed"
    assert pc.card_skip_reason("Your Hotel") == "unnamed"
    assert pc.card_skip_reason("Sod Camp") == "logistics_place"
    assert pc.card_skip_reason("  ") == "empty"


def test_unnamed_rule_is_the_ranking_rule():
    """C9: one rule — a place ranking calls `unnamed_place` never gets a card."""
    from services.acp_contract.atom_ranking import classify_exclusion
    for place in SKIPPED + KEPT:
        if classify_exclusion(place, "visit") == "unnamed_place":
            assert pc.card_skip_reason(place) is not None, place


def test_fold_keeps_letters_with_accents():
    assert pc.fold("Điện Biên Phủ") == pc.fold("Dien Bien Phu") == "dien bien phu"
    assert pc.fold("Khövsgöl") == "khovsgol"
    assert pc.fold("Tiger’s Nest") == pc.fold("Tiger's Nest") == "tigers nest"


def test_place_norm_drops_leading_fillers():
    assert pc.place_norm("the Standard Ger Camp") == "ger camp"
    assert pc.place_norm("Local") == "local"


def test_core_key_merges_neutral_kind_words_only():
    assert pc.core_key("Lake Khövsgöl") == pc.core_key("Khövsgöl Lake") == pc.core_key("Khövsgöl Nuur")
    assert pc.core_key("Paro Town") == pc.core_key("Paro")
    assert pc.core_key("Punakha Dzong") != pc.core_key("Punakha")
    assert pc.core_key("Kharkhorin Museum") != pc.core_key("Kharkhorin")
    assert pc.core_key("Lake") == "lake"
    assert pc.name_forms("Bayanzag (Flaming Cliffs)") == {"bayanzag flaming cliffs", "bayanzag", "flaming cliffs"}


def test_place_key_has_no_locality():
    assert pc.place_key("Khövsgöl Lake", "Mongolia") == "khovsgol|mongolia"


def test_miss_is_active_policy():
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    row = {"rule_version": pc.RULE_VERSION, "prompt_sha": pc.PROMPT_SHA, "retry_after": now + timedelta(days=1)}
    assert pc.miss_is_active(row, now)
    assert not pc.miss_is_active(dict(row, retry_after=now - timedelta(seconds=1)), now)
    assert not pc.miss_is_active(dict(row, prompt_sha="old"), now)
    assert not pc.miss_is_active(dict(row, rule_version=pc.RULE_VERSION - 1), now)
    assert not pc.miss_is_active(None, now)
    assert pc.MISS_RETRY["no_name"] > pc.MISS_RETRY["identity_rejected"] > pc.MISS_RETRY["error"]


def test_prompts_allow_a_neighbouring_country_and_ask_fewer_facts():
    client = MagicMock()
    client.generate.return_value = LLMResponse(content='{"identity_ok": true, "verdicts": []}', model_used="m",
                                               provider="p", cost_usd=0)
    atom = {"place": "Mai Chau", "country": "Laos", "tour_name": "Laos to Vietnam by Bicycle", "evidence": "Ride."}
    with patch.object(pc, "LLMClient", return_value=client):
        pc.draft_card(atom)
        pc.check_card({"display_name": "Mai Chau", "country": "Vietnam", "facts": []}, atom)
    draft, check = (c.args[0] for c in client.generate.call_args_list)
    assert draft.max_tokens == pc.DRAFT_MAX_TOKENS == 4000  # AA-784: GPT-6 Luna reasons inside max_tokens
    assert check.max_tokens == pc.CHECK_MAX_TOKENS == 1024
    assert "4-6" in draft.system_prompt and '"aliases"' in draft.system_prompt
    assert "neighbouring country" in draft.user_prompt
    assert "tour sold as Laos; the place may be in a neighbouring country" in check.user_prompt
    assert "Laos to Vietnam" in check.user_prompt
    assert pc.MAX_FACTS == 6


# ── fake DB for ensure_place_cards ──────────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self, atoms, cards=(), aliases=None):
        self.atoms = {a["atom_id"]: a for a in atoms}
        self.links: dict[str, dict] = {}
        self.cards = {c["card_id"]: dict(c) for c in cards}
        self.aliases = dict(aliases or {})
        self.misses: dict[tuple, dict] = {}

    # asyncpg-like surface
    async def fetch(self, sql, *args):
        if "FROM acp_shared.atom_place_card apc" in sql:
            return [{"atom_id": a} for a in args[0] if self.links.get(a, {}).get("card_id")
                    and self.cards[self.links[a]["card_id"]]["status"] != "rejected"]
        if "v_active_tour_atoms" in sql:
            return [self.atoms[a] for a in args[0] if a in self.atoms]
        if "FROM acp_shared.place_card_alias a" in sql:
            return [{"alias_norm": k[0], "country_norm": k[1], "card_id": v} for k, v in self.aliases.items()
                    if k[0] in args[0] and k[1] in args[1] and self.cards[v]["status"] != "rejected"]
        if "FROM acp_shared.place_card_miss" in sql:
            return [dict(m, place_norm=k[0], country_norm=k[1]) for k, m in self.misses.items()
                    if k[0] in args[0] and k[1] in args[1]]
        if "AA-809" in sql:                             # post-draft resolver: one country's cards
            return [c for c in self.cards.values() if c["status"] != "rejected"
                    and (c["place_key"].endswith(args[0][1:]) or (c.get("country") or "").lower() == args[1])]
        if "AA-784 held" in sql:                        # rejected cards under another key, by country spelling
            return [c for c in self.cards.values() if c["status"] == "rejected" and c["place_key"] != args[0]
                    and any(p.strip("%") in (c.get("country") or "").lower() for p in args[1])]
        if "split_part(place_key" in sql:
            return [c for c in self.cards.values() if c["status"] != "rejected"
                    and c["place_key"].split("|")[0] in args[0]]
        if "FROM acp_shared.place_card" in sql:
            return [c for c in self.cards.values() if c["status"] != "rejected"
                    and (c["place_key"] in args[0] or c["place_key"].count("|") == 2)]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        assert "WHERE place_key = $1 FOR UPDATE" in sql
        return next((dict(c) for c in self.cards.values() if c["place_key"] == args[0]), None)

    async def fetchval(self, sql, *args):
        assert "INSERT INTO acp_shared.place_card" in sql
        key, name, locality, region, country, note, facts, cost = args
        for c in self.cards.values():
            if c["place_key"] == key:
                c["cost_usd"] += cost
                return c["card_id"]
        cid = str(uuid.uuid4())
        self.cards[cid] = {"card_id": cid, "place_key": key, "display_name": name, "locality": locality,
                           "region": region, "country": country, "status": "auto_checked", "cost_usd": cost,
                           "facts": json.loads(facts)}
        return cid

    async def execute(self, sql, *args):
        if "INSERT INTO acp_shared.place_card_miss" in sql:
            pn, cn, reason, rule, sha, model, retry = args
            old = self.misses.get((pn, cn), {"attempts": 0})
            self.misses[(pn, cn)] = {"reason": reason, "rule_version": rule, "prompt_sha": sha,
                                     "retry_after": retry, "attempts": old["attempts"] + 1}
        elif "DELETE FROM acp_shared.place_card_miss" in sql:
            self.misses.pop((args[0], args[1]), None)
        elif "status = 'auto_checked'" in sql:          # a rejected card replaced by a new identity
            c = self.cards[args[0]]
            c.update(display_name=args[1], locality=args[2], region=args[3], country=args[4],
                     facts=json.loads(args[6]), status="auto_checked", cost_usd=c["cost_usd"] + args[7])
        elif "SET cost_usd = cost_usd + $2" in sql:
            self.cards[args[0]]["cost_usd"] += args[1]
        else:
            raise AssertionError(sql)

    async def executemany(self, sql, rows):
        for r in rows:
            if "atom_place_card" in sql and "miss_reason) VALUES ($1, $2::uuid, NULL)" in sql:
                self.links[r[0]] = {"card_id": r[1], "miss_reason": None}
            elif "atom_place_card" in sql:
                if not self.links.get(r[0], {}).get("card_id"):
                    self.links[r[0]] = {"card_id": None, "miss_reason": r[1]}
            elif "place_card_alias" in sql:
                self.aliases.setdefault((r[0], r[1]), r[2])
            else:
                raise AssertionError(sql)

    def transaction(self):
        tx = MagicMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=False)
        return tx

    def pool(self):
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=self)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire.return_value = ctx
        return pool


def _atom(atom_id, place, country="Mongolia", evidence="Visit it."):
    return {"atom_id": atom_id, "place": place, "country": country, "evidence": evidence, "itinerary_day": 1,
            "tour_name": "Tour"}


GOOD_CARD = {"display_name": "Khövsgöl Lake", "locality": "Khatgal", "region": "Khövsgöl Province",
             "country": "Mongolia", "aliases": ["Lake Hovsgol"],
             "facts": [{"text": "It is a freshwater lake.", "kind": "what_it_is"}]}


class FakeLLM:
    """Answers draft/check by stage; records the draft prompts."""
    def __init__(self, card=GOOD_CARD, identity_ok=True, fail=False):
        self.card, self.identity_ok, self.fail = card, identity_ok, fail
        self.drafts: list[str] = []
        self.checks = 0

    def generate(self, req):
        if self.fail:
            raise RuntimeError("bedrock down")
        if req.stage == "a3_place_card":
            self.drafts.append(req.user_prompt)
            body = self.card
        else:
            self.checks += 1
            body = {"identity_ok": self.identity_ok, "verdicts": ["supported"] * len(self.card.get("facts") or [])}
        return LLMResponse(content=json.dumps(body), model_used="haiku", provider="claude", cost_usd=0.002)


def _ensure(db, llm, ids, **kw):
    with patch.object(pc, "LLMClient", return_value=llm), \
            patch.object(pc, "record_call_with_pool", new=AsyncMock()):
        return asyncio.run(pc.ensure_place_cards(ids, db.pool(), **kw))


def test_atoms_of_one_place_share_one_draft_and_all_get_linked():
    db = FakeDB([_atom("a1", "Lake Khövsgöl"), _atom("a2", "Lake Khovsgol"), _atom("a3", "the Lake Khövsgöl")])
    llm = FakeLLM()
    out = _ensure(db, llm, ["a1", "a2", "a3"])
    assert len(llm.drafts) == 1 and llm.checks == 1
    assert out["drafted"] == 1 and out["built"] == 3 and out["places"] == 1
    card_ids = {db.links[a]["card_id"] for a in ("a1", "a2", "a3")}
    assert len(card_ids) == 1 and None not in card_ids
    card = db.cards[card_ids.pop()]
    assert card["place_key"] == "khovsgol|mongolia"
    assert ("lake hovsgol", "mongolia") in db.aliases and ("lake khovsgol", "mongolia") in db.aliases


def test_guard_at_most_one_draft_per_place_norm_per_call():
    """lessons.md: drafts per resulting card must stay ≤1.2 — here exactly ≤1 per place group."""
    places = ["Lake Khövsgöl", "Lake Khovsgol", "Mai Chau", "MAI CHAU", "Mai  Chau", "Paro", "PARO"]
    db = FakeDB([_atom(f"a{i}", p) for i, p in enumerate(places)])
    llm = FakeLLM()
    _ensure(db, llm, [f"a{i}" for i in range(len(places))])
    drafted_places = [re.search(r"NAMES IT: (.*)\n", d).group(1) for d in llm.drafts]
    norms = [pc.place_norm(p) for p in drafted_places]
    assert len(norms) == len(set(norms)) == 3


def test_rule_skipped_atoms_cost_nothing_and_record_why():
    db = FakeDB([_atom("a1", "standard ger camp"), _atom("a2", "Sod Camp"), _atom("a3", "ruins of an ancient monastery")])
    llm = FakeLLM()
    out = _ensure(db, llm, ["a1", "a2", "a3"])
    assert llm.drafts == [] and out["skipped_by_rule"] == 3 and out["drafted"] == 0
    assert db.links["a2"] == {"card_id": None, "miss_reason": "logistics_place"}
    assert db.links["a3"]["miss_reason"] == "unnamed"


def test_negative_cache_blocks_a_second_draft_until_retry_after_or_prompt_change():
    db = FakeDB([_atom("a1", "Nong Khouay", "Laos")])
    llm = FakeLLM(card={"display_name": "", "facts": []})
    out1 = _ensure(db, llm, ["a1"])
    assert out1["missed"] == 1 and len(llm.drafts) == 1
    assert db.misses[("nong khouay", "laos")]["reason"] == "no_name"
    assert db.links["a1"] == {"card_id": None, "miss_reason": "no_name"}
    out2 = _ensure(db, llm, ["a1"])                     # T8/T9 re-run: 0 calls
    assert out2["skipped_cached"] == 1 and len(llm.drafts) == 1
    db.misses[("nong khouay", "laos")]["prompt_sha"] = "older-prompt"
    _ensure(db, llm, ["a1"])                            # a prompt change re-tries once
    assert len(llm.drafts) == 2 and db.misses[("nong khouay", "laos")]["attempts"] == 2
    db.misses[("nong khouay", "laos")]["retry_after"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    _ensure(db, llm, ["a1"])                            # expired → one more try
    assert len(llm.drafts) == 3


def test_force_ignores_the_miss_cache():
    db = FakeDB([_atom("a1", "Nong Khouay", "Laos")])
    llm = FakeLLM(card={"display_name": "", "facts": []})
    _ensure(db, llm, ["a1"])
    _ensure(db, llm, ["a1"], force=True)
    assert len(llm.drafts) == 2


def test_identity_rejection_and_errors_are_cached_with_their_reason():
    db = FakeDB([_atom("a1", "Pha Din Pass", "Laos")])
    _ensure(db, FakeLLM(identity_ok=False), ["a1"])
    assert db.misses[("pha din pass", "laos")]["reason"] == "identity_rejected"
    db2 = FakeDB([_atom("a1", "Pha Din Pass", "Laos")])
    out = _ensure(db2, FakeLLM(fail=True), ["a1"])
    assert out["errors"] == 1 and db2.misses[("pha din pass", "laos")]["reason"] == "error"
    assert db2.misses[("pha din pass", "laos")]["retry_after"] < datetime.now(timezone.utc) + timedelta(days=2)


def test_a_card_resets_its_miss():
    db = FakeDB([_atom("a1", "Lake Khövsgöl")])
    db.misses[("lake khovsgol", "mongolia")] = {"reason": "error", "rule_version": pc.RULE_VERSION,
                                                "prompt_sha": pc.PROMPT_SHA, "attempts": 1,
                                                "retry_after": datetime.now(timezone.utc) - timedelta(hours=1)}
    _ensure(db, FakeLLM(), ["a1"])
    assert ("lake khovsgol", "mongolia") not in db.misses and db.links["a1"]["card_id"]


def test_alias_hit_links_with_no_llm_call():
    card = {"card_id": "c-bay", "place_key": "bayanzag|mongolia", "display_name": "Bayanzag (Flaming Cliffs)",
            "country": "Mongolia", "status": "auto_checked", "cost_usd": 0.01}
    db = FakeDB([_atom("a1", "Flaming Cliffs")], cards=[card], aliases={("flaming cliffs", "mongolia"): "c-bay"})
    llm = FakeLLM()
    out = _ensure(db, llm, ["a1"])
    assert llm.drafts == [] and out["linked_existing"] == 1 and db.links["a1"]["card_id"] == "c-bay"


def test_legacy_card_is_reused_by_core_name_before_the_key_backfill():
    """S233 cards still carry the 3-part key (name|locality|country): matched on the core name."""
    legacy = {"card_id": "c-old", "place_key": "kh vsg l lake|kh vsg l province|mongolia",
              "display_name": "Khövsgöl Lake", "country": "Mongolia", "status": "auto_checked", "cost_usd": 0.01}
    db = FakeDB([_atom("a1", "Lake Khövsgöl"), _atom("a2", "Khovsgol Nuur")], cards=[legacy])
    llm = FakeLLM()
    out = _ensure(db, llm, ["a1", "a2"])
    assert llm.drafts == [] and out["linked_existing"] == 2
    assert db.links["a1"]["card_id"] == db.links["a2"]["card_id"] == "c-old"


def test_rejected_cards_are_never_reused():
    rejected = {"card_id": "c-rej", "place_key": "paro|bhutan", "display_name": "Paro", "country": "Bhutan",
                "status": "rejected", "cost_usd": 0.01}
    db = FakeDB([_atom("a1", "Paro", "Bhutan")], cards=[rejected], aliases={("paro", "bhutan"): "c-rej"})
    llm = FakeLLM(card=dict(GOOD_CARD, display_name="Paro", country="Bhutan"))
    _ensure(db, llm, ["a1"])
    assert len(llm.drafts) == 1


def _existing(status, **kw):
    return dict({"card_id": "c1", "place_key": "khovsgol|mongolia", "display_name": "Khövsgöl Lake",
                 "locality": None, "region": "Khövsgöl Province", "country": "Mongolia", "status": status,
                 "cost_usd": 0.01}, **kw)


def test_key_conflict_keeps_the_card_but_adds_the_cost():
    db = FakeDB([_atom("a1", "Hovsgol")], cards=[_existing("auto_checked")])
    _ensure(db, FakeLLM(), ["a1"])
    assert db.links["a1"]["card_id"] == "c1" and db.cards["c1"]["cost_usd"] == pytest.approx(0.014)


def test_key_conflict_with_a_rejected_card_of_the_same_identity_links_nothing_and_caches_a_miss():
    """Review fix: atoms were linked to the rejected card (invisible to writers) for good."""
    db = FakeDB([_atom("a1", "Hovsgol")], cards=[_existing("rejected")])
    llm = FakeLLM()
    out = _ensure(db, llm, ["a1"])
    assert out["missed"] == 1 and db.links["a1"] == {"card_id": None, "miss_reason": "identity_rejected"}
    assert db.cards["c1"]["status"] == "rejected" and db.cards["c1"]["cost_usd"] == pytest.approx(0.014)
    assert db.misses[("hovsgol", "mongolia")]["reason"] == "identity_rejected"
    _ensure(db, llm, ["a1"])                            # cached: no second paid draft
    assert len(llm.drafts) == 1


def test_key_conflict_with_a_rejected_card_and_a_new_identity_replaces_it():
    db = FakeDB([_atom("a1", "Hovsgol")], cards=[_existing("rejected", locality="Murun", region="Wrong")])
    _ensure(db, FakeLLM(), ["a1"])
    card = db.cards["c1"]
    assert db.links["a1"]["card_id"] == "c1" and card["status"] == "auto_checked"
    # AA-784: a lake has no locality — the drafted nearest town (Khatgal) is cleared before the check
    assert card["locality"] is None and card["facts"] == GOOD_CARD["facts"]


def test_atoms_linked_to_a_rejected_card_are_resolved_again():
    db = FakeDB([_atom("a1", "Lake Khövsgöl")], cards=[_existing("rejected", place_key="old|key|mongolia")])
    db.links["a1"] = {"card_id": "c1", "miss_reason": None}
    out = _ensure(db, FakeLLM(), ["a1"])
    assert out["already_linked"] == 0 and out["drafted"] == 1
    assert db.links["a1"]["card_id"] not in (None, "c1")


# ── generic names (review fix: City Palace Jaipur != Udaipur) ─────────────────────────────────────

def test_core_key_strips_neutral_words_only_at_the_edges_of_a_distinctive_name():
    assert pc.core_key("City Palace") == "city palace" != pc.core_key("Lake Palace") == "lake palace"
    assert pc.core_key("Old Town") == "old town" != pc.core_key("Old City") == "old city"
    assert pc.core_key("Ho Chi Minh City") == "ho chi minh"
    assert pc.core_key("Khongoryn Els") == "khongoryn"
    assert pc.core_key("Gobi Sand Dunes") == "gobi"
    assert pc.core_key("Hoi An Old Town") == "hoi an old town"


@pytest.mark.parametrize("name", ["City Palace", "Lake Palace", "Night Market", "National Museum", "Old Quarter",
                                  "Central Market", "Grand Palace", "Old Town"])
def test_generic_names(name):
    assert pc.is_generic_name(name)


@pytest.mark.parametrize("name", ["Khövsgöl Lake", "Punakha Dzong", "Hoi An Old Town", "Jaipur City Palace",
                                  "Bayanzag (Flaming Cliffs)", "Tiger's Nest"])
def test_distinctive_names(name):
    assert not pc.is_generic_name(name)


def test_place_key_keeps_the_locality_for_a_generic_name():
    assert pc.place_key("City Palace", "India", "Jaipur") != pc.place_key("City Palace", "India", "Udaipur")
    assert pc.place_key("City Palace", "India", "Jaipur") != pc.place_key("Lake Palace", "India", "Jaipur")
    assert pc.place_key("Khövsgöl Lake", "Mongolia", "Khatgal") == "khovsgol|mongolia"


def _palace(locality):
    return {"display_name": "City Palace", "locality": locality, "region": "Rajasthan", "country": "India",
            "aliases": ["City Palace"], "facts": [{"text": f"It is in {locality}.", "kind": "setting"}]}


def test_city_palace_in_two_cities_stays_two_cards_and_is_never_linked_by_name():
    db = FakeDB([_atom("a1", "City Palace", "India", "Visit the City Palace in Jaipur.")])
    _ensure(db, FakeLLM(card=_palace("Jaipur")), ["a1"])
    assert not any(k[0] == "city palace" for k in db.aliases)          # no country-wide generic alias
    db.atoms["a2"] = _atom("a2", "City Palace", "India", "Tour the City Palace on Lake Pichola in Udaipur.")
    db.atoms["a3"] = _atom("a3", "Lake Palace", "India", "See the Lake Palace from the boat.")
    llm = FakeLLM(card=_palace("Udaipur"))
    out = _ensure(db, llm, ["a2", "a3"])
    assert out["linked_existing"] == 0 and len(llm.drafts) == 2
    assert db.links["a2"]["card_id"] != db.links["a1"]["card_id"]
    assert db.cards[db.links["a2"]["card_id"]]["locality"] == "Udaipur"


def test_generic_name_reuses_the_card_whose_locality_the_sentence_names():
    db = FakeDB([_atom("a1", "City Palace", "India", "Visit the City Palace in Jaipur.")])
    _ensure(db, FakeLLM(card=_palace("Jaipur")), ["a1"])
    db.atoms["a2"] = _atom("a2", "City Palace", "India", "Afternoon at the City Palace, Jaipur.")
    llm = FakeLLM()
    out = _ensure(db, llm, ["a2"])
    assert llm.drafts == [] and out["linked_existing"] == 1
    assert db.links["a2"]["card_id"] == db.links["a1"]["card_id"]


def test_night_market_in_two_cities_is_not_auto_linked_and_misses_stay_per_atom():
    db = FakeDB([_atom("a1", "Night Market", "Laos", "Evening at the Luang Prabang night market.")])
    market = {"display_name": "Night Market", "locality": "Luang Prabang", "region": "", "country": "Laos",
              "aliases": [], "facts": [{"text": "Stalls line Sisavangvong Road.", "kind": "setting"}]}
    _ensure(db, FakeLLM(card=market), ["a1"])
    db.atoms["a2"] = _atom("a2", "Night Market", "Laos", "Stroll the riverside market in Vientiane.")
    llm = FakeLLM(card={"display_name": "", "facts": []})
    _ensure(db, llm, ["a2"])
    assert len(llm.drafts) == 1 and db.links["a2"]["card_id"] is None
    assert ("night market", "laos") not in db.misses                  # the miss does not block the country


def test_generic_card_with_no_locality_is_a_miss():
    db = FakeDB([_atom("a1", "Old Town", "Vietnam")])
    out = _ensure(db, FakeLLM(card=dict(_palace(""), display_name="Old Town", region="", country="Vietnam")), ["a1"])
    assert out["missed"] == 1 and not db.cards


def test_pick_local_card_needs_exactly_one_locality_in_the_sentence():
    cards = [{"card_id": "j", "display_name": "City Palace", "locality": "Jaipur", "country": "India"},
             {"card_id": "u", "display_name": "City Palace", "locality": "Udaipur", "country": "India"}]
    assert pc.pick_local_card("City Palace", "India", "Visit Udaipur's City Palace — Udaipur", cards) == "u"
    assert pc.pick_local_card("City Palace", "India", "Jaipur then Udaipur", cards) is None
    assert pc.pick_local_card("City Palace", "India", "Visit the palace.", cards) is None
    assert pc.pick_local_card("City Palace", "Nepal", "Jaipur", cards) is None


def test_cross_border_place_is_found_again_under_the_tour_country():
    """A Vietnamese place on a tour sold as Laos: the alias is stored under 'laos' too."""
    db = FakeDB([_atom("a1", "Mai Chau", "Laos")])
    llm = FakeLLM(card=dict(GOOD_CARD, display_name="Mai Chau", country="Vietnam", aliases=[]))
    _ensure(db, llm, ["a1"])
    db.atoms["a2"] = _atom("a2", "Mai Chau", "Laos")
    _ensure(db, llm, ["a2"])
    assert len(llm.drafts) == 1 and db.links["a2"]["card_id"] == db.links["a1"]["card_id"]
    assert ("mai chau", "vietnam") in db.aliases and ("mai chau", "laos") in db.aliases


def test_max_places_caps_paid_drafts():
    db = FakeDB([_atom(f"a{i}", f"Place{i} Peak") for i in range(5)])
    llm = FakeLLM(card={"display_name": "", "facts": []})
    out = _ensure(db, llm, [f"a{i}" for i in range(5)], max_places=2)
    assert len(llm.drafts) == 2 and out["over_cap"] == 3


# ── duplicates + merge ──────────────────────────────────────────────────────────────────────────

def _card(cid, name, country="Mongolia", atoms=1):
    return {"card_id": cid, "display_name": name, "country": country, "atoms": atoms}


def test_duplicate_clusters():
    cards = [_card("1", "Khövsgöl Lake"), _card("2", "Lake Khövsgöl"), _card("3", "Khovsgol Nuur"),
             _card("4", "Paro", "Bhutan"), _card("5", "Paro Town", "Bhutan"),
             _card("6", "Bayanzag (Flaming Cliffs)"), _card("7", "Flaming Cliffs"),
             _card("8", "Ulaanbaatar"), _card("9", "Paro", "Mongolia"), _card("10", "Gobi")]
    clusters = pc.duplicate_clusters(cards, [{"alias_norm": "the gobi desert", "country_norm": "mongolia",
                                              "card_id": "10"}])
    groups = {frozenset(c["card_id"] for c in cl["cards"]): cl["reasons"] for cl in clusters}
    assert groups[frozenset({"1", "2", "3"})] == ["same_core_name"]
    assert frozenset({"4", "5"}) in groups           # Paro / Paro Town (Bhutan only)
    assert frozenset({"6", "7"}) in groups           # parenthetical name
    assert all("9" not in g and "8" not in g and "10" not in g for g in groups)


def test_duplicate_clusters_skip_rejected_cards_and_split_generic_names_by_locality():
    cards = [_card("1", "Khövsgöl Lake"), dict(_card("2", "Lake Khövsgöl"), status="rejected"),
             dict(_card("3", "City Palace", "India"), locality="Jaipur"),
             dict(_card("4", "City Palace", "India"), locality="Udaipur"),
             dict(_card("5", "City Palace", "India"), locality="jaipur")]
    groups = [frozenset(c["card_id"] for c in cl["cards"]) for cl in pc.duplicate_clusters(cards)]
    assert groups == [frozenset({"3", "5"})]


def test_duplicate_clusters_by_alias():
    cards = [_card("1", "Erdene Zuu Monastery"), _card("2", "Erdene Zuu Khiid")]
    clusters = pc.duplicate_clusters(cards, [{"alias_norm": "erdene zuu khiid", "country_norm": "mongolia",
                                              "card_id": "1"}])
    assert clusters[0]["reasons"] == ["alias"]


def test_merged_facts_never_drops_and_caps_additions():
    keep = [{"text": f"Fact {i}.", "kind": "history"} for i in range(7)]
    assert pc.merged_facts(keep, [[{"text": "New.", "kind": "setting"}]]) == keep
    out = pc.merged_facts(json.dumps(keep[:2]), [[{"text": "fact 0", "kind": "x"}, {"text": "New.", "kind": "s"}]])
    assert [f["text"] for f in out] == ["Fact 0.", "Fact 1.", "New."]


class MergeConn:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        return [r for r in self.rows if r["card_id"] in args[0]]

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return 3

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))

    async def executemany(self, sql, rows):
        self.calls.append(("executemany", sql, rows))


K, F = str(uuid.uuid4()), str(uuid.uuid4())


def _merge_rows(country2="Mongolia"):
    return [{"card_id": K, "display_name": "Khövsgöl Lake", "country": "Mongolia",
             "facts": [{"text": "A.", "kind": "x"}], "cost_usd": 0.01},
            {"card_id": F, "display_name": "Lake Khövsgöl", "country": country2,
             "facts": [{"text": "B.", "kind": "x"}], "cost_usd": 0.02}]


def test_merge_cards_relinks_aliases_sums_cost_and_deletes():
    conn = MergeConn(_merge_rows())
    out = asyncio.run(pc.merge_cards(conn, K, [F, K]))
    assert out["atoms_moved"] == 3 and out["merged_ids"] == [F] and out["cost_usd"] == pytest.approx(0.03)
    sqls = [c[1] for c in conn.calls]
    assert any("UPDATE acp_shared.atom_place_card" in s for s in sqls)
    assert any("UPDATE acp_shared.place_card_alias" in s for s in sqls)
    alias = next(c for c in conn.calls if c[0] == "executemany")
    assert (("lake khovsgol", "mongolia", K, "merge")) in alias[2]
    upd = next(c for c in conn.calls if "SET facts" in c[1])
    assert [f["text"] for f in json.loads(upd[2][1])] == ["A.", "B."]
    assert "DELETE FROM acp_shared.place_card WHERE" in sqls[-1] and conn.calls[-1][2] == ([F],)


def test_merge_takes_no_facts_from_a_rejected_card_and_adds_none_to_a_verified_one():
    rows = _merge_rows()
    rows[1]["status"] = "rejected"
    conn = MergeConn(rows)
    asyncio.run(pc.merge_cards(conn, K, [F]))
    upd = next(c for c in conn.calls if "SET facts" in c[1])
    assert [f["text"] for f in json.loads(upd[2][1])] == ["A."]
    rows = _merge_rows()
    rows[0]["status"] = "verified"
    conn = MergeConn(rows)
    out = asyncio.run(pc.merge_cards(conn, K, [F]))
    assert out["facts"] == 1 and out["atoms_moved"] == 3
    rows = _merge_rows()
    rows[0]["status"] = "rejected"
    with pytest.raises(pc.MergeError):
        asyncio.run(pc.merge_cards(MergeConn(rows), K, [F]))


def test_merge_cards_rejects_other_country_and_missing_cards():
    with pytest.raises(pc.MergeError):
        asyncio.run(pc.merge_cards(MergeConn(_merge_rows("Bhutan")), K, [F]))
    with pytest.raises(LookupError):
        asyncio.run(pc.merge_cards(MergeConn(_merge_rows()[:1]), K, [F]))
    with pytest.raises(pc.MergeError):
        asyncio.run(pc.merge_cards(MergeConn(_merge_rows()), K, [K]))


# ── router ──────────────────────────────────────────────────────────────────────────────────────

SECRET = "s3cret"


@pytest.fixture
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


def _request(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    req = MagicMock()
    req.app.state.pool.acquire.return_value = ctx
    return req


@pytest.mark.asyncio
async def test_merge_endpoint_writes_audit_row_in_the_same_transaction(_secret):
    from api.routers import admin_content_quality as cq
    conn = AsyncMock()
    result = {"keep_id": K, "merged_ids": [F], "atoms_moved": 2}
    with patch.object(cq.place_cards, "merge_cards", new=AsyncMock(return_value=result)) as merge:
        out = await cq.merge_place_cards(K, cq.PlaceCardMerge(from_ids=[F]), _request(conn),
                                         x_admin_secret=SECRET, x_admin_user_id="u1")
    assert out == result and merge.await_args.args[1:] == (K, [F])
    audit = conn.execute.await_args.args
    assert "acp_shared.audit_log" in audit[0] and audit[3] == "place_card.merge" and audit[5] == K
    conn.transaction.assert_called_once()


@pytest.mark.asyncio
async def test_merge_endpoint_maps_errors(_secret):
    from api.routers import admin_content_quality as cq
    with pytest.raises(HTTPException) as e:
        await cq.merge_place_cards("not-a-uuid", cq.PlaceCardMerge(from_ids=[F]), _request(AsyncMock()),
                                   x_admin_secret=SECRET)
    assert e.value.status_code == 422
    for exc, code in ((pc.MergeError("x"), 422), (LookupError("x"), 404)):
        conn = AsyncMock()
        with patch.object(cq.place_cards, "merge_cards", new=AsyncMock(side_effect=exc)):
            with pytest.raises(HTTPException) as e:
                await cq.merge_place_cards(K, cq.PlaceCardMerge(from_ids=[F]), _request(conn), x_admin_secret=SECRET)
        assert e.value.status_code == code
        conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicates_endpoint(_secret):
    from api.routers import admin_content_quality as cq
    conn = AsyncMock()
    conn.fetch.side_effect = [
        [dict(_card("1", "Paro", "Bhutan"), facts=json.dumps([{"text": "x"}]), cost_usd=0),
         dict(_card("2", "Paro Town", "Bhutan"), facts="[]", cost_usd=0)],
        [],
    ]
    out = await cq.list_place_card_duplicates(_request(conn), x_admin_secret=SECRET)
    assert out["total"] == 1 and {c["card_id"] for c in out["data"][0]["cards"]} == {"1", "2"}
    assert out["data"][0]["cards"][0]["facts"] == 1


@pytest.mark.asyncio
async def test_build_endpoint_passes_force(_secret):
    from api.routers import admin_content_quality as cq
    with patch("shared.jobs.queue.enqueue", new=AsyncMock(return_value=("j1", True))) as enq:
        await cq.build_place_cards(cq.PlaceCardBuild(tour_id="t1", force=True), _request(AsyncMock()),
                                   x_admin_secret=SECRET)
    assert enq.await_args.args[2] == {"tour_id": "t1", "force": True}


# ── job ─────────────────────────────────────────────────────────────────────────────────────────

def test_job_caps_on_distinct_places_and_returns_counters():
    from services.jobs import place_cards_job as job
    conn = AsyncMock()
    conn.fetch.return_value = [{"atom_id": "a1"}, {"atom_id": "a2"}]
    ctx = MagicMock()
    ctx.payload = {"tour_id": "t1", "force": True}
    ctx.pool = _request(conn).app.state.pool
    counters = dict(pc._empty_counters(2), drafted=1, built=2)
    with patch.object(job, "ensure_place_cards", new=AsyncMock(return_value=counters)) as ens:
        out = asyncio.run(job.run(ctx))
    assert ens.await_args.kwargs == {"force": True, "max_places": job.MAX_PLACES} and job.MAX_PLACES == 40
    assert out["drafted"] == 1 and out["built"] == 2 and out["tour_id"] == "t1"


# ── migration 221 + stage defaults ──────────────────────────────────────────────────────────────

def test_migration_221_is_idempotent_and_seeds_both_stages():
    sql = MIGRATION.read_text()
    assert "CREATE TABLE IF NOT EXISTS acp_shared.place_card_miss" in sql
    assert "CREATE TABLE IF NOT EXISTS acp_shared.place_card_alias" in sql
    assert "ADD COLUMN IF NOT EXISTS miss_reason" in sql and "ALTER COLUMN card_id DROP NOT NULL" in sql
    assert sql.count("ON CONFLICT") >= 2 and "VALUES ('221'" in sql
    assert "('t9_repair', 'writer', 'claude', 'haiku', 'acc3', true, ARRAY['sonnet']" in sql
    assert "('t10_claims', 'validate', 'openai', 'gpt-6-luna', 'acc3', true, ARRAY['gpt-5.6-luna']" in sql
    for reason in pc.MISS_RETRY:
        assert f"'{reason}'" in sql


def test_safe_defaults_mirror_migration_221():
    r, c = SAFE_DEFAULTS["t9_repair"], SAFE_DEFAULTS["t10_claims"]
    assert (r.role, r.provider, r.model_id, r.account_route, tuple(r.fallback_model_ids)) == \
        ("writer", "claude", "haiku", "acc3", ("sonnet",))
    assert (c.role, c.provider, c.model_id, c.account_route, tuple(c.fallback_model_ids), c.shadow_sample_pct) == \
        ("validate", "openai", "gpt-6-luna", "acc3", ("gpt-5.6-luna",), 0)


def test_place_card_draft_and_check_run_on_different_vendors():
    """S234 migration 222: draft on GPT-5.6 Luna, check on Claude Haiku — both `validate` stages
    (ADR 0008), never the same vendor. AA-784 (S252, migration 286): the draft moves to GPT-6 Luna with a
    GPT-5.6 Luna fallback, so every model of the draft chain is another vendor than every model of the check
    chain (222's Sonnet draft fallback was not); SAFE_DEFAULTS mirrors the migrations."""
    from pathlib import Path
    from shared.llm_client.role_config import SAFE_DEFAULTS

    draft, check = SAFE_DEFAULTS["a3_place_card"], SAFE_DEFAULTS["a3_place_card_check"]
    assert (draft.role, draft.provider, draft.model_id, draft.account_route, tuple(draft.fallback_model_ids)) == \
        ("validate", "openai", "gpt-6-luna", "acc3", ("gpt-5.6-luna",))
    assert (check.role, check.provider, check.model_id, tuple(check.fallback_model_ids)) == \
        ("validate", "claude", "haiku", ("sonnet",))
    assert draft.shadow_model_id is None and draft.shadow_sample_pct == 0
    vendor = {"gpt-6-luna": "openai", "gpt-5.6-luna": "openai", "haiku": "anthropic", "sonnet": "anthropic"}
    draft_vendors = {vendor[k] for k in (draft.model_id, *draft.fallback_model_ids)}
    check_vendors = {vendor[k] for k in (check.model_id, *check.fallback_model_ids)}
    assert draft_vendors == {"openai"} and check_vendors == {"anthropic"}

    migrations = Path(__file__).resolve().parents[2] / "api/migrations"
    sql = (migrations / "222_s234_place_card_luna.sql").read_text()
    assert "model_id = 'gpt-5.6-luna'" in sql and "model_id = 'haiku'" in sql
    assert sql.count("role = 'validate'") == 2
    sql = (migrations / "286_place_card_draft_gpt6_luna.sql").read_text()
    body = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    assert body.strip().startswith("BEGIN;") and body.strip().endswith("COMMIT;")
    assert ("SET role = 'validate', provider = 'openai', model_id = 'gpt-6-luna', account_route = 'acc3',\n"
            "       fallback_model_ids = ARRAY['gpt-5.6-luna']::text[]") in body
    assert body.count("WHERE stage = 'a3_place_card'") == 2 and "stage = 'a3_place_card_check'" not in body
    assert "VALUES ('286'" in body and "ON CONFLICT (version) DO NOTHING" in body
    assert "'sonnet'" not in body and "'haiku'" not in body  # no Claude key on the draft route
