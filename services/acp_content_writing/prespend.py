"""AA-1049 W3 (root ADR 0020 "Plan" step, D2) — pre-spend angle tripwires, REPORT mode.

Before the paid T9 write, the chosen angle (the brief: name, why it works, formula fit, best final style) is read
against the source the gates will use (the FactSheet render). D2: the brief is an instruction, never a source, so a
concrete trip promise or a number in the angle that the source does not carry is something the writer will be pushed
to state and the gates will then flag.

This step only RECORDS what it finds (`writer_input.prespend`, admin-only) and changes nothing: rejecting or
re-picking an angle needs a measured precision for these tripwires on briefs (W2 labels), and the pre-spend
near-duplicate check needs an embedding call and an angle-text threshold that has not been calibrated (the F10
threshold is for whole pieces). Both stay out until measured; the record is the measurement input.

Pure: no model call, no DB.
"""
from __future__ import annotations

import re
from typing import Optional

from services.acp_content_writing import claim_rules
from services.acp_content_writing.hygiene import fold

MODE = "report"
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_DIGITS_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _angle_parts(angle: Optional[dict]) -> list[tuple[str, str]]:
    out = []
    for key in ("name", "why_it_works", "formula_fit", "best_final_style"):
        text = " ".join(str((angle or {}).get(key) or "").split())
        out += [(key, s.strip()) for s in _SENTENCE_RE.split(text) if s.strip()]
    return out


def angle_tripwires(angle: Optional[dict], source_text: Optional[str]) -> dict:
    """{"mode": "report", "sentences": n, "findings": [{field, sentence, wires, promise_words, numbers}]}: each angle
    sentence with a concrete trip word (claim_rules.CONCRETE_TRIP_RE) or a number the source does not contain."""
    src = fold(source_text or "")
    src_words = set(re.findall(r"[a-z0-9]+", src))
    findings = []
    parts = _angle_parts(angle)
    for field, sentence in parts:
        promise = sorted({m.group(0).lower() for m in claim_rules.CONCRETE_TRIP_RE.finditer(sentence)
                          if not set(re.findall(r"[a-z0-9]+", fold(m.group(0)))) <= src_words})
        numbers = sorted({n for n in _DIGITS_RE.findall(sentence) if n not in src})
        numbers += claim_rules.find_novel_word_numbers(sentence, [source_text or ""])
        if promise or numbers:
            findings.append({"field": field, "sentence": sentence[:200],
                             "wires": sorted(claim_rules.tripwires(sentence)),
                             "promise_words": promise, "numbers": numbers})
    return {"mode": MODE, "sentences": len(parts), "findings": findings}
