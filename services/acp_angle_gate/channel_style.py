"""
services.acp_angle_gate.channel_style — the 7-channel style table (T8 workflow step 4/5: "Best
final style" is tra'd from here, per the `channel` already known from the request's input).

Verbatim from Bang 2, docs/claude_tasks/AA-449-00-step0-t8-angle-gate-investigation.md — STEP0
§1b confirmed this is `Channel Output Structures.xlsx` (found earlier, AA-439-07 §A3), not
SKILL_v2.md's own much shorter "Channel Rules" prose section.

Keys match services.acp_planning.models.Channel's Literal exactly (snake_case for the two
multi-word channels: "landing_page"). "email" here maps to Bang 2's "Email / Newsletter" row.

"blog" has NO row in Bang 2 at all (STEP0 §5 flagged this gap explicitly) — kept as a Channel
value for backward compatibility (T7 already had it), but its entry below is a NEW, self-authored
fallback (not from Bang 2 or any other source document), consistent with the existing
FRAMEWORK_TABLE's own "hub"/long-form treatment of blog elsewhere in services/acp_planning. If
Ms. Thu supplies a real blog row for this table later, replace this entry, don't extend it.
"""
from __future__ import annotations

from typing import TypedDict


class ChannelStyle(TypedDict):
    channel: str
    display_name: str
    use_when: str
    structure: str
    style: str
    avoid: str


# AA-771 (S233) — translated to English and made brand-neutral. The Bang 2 text was Vietnamese
# with Adventure Asia's own positioning baked in ("AA positioning", "audience AA", "premium không
# kiêu", "điểm khác biệt AA") and went verbatim into every tenant's English writer prompt; S232
# traced 25/29 LinkedIn posts with an invented "At <Brand>, we…" paragraph to it. Tone now comes
# from the tenant's brand pack. `avoid` items are plain comma-separated phrases so ranking.py's
# word-boundary match can find them in an angle (quoted/Vietnamese items never matched).
# S234 (AA-771) — the LinkedIn row lost "B2B and partner audiences" and "calm": every brand got the
# same calm, trade-facing register (Trail Pulse wrote to "active travel brands" 3/3). The
# register-specific use case / structure / avoid list and the numeric limits come from
# services/acp_shared/channel_spec.py::linkedin_spec(pack); this row is the brand-neutral base.
CHANNEL_STYLES: list[ChannelStyle] = [
    {
        "channel": "linkedin",
        "display_name": "LinkedIn",
        "use_when": "Thought leadership and considered positioning for the brand's own audience (its Audience line)",
        "structure": (
            "Hook insight → short paragraphs → one clear insight → at most one sentence on how the "
            "brand approaches it (only what its brand identity states) → light, reflective CTA"
        ),
        "style": "insight-led, specific, confident without arrogance",
        "avoid": (
            "many emojis, generic travel copy, hard sell, long itinerary lists, over-emotional "
            "storytelling, hidden gem, bucket list, paradise awaits, markdown formatting"
        ),
    },
    {
        "channel": "facebook",
        "display_name": "Facebook",
        "use_when": "Engagement, destination introductions, community trust, soft promotion",
        "structure": (
            "Warm, concrete opening → the feel of the place with concrete details from the source → "
            "a practical trip idea → why it suits this brand's audience → friendly CTA"
        ),
        "style": "human, warm, clear, travel-led, more relaxed than LinkedIn",
        "avoid": (
            "corporate tone, discover paradise, facts with no feeling, cheap wording, vague "
            "scenery language"
        ),
    },
    {
        "channel": "instagram",
        "display_name": "Instagram",
        "use_when": "Visual inspiration, destination mood, brand awareness, short engagement",
        "structure": (
            "Short sensory hook → easy-to-scan lines → 3-5 concrete details from the source → a "
            "light brand line → simple CTA"
        ),
        "style": "visual, precise, sensory, minimal, elegant",
        "avoid": "long paragraphs, vague captions, hashtag stuffing, generic adjectives, over-dramatic language",
    },
    {
        "channel": "tiktok",
        "display_name": "TikTok",
        "use_when": "Short attention, curiosity, education, reframing a destination",
        "structure": (
            "Sharp opening line → simple spoken setup → 3 quick points → curiosity without "
            "clickbait → optional visual direction"
        ),
        "style": "direct, clear, conversational, fast-moving, useful",
        "avoid": (
            "cheap viral hooks, dangerous exaggeration, fake urgency, luxury flexing, complex "
            "travel jargon"
        ),
    },
    {
        "channel": "email",
        "display_name": "Email / Newsletter",
        "use_when": (
            "Nurture, trust-building, product explanation, trip announcements, seasonal ideas, "
            "partners"
        ),
        "structure": "Clear subject → calm opening → one main idea → useful editorial explanation → one clear CTA",
        "style": "personal but polished, calm, useful, trust-building, editorial",
        "avoid": "too many links, too many CTAs, generic promo language, long text blocks, fake urgency",
    },
    {
        "channel": "landing_page",
        "display_name": "Landing Page / Sales Page",
        "use_when": "Conversion, product explanation, campaign landing, destination or trip page",
        "structure": (
            "Clear value proposition → who it suits → why this place or experience matters → what "
            "the brand takes care of (only what its brand identity states) → trust signals or "
            "process → clear CTA"
        ),
        "style": "precise, benefit-led, grounded, easy to scan",
        "avoid": (
            "overwriting, generic luxury claims, superlatives without proof, tangled itinerary "
            "detail, brand philosophy before the offer is explained"
        ),
    },
    {
        "channel": "ads",
        "display_name": "Ads",
        "use_when": "Lead generation, retargeting, destination promotion, campaign tests",
        "structure": (
            "One clear hook → one benefit for the audience → one thing that sets the brand apart "
            "(from its brand identity) → one CTA"
        ),
        "style": "clear, specific, benefit-led, calm but persuasive",
        "avoid": (
            "clickbait, generic travel slogans, more than one idea per ad, unfounded urgency, "
            "over-promising"
        ),
    },
    # NEW, self-authored fallback — NOT in Bang 2 (STEP0 §5). See module docstring.
    {
        "channel": "blog",
        "display_name": "Blog",
        "use_when": "Long-form SEO content, hub/pillar pages, in-depth destination guides",
        "structure": (
            "Hook → context / why this place → structured H2 sections → call-to-action paragraph "
            "→ FAQ (if top-of-funnel)"
        ),
        "style": "informative, structured, SEO-aware, still specific (not generic travel copy)",
        "avoid": "keyword stuffing, generic listicle tone, cliché superlatives without proof",
    },
]

CHANNEL_STYLES_BY_KEY: dict[str, ChannelStyle] = {c["channel"]: c for c in CHANNEL_STYLES}


def get_channel_style(channel: str) -> ChannelStyle | None:
    return CHANNEL_STYLES_BY_KEY.get(channel)


__all__ = ["ChannelStyle", "CHANNEL_STYLES", "CHANNEL_STYLES_BY_KEY", "get_channel_style"]
