"""AA-768 (S234) — seed slimming (sibling rules, thin moments, same-place merge), the route walk in
day order without excluded Segments, the cited-only repair source, the scope-aware keyword / SEO
fit, and the t9_repair stage."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import generate, service
from services.acp_content_writing import hygiene as h
from services.acp_shared import atom_seed as seed_mod
from services.acp_shared.atom_seed import (MAX_SIBLINGS, compose_route_moment, compose_seed, group_route_moments,
                                           merge_keys,
                                           moment_fact_count, pick_siblings)
from shared.llm_client.role_config import SAFE_DEFAULTS

pytestmark = pytest.mark.real_atom_seed

CARD = {"card_id": "c1", "display_name": "Lake Khövsgöl", "locality": None, "region": "Khövsgöl Province",
        "country": "Mongolia", "naming_note": None,
        "facts": [{"text": "It is one of the deepest lakes in Asia."}, {"text": "It drains into the Egiin River."},
                  {"text": "The lake freezes in winter."}]}


# ---------------------------------------------------------------- siblings

def test_no_siblings_when_the_card_is_rich():
    seed = compose_seed("Lake Khövsgöl — boat ride", "Take a boat on the lake.", None, None,
                        ["Walk the lake shore at dusk."], place="Lake Khövsgöl", card=CARD)
    assert "Other trips" not in seed


def test_siblings_capped_and_foreign_places_dropped():
    own = "ruins of an ancient monastery — visit\nSource itinerary: visit the ruins of a monastery destroyed in 1937."
    sibs = ["A half-day tour of Ulaanbaatar includes a visit to Gandan Monastery.",
            "The primary site is Erdene Zuu Monastery, dating to the late 16th century.",
            "Walk among the ruined walls of the old monastery.",
            "The monastery was destroyed during the purges.",
            "A third plain line about ruins."]
    picked = pick_siblings(sibs, own, own_evidence=None, card_fact_count=0)
    assert picked == ["Walk among the ruined walls of the old monastery.", "The monastery was destroyed during the purges."]
    assert len(picked) == MAX_SIBLINGS == 2


# ---------------------------------------------------------------- fact count / thin

RUINS_BLOCK = ("ruins of an ancient monastery — visit\nLocation: ruins of an ancient monastery, Mongolia\n"
               "Day 14 of the trip\nSource itinerary: visit the ruins of an ancient monastery destroyed during the "
               "political purges of 1937.\nWhy it matters: encounter historical loss")


def test_moment_fact_count():
    assert moment_fact_count(RUINS_BLOCK) == 1
    rich = compose_seed("Uran Togoo — trek", "Journey to Uran Togoo. It is dormant.", "descend into a crater", None,
                        [], place="Uran Togoo", card=CARD)
    assert moment_fact_count(rich) == 2 + 3


def _part(atom_id, day, text, card=None, evidence=None, hook=None, place=None):
    return {"atom_id": atom_id, "day": day, "text": text, "card": card, "evidence": evidence, "hook": hook,
            "season": None, "place": place, "country": "Mongolia"}


def test_single_thin_moment_is_tagged():
    block = compose_route_moment([_part("a1", 14, "ruins of an ancient monastery — visit",
                                        evidence="visit the ruins of an ancient monastery destroyed in 1937.")])
    assert block.endswith(seed_mod.thin_note(1)) and "never make it the hook" in block
    assert "Other trips" not in block


def test_same_place_moments_merge_into_one_block():
    poor = {**CARD, "card_id": "c1", "facts": [{"text": "It holds 70% of the fresh water."}]}
    group = [_part("a1", 4, "Lake Khövsgöl — arrive", card=poor, evidence="Arrive at the lake.", hook="a vast lake"),
             _part("a2", 5, "Lake Khövsgöl — boat ride", card=CARD, evidence="Boat on the lake.", hook="a vast lake")]
    block = compose_route_moment(group)
    assert block.count("Location:") == 1 and block.count("Why it matters") == 1
    assert "Day 4 of the trip: Lake Khövsgöl — arrive — Source itinerary: Arrive at the lake." in block
    assert "Day 5 of the trip: Lake Khövsgöl — boat ride" in block
    assert "deepest lakes" in block and "70%" not in block  # only the richest card's facts
    assert "Thin moment" not in block


def test_merge_keys_card_and_core_name():
    assert merge_keys({"card": {"card_id": "c1"}}) == ["card:c1"]
    assert merge_keys({"card": {"card_id": "c1", "display_name": "Lake Khövsgöl", "country": "Mongolia"}}) == \
        ["card:c1", "name:khovsgol|mongolia"]
    # word order and generic words do not matter; the moment's country is the fallback
    assert merge_keys({"card": {"display_name": "Khövsgöl Lake"}, "country": "Mongolia"}) == ["name:khovsgol|mongolia"]
    assert merge_keys({"card": None}) == [] and merge_keys({"card": {"display_name": "Lake"}}) == []


def test_merge_keys_follow_the_place_card_identity_rule():
    """C10 uses AA-772's place_key: a dzong is not its town, and a generic name needs a locality."""
    town = merge_keys({"card": {"display_name": "Punakha", "country": "Bhutan"}})
    dzong = merge_keys({"card": {"display_name": "Punakha Dzong", "country": "Bhutan"}})
    assert town and dzong and set(town).isdisjoint(dzong)
    assert merge_keys({"card": {"display_name": "City Palace", "country": "India"}}) == []
    jaipur = merge_keys({"card": {"display_name": "City Palace", "locality": "Jaipur", "country": "India"}})
    udaipur = merge_keys({"card": {"display_name": "City Palace", "locality": "Udaipur", "country": "India"}})
    assert jaipur and udaipur and set(jaipur).isdisjoint(udaipur)
    assert jaipur == merge_keys({"card": {"display_name": "the City Palace", "locality": "Jaipur",
                                          "country": "India"}})


