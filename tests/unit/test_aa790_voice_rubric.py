"""AA-790 (slice B) — the deterministic tenant-voice rubric, an offline scorer (S250 go/no-go 6b: no ledger entry,
no registry gate, no pipeline reader during the gate freeze). Fixtures: the real S234 r4 Trail Pulse false passes
(test_aa780 R4_7 = label-pack r4 #6, R4_12 = r4 #11) and on-voice posts written from each pilot brand's own style
guide."""
from __future__ import annotations

import pathlib

import pytest

from services.acp_content_writing import voice_rubric as vr
from tests.unit.test_aa780_linkedin_floor_voice import AH, R4_7, R4_12, TRAIL

TERRA = {"brand_name": "Terra Family Expeditions", "brand_type": "Premium family adventure travel brand",
         "customer_segment": "Affluent families with children aged 7–16",
         "voice": ["Warm", "Reassuring", "Clear", "Practical", "Family-aware", "Encouraging", "Trustworthy"]}
WILD = {"brand_name": "WildKind Travel", "brand_type": "Responsible nature and conservation travel brand",
        "voice": ["Thoughtful", "Grounded", "Respectful", "Nature-led", "Ethical", "Calm", "Informative"]}
TRAIL_CTA = "Get the full Trail Pulse itinerary and dates"

TRAIL_ON_VOICE = (
    "Day 2 starts on a forest trail and ends in turquoise water.\n\n"
    "You'll hike an easy hour to Kuang Si Falls, then it's swim time. Jump into the lower pools, grab a spot on "
    "the rocks and let the cold water wake you up.\n\n"
    "Lunch is at the café above the cascade. Bring a dry shirt.\n\n"
    f"{TRAIL_CTA}.\n\n#LuangPrabang #KuangSiFalls #Laos")
AH_ON_VOICE = (
    "Punakha Dzong sits where the Pho Chhu and the Mo Chhu meet, its whitewashed walls rising above the water.\n\n"
    "The courtyards open one into the next, and a private guide sets the pace through the assembly hall.\n\n"
    "Talk to an Atlas & Hearth travel designer about this journey.")
TERRA_ON_VOICE = (
    "## The Pools\n\nFamilies reach the falls on an easy trail, and children can swim in the shallow lower pools.\n\n"
    "## Lunch\n\nParents can rest at the café while the kids dry off in the sun.")
TERRA_SCHOOL = (
    "## The Pools\n\nFamilies reach the falls on an easy trail.\n\n"
    "## Lunch\n\nThe walk works like a field trip for children, a lesson plan in limestone.")
WILD_ON_VOICE = (
    "The forest around Kuang Si is home to sun bears rescued from the wildlife trade.\n\n"
    "Below the falls, the limestone pools hold small fish and the river runs clear through the trees.")
WILD_ARCHITECTURE = (
    "Punakha Dzong was built in 1637 as the seat of government.\n\n"
    "Its assembly hall holds a hundred gilded columns.\n\n"
    "The administrative wing faces the courtyard.\n\n"
    "The towers rise six storeys above the gate.")


class TestProfile:
    @pytest.mark.parametrize("pack, profile", [(TRAIL, "energetic"), (AH, "calm"), (TERRA, "family"),
                                               (WILD, "wildlife"), ({}, "neutral")])
    def test_the_pilot_packs_compile_to_their_profile(self, pack, profile):
        rubric = vr.compile_profile(pack)
        assert rubric.profile == profile and rubric.source == "compiled"
        assert rubric.targets == vr.DEFAULT_TARGETS[profile]

    def test_the_custom_validators_override_wins(self):
        override = vr.override_from_validators(
            {"signature_words": ["x"], "voice_rubric": {"profile": "calm", "targets": {"staccato_max": 0}}})
        rubric = vr.compile_profile(TRAIL, override)
        assert (rubric.profile, rubric.source) == ("calm", "override")
        assert rubric.targets["staccato_max"] == 0 and "exclamations_max" in rubric.targets
        assert vr.override_from_validators({"signature_words": ["x"]}) is None
        assert vr.override_from_validators(None) is None

    def test_an_override_without_a_profile_only_tunes_the_targets(self):
        rubric = vr.compile_profile(TRAIL, {"targets": {"mean_words_max": 12, "bad": "x"}})
        assert rubric.profile == "energetic" and rubric.targets["mean_words_max"] == 12 and "bad" not in rubric.targets


