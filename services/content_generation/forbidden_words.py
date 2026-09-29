"""AA-641: the validate-node forbidden list + its matching rule, in a neutral module.

graph.py imports flag_fix_node, so flag_fix cannot import graph.py back (circular) — same reason
seo_meta_utils.py exists (AA-205). validate_node and flag_fix now share one list and one matching
rule, so flag_fix can tell whether its own repair introduced a word validate will fire on.
"""
import json

# AA-240: canonical validate-node forbidden list (graph.py re-exports it as _VALIDATE_FORBIDDEN).
VALIDATE_FORBIDDEN = [
    "curated", "pristine", "refined", "tailored", "bespoke",
    "stunning", "breathtaking", "magical", "paradise",
    "cheap", "deal", "book now", "instant booking", "discount",
]


def all_forbidden(tenant_words=None) -> list[str]:
    """The AA list + a tenant's own words (lowercased, deduped, order kept) — what validate uses."""
    tenant = [w.lower().strip() for w in (tenant_words or []) if w and w.strip()]
    return list(dict.fromkeys(VALIDATE_FORBIDDEN + tenant))


def forbidden_in(value, words) -> set[str]:
    """Forbidden words present in `value` (any JSON-able field), matched exactly as validate_node
    matches: substring of the lowercased JSON dump."""
    text = json.dumps(value).lower()
    return {w for w in words if w in text}
