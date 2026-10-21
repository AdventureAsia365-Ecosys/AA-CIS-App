"""AA-794 — T2 seo_meta cut mid-sentence + wrong-country suffix (S234 T2 rounds, report 12 #10).

5/14 S234 T2 metas ended on a cut content word ("…mountain valleys with expert.") — the last-resort
word-boundary cut in _cut_long_meta. 2/14 got ", in Laos." on a Laos→Vietnam tour — the fact-clause
tail used raw_tours.country (one value). The same helpers run on A1, so A1 is covered too.

AA-925: the word / clause cut is gone altogether (it was "cut, then add a period": "...a 20-kilometre."), so
every over-length one-sentence fragment below is now returned unchanged for the LLM meta repair; T3 runs a
meta-only regenerate before it would ask for a whole-tour rewrite.
"""
from unittest.mock import AsyncMock, patch

import pytest

from services.content_generation.seo_meta_utils import (
    SEO_META_MAX, SEO_META_MIN, _countries_named, fit_seo_meta_final, meta_complete_sentence,
)

# Each S234 fragment meta, with a plausible original tail restored (the writer's > 155-char meta
# that the old word cut chopped). `bad_end` is the cut-off word the old code ended on.
_FRAGMENTS = [
    ("cbba9416", "Bhutan", "expert",
     "A private 10-day journey through Bhutan from Tashigang to Paro, tracing food traditions through "
     "farmhouses, temples, and mountain valleys with expert guides and private drivers throughout the route."),
    ("9a4b7959", "Bhutan", "daily",
     "A 10-day Bhutan journey from Samdrup Jongkhar to Paro covering farmhouse brewing, buckwheat noodles, "
     "Tiger's Nest, and Punakha Dzong with active daily rides and hikes on mountain trails."),
    ("81102180", "Bhutan", "valley",
     "This 10-day Bhutan journey links eastern farmhouses, Bumthang temples, and Thimphu markets through "
     "food traditions rooted in mountain farming and valley harvests that shape every meal."),
    ("26e0ba34", "Mongolia", "fragile",
     "A 15-day Mongolia journey from Khövsgöl Lake and Tsaatan communities to Gobi Desert habitats, "
     "volcanic parks, and ancient monastic ruins across fragile ecosystems and open steppe."),
    ("5b442cdf", "Bhutan", "mountain",
     "A 10-day Bhutan journey from the eastern valleys to Paro, covering Tiger's Nest, Punakha Dzong, "
     "and farmhouse food traditions across distinct mountain regions and river valleys."),
]

_LAOS_VN = {"duration": "12 Days", "country": "Laos",
            "name": "Laos to Vietnam by Bicycle: Nam Ou River to Hanoi — 12 Days"}


def _in_band(m: str) -> bool:
    return SEO_META_MIN <= len(m) <= SEO_META_MAX and meta_complete_sentence(m)


def _last_word(m: str) -> str:
    return m.rstrip(".").split()[-1].lower()


@pytest.mark.parametrize("vid,country,bad_end,original", _FRAGMENTS, ids=[f[0] for f in _FRAGMENTS])
def test_fragment_is_never_cut_mid_clause(vid, country, bad_end, original):
    # AA-925: no cut at all — SEO_META_TOO_LONG stays and flag_fix's LLM repair (T3: meta-only regenerate) runs
    assert len(original) > SEO_META_MAX
    assert fit_seo_meta_final(original, {"duration": "10 Days", "country": country, "name": "T"}) == original


@pytest.mark.parametrize("meta", [
    # 79038b8c (Terra) and 00cf8f28 (WildKind) before the ", in Laos." tail was appended
    "A 12-day family cycling journey from Luang Prabang to Hanoi, covering the Nam Ou gorge, Dien Bien Phu, "
    "and Cuc Phuong National Park.",
    "A 12-day cycling and river journey from Luang Prabang through the Nam Ou gorge to Hanoi, via Dien Bien "
    "Phu and Cuc Phuong National Park.",
])
def test_multi_country_tour_gets_no_country_tail(meta):
    assert len(meta) < SEO_META_MIN
    out = fit_seo_meta_final(meta, _LAOS_VN)
    assert "in Laos" not in out


