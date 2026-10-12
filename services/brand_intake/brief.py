"""T0 — a brand brief (.docx) → the Brand Identity form fields (AA-741, S229).

The portal upload used to only store the file; the old `/admin/brands/parse-docx` parser had no
caller and folded "Should Not Write" into "Should Write" (opposite meaning). This parser reads the
section headings the AA brief template uses, splits do / don't correctly, and returns fields for
the tenant to REVIEW in the form before saving — it never saves on its own.

Pure (bytes in, dict out) so it is unit-tested without a server.
"""
from __future__ import annotations

import io
import re

# Section headings (case-insensitive, trailing ":" ignored) → internal section key.
_HEADINGS = {
    "brand identity": "identity", "brand": "identity", "about": "identity",
    "target market": "market", "target markets": "market", "market": "market", "markets": "market",
    "tone of voice": "tone", "tone": "tone", "voice": "tone",
    "writing style": "style", "style": "style", "style guide": "style",
    "good example": "good", "good examples": "good", "example": "good", "examples": "good",
    "should write": "should", "do": "should", "write like this": "should",
    "should not write": "should_not", "should not": "should_not", "don't": "should_not",
    "do not write": "should_not", "avoid": "should_not",
    "forbidden words": "forbidden", "forbidden": "forbidden", "banned words": "forbidden",
}
# Inline labels inside a section ("Brand type: …", "Primary markets: …").
_LABELS = {
    "brand type": "brand_type", "core idea": "core_idea",
    "primary markets": "markets", "markets": "markets", "target markets": "markets",
    "customer segment": "customer_segment", "customer mindset": "customer_mindset",
}
_KEY_RE = re.compile(r"\bcis_[A-Za-z0-9_\-]{20,}\b")
_QUOTED_RE = re.compile(r"[“\"]([^”\"]{2,60})[”\"]")
_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z ]{1,30}):\s*(.*)$")


def _lines(text: str) -> list[str]:
    return [x.strip() for x in text.splitlines() if x.strip()]


def parse_brief_paragraphs(paragraphs: list[str]) -> dict:
    """Paragraph texts (first = brand name) → {"fields": {...}, "warnings": [...]}."""
    warnings: list[str] = []
    paras = [p.strip() for p in paragraphs if p and p.strip()]
    if any(_KEY_RE.search(p) for p in paras):
        warnings.append("The brief contains what looks like an API key. It was ignored — remove it "
                        "from the document, it should not be shared.")
    paras = [_KEY_RE.sub("", p).strip() for p in paras]
    paras = [p for p in paras if p]
    if not paras:
        return {"fields": {}, "warnings": ["The document is empty."]}

    sections: dict[str, list[str]] = {}
    labels: dict[str, list[str]] = {}
    current, current_label = None, None
    for p in paras[1:]:
        head = _HEADINGS.get(p.lower().rstrip(":").strip())
        if head:
            current, current_label = head, None
            continue
        for line in _lines(p):
            m = _LABEL_RE.match(line)
            lab = _LABELS.get(m.group(1).strip().lower()) if m else None
            if lab:
                current_label = lab
                if m.group(2).strip():
                    labels.setdefault(lab, []).append(m.group(2).strip())
                continue
            if current_label and current in ("identity", "market"):
                labels.setdefault(current_label, []).append(line)
            else:
                sections.setdefault(current or "identity", []).append(line)
        # a labelled value runs to the end of its paragraph only
        current_label = None

    def text(key):
        return "\n".join(sections.get(key, []))

    def lab(key, sep=" "):
        return sep.join(labels.get(key, [])).strip()

    should_not = sections.get("should_not", [])
    phrases = [q.strip(" ,.;").strip() for line in should_not for q in _QUOTED_RE.findall(line)]
    forbidden = list(dict.fromkeys([*phrases, *[w.strip(" ,.;") for w in sections.get("forbidden", [])]]))
    markets = [m.strip() for v in labels.get("markets", []) for m in re.split(r"[,;/]", v) if m.strip()]
    style = text("style")
    do, dont = text("should"), "\n".join(should_not)
    style_guide = "\n\n".join(x for x in [
        style, f"Write like this:\n{do}" if do else "", f"Avoid:\n{dont}" if dont else ""] if x)

    fields = {
        "brand_name": paras[0][:200],
        "brand_type": lab("brand_type"),
        "core_idea": " ".join(x for x in [lab("core_idea"), text("identity")] if x).strip(),
        "target_markets": markets,
        "customer_segment": lab("customer_segment", "\n"),
        "customer_mindset": lab("customer_mindset", "\n"),
        "tone_of_voice": [t.strip(" ,.;") for t in sections.get("tone", []) if t.strip(" ,.;")],
        "style_guide": style_guide,
        "good_examples": "\n".join(x for x in [text("good"), do] if x),
        "forbidden_words": forbidden,
    }
    for k, label in (("core_idea", "Core idea"), ("style_guide", "Writing style"),
                     ("tone_of_voice", "Tone of voice"), ("target_markets", "Target markets")):
        if not fields[k]:
            warnings.append(f"{label} was not found in the brief — fill it in before saving.")
    if len(forbidden) > 20:
        warnings.append(f"{len(forbidden)} phrases to avoid were found; only the first 20 are kept "
                        "as forbidden words. The rest stay in the style guide under Avoid.")
        fields["forbidden_words"] = forbidden[:20]
    return {"fields": fields, "warnings": warnings}


def parse_brief_docx(content: bytes) -> dict:
    from docx import Document  # python-docx
    doc = Document(io.BytesIO(content))
    paras = [p.text for p in doc.paragraphs]
    for table in doc.tables:                       # briefs written as a 2-column table
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            paras.extend(cells)
    return parse_brief_paragraphs(paras)
