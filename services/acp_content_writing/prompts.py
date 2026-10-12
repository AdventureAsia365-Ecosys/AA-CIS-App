"""
services.acp_content_writing.prompts — T9 write-step (SKILL_v2.md workflow step 9) prompt
construction. Referenced (not reused, ADR §0.5) against services/acp_s4_social/writer.py's
ContentBrief-assembly shape — written fresh here against T8's real inputs (angle_gate_request/
angle_gate_option, channel_style.py, goals.py, brand_audience.py), not the old ContentBrief.

No hardcoded per-channel word-count numbers (STEP0 §Open Question #3 / build task's own explicit
instruction — neither SKILL_v2.md's own Channel Rules nor T8's real Bảng-2 source,
channel_style.py, states one) — length guidance is qualitative, from channel_style.py's
`structure`/`style` fields, same as the LLM already gets for angle generation (generate.py).

AA-452: `channel_style.py`'s own `blog` entry describes an N7-shaped structure ("Hook→context→
structured H2 sections→FAQ (if TOFU)→CTA") that this module never actually asked the model to
produce in markup terms until now — investigation confirmed T9's blog channel genuinely can
carry N7's F3 (structural variance)/F5 (atom density)/F7 (FAQ dedup) gates, not just F1/F2/F4/F6/
F8/F9. `_BLOG_FORMAT_INSTRUCTIONS` below is appended ONLY when `channel_style['channel'] ==
'blog'` — the other 7 channels' prompts are byte-for-byte unchanged. Two requirements, both new
markup the writer wasn't asked for before:
  1. Real markdown `## ` H2 headers per section (+ `## FAQ` with `**Q: ...**`/`A: ...` pairs if
     the piece includes one) — so `quality_gates.gate_structural_variance()`/`gate_faq_dedup()`
     (ported from `acp_produce/gates.py`, same regexes) have real structure to check.
  2. A `[R:{atom_id}]` tag after every sentence built from a seed fact — same tag shape N7 uses,
     but T9 has exactly one atom per piece so there's only ever one id (no closed-world check
     needed, same simplification `gate_grounding()` already made in AA-450). This is INTERNAL
     provenance markup only, for `gate_atom_density()`/`gate_grounding()` to see — it is never
     shown to the tenant: `quality_gates.strip_citation_tags()` removes every tag from
     `content_text` (and from every gate_ledger/repair_log violation string) before
     `service.write_and_check()` ever persists or returns a piece, for every channel, blog
     included. Non-blog channels never receive this instruction, so they never produce a tag to
     strip in the first place — `strip_citation_tags()` runs on their output too (a deliberate,
     no-cost safety net, not dead code — see that function's own docstring).
"""
from __future__ import annotations

from services.acp_angle_gate.brand_audience import BrandAudience
from services.acp_shared.brand_pack import render_brand_block
from services.acp_angle_gate.channel_style import ChannelStyle
from services.acp_angle_gate.goals import Goal

_BLOG_FORMAT_INSTRUCTIONS = """

BLOG-SPECIFIC FORMAT REQUIREMENTS (this channel only — required markup, not a style suggestion):
- Structure the body with real markdown H2 headers: a line starting with exactly "## " for every
  major section (e.g. "## Why Southern Laos"). Do not skip this.
- Put the call to action in the last body paragraph, BEFORE any FAQ — never after the FAQ.
- If, and only if, the piece includes a FAQ section, put it LAST (after the call-to-action
  paragraph), headed by a line that is exactly "## FAQ", followed by one or more Q/A pairs in
  this exact format and nothing else — each answer must add something the body did not say:
  **Q: <question>**
  A: <answer>
- Vary the rhythm: include at least one paragraph that is a single sentence, use at most one
  bulleted list, and give the richest moment (or section) clearly more depth — at least half as
  long again as any other section.
- Immediately after every sentence that uses a specific fact, number, or detail drawn from the
  content seed, append the tag [R:{atom_id}] (this literal id, no spaces inside the brackets). A
  sentence with no such detail needs no tag. This tag is internal provenance markup that will be
  removed before the reader ever sees this piece — write the surrounding sentence exactly as if
  the tag weren't there; it must never change your wording or read as part of the sentence.
- If a sentence instead uses a detail from the FACTS section below (not the itinerary content
  above) — price, season, visa, transfer time, or similar — tag it [F:<fact id>] using the id
  shown for that fact there, the exact same way, instead of [R:{atom_id}]."""