def _kh(atom_id, card_id, name, pct):
    return {"atom_id": atom_id, "country": "Mongolia",
            "card": {"card_id": card_id, "display_name": name, "country": "Mongolia",
                     "facts": [{"text": f"It holds {pct} of the fresh water."}]}}


def test_khovsgol_moments_on_different_cards_merge_by_core_name():
    """S233 — five Khövsgöl moments on five cards ('Lake Khövsgöl' x3, 'Khövsgöl Lake' x2) are one
    block without PR1's card merge; 'Sod Camp, Khövsgöl Lake area' stays its own."""
    parts = [_kh("a1", "c1", "Lake Khövsgöl", "2%"), _kh("a2", "c2", "Khövsgöl Lake", "0.4%"),
             _kh("sod", "c9", "Sod Camp, Khövsgöl Lake area", "x"), _kh("a3", "c3", "Lake Khövsgöl", "70%"),
             _kh("a4", "c4", "Khövsgöl Lake", "1%"), _kh("a5", "c1", "Lake Khovsgol (old card)", "2%")]
    groups = group_route_moments(parts)
    assert [[p["atom_id"] for p in g] for g in groups] == [["a1", "a2", "a3", "a4", "a5"], ["sod"]]


def test_card_id_or_name_chains_merge_transitively():
    parts = [_kh("a1", "c1", "Lake Khövsgöl", "2%"), _kh("a2", "c2", "Khövsgöl Nuur", "1%"),
             _kh("a3", "c2", "Khövsgöl Lake", "1%")]  # a2–a3 share c2, a3–a1 share the name
    assert [[p["atom_id"] for p in g] for g in group_route_moments(parts)] == [["a1", "a2", "a3"]]
    other_country = {**_kh("b1", "c7", "Lake Khövsgöl", "1%"), "country": "Russia"}
    other_country["card"] = {**other_country["card"], "country": "Russia"}
    assert len(group_route_moments([_kh("a1", "c1", "Lake Khövsgöl", "2%"), other_country])) == 2


@pytest.mark.asyncio
async def test_route_moment_seeds_merges_by_card_and_falls_back_to_label():
    atom_row = {"text": "Lake — visit", "evidence": "Visit.", "emotional_hook": None, "season_note": None,
                "place": "Lake", "country": "Mongolia", "itinerary_day": 4}
    conn = AsyncMock()
    conn.fetchrow.side_effect = [atom_row, CARD, {**atom_row, "itinerary_day": 5}, CARD, None]
    out = await seed_mod.route_moment_seeds(conn, [("a1", "L1"), ("a2", "L2"), ("gone", "Gone label")], "t1")
    assert [a for a, _ in out] == ["a1", "gone"]
    assert out[1][1] == "Gone label"
    conn.fetch.assert_not_called()  # no sibling query on a route


