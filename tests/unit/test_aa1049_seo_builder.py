"""AA-1049 W3 (ADR 0020 S3, D5) — code-composed T9 SEO fields: whole-sentence / separator prefixes only, accepted only
through the caller's own acceptance predicates (the policy band read from the registry). Invented places only."""
from __future__ import annotations

from services.acp_content_writing import seo_builder as sb
from services.quality import writing_policy as wp

META_LO, META_HI = wp.band("meta")
TITLE_LO, TITLE_HI = wp.band("title")


def _meta_ok(v):
    return isinstance(v, str) and META_LO <= len(v) <= META_HI and v.endswith((".", "!", "?"))


def _title_ok(v):
    return isinstance(v, str) and TITLE_LO <= len(v) <= TITLE_HI


FIRST = ("Walk the harbour wall of Dunmere at low tide, then follow the net-mender's lane up to the old signal "
         "station that still looks over the grey bay.")


def test_meta_candidates_are_whole_leading_sentences_only():
    meta = FIRST + " Then take the ferry across to Brecca. Lunch is on the quay."
    assert sb.meta_candidates(meta) == [FIRST + " Then take the ferry across to Brecca.", FIRST]
    assert sb.meta_candidates("One sentence only.") == [] and sb.meta_candidates(None) == []
    # a decimal or an abbreviation is not a sentence end
    assert sb.meta_candidates("Walk 2.5 km past Mt. Orvine. See the lake.") == ["Walk 2.5 km past Mt. Orvine."]


def test_title_candidates_cut_before_a_separator():
    t = "Dunmere Harbour Walks for Families: Nets, Boats and the Sea Wall | Velmora Trails"
    assert sb.title_candidates(t) == ["Dunmere Harbour Walks for Families: Nets, Boats and the Sea Wall",
                                      "Dunmere Harbour Walks for Families"]
    assert sb.title_candidates("No separator here") == []


def test_build_takes_the_first_passing_prefix_and_never_invents():
    assert META_LO <= len(FIRST) <= META_HI
    seo = {"seo_title": "Harbour Walks in Dunmere for Families and Children | Velmora Trails Guide Book",
           "meta_description": FIRST + " Then take the ferry across to Brecca village for lunch on the quay.",
           "slug": "dunmere-harbour-walks"}
    out, log = sb.build_seo(seo, title_ok=_title_ok, meta_ok=_meta_ok)
    assert out["meta_description"] == FIRST
    assert out["seo_title"] == "Harbour Walks in Dunmere for Families and Children"
    assert out["slug"] == seo["slug"] and len(log) == 2
    assert seo["meta_description"].startswith(out["meta_description"])


def test_build_keeps_a_passing_value_and_a_field_with_no_passing_prefix():
    short = "Walk the Dunmere harbour wall."
    out, log = sb.build_seo({"seo_title": "Dunmere Harbour Walks for the Whole Family Today",
                             "meta_description": short}, title_ok=_title_ok, meta_ok=_meta_ok)
    assert out == {"seo_title": "Dunmere Harbour Walks for the Whole Family Today", "meta_description": short}
    assert log == []
    assert not sb.fields_ok(out, title_ok=_title_ok, meta_ok=_meta_ok)


def test_build_respects_a_stricter_caller_predicate():
    # the caller's predicate (keyword, scope, products, authenticity) decides; a prefix it refuses is never taken
    def meta_ok(v):
        return _meta_ok(v) and "Brecca" in v
    seo = {"meta_description": FIRST + " Then take the ferry across to Brecca village for lunch on the quay."}
    out, log = sb.build_seo(seo, title_ok=lambda v: True, meta_ok=meta_ok)
    assert out["meta_description"] == seo["meta_description"] and log == []


def test_contract_counts():
    cta = "Ask Velmora Trails for the harbour walk dates."
    text = ("Intro about Dunmere.\n\n## FAQ\n\n**Q: Is the wall walk steep?**\nA: No, it follows the harbour.\n"
            "**Q: Where does it start?**\nA: At the net sheds.\n\n" + cta)
    counts = sb.contract_counts(text, cta)
    assert counts["cta"] == 1 and counts["faq_pairs"] == 2
    assert sb.contract_counts("x", None) == {"cta": 0, "faq_pairs": 0}


# Review S272 (finding 6): the old 1-3 capital-letter rule made "approx.", "vs.", "Prof.", "Capt." and "U.S." sentence
# ends, so a mid-sentence prefix passed the real meta band and shipped with 0 fit calls; and "Bay." / "Hue." were
# dropped as abbreviations. An explicit list + dotted initialisms now decide.
def test_no_cut_after_an_abbreviation_or_a_dotted_initialism():
    assert sb.meta_candidates("Ride with a guide for approx. 3 hours. Lunch follows.") == [
        "Ride with a guide for approx. 3 hours."]
    assert sb.meta_candidates("Ride from Saigon vs. Hanoi traffic. Lunch follows.") == [
        "Ride from Saigon vs. Hanoi traffic."]
    assert sb.meta_candidates("Walk with Prof. Tran through the old quarter. Lunch follows.") == [
        "Walk with Prof. Tran through the old quarter."]
    assert sb.meta_candidates("Sail with Capt. Orvine. Then lunch.") == ["Sail with Capt. Orvine."]
    assert sb.meta_candidates("Visit the U.S. Consulate, e.g. Monday. Then lunch.") == [
        "Visit the U.S. Consulate, e.g. Monday."]
    assert sb.meta_candidates("Start at 7 a.m. Bikes are ready. Then lunch.")[-1] == "Start at 7 a.m. Bikes are ready."


def test_a_real_word_at_a_sentence_end_is_an_end():
    assert sb.meta_candidates("Cruise Ha Long Bay. Then kayak past the caves.") == ["Cruise Ha Long Bay."]
    assert sb.meta_candidates("Start in Hue. Then ride south.") == ["Start in Hue."]
    assert sb.meta_candidates("Rest at the Spa. Then dinner.") == ["Rest at the Spa."]


def test_fit_seo_meta_never_ships_a_cut_after_approx():
    """End to end through service._fit_seo_meta with the real band and meta_ok: the writer meta has no whole-sentence
    prefix, so the fit call is made (as on main) and the delivered meta is never the cut after 'approx.'."""
    import asyncio
    from unittest.mock import patch
    from uuid import uuid4

    from services.acp_content_writing import service
    meta = ("Join our Hoi An cycling tour through Tra Que village, past rice paddies, herb gardens and quiet riverside "
            "lanes with a local guide for approx. 3 hours, then lunch with a farming family.")
    cut = meta[:meta.index("approx.") + len("approx.")]
    body = "A Hoi An cycling tour rides through Tra Que village, its herb gardens and a farming family's lunch."
    assert META_LO <= len(cut) <= META_HI and service._meta_ok(cut, "hoi an cycling tour")  # the old trap
    assert not service.seo_grounding.meta_unsupported_products(cut, body, body)
    seo = {"seo_title": "Hoi An Cycling Tour Through Tra Que Village Gardens", "meta_description": meta}
    with patch.object(service, "fit_seo_fields", side_effect=RuntimeError("fit outage")) as fit:
        out, cost = asyncio.run(service._fit_seo_meta(seo, "hoi an cycling tour", body=body,
                                                      context={"atom_text": body}, request_id=uuid4()))
    assert fit.call_count == 1 and out["meta_description"] == meta and cost == 0.0
