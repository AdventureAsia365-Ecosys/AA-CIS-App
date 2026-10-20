"""AA-641: the validate-node forbidden list + its matching rule, in a neutral module.

graph.py imports flag_fix_node, so flag_fix cannot import graph.py back (circular) — same reason
seo_meta_utils.py exists (AA-205). validate_node and flag_fix now share one list and one matching
rule, so flag_fix can tell whether its own repair introduced a word validate will fire on.
"""
import json
import re
from functools import lru_cache

# AA-240: canonical validate-node forbidden list (graph.py re-exports it as _VALIDATE_FORBIDDEN).
# AA-800: this is the A1 list, kept byte for byte. It is the union of the three layers of
# services/acp_shared/signature_vocabulary (platform truth + AA stock superlatives + AA positioning words,
# minus the prompt-only "luxury"); tenant paths (T2) read the layered list through `state_forbidden`.
VALIDATE_FORBIDDEN = [
    "curated", "pristine", "refined", "tailored", "bespoke",
    "stunning", "breathtaking", "magical", "paradise",
    "cheap", "deal", "book now", "instant booking", "discount",
]


def all_forbidden(tenant_words=None) -> list[str]:
    """The AA list + a tenant's own words (lowercased, deduped, order kept) — what validate uses."""
    tenant = [w.lower().strip() for w in (tenant_words or []) if w and w.strip()]
    return list(dict.fromkeys(VALIDATE_FORBIDDEN + tenant))


def state_forbidden(state: dict, content=None) -> list[str]:
    """AA-800: the words validate_node fires FORBIDDEN_WORD on for this graph state.

    A1 (no `is_tenant_rewrite`): `all_forbidden(brand_forbidden_words)`, exactly as before. T2: the layered
    tenant list (signature_vocabulary.tenant_blocked_words) — platform truth, the positioning words the
    tenant did not declare, its own list, and the stock superlatives of `content` (any field shape) when
    it holds more than one."""
    if not (state or {}).get("is_tenant_rewrite"):
        return all_forbidden((state or {}).get("brand_forbidden_words"))
    from services.acp_shared.signature_vocabulary import tenant_blocked_words
    from .batch_prompt import brand_pack_from_state
    text = None if content is None else copy_text(_writer_fields(content))
    return tenant_blocked_words(brand_pack_from_state(state), text)


def _writer_fields(content):
    """The fields the writer authored (validate's scan skips the DFS keyword metadata, AA-738)."""
    if isinstance(content, dict):
        return {k: v for k, v in content.items() if k not in ("seo_keywords_used",)}
    return content


@lru_cache(maxsize=512)
def _word_re(word: str) -> re.Pattern:
    # S207: whole words (plus a plural "s"), not substrings. The substring rule fired on "depicts"
    # (epic), "fundamental"/"fund" (fun), "ideal" (deal), "epicenter" — measured on the 58 HITL
    # versions of the 17/09 run, where FORBIDDEN_WORD was on 50 of them.
    return re.compile(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"s?(?![a-z0-9])")


def has_word(text: str, word: str) -> bool:
    """True when `word` (a word or phrase) appears in `text` as a whole word, case-insensitive."""
    return bool(word) and _word_re(word).search((text or "").lower()) is not None


def copy_text(value) -> str:
    """AA-738: the plain text of a field (str / list / dict, nested), strings joined by newlines.
    Word scans must run on this, not on json.dumps(): the dump escapes "\n" and "—" (\u2014), so
    the letter before a word becomes "n" or "4" and the whole-word lookbehind misses it — S218: a
    master went live with "—explore" in its itinerary."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(copy_text(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(copy_text(v) for v in value)
    return "" if value is None else str(value)


def forbidden_in(value, words) -> set[str]:
    """Forbidden words present in `value` (any field shape), matched exactly as validate_node
    matches: whole words in the field's plain text (copy_text)."""
    text = copy_text(value)
    return {w for w in words if has_word(text, w)}
