"""
services.acp_angle_gate.prompts — builds the system/user prompt for T8's one real LLM call
(generate.py::generate_angles()).

Workflow steps 4-6 (docs/claude_tasks/AA-449-00-step0-t8-angle-gate-investigation.md's saved
build task): apply the goal's formula -> generate exactly 3 angles, each shaped by that formula
applied a different way to the same title/content seed -> recommend the strongest one. All 3
folded into ONE LLM call (not 3 separate calls) — same "one call returns the full set" shape
generate_node() in services/content_generation/graph.py already uses.
"""
from __future__ import annotations

import os

from services.acp_angle_gate.brand_audience import BrandAudience
from services.acp_angle_gate.channel_style import ChannelStyle
from services.acp_shared.brand_pack import render_brand_block
from services.acp_angle_gate.goals import Goal
from services.acp_shared.dfs_relevance import SearchDemandSignal
from services.acp_shared.piece_history import PriorPiece

# AA-469 Việc 4 (flow-order fix, this session) — channel moved OUT of angle generation entirely.
# Confirmed with Nghiệp: the real order is atom+DFS/PAA+brand -> Goal -> 3 angles -> pick 1 ->
# THEN pick Channel -> T9 write. Angle generation used to take a `channel_style` param and
# include a CHANNEL block here; both are gone. This is safe, not a content-quality regression:
# services/acp_content_writing/prompts.py::build_user_prompt() (T9's OWN prompt, unchanged by
# this session) already includes the FULL channel_style block (structure/style/avoid) at write
# time, independently of whatever this module produces — channel-fit was always re-applied in
# full at write time regardless, this only removes an early, now-redundant channel-conditioning
# pass over the angle's own `best_final_style` field.

SYSTEM_PROMPT = """You are a senior content strategist for the travel brand described in the \
BRAND block below. Your job in this step is ONLY to generate 3 distinct content angles for one \
piece of content for the channel given below. You are NOT writing the final content itself (a \
writer does that later, from the angle a human picks).

An "angle" here means: one specific strategic approach to the SAME content seed/topic, shaped by \
a chosen goal and its marketing formula. The 3 angles you generate must be genuinely different \
approaches, not 3 small wording variations of the same idea.

Each angle needs exactly 4 required fields, plus a 5th when questions are supplied below:
- name: a short, specific label for this angle (not generic, e.g. not just "Angle 1")
- why_it_works: 1-2 sentences, a concrete business reason this angle fits the goal/audience, \
never vague ("this is engaging")
- formula_fit: name the marketing formula step-shape this angle follows (from the goal's own \
formula, given below) and briefly how this specific angle realizes it
- best_final_style: a brief for the writer: the shape of the piece, what to emphasise, and WHICH \
details from the CONTENT SEED to lead with. It never sets a voice or mood (the writer takes its \
voice only from the BRAND block), never writes or prescribes the first sentence (no "Open with …"), \
and never names an audience other than the brand's own Audience line. The writer follows it, so \
it must not name any object, scene, sound, smell, number, activity, form of access or promise that \
is not in the CONTENT SEED (no "the guide unrolling a map", no "the smell of incense" unless the \
seed says so).
- answers: a list of the questions from "Travelers also ask" below that THIS SPECIFIC \
angle would genuinely answer if written, quoted EXACTLY as given, word for word. Claim only \
what the angle really covers: an angle answering 2 questions well beats one listing 6 it barely \
touches. Empty list if none of the given questions fit this angle, or if no questions were given.

If the CONTENT SEED is a ROUTE with ANCHOR CANDIDATES and THIN MOMENTS, every angle leads with \
an anchor candidate. A thin moment has almost no source facts: it may only be named in passing \
and is never the hook, the title (name) or the climax of an angle.

Never invent facts, statistics, testimonials, scene detail or claims not present in the content \
seed given to you. If the content seed lacks a concrete detail a strong angle would want, build \
the angle on what the seed does have rather than fabricating one.

Every angle must fit the BRAND: follow every rule in its style guide (including each "do not"; \
e.g. a brand that says not to make a trip sound like a school trip gets no classroom framing), \
never use its forbidden words, and never promise exclusivity, rare access or transformation the \
seed does not state. Never name any brand other than the one in the BRAND block, and never \
mention the platform this material came from.

If a SEARCH DEMAND SIGNAL block is given below, treat it as real traveler search behavior you \
may use to judge which angle is most relevant or timely, never as a source of new facts about \
the trip itself. A "People also ask" question is evidence people search for that topic, not a \
claim about this specific trip; do not answer it as though it were a verified fact from the \
content seed. If it doesn't naturally fit the content seed, ignore it rather than forcing an \
angle around it.

If a PRIOR PIECES block is given below, it lists real content already written for this exact \
content seed on a previous occasion (angle used, channel, and a short summary). Your 3 new \
angles must be genuinely different from every angle listed there; do not regenerate a close \
variant of one already used. This is about avoiding repetition, not about matching or referencing \
their style.

Return ONLY valid JSON, no markdown fences, no commentary, in exactly this shape:
{
  "angles": [
    {"name": "...", "why_it_works": "...", "formula_fit": "...", "best_final_style": "...", "answers": []},
    {"name": "...", "why_it_works": "...", "formula_fit": "...", "best_final_style": "...", "answers": []},
    {"name": "...", "why_it_works": "...", "formula_fit": "...", "best_final_style": "...", "answers": []}
  ],
  "recommended_index": 0,
  "recommendation_reason": "short reason this one is the strongest of the 3"
}
recommended_index must be 0, 1, or 2. The caller re-ranks the 3 angles by measurable criteria \
(real PAA-answer count, checked against your "answers" claims, and avoid-list hits) and may \
override recommended_index."""


