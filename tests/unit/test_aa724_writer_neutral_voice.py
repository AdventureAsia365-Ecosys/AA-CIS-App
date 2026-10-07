"""AA-724 — adventure/cycle tours failed brand-fit because the S1 writer echoed the source's
persona/hype voice, and SEO_META_TOO_LONG recurred because the S1 path (unlike T2's AA-641) had no
deterministic seo_meta fit. Two fixes, both tested mock-only (no AWS/DB/LLM):

(a) SYSTEM_PROMPT now tells the writer to neutralize promotional source copy into third-person
    editorial voice (the lever for the brand_fit failures regenerate couldn't fix).
(b) _apply_seo_meta_fit runs fit_seo_meta before validate scores the meta, on both the live and the
    Bedrock Batch seed path.
"""
from services.content_generation import prompts
from services.content_generation.graph import _apply_seo_meta_fit
from services.content_generation.seo_meta_utils import SEO_META_MAX, SEO_META_MIN, meta_complete_sentence

_HEAD = ("This Thailand ride threads Mae Taeng valley farmland, hill temples and quiet back roads "
         "with unhurried pacing and expert local guides throughout")


def _sentence(n: int) -> str:
    pad = n - len(_HEAD) - 2
    return _HEAD + " " + ("o" * pad) + "."


# ── (a) writer prompt steers to neutral catalogue voice ──────────────────────────────────────────

def test_system_prompt_instructs_neutralizing_promotional_source():
    sp = prompts.SYSTEM_PROMPT
    assert "AA-724" in sp
    # The core instruction: a promotional source must not become promotional output.
    assert "neutral" in sp.lower() and "third-person" in sp.lower()
    assert "promotional source is not a licence" in sp.lower()


def test_system_prompt_bans_second_person_and_imperatives():
    sp = prompts.SYSTEM_PROMPT.lower()
    assert "never address the reader" in sp or 'never use imperatives' in sp
    assert '"you"' in sp


def test_system_prompt_names_the_concrete_hype_phrases_from_the_repro_tours():
    # The judge feedback on the two Thailand cycling tours quoted these; the prompt should call
    # out the same persona/mood register so the writer strips it instead of echoing it.
    sp = prompts.SYSTEM_PROMPT.lower()
    for phrase in ("sunglasses on", "made for riding", "playlist-worthy", "on your own terms"):
        assert phrase in sp, f"prompt should name the hype phrase: {phrase}"


def test_neutral_voice_rule_propagates_through_build_s1_system_prompt():
    from services.content_generation.batch_prompt import build_s1_system_prompt
    state = {"tour": {"name": "T", "country": "Thailand"}, "seo": {}}
    built = build_s1_system_prompt(state)
    assert "AA-724" in built and "neutral" in built.lower()


# ── (b) deterministic seo_meta fit on the S1 path ────────────────────────────────────────────────

def test_apply_seo_meta_fit_trims_over_long_meta_in_the_generated_dict():
    generated = {"seo_meta": _sentence(150) + " A trailing second sentence that blows the limit wide open."}
    assert len(generated["seo_meta"]) > SEO_META_MAX
    _apply_seo_meta_fit(generated, {"brand_forbidden_words": []})
    assert SEO_META_MIN <= len(generated["seo_meta"]) <= SEO_META_MAX
    assert meta_complete_sentence(generated["seo_meta"]) and generated["seo_meta"] == _sentence(150)


def test_apply_seo_meta_fit_leaves_an_in_band_meta_untouched():
    ok = _sentence(150)
    generated = {"seo_meta": ok}
    _apply_seo_meta_fit(generated, {})
    assert generated["seo_meta"] == ok


def test_apply_seo_meta_fit_honours_brand_forbidden_words():
    # the only in-band complete-sentence prefix contains a brand forbidden word -> left unchanged
    meta = _sentence(150) + " Another sentence to force a trim."
    generated = {"seo_meta": meta}
    _apply_seo_meta_fit(generated, {"brand_forbidden_words": ["unhurried"]})
    assert generated["seo_meta"] == meta


def test_apply_seo_meta_fit_is_a_noop_when_no_meta():
    generated = {"name": "no meta here"}
    _apply_seo_meta_fit(generated, {})
    assert "seo_meta" not in generated
