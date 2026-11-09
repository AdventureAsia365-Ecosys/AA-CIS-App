"""AA-1047 (root ADR 0020, critic fix F9): the writer-prompt lines the writing policy changed on purpose.

The T9 writer prompt stays byte-equal to its pre-AA-1047 text EXCEPT the contradiction-resolving lines listed here
(place background, D1; the CTA as the literal line, word for word). `pre_aa1047(text)` puts the old lines back, so a
prompt-pin test keeps proving that nothing else moved (docs/implementation-notes/AA-1047-policy-registry.md lists
each line). Synthetic: no prompt text is copied from the database.
"""
from __future__ import annotations

from services.quality import writing_policy as wp

OLD_LINES = {
    "t9_writer.background_allowed": (
        "ALLOWED: short, widely known background about a place the content seed names: what it is,\n"
        "  its history and era, who founded it, its religious or cultural role, whether it is still in\n"
        "  use, written in words, without numbers unless the seed has them."),
    "t9_writer.cta": (
        "- Include the given call to action, worked naturally into the piece, not pasted on as a bare\n"
        "  final sentence unless the channel's own style calls for that."),
    "t9_writer.cta_close_example": "so the piece closes on its last fact with the call to action worked into it.",
    "t9_writer.cta_dont_write": (
        "WRITE the call\n"
        "      to action as the piece's own sentence, worked into the last fact, with no label line before it."),
}


def pre_aa1047(text: str) -> str:
    """`text` with each AA-1047 writer line replaced by the line it had before (a pin test hashes the result)."""
    for key, old in OLD_LINES.items():
        new = wp.fragment(key)
        assert wp.compiled()["fragment_status"][key] != "byte_equal", key
        text = text.replace(new, old)
    return text
