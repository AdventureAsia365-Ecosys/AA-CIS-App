"""T0 — a brand brief (.docx) → the Brand Identity form fields (AA-741, S229).

The portal upload used to only store the file; the old `/admin/brands/parse-docx` parser had no
caller and folded "Should Not Write" into "Should Write" (opposite meaning). This parser reads the
section headings the AA brief template uses, splits do / don't correctly, and returns fields for
the tenant to REVIEW in the form before saving — it never saves on its own.

Pure (bytes in, dict out) so it is unit-tested without a server.

AA-1080 (S277): the import also fills the three brand-pack fields it used to leave empty on every brief
(AA-741 phase 3, 4 of 4 audit brands):
- `system_prompt` (the form's "Brand context"): a "Brand context" / "Instructions for the writer" section
  when the brief has one, else a short brand summary drafted from the brief's own fields;
- `default_cta`: a "Call to action" section or label, else the brief's own call-to-action sentence, else a
  neutral line naming the brand;
- `signature_words`: a "Signature words" section or label, short quoted phrases under Should Write /
  Good Example, and the positioning words the brief declares as its own (AA-800). Never invented: none
  found = an empty list, reported on the review screen.
A drafted value is listed in `drafted` so the review screen says "drafted from your brief, check it".
A section heading the parser does not know is no longer folded into the previous section: its text is
kept out of the form and listed in `unmapped_fields` (heading + size) for the review screen.
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
    # AA-1080 — the three brand-pack fields the import used to leave empty.
    "brand context": "context", "context": "context", "system prompt": "context",
    "instructions for the writer": "context", "writer instructions": "context", "brief for the writer": "context",
    "call to action": "cta", "call-to-action": "cta", "calls to action": "cta", "cta": "cta",
    "default cta": "cta", "closing line": "cta", "sign-off": "cta", "sign off": "cta",
    "signature words": "signature", "signature vocabulary": "signature", "brand vocabulary": "signature",
    "words we use": "signature", "words to use": "signature", "our words": "signature",
}
# Inline labels inside a section ("Brand type: …", "Primary markets: …").
_LABELS = {
    "brand type": "brand_type", "core idea": "core_idea",
    "primary markets": "markets", "markets": "markets", "target markets": "markets",
    "customer segment": "customer_segment", "customer mindset": "customer_mindset",
    # AA-1080
    "call to action": "cta", "cta": "cta", "default cta": "cta",
    "signature words": "signature", "brand context": "context",
}
_KEY_RE = re.compile(r"\bcis_[A-Za-z0-9_\-]{20,}\b")
_QUOTED_RE = re.compile(r"[“\"]([^”\"]{2,60})[”\"]")
_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z ]{1,30}):\s*(.*)$")
# AA-1080 — a sentence of the brief that asks the reader to act (the brand's own call to action).
_CTA_RE = re.compile(r"\b(enquire|inquire|get in touch|contact us|talk to|speak (?:to|with)|plan your|"
                     r"start planning|book a call|request a quote|ask us|write to us|reach out)\b", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]?")
# A heading-like paragraph: short, no sentence inside, ends with ":" (a brief typed without Word styles).
_LOOSE_HEADING_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 &/'()\-]{1,48}:$")
_SIGNATURE_MAX = 20
_SIGNATURE_WORD_MAX = 40


def _lines(text: str) -> list[str]:
    return [x.strip() for x in text.splitlines() if x.strip()]


def parse_brief_paragraphs(paragraphs: list[str], heading_flags: list[bool] | None = None) -> dict:
    """Paragraph texts (first = brand name) → {"fields", "warnings", "drafted", "unmapped_fields"}.

    `heading_flags` (same length as `paragraphs`, from the .docx paragraph styles) marks the paragraphs
    Word styles as a heading; a styled heading the parser does not know opens an unmapped section."""
    warnings: list[str] = []
    flags = list(heading_flags) if heading_flags is not None else [False] * len(paragraphs)
    flags += [False] * (len(paragraphs) - len(flags))
    pairs = [(p.strip(), f) for p, f in zip(paragraphs, flags) if p and p.strip()]
    if any(_KEY_RE.search(p) for p, _ in pairs):
        warnings.append("The brief contains what looks like an API key. It was ignored — remove it "
                        "from the document, it should not be shared.")
    pairs = [(_KEY_RE.sub("", p).strip(), f) for p, f in pairs]
    pairs = [(p, f) for p, f in pairs if p]
    if not pairs:
        return {"fields": {}, "warnings": ["The document is empty."], "drafted": [], "unmapped_fields": []}
    paras = [p for p, _ in pairs]

    sections: dict[str, list[str]] = {}
    labels: dict[str, list[str]] = {}
    unmapped: dict[str, list[str]] = {}
    current, current_label = None, None
    for p, styled in pairs[1:]:
        head = _HEADINGS.get(p.lower().rstrip(":").strip())
        if head:
            current, current_label = head, None
            continue
        label_head = _LABELS.get(p.lower().rstrip(":").strip())
        if label_head and (styled or p.endswith(":")):
            # a label typed as its own heading ("Core idea" in a Word heading style): its section is the value
            current, current_label = "label:" + label_head, None
            continue
        if styled or _LOOSE_HEADING_RE.match(p):
            # AA-1080: a heading we do not know — keep its text out of the previous section.
            name = p.rstrip(":").strip()
            current, current_label = "unmapped:" + name, None
            unmapped.setdefault(name, [])
            continue
        if current and current.startswith("unmapped:"):
            unmapped[current.split(":", 1)[1]].extend(_lines(p))
            continue
        section_label = current.split(":", 1)[1] if current and current.startswith("label:") else None
        for line in _lines(p):
            m = _LABEL_RE.match(line)
            lab = _LABELS.get(m.group(1).strip().lower()) if m else None
            if lab:
                current_label = lab
                if m.group(2).strip():
                    labels.setdefault(lab, []).append(m.group(2).strip())
                continue
            if section_label and not current_label:
                labels.setdefault(section_label, []).append(line)
            elif current_label and (current in ("identity", "market") or section_label):
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

    # AA-1080 — Brand context, default CTA, signature words.
    drafted: list[str] = []
    context = "\n".join(x for x in [lab("context"), text("context")] if x).strip()
    if not context:
        context = draft_brand_context(fields)
        if context:
            drafted.append("system_prompt")
    fields["system_prompt"] = context

    cta = " ".join(x for x in [lab("cta"), text("cta")] if x).strip()
    if not cta:
        cta = brief_cta_sentence([text("good"), do, text("identity"), style])
        if not cta:
            cta = draft_default_cta(fields["brand_name"])
        if cta:
            drafted.append("default_cta")
    fields["default_cta"] = cta

    explicit = _word_list([*labels.get("signature", []), *sections.get("signature", [])])
    quoted = [q for q in _short_quotes([*sections.get("should", []), *sections.get("good", [])])]
    declared = _declared_positioning(fields)
    taken = {w.lower() for w in fields["forbidden_words"]}
    words = [w for w in dict.fromkeys([*explicit, *quoted, *declared]) if w.lower() not in taken]
    fields["signature_words"] = words[:_SIGNATURE_MAX]
    if words and not explicit:
        drafted.append("signature_words")
    if not words:
        warnings.append("No signature words were found in the brief — add the words your brand uses as "
                        "its own, or leave the list empty.")

    unmapped_fields = [{"heading": h, "chars": sum(len(x) for x in lines)} for h, lines in unmapped.items()]
    if unmapped_fields:
        warnings.append("Some sections of the brief were not imported: "
                        + ", ".join(u["heading"] for u in unmapped_fields)
                        + ". Copy what you need into the form before saving.")
    return {"fields": fields, "warnings": warnings, "drafted": drafted, "unmapped_fields": unmapped_fields}


def draft_brand_context(fields: dict) -> str:
    """A short brand summary built only from the brief's own fields (no invented text): who the brand is,
    its core idea, its readers and its voice. Empty when the brief names nothing beyond the brand."""
    name = (fields.get("brand_name") or "").strip()
    kind = (fields.get("brand_type") or "").strip()
    lines = []
    if name or kind:
        lines.append(f"Write as {name or 'the brand'}" + (f", {_lower_first(kind)}." if kind else "."))
    for label, key in (("Core idea", "core_idea"), ("Readers", "customer_segment"),
                       ("What readers want", "customer_mindset")):
        value = " ".join((fields.get(key) or "").split())
        if value:
            lines.append(f"{label}: {value}")
    voice = [v for v in (fields.get("tone_of_voice") or []) if v]
    if voice:
        lines.append("Voice: " + ", ".join(voice) + ".")
    if len(lines) <= 1 and not kind:
        return ""
    from shared.validators.prompt_sanitize import MAX_SYSTEM_PROMPT_LEN
    return "\n".join(lines)[:MAX_SYSTEM_PROMPT_LEN]


def brief_cta_sentence(texts: list[str]) -> str:
    """The first sentence of the brief that asks the reader to act (its own call to action), or ""."""
    for block in texts:
        for line in _lines(block or ""):
            for m in _SENTENCE_RE.finditer(line):
                sentence = m.group(0).strip().strip("“”\"").strip()
                if _CTA_RE.search(sentence) and 3 <= len(sentence.split()) <= 30 \
                        and not re.match(r"^(do not|don't|never|avoid)\b", sentence, re.IGNORECASE):
                    return sentence
    return ""


def draft_default_cta(brand_name: str) -> str:
    """A neutral call to action naming the brand: no promise, no service level (F17 / F19 safe)."""
    name = (brand_name or "").strip()
    return f"Get in touch with {name} to plan this trip." if name else ""


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def _word_list(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        for part in re.split(r"[,;\n]", line):
            w = part.strip(" .;:•-–\"“”'")
            if w and len(w) <= _SIGNATURE_WORD_MAX:
                out.append(w)
    return list(dict.fromkeys(out))


def _short_quotes(lines: list[str]) -> list[str]:
    """Quoted phrases of 1 to 3 words with no sentence inside: the brand's own words, not an example line."""
    out = []
    for line in lines:
        for q in _QUOTED_RE.findall(line):
            q = q.strip(" ,;")
            if 1 <= len(q.split()) <= 3 and not re.search(r"[.!?]", q):
                out.append(q)
    return out


def _declared_positioning(fields: dict) -> list[str]:
    """AA-800 — the positioning words the brief itself declares (signature_vocabulary.signature_words)."""
    from services.acp_shared.signature_vocabulary import signature_words
    return signature_words({
        "brand_type": fields.get("brand_type"), "core_idea": fields.get("core_idea"),
        "voice": fields.get("tone_of_voice"), "style_guide": fields.get("style_guide"),
        "good_examples": fields.get("good_examples"), "forbidden_words": fields.get("forbidden_words"),
    })


def parse_brief_docx(content: bytes) -> dict:
    from docx import Document  # python-docx
    doc = Document(io.BytesIO(content))
    paras = [p.text for p in doc.paragraphs]
    # AA-1080: Word heading styles mark the sections, so an unknown section is reported, not merged.
    flags = [_is_heading_style(p) for p in doc.paragraphs]
    for table in doc.tables:                       # briefs written as a 2-column table
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            paras.extend(cells)
            flags.extend([False] * len(cells))
    return parse_brief_paragraphs(paras, flags)


def _is_heading_style(paragraph) -> bool:
    try:
        name = (paragraph.style.name or "").lower()
    except Exception:
        return False
    return name.startswith("heading") or name == "title"