# ---------------------------------------------------------------- AA-790 round 8 (S255): the brief contract
# Rounds 4-7 tuned the T9 writer; round 7 (a generic BRAND FOCUS block in the T9 prompt) failed the blind panel on
# brand voice and reader value and was reverted (CIS #1087). Round 8 changes what T8 hands the writer instead: a short
# brief built for THIS brand's reader, tied to seed facts, with no fact inventory, no thesis to prove and no ask the
# seed cannot meet (diagnosis: docs/implementation-notes/AA-790-writer-ai-tells.md, "Round 8"). Behind a mode switch
# so the paid A/B can run both briefs on one deployed image: api/worker env `T8_ANGLE_BRIEF_MODE` = `r8` turns it
# on; anything else (unset by default) keeps SYSTEM_PROMPT byte for byte. The user prompt is the same in both modes.
BRIEF_MODE_ENV = "T8_ANGLE_BRIEF_MODE"
BRIEF_MODE_OFF = "off"
BRIEF_MODE_R8 = "r8"
BRIEF_MODES = (BRIEF_MODE_OFF, BRIEF_MODE_R8)

# The four labelled parts of a round-8 brief, in order (generate.r8_brief_problems checks them).
R8_BRIEF_LABELS = ("Reader:", "Brand:", "Lead with:", "Shape:")
R8_BRIEF_TARGET_WORDS = 90      # what the prompt asks for
R8_BRIEF_MAX_WORDS = 120        # what the contract check allows before one retry (LLMs overshoot a word target)
R8_BRIEF_MAX_FIGURES = 5        # the prompt says at most three facts, dates or figures; day numbers make it 5

# The exact round-6 bullet SYSTEM_PROMPT carries (a test pins that it is there, so the replace below never misses).
_BRIEF_SPEC_OFF = """- best_final_style: a brief for the writer: the shape of the piece, what to emphasise, and WHICH \
details from the CONTENT SEED to lead with. It never sets a voice or mood (the writer takes its \
voice only from the BRAND block), never writes or prescribes the first sentence (no "Open with …"), \
and never names an audience other than the brand's own Audience line. The writer follows it, so \
it must not name any object, scene, sound, smell, number, activity, form of access or promise that \
is not in the CONTENT SEED (no "the guide unrolling a map", no "the smell of incense" unless the \
seed says so).
"""