@pytest.mark.asyncio
async def test_atom_seed_parts_skips_sibling_query_for_a_rich_card():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [{"text": "L", "evidence": None, "emotional_hook": None, "season_note": None,
                                  "place": "P", "country": "C", "itinerary_day": None}, CARD]
    await seed_mod.atom_seed_parts(conn, "a1", "t1")
    conn.fetch.assert_not_called()


# ---------------------------------------------------------------- route walk

def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.mark.asyncio
async def test_route_walk_sorted_by_day_and_excluded_segments_skipped():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"atom_id": "ruins", "text": "ruins", "itinerary_day": 14, "excluded": None},
        {"atom_id": "sod", "text": "Sod Camp", "itinerary_day": 3, "excluded": False},
        {"atom_id": "lunch", "text": "Moron lunch", "itinerary_day": 4, "excluded": True},
    ]
    atoms: list = []
    with patch.object(service, "route_moment_seeds", new=AsyncMock(side_effect=lambda c, a, t: list(a))):
        out = await service._fetch_route_segments(uuid.uuid4(), "trip", ["s1", "s2", "s3"], _pool(conn),
                                                  atoms_out=atoms)
    assert out == [("sod", "Sod Camp"), ("ruins", "ruins")]
    assert atoms == out
    assert "atom_ranking" in conn.fetchrow.call_args.args[0] and "superseded_at IS NULL" in conn.fetchrow.call_args.args[0]


@pytest.mark.asyncio
async def test_route_walk_keeps_everything_when_all_excluded():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [{"atom_id": "a", "text": "A", "itinerary_day": 2, "excluded": True}]
    with patch.object(service, "route_moment_seeds", new=AsyncMock(side_effect=lambda c, a, t: list(a))):
        out = await service._fetch_route_segments(uuid.uuid4(), "trip", ["s1"], _pool(conn))
    assert out == [("a", "A")]


@pytest.mark.asyncio
async def test_resolve_facts_rereads_merged_route_seeds_from_every_atom():
    """T9 never drafts a card (plan C8, AA-772); it re-merges the route seeds from every kept
    moment, so a card built since start_write reaches the merged block."""
    ctx = {"facts_pending": True, "tenant_id": str(uuid.uuid4()), "route_segments": [["a1", "old"]],
           "route_atoms": [["a1", "L1"], ["a2", "L2"]], "atom_text": "old", "destination": None}
    with patch.object(service, "route_moment_seeds", new=AsyncMock(return_value=[("a1", "merged")])) as rms, \
         patch.object(service, "fetch_facts_for_writing", new=AsyncMock(return_value=[])):
        await service.resolve_facts(uuid.uuid4(), ctx, _pool(AsyncMock()))
    assert rms.call_args.args[1] == [("a1", "L1"), ("a2", "L2")]
    assert ctx["route_segments"] == [("a1", "merged")] and ctx["atom_text"] == "merged"
    assert not hasattr(service, "ensure_place_cards")


@pytest.mark.asyncio
async def test_resolve_facts_without_route_atoms_rereads_from_the_segments():
    """A context built before S234 has no route_atoms: the stored segments are the input, and an
    empty re-read keeps them."""
    ctx = {"facts_pending": True, "tenant_id": str(uuid.uuid4()), "route_segments": [("a1", "s1"), ("a2", "s2")],
           "atom_text": "s1\n\ns2", "destination": None}
    with patch.object(service, "route_moment_seeds", new=AsyncMock(return_value=[])) as rms, \
         patch.object(service, "fetch_facts_for_writing", new=AsyncMock(return_value=[])):
        await service.resolve_facts(uuid.uuid4(), ctx, _pool(AsyncMock()))
    assert rms.call_args.args[1] == [("a1", "s1"), ("a2", "s2")]
    assert ctx["route_segments"] == [("a1", "s1"), ("a2", "s2")] and ctx["atom_text"] == "s1\n\ns2"


# ---------------------------------------------------------------- repair source / grounding

def _route_ctx():
    return {"atom_text": "M1 text\n\nM2 text", "route_segments": [("m1", "M1 text"), ("m2", "M2 text")],
            "facts_text": "[Fact id=f1]\nVisa: 30 days.\n\n[Fact id=f2]\nSeason: June to August.",
            "brand_audience": {"brand_name": "Terra", "customer_segment": "families aged 7-16"}}


