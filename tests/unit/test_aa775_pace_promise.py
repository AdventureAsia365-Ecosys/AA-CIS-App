"""AA-775 (S235) — the style guide is fenced as "never a source", and F17 gets a `pace_promise` finding
for pace / rest / balance sentences the moment's own source does not back (S234 r1-r4: Terra 6/11/15/12,
A&H 5/2/7/3 pace sentences copied from the tenant style guide)."""
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import service as writing_service
from services.acp_content_writing import trip_facts as tf
from services.acp_shared.brand_pack import render_brand_block

SEED_NO_PACE = ("Lake Khövsgöl — boat ride\nSource itinerary: Take a boat ride on the lake and walk the shore "
                "forest paths near the camp.")
SEED_SWIM_REST = ("Kuang Si Falls — swim\nSource itinerary: A short trek ends at the tiered limestone pools, "
                  "with time to swim and rest before lunch at the View Café.")
TERRA_CTA = "Ask Terra Family Expeditions to plan this trip for your family."


def _blog(*sentences: str) -> str:
    return "## Lake Khövsgöl\n\n" + " ".join(sentences) + "\n"


# ---------------------------------------------------------------- the fence (render_brand_block)

def test_brand_block_fences_the_style_guide():
    block = render_brand_block({"brand_name": "Terra Family Expeditions",
                                "style_guide": "Explain the pace clearly."})
    assert "This is style and never a source" in block
    assert "never repeat one of its example sentences" in block
    assert "pace, rest, balance or difficulty" in block and "unless the content seed says so" in block
    assert block.rstrip().endswith("never invent how the brand works, its group sizes, guides, partners, "
                                   "history or numbers.")
    assert "Explain the pace clearly." in block


def test_brand_block_without_style_guide_has_no_fence():
    assert "never a source" not in render_brand_block({"brand_name": "Trail Pulse", "core_idea": "Move."})


# ---------------------------------------------------------------- pace_claims

def test_flags_a_pace_line_the_seed_does_not_make():
    text = _blog("The lake is large.", "The days here are full without being relentless.")
    out = tf.pace_claims(text, tf.TripFactContext(seed=SEED_NO_PACE).source)
    assert len(out) == 1
    assert out[0].startswith("states a pace or rest promise the source does not make ('relentless')")
    assert out[0].endswith("'The days here are full without being relentless.'")


def test_flags_the_style_guide_balance_line():
    text = _blog("Terra Family Expeditions builds journeys so that active discovery is balanced with time "
                 "to rest, giving both children and parents space.")
    assert len(tf.pace_claims(text, tf.TripFactContext(seed=SEED_NO_PACE).source)) == 1


def test_seed_supported_rest_is_not_flagged():
    text = _blog("The itinerary builds in time to swim and rest here before lunch.")
    assert tf.pace_claims(text, tf.TripFactContext(seed=SEED_SWIM_REST).source) == []


def test_the_rest_of_is_not_a_pace_word():
    text = _blog("The rest of the day is spent on the shore.")
    assert tf.pace_claims(text, tf.TripFactContext(seed=SEED_NO_PACE).source) == []


def test_the_rest_of_does_not_hide_a_second_pace_word():
    text = _blog("The rest of the day moves at an unhurried pace.")
    assert len(tf.pace_claims(text, tf.TripFactContext(seed=SEED_NO_PACE).source)) == 1


def test_configured_cta_is_not_flagged():
    cta = "Talk to an Atlas & Hearth travel designer about how this journey could be shaped around your own pace."
    text = _blog("The lake is large.", cta)
    assert tf.pace_claims(text, "", cta=cta, brand_name="Atlas & Hearth") == []


def test_cta_paraphrase_is_not_flagged():
    cta = "Speak with Atlas & Hearth about this journey."
    for line in ("Talk to an Atlas & Hearth travel designer about this journey, and how it might be shaped "
                 "around your particular interests and pace.",
                 "If this part of the world has your attention, talk to an Atlas & Hearth travel designer "
                 "about how it might be shaped around your interests and pace.",
                 "Ask Terra Family Expeditions to plan this trip, and we will talk through the itinerary and "
                 "the pace.",
                 "Talk to us about the pace that suits your family."):
        assert tf.pace_claims(_blog(line), "", cta=cta, brand_name="Terra Family Expeditions"
                              if line.startswith("Ask") else "Atlas & Hearth") == [], line


def test_brand_self_description_is_still_flagged():
    """The brand's own line opens with no invitation verb: the pace promise is the style-guide paste."""
    text = _blog("Terra Family Expeditions plans itineraries for families at a pace that works for "
                 "children and adults alike.")
    assert len(tf.pace_claims(text, "", cta="Speak with our team about this journey.",
                              brand_name="Terra Family Expeditions")) == 1


# ---------------------------------------------------------------- wiring (blog only) + repair route

def _ctx(channel):
    return tf.TripFactContext(seed=SEED_NO_PACE, channel=channel, brand_name="Terra Family Expeditions",
                              cta=TERRA_CTA)


def test_linkedin_never_gets_a_pace_finding():
    text = "The lake is big.\n\nThe days here are full without being relentless.\n"
    assert not any("pace or rest" in v for v in tf.find_trip_fact_issues(text, _ctx("linkedin"))["trip_fact"])
    assert not any("pace or rest" in v for v in tf.find_trip_fact_issues(text, _ctx(None))["trip_fact"])


def test_blog_pace_finding_blocks_and_goes_to_sentence_repair():
    text = _blog("The lake is large.", "The days here are full without being relentless.")
    f17, _f18 = qg.gate_trip_facts(text, _ctx("blog"))
    assert f17["gate"] == "F17_trip_fact" and not f17["passed"] and f17["blocking"] and f17["repairable"]
    assert any("pace or rest promise" in v for v in f17["violations"])
    items = sr.plan(text, [f17])
    assert items and items[0]["sentence"] == "The days here are full without being relentless."
    assert items[0]["problem"].startswith("states a pace or rest promise the source does not make")
    assert writing_service._style_only_repair([f17])  # cheap t9_repair stage, never a full rewrite


def test_problem_text_parses_as_one_quoted_item():
    v = tf._quoted(tf.PACE_PROBLEM.format(word="pace"), "The pace is deliberate.")
    m = sr._BRAND_QUOTE_RE.search(v)
    assert m and m.group(2) == "The pace is deliberate."
