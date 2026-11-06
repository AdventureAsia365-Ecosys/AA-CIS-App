"""AA-741 (S271 phase-1 audit) — the T9 blog writer's output contract keeps a blog write well under its ceiling.

Finding (4 audit tenants, 06/11/2026): t9_write for a blog averaged 1,846 output tokens but reached 11,465, and 4 calls
hit the 12,000 ceiling (3 cut, 1 billed but unusable), while the articles were ~620-950 words. The blog prompt had no
length (only the F4 gate's 4,700-13,000 character band), the channel asks for "in-depth destination guides", and the
shared silent reread let an adaptive-thinking model draft the article, check it sentence by sentence and copy it out
again. t9_write v2 adds one block to every blog prompt (write and rewrite, single moment and route): a word band inside
the F4 band, a section cap, one plan-then-write pass, one article only. Other channels and SYSTEM_PROMPT are unchanged.

No model is called (fake clients); invented places only.
"""
import json
import re
from unittest.mock import MagicMock, patch

import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import get_goal
from services.acp_content_writing import generate as t9
from services.acp_content_writing import prompts
from services.acp_content_writing import quality_gates as qg
from services.acp_shared import token_budget as tb
from shared.llm_client.models import LLMResponse
from shared.llm_client.prompt_registry import load_manifest, newest, prompt_hash_of

GOAL = get_goal("promotion")
ANGLE = {"name": "Shore Walk", "why_it_works": "w", "formula_fit": "f", "best_final_style": "Plain and warm."}
SEED = ("Lake Aurel — walk the shore\nLocation: Lake Aurel, Valdoria\nDay 3 of the trip\n"
        "Source itinerary: Walk the shore path to the ferry pier. Boat to Tamsel Isle and visit the chapel.")
SEED2 = ("Corvin Pass — cross on foot\nLocation: Corvin Pass, Valdoria\nDay 4 of the trip\n"
         "Source itinerary: Cross the pass on foot with a local guide. Lunch at the mountain hut.")
BLOCK = prompts._BLOG_LENGTH_INSTRUCTIONS
ENVELOPE_KEYS = {"seo_title", "meta_description", "slug", "body", "summary"}


def _prompt(channel="blog", **kw):
    extra = {"atom_id": "a1"} if channel == "blog" else {}
    return prompts.build_user_prompt(content_seed=SEED, goal=GOAL, channel_style=get_channel_style(channel),
                                     brand_audience={"brand_name": "Pine Lantern Trips", "voice": ["Clear"]},
                                     angle=ANGLE, cta="Message us.", **extra, **kw)


# ---------------------------------------------------------------- the prompt contract

@pytest.mark.parametrize("kw", [
    {},                                                                          # single moment
    {"route_segments": [("a1", SEED), ("a2", SEED2)]},                           # a walk (route-aware variant)
    {"revision_feedback": ["F1: remove the number"], "keyword": "lake aurel ferry"},  # the rewrite
])
def test_every_blog_prompt_carries_the_block_once_between_format_and_envelope(kw):
    p = _prompt(**kw)
    assert p.count(BLOCK) == 1
    fmt = p.index("BLOG-SPECIFIC FORMAT REQUIREMENTS")
    assert fmt < p.index("LENGTH AND EFFORT FOR THIS BLOG") < p.index("OUTPUT FORMAT FOR THIS CHANNEL ONLY")


@pytest.mark.parametrize("channel", ["linkedin", "facebook", "instagram", "tiktok", "ads", "email", "landing_page"])
def test_no_other_channel_sees_the_block(channel):
    assert "LENGTH AND EFFORT FOR THIS BLOG" not in _prompt(channel)


def test_system_prompt_is_unchanged_by_v2():
    assert "LENGTH AND EFFORT" not in prompts.SYSTEM_PROMPT
    assert "Write the article once" not in prompts.SYSTEM_PROMPT


def test_block_asks_for_one_pass_and_one_article():
    assert "Write the article once." in BLOCK
    assert "Never write a full draft and then copy it out again" in BLOCK
    assert "the silent reread is one quick pass" in BLOCK
    assert "Return one article only: no alternative versions, no notes, no outline" in BLOCK
    assert "never with invented facts or filler" in BLOCK      # a thin seed reaches the floor from its own facts
    assert "—" not in BLOCK                              # AA-778: no em-dash in our prompt text


def test_word_band_sits_inside_the_f4_character_band():
    lo, hi = prompts._BLOG_WORDS
    assert lo < hi < prompts._BLOG_WORDS_MAX
    # ~5.3-6.5 characters a word with its space: the target floor clears F4's floor, the hard max stays well under
    # F4's ceiling (the model overshoots counts, lessons: "LLM counts poorly").
    assert lo * 5.3 >= qg._BLOG_MIN_CHARS
    assert prompts._BLOG_WORDS_MAX * 6.5 <= qg._BLOG_MAX_CHARS * 0.85
    numbers = re.findall(r"\d[\d,]*", BLOCK)
    assert f"{lo:,}" in numbers and f"{hi:,}" in numbers and f"{prompts._BLOG_WORDS_MAX:,}" in numbers
    assert f"At most {prompts._BLOG_SECTIONS_MAX} " in BLOCK