def test_repair_source_is_only_the_cited_moment_and_fact():
    text = "The lake is deep. [R:m2] The visa is easy. [F:f2]"
    items = [{"start": 0, "end": text.find("The visa")}, {"start": text.find("The visa"), "end": len(text)}]
    src = service._repair_source(text, items, _route_ctx())
    assert src == "M2 text\n\n[Fact id=f2]\nSeason: June to August.\n\nBRAND: Terra"
    assert "families aged" not in src


def test_repair_source_uncited_route_sentence_gets_the_whole_grounding():
    text = "An invented sentence with no tag."
    src = service._repair_source(text, [{"start": 0, "end": len(text)}], _route_ctx())
    assert src == service._grounding_text(_route_ctx())


def test_grounding_text_relabels_audience():
    g = service._grounding_text(_route_ctx())
    assert "AUDIENCE (who the brand writes for — not a fact about this trip): families aged 7-16" in g


# ---------------------------------------------------------------- keyword / SEO fit

SEED = ("Sod Camp — overnight\nLocation: Sod Camp, Khövsgöl Province, Mongolia (do not place it elsewhere)\n"
        "Other trips that include this same moment (x):\n- A day at Erdene Zuu Monastery in Kharkhorin.")


def test_choose_keyword_rejects_out_of_scope_place():
    lex = h.build_scope_lexicon(SEED, None, "Mongolia: Khövsgöl Lake and Gobi Desert — 15 Days")
    kws = ["khovsgol and gobi tour mongolia", "sod camp khovsgol"]
    assert service.choose_keyword(kws, SEED) == "khovsgol and gobi tour mongolia"
    assert service.choose_keyword(kws, SEED, out_of_scope=lex.gazetteer) == "sod camp khovsgol"


@pytest.mark.asyncio
async def test_seo_fit_runs_for_an_out_of_scope_title_and_rejects_a_fit_that_keeps_it():
    hyg = h.HygieneContext(seed=SEED, trip_name="Mongolia: Khövsgöl Lake and Gobi Desert — 15 Days")
    meta = "A family guide to Sod Camp on the shore of Lake Khövsgöl in northern Mongolia, with what each day of the stay holds."
    seo = {"seo_title": "Khovsgol and Gobi Family Journey Guide for Parents", "meta_description": meta, "slug": "s"}
    calls = []

    def fake_fit(**kw):
        calls.append(kw)
        return {"seo_title": "Gobi and Sod Camp: A Family Guide to Khövsgöl Lake", "meta_description": None}, 0.003

    with patch.object(service, "fit_seo_fields", side_effect=fake_fit):
        out, cost = await service._fit_seo_meta(seo, None, body="b", context={}, request_id="r", hyg=hyg)
    assert calls and calls[0]["forbidden_words"] == ["Gobi"]
    assert out["seo_title"] == seo["seo_title"]  # the fitted title still names Gobi → rejected
    assert len(calls) == 2 and cost == pytest.approx(0.006)


@pytest.mark.asyncio
async def test_seo_fit_not_called_without_scope_problem():
    seo = {"seo_title": "Sod Camp Family Guide: A Night by Lake Khövsgöl",
           "meta_description": "A family guide to Sod Camp on the shore of Lake Khövsgöl in northern Mongolia, "
                               "with what each part of the stay holds for you.", "slug": "s"}
    with patch.object(service, "fit_seo_fields") as fit:
        await service._fit_seo_meta(seo, None, body="b", context={}, request_id="r", hyg=h.HygieneContext(seed=SEED))
    fit.assert_not_called()


# ---------------------------------------------------------------- generate / stage

def _llm(content):
    fake = MagicMock()
    fake.generate.return_value = MagicMock(content=content, model_used="haiku", cost_usd=0.001, input_tokens=10,
                                           output_tokens=10, stop_reason="end_turn", satellite_account="acc3",
                                           fallback_used=False)
    return fake


def test_seo_fit_routes_on_t9_repair_with_forbidden_words():
    with patch.object(generate, "LLMClient", return_value=_llm('{"seo_title": "T", "meta_description": "M."}')) as m, \
         patch.object(generate, "record_call_sync") as log:
        generate.fit_seo_fields(body="b", seo_title="x", meta_description="y", keyword=None, forbidden_words=["Gobi"])
    req = m.return_value.generate.call_args.args[0]
    assert req.stage == "t9_repair" == log.call_args.kwargs["stage"]
    assert "NEVER USE (outside this post's scope): Gobi" in req.user_prompt


