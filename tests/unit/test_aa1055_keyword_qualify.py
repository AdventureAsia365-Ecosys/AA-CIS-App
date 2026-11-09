"""AA-1055 (S274) — keyword qualification in the keyword map (phase A of segment research) and the namesake guard on
the demand claim. Pure rules + the batch over mocks. Invented place names and localities only."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import atom_ranking as ar
from services.acp_contract import keyword_qualify as kq
from services.acp_contract import segment_research as sr
from services.acp_contract import segment_research_batch as rb
from shared.llm_client.decide import Decision, Verdict
from shared.llm_client.models import LLMResponse

US = [(2840, "United States", "en")]
WHERE = kq.PlaceLocation(locality="Zarvoka", region="Quell Province", country="Brindland")


# ── the three rules ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, namesake", [
    ("Holy Trinity Chapel", True),          # dedication + type word
    ("Golden Lotus Hotel", True),           # stock hotel name
    ("Hotel Falcon's Nest", True),          # possessive "s" is filler; stock words count on a lodging
    ("Eagle's Nest", True),
    ("Royal Palace Hotel", True),
    ("Grand Inn", True),                    # "grand" is a stock word on a lodging
    ("Old Night Market", True),
    ("City Palace", True),                  # a kind word beside a namesake type
    ("Zarvok Temple", False),               # one distinctive word clears it
    ("Holy Trinity Chapel (Zarvoka)", False),   # the parenthetical names where it is
    ("Saint Melnith Church", False),
    ("Golden Lotus", False),                # no type word: not a place name of that shape
    ("Zarvok", False),
    ("", False),
    # a stock word on a landmark, a natural feature or "grand" / "great" names one particular place
    ("Blue Pagoda", False),
    ("Red Citadel Fort", False),
    ("Golden Cliff Temple", False),
    ("Silver Lotus Shrine", False),
    ("Tiger Nest Monastery", False),
    ("Dragon's Nest", False),
    ("Lion Rock", False),
    ("Great Rampart Wall", False),
    ("Grand Palace", False),
    ("Mount Phoenix", False),
    ("Mount Zarvok", False),
])
def test_namesake_name(name, namesake):
    assert kq.is_namesake_name(name) is namesake


def test_not_a_place_reads_the_card_fact_first():
    assert kq.not_a_place("Quellberry", "Quellberry is a species of palm grown on the plateau.")
    assert kq.not_a_place("Brindle cake", "Brindle cake is a sweet dish made of rice flour.")
    # a type word before the thing, or a site word anywhere: a place
    assert not kq.not_a_place("Melnith Lake", "Melnith Lake is a lake known for fish and birds.")
    assert not kq.not_a_place("Zarvok Bodhi", "Zarvok Bodhi is a sacred fig tree in the temple grounds.")
    # the card wins over the head noun of the name
    assert not kq.not_a_place("Zarvok tree", "Zarvok Tree is a hill village above the river.")


@pytest.mark.parametrize("fact", [
    "Quell Fish Market is a fish market on the harbour front.",
    "Zarvoka Flower Market is a flower market open at dawn.",
    "The Melnith Spice Bazaar is a spice market under a stone roof.",
    "It is a fruit market held on boats.",
    "Brindle Hall is a food court with forty stalls.",
    "Zarvok Bites is a famous food stall.",
    "Quellberry Farm is a fruit farm on the slope.",
])
def test_a_venue_head_noun_keeps_a_food_or_plant_venue_a_place(fact):
    assert not kq.not_a_place("Zarvok Venue", fact)


@pytest.mark.parametrize("fact", [
    "Quellberry is a palm native to the plateau.",
    "Brindle rolls are a popular street food.",
    "Melnith is a fish of the upper lake.",
    "Zarvok cha is a variety of tea grown in the hills.",
])
def test_a_thing_head_noun_is_not_a_place(fact):
    assert kq.not_a_place("Zarvok Thing", fact)


def test_not_a_place_falls_back_to_the_head_noun_without_a_card():
    assert kq.not_a_place("Quellberry tree")
    assert kq.not_a_place("Brindle spices", "")
    assert not kq.not_a_place("Quellberry Garden")


@pytest.mark.parametrize("keyword, reason", [
    ("km9 zarvok", "glued_number"),
    ("zarvok 2day trek", "glued_number"),
    ("admire zarvok lake", "brochure_verb"),
    ("4 chapel sightsee", "brochure_verb"),
    ("zarvok", "too_short"),
    ("the 5", "too_short"),
    ("zarvok lake", None),
    ("1st avenue zarvok", None),             # an ordinal is not a glued number
    ("7 brothers waterfall zarvoka", None),  # a separate number is fine
])
def test_garbled_reason(keyword, reason):
    assert kq.garbled_reason(keyword) == reason


def test_locality_phrases_skip_empty_generic_long_and_own_words():
    where = kq.PlaceLocation(locality="Old Town", region="Upper Quell Valley Region North", country="Brindland")
    assert where.phrases("Holy Trinity Chapel") == ["brindland"]
    assert kq.PlaceLocation(locality="Zarvoka").phrases("Zarvoka Chapel") == []
    assert WHERE.phrases("Royal Palace Hotel") == ["zarvoka", "quell province", "brindland"]


def test_qualify_namesake_replaces_the_plain_name_and_drops_unqualified_keywords():
    q = kq.qualify("Holy Trinity Chapel",
                   ["holy trinity chapel", "holy trinity chapel brindland", "holy trinity chapel hours",
                    "admire holy trinity chapel"], "holy trinity chapel", WHERE)
    assert q.flag == "namesake"
    assert q.plain == "holy trinity chapel zarvoka"
    assert q.kept == ["holy trinity chapel zarvoka", "holy trinity chapel brindland"]
    assert q.dropped == {"holy trinity chapel": "unqualified", "holy trinity chapel hours": "unqualified",
                         "admire holy trinity chapel": "garbled_brochure_verb"}


def test_qualify_namesake_without_any_locality_keeps_nothing():
    q = kq.qualify("Golden Lotus Hotel", ["golden lotus hotel", "golden lotus hotel spa"], "golden lotus hotel", None)
    assert q.kept == [] and q.plain is None
    assert set(q.dropped.values()) == {"unqualified"}


def test_qualify_not_a_place_buys_nothing():
    where = kq.PlaceLocation(country="Brindland", what_it_is="Quellberry is a species of palm.")
    q = kq.qualify("Quellberry", ["quellberry", "quellberry syrup"], "quellberry", where)
    assert q.flag == "not_a_place" and q.kept == [] and q.dropped == {
        "quellberry": "not_a_place", "quellberry syrup": "not_a_place"}


def test_qualify_a_distinct_place_only_loses_garbled_keywords():
    q = kq.qualify("Zarvok Temple", ["zarvok temple", "zarvok temple sunrise", "zarvok"], "zarvok temple", WHERE)
    assert q.flag is None and q.plain == "zarvok temple"
    assert q.kept == ["zarvok temple", "zarvok temple sunrise"]
    assert q.dropped == {"zarvok": "garbled_too_short"}


# ── ranking side: a namesake place never claims its bare name ─────────────────────────────────

ROWS = [("holy trinity chapel", "US", 90500), ("holy trinity chapel hours", "US", 8100),
        ("holy trinity chapel zarvoka", "US", 70), ("zarvok temple", "US", 1300)]


def test_namesake_segment_claims_only_qualified_keywords():
    assert ar.compute_demand("Holy Trinity Chapel", "visit", ROWS) == {"US": 70}
    assert ar.demand_candidates("Holy Trinity Chapel", "visit", ROWS, "US") == [("holy trinity chapel zarvoka", 70)]
    # a distinct place is unchanged
    assert ar.compute_demand("Zarvok Temple", "visit", ROWS) == {"US": 1300}
    assert kq.unqualified_words("Zarvok Temple") is None


def test_namesake_segment_keeps_its_bare_name_until_a_qualified_keyword_is_stored():
    # before the requalify run buys "<name> <locality>", ranking keeps today's demand
    rows = [r for r in ROWS if r[0] != "holy trinity chapel zarvoka"]
    assert ar.compute_demand("Holy Trinity Chapel", "visit", rows) == {"US": 90500}
    assert ar.demand_candidates("Holy Trinity Chapel", "visit", rows, "US")[0] == ("holy trinity chapel", 90500)
    assert kq.bare_words_to_skip("Holy Trinity Chapel", [r[0] for r in rows]) is None
    # a keyword of the bare words plus a search modifier is not a qualified one
    assert kq.bare_words_to_skip("Holy Trinity Chapel", ["holy trinity chapel hours"]) is None
    assert kq.bare_words_to_skip("Holy Trinity Chapel", ["holy trinity chapel zarvoka"]) is not None


def test_namesake_segment_lands_no_question_of_its_bare_name():
    paa = [("holy trinity chapel", "US", ["When was it built?"]),
           ("holy trinity chapel zarvoka", "US", ["Can you climb the tower?"])]
    assert ar._candidate_questions("Holy Trinity Chapel", "visit", paa) == {"Can you climb the tower?"}


# ── the keyword map ──────────────────────────────────────────────────────────────────────────

def _decision(key: str) -> Decision:
    return Decision(verdicts={key: Verdict(key, "shadow", "grey")})


@pytest.fixture(autouse=True)
def _no_jev():
    asked = []

    async def fake(stage, subject, state, keys, **kw):
        asked.append(state["keyword"])
        return _decision(keys[0])
    with patch.object(rb, "decide", new=fake):
        yield asked


def _pool(cards=(), maps=(), fresh=(), scope=()):
    conn = MagicMock()

    async def _fetch(sql, *args):
        if "acp_shared.place_card" in sql:
            return [dict(c) for c in cards]
        if "mode() WITHIN GROUP" in sql:
            return []
        if "place_keyword" in sql:
            return list(maps)
        if "segment_research_log" in sql:
            return [{"canonical_place": p, "market": m, "tier": t} for p, m, t in fresh]
        if "GROUP BY s.segment_id" in sql:
            return list(scope)
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _llm(content: str):
    fake = MagicMock()
    fake.generate.return_value = LLMResponse(content=content, model_used="haiku-4-5", provider="bedrock",
                                             input_tokens=100, output_tokens=50, cost_usd=0.004)
    return fake


PROPOSALS = ('{"places": ['
             '{"place": "Holy Trinity Chapel", "keywords": ["holy trinity chapel", "holy trinity chapel mass times",'
             ' "holy trinity chapel zarvoka history"]},'
             '{"place": "Quellberry tree", "keywords": ["quellberry tree", "quellberry tree uses"]},'
             '{"place": "Zarvok Temple", "keywords": ["zarvok temple", "km9 zarvok", "zarvok temple sunrise"]}]}')
CARDS = [{"canonical_place": "Holy Trinity Chapel", "locality": "Zarvoka", "region": None, "country": "Brindland",
          "what_it_is": "Holy Trinity Chapel is a stone chapel built by settlers."}]


def _map_rows(conn) -> list[tuple]:
    return [r for c in conn.executemany.await_args_list if "place_keyword" in c.args[0] for r in c.args[1]]


@pytest.mark.asyncio
async def test_propose_qualifies_namesakes_drops_non_places_and_garbled_and_stores_rule_rows(_no_jev):
    places = [rb._Place("Holy Trinity Chapel", ["visit"], ["US"]), rb._Place("Quellberry tree", ["see"], ["US"]),
              rb._Place("Zarvok Temple", ["visit"], ["US"])]
    pool, conn = _pool(cards=CARDS)
    fake = _llm(PROPOSALS)
    with patch.object(rb, "LLMClient", return_value=fake), \
         patch.object(rb, "record_call_with_pool", new=AsyncMock()) as m_log:
        stats = await rb._propose(places, pool, sr._RunGuard(), None)

    # the prompt tells the model which town a namesake must carry; a distinct place gets no hint
    listing = fake.generate.call_args.args[0].user_prompt
    assert "- Holy Trinity Chapel (itineraries: visit) — always add: zarvoka" in listing
    assert "- Zarvok Temple (itineraries: visit)\n" in listing + "\n"
    m_log.assert_awaited_once()

    chapel, tree, temple = places
    assert chapel.plain == "holy trinity chapel zarvoka"
    assert chapel.keywords == ["holy trinity chapel zarvoka", "holy trinity chapel zarvoka history"]
    assert tree.keywords == [] and tree.plain is None
    assert temple.keywords == ["zarvok temple", "zarvok temple sunrise"]
    # the qualified plain name is the place itself: never asked to Jev
    assert sorted(_no_jev) == ["holy trinity chapel zarvoka history", "zarvok temple sunrise"]
    assert stats["places_namesake"] == 1 and stats["places_not_a_place"] == 1
    assert stats["keywords_rule_dropped"] == 2 + 2 + 1

    rows = _map_rows(conn)
    assert ("Holy Trinity Chapel", "holy trinity chapel zarvoka", 0, "place_name", rb.PROMPT_VERSION, "not_asked",
            None, None) in rows
    rule = {(r[0], r[1]): (r[3], r[5], r[6]) for r in rows if r[5] == "skipped"}
    assert rule[("Holy Trinity Chapel", "holy trinity chapel")] == ("place_name", "skipped", "rule:unqualified")
    assert rule[("Holy Trinity Chapel", "holy trinity chapel mass times")][2] == "rule:unqualified"
    assert rule[("Quellberry tree", "quellberry tree")] == ("place_name", "skipped", "rule:not_a_place")
    assert rule[("Zarvok Temple", "km9 zarvok")][2] == "rule:garbled_glued_number"


def test_prompt_version_carries_the_qualification_rule():
    assert "always add" in rb.PROPOSE_SYSTEM and rb.PROMPT_VERSION.startswith("a3_propose@")


def _row(subject, keyword, *, mode=None, zone="grey", version=None):
    return {"subject": subject, "keyword": keyword, "rank": 0, "jev_zone": zone, "jev_mode": mode,
            "prompt_version": version or rb.PROMPT_VERSION}


def test_keywords_from_map_leaves_rule_rows_out_and_an_all_rule_map_is_usable():
    rows = [_row("Holy Trinity Chapel", "holy trinity chapel", mode="rule:unqualified", zone="skipped"),
            _row("Holy Trinity Chapel", "holy trinity chapel zarvoka", zone="not_asked")]
    assert rb.keywords_from_map(rows) == ["holy trinity chapel zarvoka"]
    only_rules = [_row("Quellberry tree", "quellberry tree", mode="rule:not_a_place", zone="skipped")]
    assert rb.keywords_from_map(only_rules) == []


def test_needs_requalify_only_for_old_maps_the_rules_would_change():
    old = "a3_propose@oldprompt"
    assert rb.needs_requalify("Holy Trinity Chapel", [_row("Holy Trinity Chapel", "holy trinity chapel",
                                                           version=old)])
    assert rb.needs_requalify("Quellberry tree", [_row("Quellberry tree", "quellberry tree", version=old)])
    assert rb.needs_requalify("Zarvok Temple", [_row("Zarvok Temple", "zarvok temple", version=old),
                                                _row("Zarvok Temple", "4 chapel sightsee", version=old)])
    # a clean old map waits for its own freshness; a current map, or no map at all, is not requalified
    assert not rb.needs_requalify("Zarvok Temple", [_row("Zarvok Temple", "zarvok temple", version=old),
                                                    _row("Zarvok Temple", "zarvok temple sunrise", version=old)])
    assert not rb.needs_requalify("Holy Trinity Chapel", [_row("Holy Trinity Chapel", "holy trinity chapel zarvoka")])
    assert not rb.needs_requalify("Holy Trinity Chapel", [])
    # an enforced Jev reject was never bought: it does not trigger a rebuild
    assert not rb.needs_requalify("Zarvok Temple", [
        _row("Zarvok Temple", "zarvok temple", version=old),
        _row("Zarvok Temple", "km9 zarvok", version=old, zone="reject", mode="enforce")])


@pytest.mark.asyncio
async def test_plan_rebuilds_fresh_places_whose_old_map_the_rules_would_change():
    old = "a3_propose@oldprompt"
    maps = [_row("Holy Trinity Chapel", "holy trinity chapel", version=old),
            _row("Zarvok Temple", "zarvok temple", version=old)]
    fresh = [(p, m, t) for p in ("Holy Trinity Chapel", "Zarvok Temple") for m, t in (("US", "volume"),
                                                                                       ("US", "serp"))]
    pool, _ = _pool(maps=maps, fresh=fresh)
    stale = await sr._plan(pool, {"Holy Trinity Chapel": ["visit"], "Zarvok Temple": ["visit"]}, ["US"],
                           ("volume", "serp"))
    assert [p.name for p in stale] == ["Holy Trinity Chapel"]
    chapel = stale[0]
    assert chapel.requalify and chapel.markets == ["US"] and chapel.serp and not chapel.has_map


def test_reusable_keywords_reuses_an_old_map_the_rules_would_not_change():
    old = "a3_propose@oldprompt"
    clean = [_row("Zarvok Temple", "zarvok temple", version=old, zone="not_asked"),
             _row("Zarvok Temple", "zarvok temple sunrise", version=old),
             _row("Zarvok Temple", "zarvok temple yoga", version=old, zone="reject", mode="enforce")]
    assert rb.reusable_keywords("Zarvok Temple", clean) == ["zarvok temple", "zarvok temple sunrise"]
    # a flagged old map, an old map without the current plain keyword and no map are proposed again
    assert rb.reusable_keywords("Holy Trinity Chapel", [_row("Holy Trinity Chapel", "holy trinity chapel",
                                                             version=old)]) is None
    assert rb.reusable_keywords("Zarvok Temple", [_row("Zarvok Temple", "zarvok shrine", version=old)]) is None
    assert rb.reusable_keywords("Zarvok Temple", []) is None
    # a current map is read as before
    assert rb.reusable_keywords("Zarvok Temple", [_row("Zarvok Temple", "zarvok temple")]) == ["zarvok temple"]


@pytest.mark.asyncio
async def test_plan_keeps_a_partly_stale_unflagged_place_on_its_old_map_and_its_stale_tiers():
    old = "a3_propose@oldprompt"
    maps = [_row("Zarvok Temple", "zarvok temple", version=old, zone="not_asked"),
            _row("Zarvok Temple", "zarvok temple sunrise", version=old)]
    # volume fresh in both markets, SERP stale: only the SERP is bought, with the old keywords, no new proposal
    fresh = [("Zarvok Temple", "US", "volume"), ("Zarvok Temple", "UK", "volume")]
    pool, _ = _pool(maps=maps, fresh=fresh)
    stale = await sr._plan(pool, {"Zarvok Temple": ["visit"]}, ["US", "UK"], ("volume", "serp"))
    assert len(stale) == 1
    temple = stale[0]
    assert temple.has_map and temple.keywords == ["zarvok temple", "zarvok temple sunrise"]
    assert temple.markets == [] and temple.serp and not temple.requalify
    # one volume market stale: only that market
    pool, _ = _pool(maps=maps, fresh=[("Zarvok Temple", "US", "volume"), ("Zarvok Temple", "US", "serp")])
    stale = await sr._plan(pool, {"Zarvok Temple": ["visit"]}, ["US", "UK"], ("volume", "serp"))
    assert stale[0].has_map and stale[0].markets == ["UK"] and not stale[0].serp


@pytest.mark.asyncio
async def test_a_place_left_with_nothing_to_buy_is_logged_in_both_tiers():
    from services.seo_intelligence.dataforseo_client import DataForSEOClient

    place = rb._Place("Quellberry tree", ["see"], ["US"], keywords=[], has_map=True, serp=True)
    pool, conn = _pool()
    client = MagicMock(spec=DataForSEOClient)
    client.fetch_volumes_bulk = AsyncMock()
    client._serp_advanced = AsyncMock()
    result = await rb.research_batch([place], US, pool, client, sr._RunGuard())
    client.fetch_volumes_bulk.assert_not_awaited()
    client._serp_advanced.assert_not_awaited()
    logs = [r for c in conn.executemany.await_args_list if "segment_research_log" in c.args[0] for r in c.args[1]]
    assert sorted(logs) == [("Quellberry tree", "US", "serp"), ("Quellberry tree", "US", "volume")]
    assert result["places_complete"] == 1 and result["places_changed"] == []


# ── the read-only count script ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_count_script_reports_flagged_places_and_the_ignored_namesake_volume():
    from scripts.aa1055 import keyword_qualify_count as script

    old = "a3_propose@oldprompt"
    maps = [_row("Holy Trinity Chapel", "holy trinity chapel", version=old, zone="not_asked"),
            _row("Holy Trinity Chapel", "holy trinity chapel zarvoka", version=old),
            _row("Quellberry tree", "quellberry tree", version=old, zone="not_asked"),
            _row("Zarvok Temple", "zarvok temple", version=old, zone="not_asked"),
            _row("Zarvok Temple", "4 chapel sightsee", version=old)]
    pool, conn = _pool(cards=CARDS)

    async def _fetch(sql, *args):
        if sql == script.MAPS_SQL:
            return maps
        if sql == script.VOLUMES_SQL:
            assert args[0] == ["holy trinity chapel"]
            return [{"keyword": "holy trinity chapel", "market": "US", "search_volume": 90500},
                    {"keyword": "holy trinity chapel", "market": "UK", "search_volume": 400}]
        if "acp_shared.place_card" in sql:
            return CARDS
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.fetchval = AsyncMock(return_value=3)
    out = await script.count(pool)
    assert out["places_with_map"] == 3 and out["namesake_places"] == 1 and out["not_a_place_places"] == 1
    assert out["unqualified_namesake_keywords"] == 1
    assert out["garbled_keywords"] == {"brochure_verb": 1}
    assert out["places_requalify_next_run"] == 3 and out["segments_of_requalify_places"] == 3
    assert out["unqualified_volume_by_market"]["US"] == {"keywords": 1, "volume": 90500, "at_or_above_blog_bar": 1}
    assert out["unqualified_volume_by_market"]["UK"]["at_or_above_blog_bar"] == 0
