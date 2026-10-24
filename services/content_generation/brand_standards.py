"""AA-135: Adventure Asia brand identity constants for LLM-as-Judge audit."""
from services.quality.registry import threshold

AA_BRAND_IDENTITY_PROMPT = """
ADVENTURE ASIA BRAND IDENTITY — EDITORIAL STANDARD

Voice attributes (must feel this way):
- calm, assured, well-traveled, selective, precise, private
- curated, composed, human, premium, controlled

Audience: Senior professionals 40-60, $250k+, US/UK/AUS markets
Brand line: "Discreet Executive Adventure"
CTA: "Design This Journey" (never "Book Now")

REQUIRED language: Design / Curated / Refined / Tailored / Journey
FORBIDDEN language: Deals / Cheap / Book Now / Instant booking / stunning /
breathtaking / unforgettable / hidden gem / bucket list / world-class / iconic /
epic / fun / exciting / amazing / discover / explore / package / dream trip /
once in a lifetime / vibrant / immersive experiences / seamless journey /
treasure-trove / glittering / diverse wonders

FIELD RULES:

AA_NAME:
- Clear, accurate, premium — never flashy
- Never ALL CAPS (normalise to Title Case)
- Never contains: "The Best Of", "Ultimate", "Top", "Must-See", "and Fun", "Expenditures"
- Portfolio: no two tours in same batch may share identical base name phrase

AA_SUBTITLE:
- Must read as a clause describing what the trip IS — never a city list or waypoint string
- Must match real trip type (no cycling language on hiking trip)
- If tour has day count, must match source duration field
- NEVER: "Bangkok, Chiang Mai, Krabi" or "Paro → Thimphu → Punakha" or "Route: ..."

AA_SUMMARY:
- Must NOT open with: "Enjoy", "Discover", "Immerse", "Why fly when", "Give X a try"
- Must NOT contain: "our tour leaders", "we acquaint ourselves", "special moments",
  "connecting with each other", "private getaway", "romance package", "concrete moments",
  "each section delivers", "clear daily structure"
- Must NOT be honeymoon/mass-market promotional language

AA_HIGHLIGHTS:
- Minimum 4 highlights
- Each must be specific and concrete — drawn from itinerary
- NEVER use: "culture", "nature", "adventure", "history", "scenic views", "local cuisine"
- NEVER start with "Optional" or contain conditional language
- For activity-specific tours: first 2 highlights MUST reflect primary activity
- Elephant riding: always FACT_CHECK_MANUAL_CHECK
- Rare wildlife + location mismatch: always FACT_CHECK_MANUAL_CHECK

AA_ITINERARIES:
- Chronological, readable, calm, structured, realistic pacing
- Must be present and substantive if source has itinerary data
- Format: "Day N -- [title]" string; each day title MUST name the place
  and/or primary activity (e.g. "Day 2 -- Trekking to Sapa Valley Villages").
  Generic titles ("Exploration", "Free Day", "Arrival", "Departure",
  "Transfer") name no place/activity — ITINERARY_DAY_TITLE_GENERIC
- NEVER invent meal names (breakfast, lunch, dinner) or clock-times
  (e.g. "7:00 AM departure") unless explicit in the source — fabricated
  meals/times are PRODUCT_TRUTH_RISK; describe activities by sequence

SEO_TITLE:
- 60 chars max
- Never ends with conjunction, preposition, or ampersand
- Never echoes AA_NAME verbatim
- Activity label must match actual tour content

SEO_META:
- 140-155 chars ideal (flag if under 140)
- Must end with a period
- Must NOT open with: "This is a", "Discover", "Book", "Find"
- Must NOT contain: "package", "reviews", "authentic reviews", price-comparison queries
- Trip type in meta must match summary and title
"""

AA_COWORK_STRUCTURE_PROMPT = """
ADVENTURE ASIA PIPELINE STRUCTURE — AUDIT REFERENCE

Workflow stages:
1. generate: rewrite via LLMClient (Bedrock Sonnet/Haiku or GPT-4.1)
2. validate: 29 structural rules, score 0-10
3. brand_audit: LLM-as-Judge (GPT-4.1) — only runs if score >= """ + f"{threshold('a1.min_quality'):.1f}" + """
4. flag_fix: targeted rewrite of flagged fields only
5. publish: gold layer

Status values:
- brand_audit_status: "pass" | "flagged" | "manual_check"

FAILURE CODES (use exactly these strings):
PRODUCT_TRUTH_RISK, SUBTITLE_TRIP_TYPE_MISMATCH, SUBTITLE_CITY_LIST,
SUBTITLE_WAYPOINT_FORMAT, SUMMARY_OFF_BRAND, SUMMARY_HONEYMOON_LANGUAGE,
SUMMARY_SELF_REFERENTIAL, GENERIC_AI_WORDING, HIGHLIGHTS_TOO_GENERIC,
HIGHLIGHTS_ORDERING_WRONG, HIGHLIGHTS_OPTIONAL_LANGUAGE,
HIGHLIGHTS_WILDLIFE_UNVERIFIED, ITINERARY_STRUCTURE_WEAK,
ITINERARY_MEAL_TIME_INVENTED, ITINERARY_DAY_TITLE_GENERIC,
SEO_TITLE_WEAK, SEO_TITLE_WRONG_ACTIVITY, META_INCOMPLETE_SENTENCE,
META_OPENER_ROBOTIC, META_PACKAGE_WORD, META_DFS_VERBATIM,
DFS_INTENT_UNDERUSED, KEYWORD_STUFFING_RISK, GENERIC_AI_WORDING,
NAME_ALL_CAPS, NAME_SUPERLATIVE, FACT_CHECK_MANUAL_CHECK,
PORTFOLIO_NAME_COLLISION

Score fields (1=pass, 0=fail):
brand_fit, human_read, seo_fit, trip_type_accuracy, publish_readiness
"""
