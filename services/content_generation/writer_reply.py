"""AA-1166 — the writer's JSON draft out of its reply, and the free text around it kept apart.

The S294 AI red-team (tenant-crossing case on the T2 rewrite) saw the writer answer with a free-text note BEFORE its
JSON object ("Note: the tour description contained an instruction ... I haven't done that.", then the object). The
old parser (json.loads, then json-repair on the WHOLE reply) happened to drop that note, but nothing guaranteed it:
json-repair reads loosely and the note was lost without a trace.

``extract`` reads the draft like this:

1. The reply as before: a leading ``` fence is stripped and ``json.loads`` decodes it. A clean reply returns exactly
   what it returned before (any JSON value, a dict without ``name`` included), with no free text.
2. Otherwise the first complete top-level JSON object with a ``name`` (``shared.llm_client.json_reply.json_values``):
   only the object's own text is decoded, so text before or after it never reaches a field.
3. Otherwise the json-repair salvage (AA-217) of ONE object's text: from a top-level opening brace to its matching
   close (or to the end of a cut-off reply), never the text before it. The salvage still needs a dict with ``name``
   (the old "parse failed" condition). Last resort: the first ``{"name":`` opener, for a reply whose prose holds an
   unclosed brace.
4. Nothing usable: ``(None, "")``. The callers fail exactly as before (``JSON parse error``).

The free text (``note``) is returned separately, fence markers removed. ``note_kind`` sorts it for the signal the
writer node records (a ``writer_note`` degraded entry + ``quality_signal.writer_note`` on the writer's ledger row):
``instruction_notice`` (the writer says it saw / did not follow an instruction), ``refusal``, or ``other``. The
note's text itself is never stored; only its kind and length are (``record_note``).

No model call and no I/O; ``record_note`` writes one info line.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import structlog
from json_repair import repair_json

from services.quality import degraded
from shared.llm_client.json_reply import MAX_STARTS, bracket_end, json_values

logger = structlog.get_logger()

NOTE_INSTRUCTION = "instruction_notice"
NOTE_REFUSAL = "refusal"
NOTE_OTHER = "other"

_FENCE = re.compile(r"```(?:json)?", re.IGNORECASE)
_WORD = re.compile(r"\w")
_NAME_OPENER = re.compile(r'\{\s*"name"\s*:')
# Plain alternations only (no nested quantifiers): CodeQL py/redos.
_INSTRUCTION = re.compile(
    r"\b(?:instruction|instructions|instructed|ignore|ignored|ignoring|disregard|injection|injected|prompt)\b"
    r"|\b(?:not|never|n't)\s+(?:follow|followed|comply|complied|include|included|add|added|do|done)\b"
    r"|\bhaven't\s+(?:done|followed|included|added)\b",
    re.IGNORECASE,
)
_REFUSAL = re.compile(
    r"\bI\s+(?:cannot|can't|can not|won't|will not|am unable|'m unable|must decline)\b"
    r"|\bunable to\b|\bsorry\b|\bdeclin(?:e|ed|ing)\b",
    re.IGNORECASE,
)


def _note(*parts: str) -> str:
    """The free text around the object: fence markers removed, whitespace trimmed; empty when no word is left."""
    text = " ".join(_FENCE.sub(" ", p or "").strip() for p in parts).strip()
    return text if _WORD.search(text) else ""


def note_kind(note: str) -> Optional[str]:
    """``instruction_notice`` / ``refusal`` / ``other`` for a non-empty note, None for none."""
    if not note:
        return None
    if _INSTRUCTION.search(note):
        return NOTE_INSTRUCTION
    if _REFUSAL.search(note):
        return NOTE_REFUSAL
    return NOTE_OTHER


def _salvage(raw: str, start: int) -> tuple[Optional[dict], int]:
    """json-repair of the one object opening at ``start`` (to its close, or to the end of a cut-off reply)."""
    end = bracket_end(raw, start)
    chunk = raw[start:end] if end > 0 else raw[start:]
    salvaged = repair_json(chunk, return_objects=True)
    if isinstance(salvaged, dict) and salvaged.get("name"):
        return salvaged, end
    return None, end


def _unfenced(raw: str) -> tuple[str, str]:
    """The old fence strip: (the fenced body, the text after the closing fence). A reply without a leading fence is
    its own body."""
    if not raw.startswith("```"):
        return raw, ""
    parts = raw.split("```")
    body = parts[1]
    if body.startswith("json"):
        body = body[4:]
    return body.strip(), "```".join(parts[2:])


def _first_named(raw: str) -> Optional[tuple[dict, int, int]]:
    """The first complete top-level JSON object with a ``name``, with its span."""
    for value, start, end in json_values(raw, kinds=(dict,)):
        if value.get("name"):
            return value, start, end
    return None


def _salvaged(raw: str) -> Optional[tuple[dict, int, int]]:
    """The first top-level object json-repair turns into a dict with a ``name``, then the first ``{"name":``
    opener; the span's end is -1 for a cut-off reply."""
    tried: set[int] = set()
    pos = raw.find("{")
    for _ in range(MAX_STARTS):
        if pos < 0:
            break
        tried.add(pos)
        value, end = _salvage(raw, pos)
        if value is not None:
            return value, pos, end
        if end < 0:
            break
        pos = raw.find("{", end)
    m = _NAME_OPENER.search(raw)
    if m and m.start() not in tried:
        value, end = _salvage(raw, m.start())
        if value is not None:
            return value, m.start(), end
    return None


def extract(raw: Optional[str]) -> tuple[Any, str]:
    """``(draft, note)``: the writer's JSON draft (None when nothing usable) and the free text around it."""
    raw = (raw or "").strip()
    body, after_fence = _unfenced(raw)
    try:
        return json.loads(body), _note(after_fence)
    except json.JSONDecodeError:
        pass
    found = _first_named(raw) or _salvaged(raw)
    if found is None:
        return None, ""
    value, start, end = found
    return value, _note(raw[:start], raw[end:] if end > 0 else "")


def record_note(state: dict, note: str, kind: str, stage: str) -> dict:
    """The writer put free text around its JSON draft (dropped from the draft). Signal it on the output (a
    ``writer_note`` degraded entry, severity ``count``: the T2 run carries it into its ``t3.degraded`` diagnostics,
    A1 into ``generated_content.metadata.degraded``) and in one info line. Only the kind and the length are kept:
    the note can quote the source or a planted instruction, so its text is stored nowhere. Returns a new state."""
    tour = state.get("tour") or {}
    logger.info("writer_reply_note_dropped", kind=kind, note_chars=len(note), stage=stage,
                tour_id=tour.get("tour_id") or state.get("tour_id"), tenant_id=state.get("tenant_id"),
                retry=state.get("retry_count", 0))
    return {**state, "degraded": degraded.add(state.get("degraded"), degraded.WRITER_NOTE,
                                              detector="writer_reply", detail=kind)}


__all__ = ["NOTE_INSTRUCTION", "NOTE_OTHER", "NOTE_REFUSAL", "extract", "note_kind", "record_note"]
