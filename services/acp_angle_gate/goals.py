"""
services.acp_angle_gate.goals — the 8-value Goal table (T8 workflow step 2).

Verbatim from Bang 1, docs/claude_tasks/AA-449-00-step0-t8-angle-gate-investigation.md — the
table Nghiep supplied directly (round 2), itself a merge of SKILL_v2.md's per-goal "logic" text
+ `writing formulars.xlsx`'s "Marketing term" column (STEP0 §1a already confirmed this, both
prior sources already found in AA-439-07, not re-derived here).

STEP0 flagged 2 wording discrepancies against SKILL_v2.md in Bang 1 (header said "7 loại" but
lists 8; Conversion's SLAP expands "Purchase" here vs. SKILL_v2.md's own "Proceed") — kept
EXACTLY as Bang 1 states, not silently "corrected" back to SKILL_v2.md, since the build task
re-supplied Bang 1 unchanged and did not ask for either correction. If Nghiep decides otherwise
later, only this file's `logic`/`marketing_term` strings need editing — nothing downstream reads
those specific words structurally (they only ever get interpolated into the LLM prompt).

`key` values are new (Bang 1 has no machine-readable slug column) — snake_case of `name`, chosen
here, not from any source document.
"""
from __future__ import annotations

from typing import TypedDict


class Goal(TypedDict):
    key: str
    name: str
    description: str
    logic: str
    marketing_term: str


# AA-771 (S233) — descriptions translated to English and made brand-neutral. They were the
# Vietnamese Bang 1 text with Adventure Asia's own framing ("quan điểm AA", "năng lực thiết kế
# route của AA", "Why AA có mặt", "Giải thích AA làm gì"), sent verbatim inside every tenant's
# English T8/T9 prompt and shown in the tenant's goal picker. Formula logic is unchanged.
GOALS: list[Goal] = [
    {
        "key": "promotion",
        "name": "Promotion",
        "description": "Promote a destination, route, trip, offer, launch or campaign",
        "logic": "Attention-Interest-Desire-Action",
        "marketing_term": "AIDA",
    },
    {
        "key": "lead_generation",
        "name": "Lead Generation",
        "description": (
            "Use AIDA when the offer is positive; use PAS when the reader's pain is planning "
            "overload, generic trips, poor routes, crowds or uncertainty"
        ),
        "logic": "Problem-Agitate-Solve",
        "marketing_term": "AIDA or PAS",
    },
    {
        "key": "conversion",
        "name": "Conversion",
        "description": "Move the reader to an enquiry, booking, waitlist, consultation or purchase",
        "logic": "Stop-Look-Act-Purchase",
        "marketing_term": "SLAP",
    },
    {
        "key": "introduction_awareness",
        "name": "Introduction / Awareness",
        "description": "Introduce a country, destination, route style, trend or the brand's own point of view",
        "logic": "Hook-Insight-CTA",
        "marketing_term": "Hook-Insight-CTA or 5W1H",
    },
    {
        "key": "trust_building",
        "name": "Trust-building",
        "description": "Build trust in how the brand plans and runs its trips, as its brand identity describes it",
        "logic": (
            "Problem-Insight-Proof-Action (the proof must be concrete and come from the source — "
            "never invent a claim)"
        ),
        "marketing_term": "FAB",
    },
    {
        "key": "engagement_conversation",
        "name": "Engagement / Conversation",
        "description": "Invite comments, shares, saves or discussion",
        "logic": "Hook-Value-CTA (the question must be grounded in the source)",
        "marketing_term": "BAB",
    },
    {
        "key": "event_announcement",
        "name": "Event Announcement",
        "description": "Announce a trade fair, event, launch or summit, or a meeting with suppliers or partners",
        "logic": "What-Why-Who-Where/When-Why the brand is there-CTA",
        "marketing_term": "5W1H + AIDA",
    },
    {
        "key": "product_service_explanation",
        "name": "Product or Service Explanation",
        "description": (
            "Explain what the brand offers and how it works — only services its brand identity "
            "states"
        ),
        "logic": "Feature-Advantage-Benefit",
        "marketing_term": "FAB",
    },
]

GOALS_BY_KEY: dict[str, Goal] = {g["key"]: g for g in GOALS}


def get_goal(key: str) -> Goal | None:
    return GOALS_BY_KEY.get(key)


__all__ = ["Goal", "GOALS", "GOALS_BY_KEY", "get_goal"]