def test_multi_country_tour_still_gets_a_days_only_tail():
    meta = ("A family cycling journey from Luang Prabang to Hanoi along the Nam Ou gorge, Dien Bien Phu "
            "and the rice valleys of Mai Chau.")
    out = fit_seo_meta_final(meta, _LAOS_VN)
    assert _in_band(out) and "12 days" in out and "Laos" not in out.replace(meta[:-1], "")


def test_meta_naming_another_country_gets_no_country_tail():
    # single-country name, but the meta itself names Vietnam while facts.country is Laos
    meta = ("A cycling journey from Luang Prabang along the Nam Ou gorge into northern Vietnam, finishing "
            "in Hanoi after Mai Chau.")
    out = fit_seo_meta_final(meta, {"duration": "12 Days", "country": "Laos", "name": "Nam Ou by Bike"})
    assert "in Laos" not in out


def test_single_country_regression_keeps_the_country_tail():
    meta = ("A trek through the Paro and Punakha valleys with visits to Tiger's Nest, Punakha Dzong and "
            "farmhouses in the hills.")
    assert len(meta) < SEO_META_MIN
    out = fit_seo_meta_final(meta, {"duration": "10 Days", "country": "Bhutan", "name": "Bhutan Highlands"})
    assert _in_band(out) and out.endswith("-day trip in Bhutan.") and "10" in out


def test_countries_named_uses_names_and_word_aliases_only():
    assert _countries_named("Laos to Vietnam by Bicycle") == {"Laos", "Vietnam"}
    assert _countries_named("From Viet Nam to Burma") == {"Vietnam", "Myanmar"}
    assert _countries_named("Indian food in Paro") == set()  # 'IND' code is not a word alias


@pytest.mark.parametrize("meta", [
    "A ten-day walking trip across western Bhutan visiting dzongs and the cliffside monastery of.",
    "A ten-day walking trip across western Bhutan visiting dzongs, farms and monasteries along the.",
    "A ten-day walking trip across western Bhutan visiting dzongs, farms and monasteries nearby.",
])
def test_complete_sentence_rejects_weak_endings(meta):
    assert meta_complete_sentence(meta) is ("nearby." in meta)


@pytest.mark.parametrize("meta", [
    "Trek the Annapurna region with permits, guides and lodges arranged in one trip, plus flights between "
    "Kathmandu and Pokhara on this short trip.",
    "A private 15-day Mongolia journey from Khövsgöl Lake and Tsaatan reindeer herders south through "
    "volcanic steppe, ancient monasteries, and the Gobi Desert.",
])
def test_complete_sentence_still_accepts_good_metas(meta):
    assert meta_complete_sentence(meta)


_LONG_META = ("Relax on Sri Lankan beaches, watch seasonal blue whales from Mirissa, and safari in Yala and "
              "Udawalawe National Parks with luxury transfers and a private guide throughout.")
_REGEN_META = ("Relax on Sri Lankan beaches, watch blue whales from Mirissa and go on safari in Yala and "
               "Udawalawe National Parks with a private guide and driver.")


def _structural(generated, tour_dict, brand_rules):
    return [] if len(generated["seo_meta"]) <= SEO_META_MAX else ["SEO_META_TOO_LONG"]


@pytest.mark.asyncio
async def test_t3_regenerates_the_meta_before_deciding_to_rewrite():
    # AA-925: one sentence > 155 — the fit no longer cuts it; T3 runs ONE meta-only regenerate (t2_flag_fix)
    # before it would pay for a whole-tour rewrite.
    from services.acp_produce import tenant_pipeline as tp
    result = {"generated": {"summary": "No numbers here.", "seo_meta": _LONG_META}}
    rewrite = AsyncMock()
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch.object(tp, "_t3_structural_issues", side_effect=_structural), \
            patch("services.content_generation.flag_fix_node._rerepair_meta", return_value=_REGEN_META) as rr:
        out = await tp.run_t3_qa_gate({"name": "Sri Lanka Coast", "country": "Sri Lanka",
                                       "duration": "10 Days"}, ["no numbers"], result, {}, tenant_id="t1")
    assert out["passed"] is True and out["attempts"] == 0
    rewrite.assert_not_awaited()
    assert rr.call_args.kwargs["stage"] == "t2_flag_fix"
    assert out["result"]["generated"]["seo_meta"] == _REGEN_META