# AA-513 — route-aware variant, used INSTEAD of _BLOG_FORMAT_INSTRUCTIONS only when
# `route_segments` has more than one moment (a Route/Blog pick walking >=2 Segments). The single-
# moment instruction above stays byte-identical and is still used for every other case (1 moment,
# or no route_segments at all) — see build_user_prompt()'s own docstring for why this can't just
# always be the route-aware version (a single literal id needs no "which moment" disambiguation).
_BLOG_FORMAT_INSTRUCTIONS_ROUTE = """

BLOG-SPECIFIC FORMAT REQUIREMENTS (this channel only — required markup, not a style suggestion):
- Structure the body with real markdown H2 headers: a line starting with exactly "## " for every
  major section (e.g. "## Why Southern Laos"). Do not skip this.
- Put the call to action in the last body paragraph, BEFORE any FAQ — never after the FAQ.
- If, and only if, the piece includes a FAQ section, put it LAST (after the call-to-action
  paragraph), headed by a line that is exactly "## FAQ", followed by one or more Q/A pairs in
  this exact format and nothing else — each answer must add something the body did not say:
  **Q: <question>**
  A: <answer>
- Vary the rhythm: include at least one paragraph that is a single sentence, use at most one
  bulleted list, and give the richest moment (or section) clearly more depth — at least half as
  long again as any other section.
- The CONTENT SEED below is organized into several moments along the route, each preceded by its
  own citation id (shown as "[Moment id=<id>]"). Immediately after every sentence that uses a
  specific fact, number, or detail drawn from one of these moments, append the tag [R:<id>] using
  THAT MOMENT'S OWN id exactly as shown — never a different moment's id, and never invent an id.
  A sentence with no such detail needs no tag. This tag is internal provenance markup that will
  be removed before the reader ever sees this piece — write the surrounding sentence exactly as
  if the tag weren't there; it must never change your wording or read as part of the sentence.
- If a sentence instead uses a detail from the FACTS section below (not one of the moments above)
  — price, season, visa, transfer time, or similar — tag it [F:<fact id>] using the id shown for
  that fact there, the exact same way, instead of a moment's [R:<id>]."""

# AA-514 — blog channel's writer-output contract becomes a JSON envelope (full port of the
# origin's structured Piece.seo_title/meta_description/slug fields, Nghiệp-confirmed real
# architecture decision — see docs/claude_audit/AA-514-step0-investigation.md §4). Appended
# AFTER whichever _BLOG_FORMAT_INSTRUCTIONS(_ROUTE) variant already ran — those still describe
# what goes INSIDE "body"; this describes the envelope around it. Non-blog channels never see
# this block, so their output contract (plain text) is completely unchanged.
#
# AA-498 (Decision 4) — "summary" key added to this same envelope, see SYSTEM_PROMPT's own
# _SUMMARY_INSTRUCTIONS for why this needs no second LLM call.
_SEO_ENVELOPE_INSTRUCTIONS = """

OUTPUT FORMAT FOR THIS CHANNEL ONLY — return ONLY valid JSON, no markdown fence, no commentary,
in exactly this shape:
{{
  "seo_title": "<the SEO search-result headline, 45-60 characters, one natural headline containing
the target keyword and the place the piece is about>",
  "meta_description": "<the search-result snippet, 140-155 characters, ONE complete sentence ending
in '.', '!' or '?', containing the target keyword>",
  "slug": "<lowercase-kebab-case URL slug, <=60 characters, e.g. 'wat-sisaket-vientiane'>",
  "body": "<the full piece itself, exactly as specified above — every H2/FAQ/citation-tag
requirement above applies to THIS field, not to seo_title/meta_description/slug>",
  "summary": "<1-2 plain-English sentences summarizing what this piece covers and the angle it
takes — internal use only, never shown to the reader, see SYSTEM_PROMPT for how this is used>"
}}
{keyword_line}"""

