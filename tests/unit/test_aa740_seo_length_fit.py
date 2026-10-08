"""AA-740 — deterministic seo_meta / seo_title length fit (S218: 15 tours scoring >= 7 were blocked
from Master only by SEO_META_TOO_LONG / META_TOO_SHORT / SEO_TITLE_TOO_LONG)."""
import pytest

from services.content_generation.seo_meta_utils import (
    SEO_META_MAX, SEO_META_MIN, fit_seo_meta_final, fit_seo_title, meta_complete_sentence, tour_days,
)

SL = {"duration": "10 DAYS", "country": "Sri Lanka"}


def _ok(m):
    return SEO_META_MIN <= len(m) <= SEO_META_MAX and meta_complete_sentence(m)


def test_in_band_meta_unchanged():
    m = ("Trek the Annapurna region with permits, guides and lodges arranged in one trip, "
         "plus flights between Kathmandu and Pokhara on this short trip.")
    assert _ok(m) and fit_seo_meta_final(m, SL) == m


def test_one_sentence_too_long_is_cut_at_a_boundary():
    m = ("Relax on Sri Lankan beaches, watch seasonal blue whales from Mirissa, and safari in Yala and "
         "Udawalawe National Parks with luxury transfers and a private guide throughout.")
    assert len(m) > SEO_META_MAX
    out = fit_seo_meta_final(m, SL)
    assert _ok(out) and m.startswith(out[:-1])
    assert not out.rstrip(".").split()[-1].lower() in {"and", "with", "a", "the"}


def test_too_short_gets_a_fact_clause():
    m = ("Visit Kandy's Temple of the Tooth, the Sigiriya rock fortress, the Dambulla caves and the tea "
         "estates around Nuwara Eliya on a guided tour.")
    assert len(m) < SEO_META_MIN
    out = fit_seo_meta_final(m, {"duration": "13 Nights 14 days", "country": "Sri Lanka"})
    assert _ok(out) and "14 days" in out


def test_short_meta_that_already_states_days_is_not_padded_twice():
    m = ("Visit Sri Lanka's sacred temples, Sigiriya Rock Fortress, Dambulla caves, and Yala National "
         "Park's wildlife on this five-day tour.")
    out = fit_seo_meta_final(m, {"duration": "5 days", "country": "Sri Lanka"})
    assert "5 days" not in out  # no "five-day tour, over 5 days"


def test_impossible_case_left_for_the_gate():
    m = "Photograph leopards and tea country on this tour."  # far too short to extend into band
    assert fit_seo_meta_final(m, SL) == m


def test_never_adds_a_forbidden_word():
    m = ("Spend ten days visiting Kandy, the tea hills around Ella and the beaches of the south coast "
         "with a guide.")
    out = fit_seo_meta_final(m, {"duration": "10 days", "country": "Sri Lanka"}, ["cheap"])
    assert "cheap" not in out.lower()


@pytest.mark.parametrize("raw,days", [("14 DAYS", 14), ("13 Nights 14 days", 14), ("08 nights", 9),
                                      ("1 day", 1), ("", None), (None, None)])
def test_tour_days(raw, days):
    assert tour_days(raw) == days


def test_title_fit_reused_on_s1():
    t = "Sri Lanka Classics: Temples, Tea Country and Wildlife Safari — 14 Days"
    assert len(fit_seo_title(t)) <= 60


def test_revalidate_refits_meta_even_without_fix_pass(monkeypatch):
    # A deterministic edit at revalidate must be re-validated, else the old code keeps blocking.
    from services.content_generation import graph
    seen = {}

    def fake_validate(state):
        seen["meta"] = state["generated"]["seo_meta"]
        return {**state, "quality_score": 8.0, "failure_codes": []}

    monkeypatch.setattr(graph, "validate_node", fake_validate)
    monkeypatch.setattr(graph, "judge_node", lambda s: s)
    monkeypatch.setattr(graph, "_apply_grounding_recheck", lambda s: s)
    long_meta = ("Relax on Sri Lankan beaches, watch seasonal blue whales from Mirissa, and safari in Yala "
                 "and Udawalawe National Parks with luxury transfers and a private guide throughout.")
    state = {"fix_pass_applied": False, "brand_forbidden_words": [], "tour": SL | {"name": "T"},
             "generated": {"seo_meta": long_meta}}
    out = graph.revalidate_node(state)
    assert _ok(seen["meta"]) and out["revalidate_ran"] is True


def test_revalidate_passthrough_when_nothing_to_fix(monkeypatch):
    from services.content_generation import graph
    monkeypatch.setattr(graph, "_apply_grounding_recheck", lambda s: s)
    m = ("Trek the Annapurna region with permits, guides and lodges arranged in one trip, "
         "plus flights between Kathmandu and Pokhara on this short trip.")
    out = graph.revalidate_node({"fix_pass_applied": False, "tour": SL, "generated": {"seo_meta": m}})
    assert out["revalidate_ran"] is False
