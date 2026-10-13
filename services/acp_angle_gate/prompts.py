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
piece of content for the channel given below — you are NOT writing the final content itself (a \
writer does that later, from the angle a human picks).

An "angle" here means: one specific strategic approach to the SAME content seed/topic, shaped by \
a chosen goal and its marketing formula. The 3 angles you generate must be genuinely different \
approaches — not 3 small wording variations of the same idea.

Each angle needs exactly 4 required fields, plus a 5th when questions are supplied below:
- name: a short, specific label for this angle (not generic, e.g. not just "Angle 1")
- why_it_works: 1-2 sentences, a concrete business reason this angle fits the goal/audience — \
never vague ("this is engaging")
- formula_fit: name the marketing formula step-shape this angle follows (from the goal's own \
formula, given below) and briefly how this specific angle realizes it
- best_final_style: a brief for the writer — the shape of the piece, what to emphasise, and WHICH \
details from the CONTENT SEED to lead with. It never sets a voice or mood (the writer takes its \
voice only from the BRAND block), never writes or prescribes the first sentence (no "Open with …"), \
and never names an audience other than the brand's own Audience line. The writer follows it, so \
it must not name any object, scene, sound, smell, number, activity, form of access or promise that \
is not in the CONTENT SEED (no "the guide unrolling a map", no "the smell of incense" unless the \
seed says so).
- answers: a list of the questions from "Travelers also ask" below that THIS SPECIFIC \
angle would genuinely answer if written — quoted EXACTLY as given, word for word. Claim only \
what the angle really covers: an angle answering 2 questions well beats one listing 6 it barely \
touches. Empty list if none of the given questions fit this angle, or if no questions were given.

If the CONTENT SEED is a ROUTE with ANCHOR CANDIDATES and THIN MOMENTS, every angle leads with \
an anchor candidate. A thin moment has almost no source facts: it may appear in one sentence at \
most and is never the hook, the title (name) or the climax of an angle.

Never invent facts, statistics, testimonials, scene detail or claims not present in the content \
seed given to you. If the content seed lacks a concrete detail a strong angle would want, build \
the angle on what the seed does have rather than fabricating one.

Every angle must fit the BRAND: follow every rule in its style guide (including each "do not" — \
e.g. a brand that says not to make a trip sound like a school trip gets no classroom framing), \
never use its forbidden words, and never promise exclusivity, rare access or transformation the \
seed does not state. Never name any brand other than the one in the BRAND block, and never \
mention the platform this material came from.

If a SEARCH DEMAND SIGNAL block is given below, treat it as real traveler search behavior you \
may use to judge which angle is most relevant or timely — never as a source of new facts about \
the trip itself. A "People also ask" question is evidence people search for that topic, not a \
claim about this specific trip; do not answer it as though it were a verified fact from the \
content seed. If it doesn't naturally fit the content seed, ignore it rather than forcing an \
angle around it.

If a PRIOR PIECES block is given below, it lists real content already written for this exact \
content seed on a previous occasion (angle used, channel, and a short summary). Your 3 new \
angles must be genuinely different from every angle listed there — do not regenerate a close \
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
    head = ("CONTENT SEED (a Route — the moments of the trip this content covers; build every angle on "
            "them):" if is_route else
            "CONTENT SEED (from a curated atom — the concrete detail this content must be about):")
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
            f"Channel — avoid: {channel_style['avoid']}"
        )
    # AA-771 (S234, C7) — the voice is the brand's; an angle brief that sets another is cut in code
    # (channel_spec.sanitize_best_final_style).
    if brand_audience.get("voice"):
        parts.append("VOICE (from the BRAND block — the writer takes it from there; an angle never sets another "
                     "voice or mood): " + ", ".join(brand_audience["voice"]))
    # AA-469 Việc 4 — optional 5th block (was 6th, back when CHANNEL was still a block here — see
    # this module's own header comment on the flow-order fix that removed it). Omitted entirely
    # (not "SEARCH DEMAND: none") when there's no seo_context row for this tour, or when the row
    # has neither PAA questions nor related keywords — an empty/absent block would just be prompt
    # noise, and the prompt stays byte-identical for every request with no real signal to add.
    if search_demand and (search_demand.people_also_ask or search_demand.related_keywords):
        lines = [f"SEARCH DEMAND SIGNAL (real traveler search behavior for this destination — "
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
        lines = ["PRIOR PIECES WRITTEN FOR THIS EXACT CONTENT (real history — find a genuinely "
                  "different angle than every one listed here):"]
        for p in piece_history:
            lines.append(
                f"- [{p.channel or 'channel unknown'}] "
                f"Angle used: {p.angle_name or '(unknown)'} — {p.summary}"
            )
        parts.append("\n".join(lines))
    if revision_feedback:
        parts.append("YOUR PREVIOUS ANGLES BROKE THESE RULES — write 3 new angles that do not:\n- "
                     + "\n- ".join(revision_feedback))
    return "\n\n".join(parts)


__all__ = ["SYSTEM_PROMPT", "build_user_prompt"]
