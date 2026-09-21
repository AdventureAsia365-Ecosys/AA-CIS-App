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
# compute_quarter_plan() (one dict literal, only used for `distinctiveness`); pulled out here
# so the SAME 3-bucket numeric ladder is reused for the new `dfs_relevance` term (AA-448,
# services/acp_shared/dfs_relevance.py) instead of that formula inventing its own scale — a
# "MED" atom and a "MED" tour-demand signal now contribute the same fractional weight.
SIGNAL_SCORE_MAP = {"HIGH": 1.0, "MED": 0.5, "LOW": 0.1}

# AA-448 — N5 quarter-plan scoring weights. Round 1 took this from 3 terms (runway_fit/richness/
# distinctiveness, original 0.4/0.3/0.3) to 4 (added dfs_relevance, ADR-2026-038 §0.4 — ADD not
# replace runway_fit, see docs/implementation-notes/AA-448-t7-content-planning.md "Decision 3"
# for the reasoning). Round 6 adds a 5th term, `engagement_adjustment` (real post-publish
# feedback, confidence-gated atom.weight rolled up to trip level — a NEW extension beyond
# aa-marketing-v2's own Module H, which never fed back into quarter-level trip selection at all,
# see that same file's "round 6" section) — done ONCE, in the same pass as this comment, per
# Nghiep's explicit instruction not to re-derive the weights a second time after dfs_relevance
# had already shipped. runway_fit stays the largest single term (the most concrete, deterministic
# signal); richness/distinctiveness equal at 0.20 each; dfs_relevance and engagement_adjustment
# both at 0.15 — smaller than the 3 established terms since both are newer/less-calibrated
# signals (dfs_relevance's thresholds are explicitly "chưa hiệu chỉnh" per the ADR; engagement
# feedback is sparse/confidence-gated early on, most trips will score the neutral 0.5 midpoint
# for a while). Kept named/importable (not inline) so `_score_reason()` and
# `compute_quarter_plan()` share one source of truth and can't drift out of sync.
QUARTER_SCORE_WEIGHTS = {
    "runway_fit": 0.30,
    "richness": 0.20,
    "distinctiveness": 0.20,
    "dfs_relevance": 0.15,
    "engagement_adjustment": 0.15,
}

# AA-603 (21/09/2026) — the feedback-loop constants (CONFIDENCE_ATOM_MIN_POSTS, ATOM_WEIGHT_MIN,
# ATOM_WEIGHT_MAX, ENGAGEMENT_RATE_BASELINE) were REMOVED with the deleted content_metrics.py /
# trust_ramp.py that were their only consumers (dead N7/N8 feedback loop, never ran on real data).