# AA-498 (Decision 4, migration 124's own header: "generated by the same LLM call that writes
# the piece — near-zero marginal cost") — universal across all 8 channels, appended to
# SYSTEM_PROMPT itself (not per-channel) so both output contracts stay covered by one rule: the
# blog channel already returns JSON (gets a "summary" key added to that envelope, see
# _SEO_ENVELOPE_INSTRUCTIONS above); the other 7 channels return plain text, so they get an
# out-of-band trailing marker line instead — parsed by generate.py::_extract_summary(), never
# persisted into content_text. A missing/unparseable summary is a soft failure (content_summary
# stays NULL) — the piece itself must never fail or hold over a missing summary, matching every
# other optional signal in this pipeline (seo_meta fields, DFS/PAA) failing soft.
_SUMMARY_INSTRUCTIONS = """

ALSO: after the content itself, on its own new line, write exactly ===SUMMARY=== followed by
1-2 plain-English sentences summarizing what this piece covers and the angle it takes. This is
for internal reference only (helping a future rewrite of the same subject pick a genuinely
different angle) — it is never shown to the reader, so it does not need to read well as prose,
just be accurate. Skip this entirely for the one channel below whose output format is a JSON
envelope — put the summary in that envelope's own "summary" field instead, not as a trailing
line outside the JSON."""


def _keyword_line(keyword: str | None) -> str:
    if not keyword:
        return (
            "No target keyword was supplied for this piece — write seo_title/meta_description "
            "naturally, without forcing one in."
        )
    return f"TARGET SEO KEYWORD (must appear in both seo_title and meta_description): {keyword}"


# AA-768 (S233) — social channels publish as plain text: markdown showed up as literal ** and
# ## in 3/29 S232 LinkedIn posts. generate.py also strips any that slips through.
PLAIN_TEXT_CHANNELS = {"linkedin", "facebook", "instagram", "tiktok", "ads"}
_PLAIN_TEXT_INSTRUCTIONS = """

FORMAT FOR THIS CHANNEL: plain text only — no markdown (no **bold**, no # headings, no
[links](...)). Short paragraphs separated by a blank line. Hashtags, if the channel uses them,
go on the last line."""


SYSTEM_PROMPT = """You are a strategy-led English content writer for the travel brand described in the BRAND
block below — write in its voice, for its audience, by its style guide.

Write ONE finished piece of content for the exact channel, goal, and angle given below — not a
draft with placeholders, not an outline, the actual final copy a person could post as-is.

Rules:
- Combine the BRAND block, the channel's structure and style, the selected goal's writing method,
  and the chosen angle every time. Do not write from channel style alone. Where the brand's
  style guide and the channel or angle disagree, the brand's style guide wins.
- Use concrete, verifiable details from the content seed given. Never invent a fact, number,
  measurement, or claim that is not in the content seed.
- SOURCE FIDELITY (same policy as the tour writer):
  ALLOWED: short, widely known background about a place the content seed names — what it is,
  its history and era, who founded it, its religious or cultural role, whether it is still in
  use — written in words, without numbers unless the seed has them.
  FORBIDDEN: anything that promises the reader something about THIS trip that the content seed
  does not state — activities, food, services, guides, transport or travel times, clock-times,
  views or wildlife sightings, accommodation features, inclusions.
  FORBIDDEN: invented scene detail — what the place looks, sounds or smells like, the state of
  its paths, steps or roads, how it is reached, how crowded it is, what "most visitors" do —
  unless the content seed says it. Make the piece vivid with the seed's own details and the
  place's real background instead.
  FORBIDDEN: contrast claims whose other half the seed does not state — "not by X but by Y",
  "unlike most …", "older than …", "few visitors …" — say what the seed says, without the
  invented comparison.
  LOCATION: name only the location the seed gives (its "Location" line); never place it in
  another town, region or country.
  NUMBERS: write a number (height, distance, year, decade, century, age, day count, duration,
  price, fee, processing time, group size) ONLY if that exact number appears in the content
  seed, its FACTS section, the TRIP name or the AUDIENCE line. Otherwise say it without the
  number ("high on a cliff face", not "900 metres up"; "for decades", not "since the 1970s").
- Every sentence must still be true of the content seed when read by an editor who has only the
  content seed in front of them.
- Include the given call to action, worked naturally into the piece — not pasted on as a bare
  final sentence unless the channel's own style calls for that.
- Avoid generic AI-style writing: no vague benefit stacks, no fake urgency, no unsupported
  superlatives, no manipulative language, no rhetorical-question padding, no clichés ("hidden
  gem", "bucket list", "must-visit", "breathtaking", "unforgettable", "nestled", "tapestry",
  "embark on", "immerse yourself", "look no further", "in today's fast-paced world",
  "game-changing", "revolutionary", "unlock your potential").
- Sound human, specific, and lightly persuasive — not corporate, not hype-driven.
- Return ONLY the final content itself — no explanation, no preamble, no markdown code fence,
  no restating the brief back.""" + _SUMMARY_INSTRUCTIONS


