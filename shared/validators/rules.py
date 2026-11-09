"""
29 Brand Rule Validators — Adventure Asia CIS
4 layers: Required Fields / Brand Voice / SEO / Structure
Each validator returns (passed: bool, issue: str)
"""
from typing import Callable

from services.quality import writing_policy as _wp

ValidatorFn = Callable[[dict], tuple[bool, str]]

# ============================================================
# LAYER 1 — Required Fields (6 rules)
# ============================================================

def v01_has_name(c: dict) -> tuple[bool, str]:
    return bool(c.get("name")), "L1-01: Missing name"

def v02_has_subtitle(c: dict) -> tuple[bool, str]:
    return bool(c.get("subtitle")), "L1-02: Missing subtitle"

def v03_has_summary(c: dict) -> tuple[bool, str]:
    return bool(c.get("summary")), "L1-03: Missing summary"

def v04_has_highlights(c: dict) -> tuple[bool, str]:
    h = c.get("highlights", [])
    return (isinstance(h, list) and len(h) >= 2), "L1-04: highlights must have ≥2 items"

def v05_has_seo_title(c: dict) -> tuple[bool, str]:
    return bool(c.get("seo_title")), "L1-05: Missing seo_title"

def v06_has_seo_meta(c: dict) -> tuple[bool, str]:
    return bool(c.get("seo_meta")), "L1-06: Missing seo_meta"

# ============================================================
# LAYER 2 — Brand Voice (11 rules)
# ============================================================

FORBIDDEN_WORDS = [
    "cheap", "cheapest", "deal", "deals", "discount", "discounted",
    "book now", "instant booking", "limited time", "hurry",
    "flash sale", "best price", "lowest price", "bargain",
]

PREFERRED_WORDS = [
    "curated", "designed", "refined", "tailored", "journey",
    "exclusive", "bespoke", "immersive", "thoughtfully",
]

def v07_no_forbidden_words(c: dict) -> tuple[bool, str]:
    text = " ".join([
        c.get("name", ""), c.get("subtitle", ""),
        c.get("summary", ""), c.get("seo_title", ""), c.get("seo_meta", ""),
    ]).lower()
    found = [w for w in FORBIDDEN_WORDS if w in text]
    return (len(found) == 0), f"L2-07: Forbidden words: {found}"

def v08_has_preferred_words(c: dict) -> tuple[bool, str]:
    text = " ".join([
        c.get("name", ""), c.get("subtitle", ""), c.get("summary", ""),
    ]).lower()
    found = [w for w in PREFERRED_WORDS if w in text]
    return (len(found) >= 1), "L2-08: No preferred brand words found"

def v09_no_exclamation_marks(c: dict) -> tuple[bool, str]:
    text = " ".join([str(v) for v in c.values() if isinstance(v, str)])
    return ("!" not in text), "L2-09: Exclamation marks not allowed"

def v10_no_all_caps_words(c: dict) -> tuple[bool, str]:
    text = " ".join([str(v) for v in c.values() if isinstance(v, str)])
    words = text.split()
    caps = [w for w in words if w.isupper() and len(w) > 2]
    return (len(caps) == 0), f"L2-10: ALL CAPS words found: {caps[:3]}"

def v11_summary_min_length(c: dict) -> tuple[bool, str]:
    summary = c.get("summary", "")
    return (len(summary) >= 80), f"L2-11: Summary too short ({len(summary)} chars, min 80)"

def v12_summary_max_length(c: dict) -> tuple[bool, str]:
    summary = c.get("summary", "")
    return (len(summary) <= 500), f"L2-12: Summary too long ({len(summary)} chars, max 500)"

def v13_subtitle_max_length(c: dict) -> tuple[bool, str]:
    subtitle = c.get("subtitle", "")
    return (len(subtitle) <= 100), f"L2-13: Subtitle too long ({len(subtitle)} chars, max 100)"

def v14_no_first_person(c: dict) -> tuple[bool, str]:
    text = " ".join([str(v) for v in c.values() if isinstance(v, str)]).lower()
    first_person = ["i am", "we are", "our company", "we offer", "we provide"]
    found = [p for p in first_person if p in text]
    return (len(found) == 0), f"L2-14: First-person language found: {found}"

def v15_no_generic_opener(c: dict) -> tuple[bool, str]:
    summary = c.get("summary", "").lower()
    generic = ["are you looking for", "do you want to", "look no further", "welcome to"]
    found = [g for g in generic if summary.startswith(g)]
    return (len(found) == 0), f"L2-15: Generic opener detected: {found}"

def v16_name_title_case(c: dict) -> tuple[bool, str]:
    name = c.get("name", "")
    words = name.split()
    skip = {"a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of"}
    violations = [w for w in words if w.lower() not in skip and w != w.title()]
    return (len(violations) == 0), f"L2-16: Name not title case: {violations}"

def v17_highlights_are_strings(c: dict) -> tuple[bool, str]:
    highlights = c.get("highlights", [])
    non_str = [h for h in highlights if not isinstance(h, str)]
    return (len(non_str) == 0), "L2-17: highlights must all be strings"

# ============================================================
# LAYER 3 — SEO Rules (7 rules)
# ============================================================