def test_the_stated_floor_is_the_f4_floor_and_nothing_licenses_a_shorter_article():
    """S271 review: the block said "a seed with few facts gives a shorter article" with no floor while F4 holds a blog
    body under 4,700 characters, so an F4 rewrite got "content is only N chars" and "shorter is fine" at once. The
    block now states a hard minimum, and that minimum in characters clears F4's floor."""
    floors = [int(n.replace(",", "")) for n in re.findall(r"never under (\d[\d,]*)", BLOCK)]
    assert floors == [prompts._BLOG_WORDS[0]]
    assert floors[0] * 5.3 >= qg._BLOG_MIN_CHARS
    lowered = BLOCK.lower()
    for licence in ("shorter article", "shorter piece", "shorter is", "fewer words", "short article"):
        assert licence not in lowered


def test_an_f4_rewrite_prompt_carries_no_contradiction():
    p = _prompt(revision_feedback=["F4_extreme_length: content is only 2100 chars — effectively empty"])
    assert p.count(BLOCK) == 1
    assert f"never under {prompts._BLOG_WORDS[0]:,}" in p
    assert "shorter article" not in p.lower()


def test_a_full_length_reply_stays_well_under_the_ceiling():
    """The largest article the contract allows, with a citation tag on every sentence, the FAQ and the envelope, is
    under a third of the blog ceiling: the ceiling is head-room, never the expected size."""
    words = prompts._BLOG_WORDS_MAX
    body_tokens = words * 1.4                 # English prose with markdown headings
    tag_tokens = (words / 15) * 8             # one [R:atom_xxxxxxxxxx] tag per ~15-word sentence
    envelope_tokens = 250                     # seo_title, meta_description, slug, summary, JSON quoting
    assert body_tokens + tag_tokens + envelope_tokens < tb.T9_WRITE_BLOG_MAX_TOKENS / 3


def test_the_envelope_keys_are_the_contract():
    keys = set(re.findall(r'^\s*"(\w+)":', prompts._SEO_ENVELOPE_INSTRUCTIONS, flags=re.M))
    assert keys == ENVELOPE_KEYS


# ---------------------------------------------------------------- the registry

def test_manifest_v2_covers_the_block():
    """v2 brought the block; every later version (v3: the walk focus, AA-741 S271 review) keeps hashing it."""
    block_symbols = {"_BLOG_LENGTH_INSTRUCTIONS", "_BLOG_WORDS", "_BLOG_WORDS_MAX", "_BLOG_SECTIONS_MAX"}
    v2 = next(e for e in load_manifest()["t9_write"] if e["version"] == "v2")
    assert block_symbols <= set(v2["symbols"])
    entry = newest(load_manifest(), "t9_write")
    assert block_symbols <= set(entry["symbols"])
    assert prompt_hash_of(entry["source_path"], entry["symbols"]) == entry["prompt_sha256"]


# ---------------------------------------------------------------- the call (fake client)

def _resp(content: str, stop: str = "end_turn") -> LLMResponse:
    return LLMResponse(content=content, model_used="model-x", provider="anthropic", cost_usd=0.03, stop_reason=stop,
                       input_tokens=4000, output_tokens=1900)


def _envelope(body: str) -> str:
    return json.dumps({"seo_title": "Lake Aurel shore walk and the Tamsel Isle ferry",
                       "meta_description": "Walk the Lake Aurel shore path, take the boat to Tamsel Isle and visit its "
                                           "chapel on day three of this Valdoria trip.",
                       "slug": "lake-aurel-shore-walk", "body": body, "summary": "The shore walk and the isle."})


BODY = ("## The shore path\nWalk the shore path to the ferry pier. [R:a1]\n\n## Tamsel Isle\nA boat crosses to "
        "Tamsel Isle, where the chapel stands. [R:a1]\n\nMessage us to plan it.\n\n## FAQ\n"
        "**Q: How do you reach Tamsel Isle?**\nA: Tamsel Isle is reached by boat from the ferry pier at the end of "
        "the Lake Aurel shore path. [R:a1]")


@pytest.mark.parametrize("fn, extra", [(t9.write_content, {}), (t9.rewrite_with_feedback,
                                                                {"revision_feedback": ["F1: remove it"]})])
def test_write_and_rewrite_send_the_block_under_the_blog_ceiling_and_parse_one_envelope(fn, extra):
    client = MagicMock()
    client.generate.side_effect = [_resp(_envelope(BODY))]
    with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
        body, cost, seo, summary = fn(content_seed=SEED, goal=GOAL, channel_style=get_channel_style("blog"),
                                      brand_audience={}, angle=ANGLE, cta="Message us.", atom_id="a1", **extra)
    request = client.generate.call_args.args[0]
    assert request.stage == "t9_write" and request.max_tokens == tb.T9_WRITE_BLOG_MAX_TOKENS
    assert request.user_prompt.count(BLOCK) == 1
    assert body == BODY and seo["slug"] == "lake-aurel-shore-walk" and summary
    assert client.generate.call_count == 1 and log.call_count == 1
    assert log.call_args.kwargs["quality_signal"]["output_len_chars"] == len(_envelope(BODY))


def test_social_write_keeps_its_own_ceiling_and_no_block():
    client = MagicMock()
    client.generate.side_effect = [_resp("A short post about the shore path.\n===SUMMARY===\nShore path.")]
    with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync"):
        t9.write_content(content_seed=SEED, goal=GOAL, channel_style=get_channel_style("linkedin"),
                         brand_audience={}, angle=ANGLE, cta="Message us.")
    request = client.generate.call_args.args[0]
    assert request.max_tokens == tb.T9_WRITE_DEFAULT_MAX_TOKENS and BLOCK not in request.user_prompt