_BRIEF_SPEC_R8 = f"""- best_final_style: a short brief for the writer, at most {R8_BRIEF_TARGET_WORDS} words, in four \
labelled parts in this order, each one or two plain sentences:
  "Reader: ..." what the reader does, sees, eats or where they stay in this piece, in the CONTENT \
SEED's own terms (a ride, a walk, a swim, a meal, a night, a visit), so the piece follows the traveller \
through the trip and never describes the place like a reference work.
  "Brand: ..." one or two points from the brand's own style guide or Audience mindset that this angle \
carries, each tied to the CONTENT SEED fact that carries it (for example "pace: the seed says the walk \
ends at the camp before lunch"). Name only points the seed can carry; when it carries none, write \
"Brand: voice only".
  "Lead with: ..." at most three details from the CONTENT SEED.
  "Shape: ..." the structure in one sentence (length, sections, where the call to action sits); it may \
name the sections but never assigns facts to each one.
The brief is not an outline of facts: it never lists more than three facts, dates or figures and never \
asks for a survey of every stop. It never gives the piece a thesis to prove or a verdict to reach (what \
a place "reveals", "means" or "represents", an organising idea, a "quiet revelation"), because the \
writer turns that into a verdict line. It never sets a voice or mood (the writer takes its voice only \
from the BRAND block), never writes or prescribes the first sentence (no "Open with …"), and never names \
an audience other than the brand's own Audience line. The writer follows it, so it must not ask for \
anything the CONTENT SEED does not give: no object, scene, sound, smell, number, activity, form of \
access or promise (no "the guide unrolling a map", no "the smell of incense" unless the seed says so), \
and no reassurance, practical note, comfort, pace, safety, logistics or "what visitors see" line unless \
the seed states it. When the brand's style guide asks for one of those and the seed has nothing for it, \
leave it out of the brief.
"""

# Inserted right before the "Every angle must fit the BRAND" paragraph.
_BRAND_ANCHOR = "Every angle must fit the BRAND: "
_BRAND_FIRST_R8 = """Build every angle for THIS brand's reader first and for search demand second. Read the BRAND \
block's Audience, Audience mindset, Core idea and style guide, and build each angle on the moments of \
the CONTENT SEED this reader comes for: two different brands given the same seed should get different \
angles. why_it_works names why this brand's audience would read the piece; search demand may support \
that reason but is never the whole of it.

"""

SYSTEM_PROMPT_R8 = SYSTEM_PROMPT.replace(_BRIEF_SPEC_OFF, _BRIEF_SPEC_R8).replace(
    _BRAND_ANCHOR, _BRAND_FIRST_R8 + _BRAND_ANCHOR, 1)


def brief_mode(value: str | None = None) -> str:
    """The brief mode: `value` when given (the A/B harness), else the env switch. Only 'r8' turns round 8 on; every
    other value, unset included, is 'off' (the round-6 prompt)."""
    raw = os.environ.get(BRIEF_MODE_ENV, "") if value is None else value
    return BRIEF_MODE_R8 if (raw or "").strip().lower() == BRIEF_MODE_R8 else BRIEF_MODE_OFF


def system_prompt(mode: str | None = None) -> str:
    """SYSTEM_PROMPT for 'off', SYSTEM_PROMPT_R8 for 'r8' (`mode` None reads the env switch)."""
    return SYSTEM_PROMPT_R8 if brief_mode(mode) == BRIEF_MODE_R8 else SYSTEM_PROMPT