def v18_seo_title_max_60(c: dict) -> tuple[bool, str]:
    t = c.get("seo_title", "")
    cap = _wp.band("title")[1]  # AA-1047: the policy title cap (writing_policy.yaml; the name keeps its old number)
    return (len(t) <= cap), f"L3-18: seo_title {len(t)} chars (max {cap})"

def v19_seo_title_min_20(c: dict) -> tuple[bool, str]:
    t = c.get("seo_title", "")
    return (len(t) >= 20), f"L3-19: seo_title {len(t)} chars (min 20)"

def v20_seo_meta_max_160(c: dict) -> tuple[bool, str]:
    m = c.get("seo_meta", "")
    return (len(m) <= 160), f"L3-20: seo_meta {len(m)} chars (max 160)"

def v21_seo_meta_min_80(c: dict) -> tuple[bool, str]:
    m = c.get("seo_meta", "")
    return (len(m) >= 80), f"L3-21: seo_meta {len(m)} chars (min 80)"

def v22_seo_title_no_forbidden(c: dict) -> tuple[bool, str]:
    title = c.get("seo_title", "").lower()
    found = [w for w in FORBIDDEN_WORDS if w in title]
    return (len(found) == 0), f"L3-22: Forbidden words in seo_title: {found}"

def v23_seo_meta_no_forbidden(c: dict) -> tuple[bool, str]:
    meta = c.get("seo_meta", "").lower()
    found = [w for w in FORBIDDEN_WORDS if w in meta]
    return (len(found) == 0), f"L3-23: Forbidden words in seo_meta: {found}"

def v24_trip_type_valid(c: dict) -> tuple[bool, str]:
    valid = {"cultural", "adventure", "wellness", "culinary", "wildlife", "coastal", "multi"}
    trip_type = c.get("trip_type", "")
    return (trip_type in valid), f"L3-24: Invalid trip_type '{trip_type}', must be one of {valid}"

# ============================================================
# LAYER 4 — Structure Rules (5 rules)
# ============================================================

def v25_highlights_max_8(c: dict) -> tuple[bool, str]:
    h = c.get("highlights", [])
    return (len(h) <= 8), f"L4-25: Too many highlights ({len(h)}, max 8)"

def v26_highlight_min_length(c: dict) -> tuple[bool, str]:
    highlights = c.get("highlights", [])
    short = [h for h in highlights if isinstance(h, str) and len(h) < 10]
    return (len(short) == 0), f"L4-26: Highlights too short (min 10 chars): {short}"

def v27_highlight_max_length(c: dict) -> tuple[bool, str]:
    highlights = c.get("highlights", [])
    long_ = [h for h in highlights if isinstance(h, str) and len(h) > 120]
    return (len(long_) == 0), f"L4-27: Highlights too long (max 120 chars): {long_[:2]}"

def v28_no_duplicate_highlights(c: dict) -> tuple[bool, str]:
    highlights = [h.lower().strip() for h in c.get("highlights", []) if isinstance(h, str)]
    return (len(highlights) == len(set(highlights))), "L4-28: Duplicate highlights detected"

def v29_name_max_length(c: dict) -> tuple[bool, str]:
    name = c.get("name", "")
    return (len(name) <= 80), f"L4-29: name too long ({len(name)} chars, max 80)"

# ============================================================
# Registry — all 29 validators in order
# ============================================================
ALL_VALIDATORS: list[ValidatorFn] = [
    v01_has_name, v02_has_subtitle, v03_has_summary,
    v04_has_highlights, v05_has_seo_title, v06_has_seo_meta,
    v07_no_forbidden_words, v08_has_preferred_words,
    v09_no_exclamation_marks, v10_no_all_caps_words,
    v11_summary_min_length, v12_summary_max_length,
    v13_subtitle_max_length, v14_no_first_person,
    v15_no_generic_opener, v16_name_title_case,
    v17_highlights_are_strings,
    v18_seo_title_max_60, v19_seo_title_min_20,
    v20_seo_meta_max_160, v21_seo_meta_min_80,
    v22_seo_title_no_forbidden, v23_seo_meta_no_forbidden,
    v24_trip_type_valid,
    v25_highlights_max_8, v26_highlight_min_length,
    v27_highlight_max_length, v28_no_duplicate_highlights,
    v29_name_max_length,
]

def validate_content(content: dict) -> dict:
    """
    Run all 29 validators.
    Returns: {score, passed, failed, issues, audit_status}
    """
    passed = []
    failed = []
    issues = []

    for validator in ALL_VALIDATORS:
        ok, message = validator(content)
        if ok:
            passed.append(validator.__name__)
        else:
            failed.append(validator.__name__)
            issues.append(message)

    total     = len(ALL_VALIDATORS)
    score     = round((len(passed) / total) * 10, 1)

    if score >= 8.0:
        audit_status = "passed"
    elif score >= 5.0:
        audit_status = "flagged"
    else:
        audit_status = "failed"

    return {
        "score":        score,
        "passed":       len(passed),
        "failed":       len(failed),
        "total":        total,
        "issues":       issues,
        "audit_status": audit_status,
    }
