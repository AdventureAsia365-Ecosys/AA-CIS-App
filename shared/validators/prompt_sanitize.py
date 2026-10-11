"""AA-487: shared prompt-injection containment for tenant-authored text that ends up inside an
LLM system_prompt.

The call site that writes `shared.tenant_brand_rules.system_prompt` (or the free-text fields
folded into it) lets a tenant's own words become instructions replayed on every future LLM call
for that tenant (T2 rewrite, F9 judge rubric, T8 brand_audience):
`api/routers/admin_pipeline.py::update_brand_identity()` — the tenant portal's "edit brand voice"
form (`BrandTab.tsx`) POSTs `system_prompt`/`style_guide` directly as free text.

(The DOCX brand-brief Lambda that also used this sanitizer was retired in AA-750.)

This is self-injection containment (a tenant can only attack their own future generations), not general-purpose prompt-injection defense (unsolved in general) — it
strips the common attack shapes and caps field length.
"""
import re

# Matches the common injection shapes: instructions telling the model to disregard prior
# instructions, to adopt a new persona/role, to reveal its own system prompt, or fake
# conversation/role markers (used to forge a fake "assistant:"/"system:" turn boundary).
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+|any\s+)?(previous|prior|above|the\s+above)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+|any\s+)?(previous|prior|above|the\s+above)", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+|any\s+)?(previous|prior|above|everything)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an|the)\b", re.IGNORECASE),
    re.compile(r"act\s+as\s+(if\s+you\s+are\s+)?(a|an|the)\b", re.IGNORECASE),
    re.compile(r"pretend\s+(you\s+are|to\s+be)\b", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"system\s*(prompt)?\s*:", re.IGNORECASE),
    re.compile(
        r"\b(reveal|print|show|output|repeat)\s+(your|the)\s+(system\s+)?(prompt|instructions)\b",
        re.IGNORECASE,
    ),
    re.compile(r"</?(system|assistant|user)>", re.IGNORECASE),
    re.compile(r"\[/?(system|inst|instructions)\]", re.IGNORECASE),
    re.compile(r"<\|.*?\|>"),  # fake special/control tokens (e.g. <|im_start|>)
]

_REDACTION = "[redacted]"

MAX_SHORT_FIELD_LEN = 200    # brand_type, core_idea, customer_segment, customer_mindset, single words
MAX_LONG_FIELD_LEN = 300     # style_guide free-text body
# update_brand_identity() lets a tenant type/replace their WHOLE system_prompt directly (unlike
# the Lambda's build_system_prompt(), which assembles it from several already-capped fields) —
# needs headroom for genuine brand-voice prose, still bounded well short of "paste a huge
# injection payload".
MAX_SYSTEM_PROMPT_LEN = 2000


def _strip_control_chars(text: str) -> str:
    # Drop non-printable/control characters (keep \n, \t) that could be used to obscure a
    # pattern or confuse downstream log/UI rendering.
    return "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())


def sanitize_text(text: str | None, max_len: int) -> str | None:
    """Sanitize one free-text field: strip control chars, redact known injection shapes,
    collapse whitespace, truncate. Returns None unchanged (absent field stays absent)."""
    if text is None:
        return None
    cleaned = _strip_control_chars(text)
    for pattern in _INJECTION_PATTERNS:
        cleaned = pattern.sub(_REDACTION, cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned[:max_len]


def sanitize_list(items: list[str], max_len: int, max_items: int = 20) -> list[str]:
    out = []
    for item in items[:max_items]:
        sanitized = sanitize_text(item, max_len)
        if sanitized:
            out.append(sanitized)
    return out