class TestStructure:
    @pytest.mark.parametrize("sentence", ["That's not filler.", "That is not a backdrop.", "It isn't a detour.",
                                          "That's not coincidence."])
    def test_staccato_negation(self, sentence):
        assert vr.staccato(sentence)

    @pytest.mark.parametrize("sentence", ["That is not what the seed says about the boat on Day 3 at all.",
                                          "The pools are not deep.", "This trail is steep."])
    def test_not_staccato(self, sentence):
        assert not vr.staccato(sentence)

    @pytest.mark.parametrize("sentence", ["That kind of sequencing is deliberate.", "It's a deliberate call.",
                                          "This is how pacing works."])
    def test_consultancy_maxim(self, sentence):
        assert vr.maxim(sentence)

    @pytest.mark.parametrize("sentence", ["This is Punakha Dzong.", "It is 5 a.m. in Paro.",
                                          "That is where you swim.", "That's not filler."])
    def test_not_a_maxim(self, sentence):
        assert not vr.maxim(sentence)

    @pytest.mark.parametrize("sentence", ["Grab a towel before lunch.", "Start the morning on a forest trail.",
                                          "You'll swim before lunch.", "Let's get you to the falls."])
    def test_invitation(self, sentence):
        assert vr.invitation(sentence)

    @pytest.mark.parametrize("sentence", ["The trek on Day 2 ends at the pools.", "Kuang Si Falls sits south.",
                                          "It takes an hour.", "When the sun is up, the water glows."])
    def test_not_an_invitation(self, sentence):
        assert not vr.invitation(sentence)


class TestScore:
    @pytest.mark.parametrize("text", [R4_7, R4_12])
    def test_the_r4_trail_pulse_false_passes_would_block(self, text):
        res = vr.score(text, vr.compile_profile(TRAIL), channel="linkedin", cta=TRAIL_CTA)
        assert res["would_block"] and any("staccato" in v for v in res["violations"])

    def test_r4_12_also_reads_formal_and_maxim(self):
        v = vr.score(R4_12, vr.compile_profile(TRAIL), cta=TRAIL_CTA)["violations"]
        assert any(x.startswith("formal register") for x in v)
        assert any("That kind of sequencing is deliberate." in x for x in v)

    @pytest.mark.parametrize("pack, text, cta", [
        (TRAIL, TRAIL_ON_VOICE, TRAIL_CTA),
        (AH, AH_ON_VOICE, "Talk to an Atlas & Hearth travel designer about this journey"),
        (TERRA, TERRA_ON_VOICE, None),
        (WILD, WILD_ON_VOICE, None),
    ])
    def test_on_voice_posts_do_not_fire(self, pack, text, cta):
        res = vr.score(text, vr.compile_profile(pack), cta=cta)
        assert res["passed"] and not res["would_block"], res["violations"]

    def test_family_school_framing_and_wildlife_architecture_are_style_notes(self):
        terra = vr.score(TERRA_SCHOOL, vr.compile_profile(TERRA))
        assert any(v.startswith("school framing") for v in terra["violations"]) and not terra["would_block"]
        wild = vr.score(WILD_ARCHITECTURE, vr.compile_profile(WILD))
        assert any(v.startswith("habitat or wildlife") for v in wild["violations"])

    def test_calm_profile_reads_hype(self):
        res = vr.score("The dzong is stunning! Book now! Wow!", vr.compile_profile(AH))
        assert any(v.startswith("hype register") for v in res["violations"])

    @pytest.mark.parametrize("text", [R4_7, R4_12, TRAIL_ON_VOICE, WILD_ARCHITECTURE])
    def test_the_result_never_blocks(self, text):
        res = vr.score(text, vr.compile_profile(TRAIL))
        assert res["blocking"] is False and res["repairable"] is False and res["rubric"] == vr.RUBRIC_VERSION


def test_no_pipeline_module_reads_the_rubric():
    """Go/no-go 6b: an offline scorer during the gate freeze — only its replay script imports it."""
    root = pathlib.Path(__file__).resolve().parents[2]
    readers = [p.relative_to(root).as_posix() for d in ("api", "services", "shared", "worker")
               for p in (root / d).rglob("*.py")
               if "voice_rubric" in p.read_text(encoding="utf-8") and p.name != "voice_rubric.py"]
    assert readers == []