def test_repair_sentences_stage_mode_piece_and_context():
    items = [{"sentence": "A.", "problem": "p", "context": ["Before.", "After."]}]
    with patch.object(generate, "LLMClient", return_value=_llm('{"replacements": ["x"]}')) as m, \
         patch.object(generate, "record_call_sync") as log:
        generate.repair_sentences(sentences=items, content_seed="seed", channel="blog", stage="t9_repair",
                                  mode="admin_suggest", content_piece_id="p1")
    req = m.return_value.generate.call_args.args[0]
    assert req.stage == "t9_repair" and "CONTEXT: Before. | After." in req.user_prompt
    assert log.call_args.kwargs["content_piece_id"] == "p1"
    assert log.call_args.kwargs["quality_signal"]["mode"] == "admin_suggest"
    with patch.object(generate, "LLMClient", return_value=_llm('{"replacements": ["x"]}')) as m, \
         patch.object(generate, "record_call_sync"):
        generate.repair_sentences(sentences=items, content_seed="seed", channel="blog")
    assert m.return_value.generate.call_args.args[0].stage == "t9_write"  # pipeline default unchanged


def test_t9_repair_safe_default_is_haiku_with_sonnet_fallback():
    cfg = SAFE_DEFAULTS["t9_repair"]
    assert (cfg.role, cfg.provider, cfg.model_id, tuple(cfg.fallback_model_ids)) == ("writer", "claude", "haiku",
                                                                                     ("sonnet",))


def test_prompt_has_faq_scope_and_social_proof_rules():
    from services.acp_content_writing import prompts
    assert "FORBIDDEN: social proof" in prompts.SYSTEM_PROMPT
    for block in (prompts._BLOG_FORMAT_INSTRUCTIONS, prompts._BLOG_FORMAT_INSTRUCTIONS_ROUTE):
        assert "each answered ONLY by a fact in the CONTENT SEED or FACTS" in block


# ---------------------------------------------------------------- write loop

@pytest.mark.asyncio
async def test_write_loop_autofixes_before_the_gates_and_flags_it():
    tn_seed = ("Tiger's Nest — hike\nLocation: Paro Taktsang, Paro, Bhutan (do not place it elsewhere)\n"
               "Source itinerary: Hike to the Paro Taktsang monastery.")
    ctx = {"atom_text": tn_seed, "goal": {"key": "promotion"}, "channel_style": {"channel": "linkedin"},
           "brand_audience": {"brand_name": "Terra"}, "chosen": {"name": "A"}, "cta": "Ask Terra.",
           "destination": None, "trip_name": None, "brand_rubric_text": "r", "channel": "linkedin",
           "atom_id": "a1"}
    passing = {"passed": True, "gate_ledger": [], "first_failure": None, "flags": []}
    with patch.object(service, "write_content", return_value=("A hike. Ask Terra.\n\n#PakoTaktsang", 0.01, {}, None)), \
         patch.object(service, "run_quality_gates", return_value=passing) as gates, \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    assert gates.call_args.kwargs["content_text"].endswith("#ParoTaktsang")
    assert gates.call_args.kwargs["hygiene_ctx"].brand_name == "Terra"
    flag = next(f for f in fin.call_args.kwargs["flags"] if f["gate"] == "F13_autofix")
    assert flag["violations"] == ["fixed hashtag #PakoTaktsang → #ParoTaktsang"]


# ---------------------------------------------------------------- admin highlights

def test_admin_highlights_f12_f13():
    from api.routers.admin_content_quality import highlights
    text = "The camp is quiet. The Gobi lies far to the south of here. #PakoTaktsang"
    ledger = [{"gate": "F12_scope", "passed": False, "blocking": False, "violations": [
        "title names 'Gobi', outside this piece's scope: 'Gobi Guide'",
        "names 'Gobi', outside this piece's scope: 'The Gobi lies far to the south of here.'"]},
        {"gate": "F13_hygiene", "passed": False, "blocking": False, "violations": [
            "misspelt hashtag '#PakoTaktsang' (did you mean '#ParoTaktsang'?): '#PakoTaktsang'"]}]
    out = highlights(text, ledger, [])
    assert [(text[o["start"]:o["end"]], o["gate"]) for o in out] == [
        ("The Gobi lies far to the south of here.", "F12_scope"), ("#PakoTaktsang", "F13_hygiene")]
