"""
Runway/quarter/allocator thresholds — N4/N5/N6 (AA-301).

Values ported verbatim from aamc/config.py (aa-marketing-v2 research build,
docs/AI-gent-for automation works/aa-marketing-v2), same porting convention
already used by services/acp_shared/atom_constants.py (AA-299/302).

THIN_TRIP_ATOM_MIN lives in services.acp_shared.atom_constants (AA-299/302,
shared by the N0-N2 decompose gate) — import it from there, do not redefine
it here.
"""

RUNWAY_OFFSETS_MONTHS = {
    "long_haul": (3, 6),         # EU/US/AU -> Asia
    "family_extended": (6, 12),
    "short_haul": (0.5, 2),      # intra-Asia, 2-8 weeks
}
LONG_HAUL_MARKETS = {"US", "USA", "UK", "GB", "DE", "FR", "NL", "AU", "CA", "ES", "IT", "SE", "CH", "EU"}

FRAMEWORK_TABLE = {
    ("TOFU", "blog"): {"framework": "hub", "faq": True, "faq_n": (4, 8)},
    ("MOFU", "blog"): {"framework": "PAS", "faq": True, "faq_n": (4, 6)},
    ("BOFU", "blog"): {"framework": "AIDA", "faq": False, "faq_n": (0, 0)},
    ("ANY", "facebook"): {"framework": "hook_story_cta", "faq": False, "words": (80, 150)},
    ("ANY", "tiktok"): {"framework": "hook_beats_payoff", "faq": False},
    ("ANY", "email"): {"framework": "reader_as_hero", "faq": False},
    # AA-449 — 4 entries added for T8's channel extension (STEP0 §5). Self-chosen from Bang 2's
    # own "Structure" column (docs/claude_tasks/AA-449-00-step0-t8-angle-gate-investigation.md),
    # same class of caveat as this file's other self-chosen constants (THIN_TRIP_MAX_SHARE,
    # ENGAGEMENT_RATE_BASELINE) — not from any prior formal spec, easy to rename later. Without
    # these 4 entries the code would NOT crash (compute_slot_grid()'s `.get(fw_key, {"framework":
    # "hub"})` falls back safely) but every one of these 4 channels' slots would silently get the
    # generic "hub" label instead of a channel-appropriate one.
    ("ANY", "linkedin"): {"framework": "insight_led", "faq": False},
    ("ANY", "instagram"): {"framework": "hook_sensory_cta", "faq": False},
    ("ANY", "landing_page"): {"framework": "AIDA", "faq": False},
    ("ANY", "ads"): {"framework": "hook_benefit_cta", "faq": False},
}

SLOT_MIX = {"evergreen": 0.65, "campaign": 0.25, "reactive_held_empty": 0.10}
ATOM_COOLDOWN_WEEKS = 6

# B5 fix (N5) — a thin trip's content share is capped at this fraction, freed
# share redistributed proportionally to non-thin trips. Not specified in the
# original issue text (only "cap thin trip's share" is mandated) — 0.15 is a
# self-chosen default, see AA-301 implementation notes.
# TẠM THỜI — chưa có xác nhận chính thức từ Ms. Thư. Xem AA-319.
# KHÔNG liên quan tới "Sapa 0.15" trong research Session 104 (đó là share
# tự tính của 1 destination bình thường, không phải ngưỡng cap chủ định
# cho tour thin) — trùng số ngẫu nhiên, đừng nhầm lẫn khi đọc lại.
THIN_TRIP_MAX_SHARE = 0.15

# AA-448 — shared HIGH/MED/LOW -> numeric mapping. Was inline in quarter.py's
# compute_quarter_plan(); pulled out here so the SAME 3-bucket numeric ladder is reused for the
# `dfs_relevance` term (AA-448, services/acp_shared/dfs_relevance.py) instead of that formula
# inventing its own scale — a "MED" tour-demand signal contributes the same fractional weight
# everywhere it is read.
SIGNAL_SCORE_MAP = {"HIGH": 1.0, "MED": 0.5, "LOW": 0.1}

# AA-448 — N5 quarter-plan scoring weights. History: round 1 took this from 3 terms
# (runway_fit/richness/distinctiveness, original 0.4/0.3/0.3) to 4 (added dfs_relevance,
# ADR-2026-038 §0.4 — ADD not replace runway_fit); round 6 added a 5th term,
# `engagement_adjustment` (real post-publish feedback, atom.weight rolled up to trip level).
#
# AA-754 — the `distinctiveness` term (atom-level competitor-overlap score) is REMOVED
# end-to-end: it was only ever a real measurement for tenant-scored atoms via the T5
# competitor-index path, which this task also removes, so every atom now carries only the
# stored default. The remaining 4 terms are RE-NORMALIZED to sum to 1.0 by dividing each by the
# old 0.80 non-distinctiveness total, which keeps their RELATIVE proportions exactly as they
# were (runway_fit stays the largest term, richness second, dfs_relevance/engagement equal and
# smallest) — the trip ranking is unchanged for today's data, where every platform atom scored a
# constant MED distinctiveness and so the term never moved the order. Kept named/importable (not
# inline) so `_score_reason()` and `compute_quarter_plan()` share one source of truth.
QUARTER_SCORE_WEIGHTS = {
    "runway_fit": 0.375,
    "richness": 0.25,
    "dfs_relevance": 0.1875,
    "engagement_adjustment": 0.1875,
}

# AA-603 (21/09/2026) — the feedback-loop constants (CONFIDENCE_ATOM_MIN_POSTS, ATOM_WEIGHT_MIN,
# ATOM_WEIGHT_MAX, ENGAGEMENT_RATE_BASELINE) were REMOVED with the deleted content_metrics.py /
# trust_ramp.py that were their only consumers (dead N7/N8 feedback loop, never ran on real data).