@pytest.mark.asyncio
async def test_t3_meta_regenerate_that_adds_a_figure_is_refused():
    # the repair contract: a regenerated meta that adds a figure the meta and the source lack is not used
    from services.acp_produce import tenant_pipeline as tp
    bad = ("Relax on Sri Lankan beaches, watch 40 blue whales from Mirissa and go on safari in Yala and "
           "Udawalawe National Parks with a private guide and driver.")
    result = {"generated": {"summary": "No numbers here.", "seo_meta": _LONG_META}}
    with patch("api.routers.v1_pipeline._rewrite_tour", AsyncMock()), \
            patch.object(tp, "_t3_structural_issues", side_effect=_structural), \
            patch("services.content_generation.flag_fix_node._rerepair_meta", return_value=bad):
        out = await tp.run_t3_qa_gate({"name": "Sri Lanka Coast", "country": "Sri Lanka",
                                       "duration": "10 Days"}, ["no numbers"], result, {}, tenant_id="t1",
                                      max_repairs=0)
    assert out["passed"] is False and out["result"]["generated"]["seo_meta"] == _LONG_META
    assert [d["name"] for d in out["degraded"]] == ["repair_contract"]


@pytest.mark.asyncio
async def test_t3_jev_outage_is_counted_not_hard():
    # AA-925 review: T3's Jev only clears numeric hits, so an outage keeps every hit (fail-closed) and nothing
    # holds the output — the entry is "count", not "hard". It reaches the outcome (job.result diag t3).
    from services.acp_produce import tenant_pipeline as tp
    hit = {"field": "summary", "sentence": "Walk 12 km.", "novel_numbers": ["12"]}

    async def jev_down(grounding, tour_dict, tenant_id, errors=None):
        errors.extend({"mode": "enforce"} for _ in grounding)
        return grounding, []

    result = {"generated": {"summary": "Walk 12 km.", "seo_meta": _REGEN_META}}
    with patch("api.routers.v1_pipeline._rewrite_tour", AsyncMock()), \
            patch.object(tp, "_t3_structural_issues", return_value=[]), \
            patch.object(tp, "_t3_grounding_check", return_value=[hit]), \
            patch.object(tp, "t3_jev_clear", side_effect=jev_down):
        out = await tp.run_t3_qa_gate({"name": "Sri Lanka Coast", "country": "Sri Lanka",
                                       "duration": "10 Days"}, ["no numbers"], result, {}, tenant_id="t1",
                                      max_repairs=0)
    assert out["passed"] is False and out["grounding_issues"] == [hit]
    assert [(d["name"], d["severity"]) for d in out["degraded"]] == [("jev_unavailable", "count")]


def test_graph_fit_passes_the_tour_name():
    from services.content_generation.graph import _apply_seo_meta_fit
    gen = {"seo_meta": "A 12-day family cycling journey from Luang Prabang to Hanoi, covering the Nam Ou gorge, "
                       "Dien Bien Phu, and Cuc Phuong National Park."}
    _apply_seo_meta_fit(gen, {"tour": _LAOS_VN, "brand_forbidden_words": []})
    assert "in Laos" not in gen["seo_meta"]


def test_flag_fix_fit_passes_the_tour_name():
    from services.content_generation.flag_fix_node import _apply_seo_fit_before_llm
    content = {"seo_meta": "A 12-day family cycling journey from Luang Prabang to Hanoi, covering the Nam Ou "
                           "gorge, Dien Bien Phu, and Cuc Phuong National Park."}
    state = {"tour": _LAOS_VN, "brand_audit_codes": ["META_TOO_SHORT"], "failure_codes": []}
    keys, dropped, changed = _apply_seo_fit_before_llm(state, content, {"seo_meta"})
    # nothing clean to append → the field stays with the LLM repair, never ", in Laos."
    assert "seo_meta" in keys and "in Laos" not in content["seo_meta"]