def build_user_prompt(
    *, content_seed: str, goal: Goal, brand_audience: BrandAudience,
    destination: str | None, trip_name: str | None,
    search_demand: SearchDemandSignal | None = None,
    piece_history: list[PriorPiece] | None = None,
    channel_style: ChannelStyle | None = None,
    revision_feedback: list[str] | None = None,
    is_route: bool = False,
) -> str:
    """`is_route` (S234): `content_seed` is the Route digest (route_digest.render_digest — every
    moment in day order with its fact count, ANCHOR CANDIDATES and THIN MOMENTS), not one atom."""
    head = ("CONTENT SEED (a Route: the moments of the trip this content covers; build every angle on "
            "them):" if is_route else
            "CONTENT SEED (from a curated atom: the concrete detail this content must be about):")
    parts = [f"{head}\n{content_seed}"]
    if trip_name:
        parts.append(f"TRIP: {trip_name}" + (f" ({destination})" if destination else ""))
    parts.append(
        f"GOAL: {goal['name']}\n"
        f"Goal description: {goal['description']}\n"
        f"Formula logic: {goal['logic']}\n"
        f"Marketing term: {goal['marketing_term']}"
    )
    parts.append(render_brand_block(brand_audience))
    # AA-771 (S233) — the channel is always known when T8 runs (pick_subject sets it), so the
    # angle is shaped for it; AA-469 removed this block when the channel came after the angle.
    if channel_style:
        parts.append(
            f"CHANNEL: {channel_style['display_name']}\n"
            f"Channel use case: {channel_style['use_when']}\n"
            f"Channel structure: {channel_style['structure']}\n"
            f"Channel avoid: {channel_style['avoid']}"
        )
    # AA-771 (S234, C7) — the voice is the brand's; an angle brief that sets another is cut in code
    # (channel_spec.sanitize_best_final_style).
    if brand_audience.get("voice"):
        parts.append("VOICE (from the BRAND block: the writer takes it from there; an angle never sets another "
                     "voice or mood): " + ", ".join(brand_audience["voice"]))
    # AA-469 Việc 4 — optional 5th block (was 6th, back when CHANNEL was still a block here — see
    # this module's own header comment on the flow-order fix that removed it). Omitted entirely
    # (not "SEARCH DEMAND: none") when there's no seo_context row for this tour, or when the row
    # has neither PAA questions nor related keywords — an empty/absent block would just be prompt
    # noise, and the prompt stays byte-identical for every request with no real signal to add.
    if search_demand and (search_demand.people_also_ask or search_demand.related_keywords):
        lines = [f"SEARCH DEMAND SIGNAL (real traveler search behavior for this destination; "
                 f"see system prompt for how to use this):\nRelevance: {search_demand.relevance}"]
        if search_demand.people_also_ask:
            # AA-512 — the FULL pool, not capped at 5 like before: an angle's "answers" claim is
            # now server-verified against exactly this list (services/acp_angle_gate/ranking.py),
            # so truncating it here would make a real answered question unmatchable.
            lines.append("Travelers also ask: " + "; ".join(search_demand.people_also_ask))
        if search_demand.related_keywords:
            lines.append("Related search terms: " + ", ".join(search_demand.related_keywords[:8]))
        parts.append("\n".join(lines))
    # AA-498 (Decision 4) — omitted entirely when empty, same "no signal -> no prompt noise"
    # rule search_demand's own block above follows.
    if piece_history:
        lines = ["PRIOR PIECES WRITTEN FOR THIS EXACT CONTENT (real history; find a genuinely "
                  "different angle than every one listed here):"]
        for p in piece_history:
            lines.append(
                f"- [{p.channel or 'channel unknown'}] "
                f"Angle used: {p.angle_name or '(unknown)'}; summary: {p.summary}"
            )
        parts.append("\n".join(lines))
    if revision_feedback:
        parts.append("YOUR PREVIOUS ANGLES BROKE THESE RULES. Write 3 new angles that do not:\n- "
                     + "\n- ".join(revision_feedback))
    return "\n\n".join(parts)


__all__ = ["BRIEF_MODE_ENV", "BRIEF_MODE_OFF", "BRIEF_MODE_R8", "BRIEF_MODES", "R8_BRIEF_LABELS",
           "R8_BRIEF_MAX_FIGURES", "R8_BRIEF_MAX_WORDS", "R8_BRIEF_TARGET_WORDS", "SYSTEM_PROMPT",
           "SYSTEM_PROMPT_R8", "brief_mode", "build_user_prompt", "system_prompt"]