def build_user_prompt(
    *, content_seed: str, goal: Goal, channel_style: ChannelStyle,
    brand_audience: BrandAudience, angle: dict, cta: str,
    destination: str | None = None, trip_name: str | None = None,
    revision_feedback: list[str] | None = None, atom_id: str | None = None,
    route_segments: list[tuple[str, str]] | None = None, keyword: str | None = None,
    facts_text: str | None = None,
) -> str:
    """`angle` is the chosen `angle_gate_option` row (name/why_it_works/formula_fit/
    best_final_style). `revision_feedback` (AA-450 Phase 1's confirmed retry shape — specific,
    per-gate violation strings, not a generic "try again") is appended only on attempt 2.

    `atom_id` (AA-452, keyword-only, defaults to `None` so every pre-AA-452 caller/test is
    unaffected): only used when `channel_style['channel'] == 'blog'`, to fill in
    `_BLOG_FORMAT_INSTRUCTIONS`' citation tag. `None`/empty falls back to the literal id "atom"
    rather than emitting a malformed `[R:]` tag — real callers (service.py) always have the
    real atom_id, this fallback only guards a caller that forgets to pass one.

    `route_segments` (AA-513, keyword-only, defaults to `None` so every pre-AA-513 caller/test is
    unaffected): a Route/Blog pick's own `[(atom_id, text), ...]` in day order (`services/
    acp_content_writing/service.py::_fetch_route_segments()`). When this has MORE THAN ONE
    moment, the CONTENT SEED section below is rendered as labeled moments instead of the flat
    `content_seed` string, and `_BLOG_FORMAT_INSTRUCTIONS_ROUTE` (not the single-moment variant)
    tells the model to tag a fact with its OWN moment's id. A single-element or empty
    `route_segments` behaves exactly like `None` — one moment needs no "which one"
    disambiguation, so the plain `content_seed`/single-id instruction is used, byte-identical to
    every non-Route caller.

    `facts_text` (AA-529, keyword-only, defaults to `None` so every pre-AA-529 caller/test is
    unaffected): a pre-formatted `[Fact id=<id>]`-labeled block from
    `services/acp_content_writing/facts.py::format_facts_block()` — platform-scope Facts Entries
    plus the writing tenant's own tenant-scope ones. Appended AFTER whichever CONTENT SEED text
    was already chosen above (flat `content_seed`, or the route-aware labeled-moments render) —
    this covers BOTH branches with one change, since the route-aware branch otherwise ignores the
    plain `content_seed` argument entirely. `None`/empty is a no-op (no FACTS section at all),
    the normal case for a tenant/trip with no Facts Entry of either scope written yet."""
    # AA-771 (S233) — the full brand pack (render_brand_block) replaces the AUDIENCE / MINDSET /
    # BRAND NAME lines; the invented "discerning travellers" fallback audience is gone.

    # AA-513 — a real Route walk (>1 moment): render the CONTENT SEED as labeled moments instead
    # of the flat string, so the model can be told which moment each fact came from. Exactly 1
    # moment (or none) is treated the same as no route_segments at all — see this function's own
    # docstring for why a single moment needs no disambiguation. Gated on channel=='blog' too —
    # route_segments is only ever populated for a Route/Blog pick (`grain: "route"` is blog-only,
    # services/acp_shared/slate.py::CHANNEL_BARS — every other channel is `grain: "segment"`), so
    # this mirrors that real domain invariant rather than assuming a caller respects it.
    is_route_aware = (
        channel_style["channel"] == "blog" and bool(route_segments) and len(route_segments) > 1
    )
    seed_text = (
        "\n\n".join(f"[Moment id={mid}]\n{text}" for mid, text in route_segments)
        if is_route_aware else content_seed
    )
    # AA-529 — appended INSIDE seed_text itself (not as a separate `lines` entry) so it rides
    # along with the ONE "CONTENT SEED" line below regardless of branch, and never disturbs the
    # `lines.insert(-2, ...)` indices destination/trip_name rely on just below. Covers both
    # branches above (flat atom text, or the route-aware moments render) with one change, since
    # the route-aware branch otherwise never sees `content_seed` again once `is_route_aware` is
    # True.
    if facts_text:
        seed_text = (
            f"{seed_text}\n\nFACTS — additional citable facts NOT in the itinerary above "
            "(reference prices, travel seasons, visa/entry requirements, estimated transfer "
            "times between places, and similar claims a tour's own itinerary text wouldn't "
            "state). Use these freely as real, verifiable source material; for the blog channel, "
            "tag a sentence built from one of these with [F:<fact id>] using the id shown, the "
            "same way an atom/moment above is tagged [R:<id>] — never invent a fact id.\n"
            f"{facts_text}"
        )

    lines = [
        f"CHANNEL: {channel_style['display_name']}",
        f"CHANNEL USE CASE: {channel_style['use_when']}",
        f"CHANNEL STRUCTURE: {channel_style['structure']}",
        f"CHANNEL STYLE: {channel_style['style']}",
        f"CHANNEL — AVOID: {channel_style['avoid']}",
        "",
        f"GOAL: {goal['name']} — {goal['description']}",
        f"WRITING METHOD FOR THIS GOAL: {goal['logic']}",
        "",
        render_brand_block(brand_audience),
        "",
        f"SELECTED ANGLE: {angle['name']}",
        f"WHY THIS ANGLE WORKS: {angle['why_it_works']}",
        f"ANGLE'S BEST FINAL STYLE: {angle['best_final_style']}",
        "",
        f"CALL TO ACTION TO INCLUDE: {cta}",
        "",
        f"CONTENT SEED (the only source of facts, together with its own FACTS section if one is "
        f"present below — do not add facts beyond either):\n{seed_text}",
    ]
    if destination:
        lines.insert(-2, f"DESTINATION: {destination}")
    if trip_name:
        lines.insert(-2, f"TRIP: {trip_name}")
    if revision_feedback:
        lines.append(
            "\nPREVIOUS ATTEMPT FAILED QUALITY REVIEW — fix EXACTLY these issues, preserve "
            "everything else that was already working:\n- " + "\n- ".join(revision_feedback)
            # AA-768 — the pilot's rewrites swapped one unsupported number for another; say how
            # a grounding/fact-check failure is fixed, not just that it failed.
            + "\nFor a sentence flagged as stating a number or claim the content seed does not "
            "contain: remove that number or claim (rewrite the sentence without it). Never "
            "replace it with a different number or claim that is also missing from the content "
            "seed, and do not add new numbers anywhere else in the piece."
        )
    prompt = "\n".join(lines)
    if channel_style["channel"] in PLAIN_TEXT_CHANNELS:
        prompt += _PLAIN_TEXT_INSTRUCTIONS
    if channel_style["channel"] == "blog":
        prompt += (
            _BLOG_FORMAT_INSTRUCTIONS_ROUTE if is_route_aware
            else _BLOG_FORMAT_INSTRUCTIONS.format(atom_id=atom_id or "atom")
        )
        # AA-514 — every blog response is the JSON envelope, revision or not.
        prompt += _SEO_ENVELOPE_INSTRUCTIONS.format(keyword_line=_keyword_line(keyword))
    return prompt


__all__ = ["SYSTEM_PROMPT", "build_user_prompt", "PLAIN_TEXT_CHANNELS"]
