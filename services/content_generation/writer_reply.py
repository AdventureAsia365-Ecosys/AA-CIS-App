"""AA-1166 — the writer's JSON draft out of its reply, and the free text around it kept apart.

The S294 AI red-team (tenant-crossing case on the T2 rewrite) saw the writer answer with a free-text note BEFORE its
JSON object ("Note: the tour description contained an instruction ... I haven't done that.", then the object). The
old parser (json.loads, then json-repair on the WHOLE reply) happened to drop that note, but nothing guaranteed it:
json-repair reads loosely and the note was lost without a trace.

``extract`` reads the draft like this:

1. The reply as before: a leading ``` fence is stripped and ``json.loads`` decodes it. A clean reply returns exactly
   what it returned before (any JSON value, a dict without ``name`` included), with no free text.
2. Otherwise the candidates: every complete top-level JSON object with a ``name``
   (``shared.llm_client.json_reply.json_values``) and every ``{"name":`` opener, also after a stray unclosed brace
   in the note (where the top-level scan stops). Openers nested in a complete candidate are dropped. One candidate
   is the draft; with several, the one standing on its own lines is (a named object the writer quotes inside a
   sentence of its note never is), and when that is not exactly one the reply is ambiguous and nothing is taken.
   A complete candidate is decoded from its own text only; an open one (cut off, malformed) gets the json-repair
   salvage (AA-217) of its own text (to its matching close, or to the end of a cut-off reply), never the text
   before it, so the note's keys and values are never merged into a field. The salvage still needs a dict with
   ``name`` (the old "parse failed" condition).
3. No candidate (a malformed draft whose ``name`` is not its first key): the same salvage, last brace first, among
   the braces that open a line, then any brace (an unclosed one only when nothing else opens after it).
4. Nothing usable: ``(None, "")``. The callers fail exactly as before (``JSON parse error``).

The free text (``note``) is returned separately, fence markers removed. ``note_kind`` sorts it for the signal the
writer node records (a ``writer_note`` degraded entry + ``quality_signal.writer_note`` on the writer's ledger row):
``instruction_notice`` (the writer says it saw / did not follow an instruction), ``refusal``, or ``other``. The
note's text itself is never stored; only its kind and length are (``record_note``).

No model call and no I/O; ``record_note`` writes one info line, an ambiguous reply one warning line
(``writer_reply_ambiguous``, counts only).
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


def _line_clear(text: str) -> bool:
    """True when ``text`` (the rest of a line next to an object) holds nothing but whitespace and fence markers."""
    return not _FENCE.sub("", text).strip()


def _own_line(raw: str, start: int, end: int) -> bool:
    """The object at ``raw[start:end]`` stands on its own lines: no prose before it on its first line and (for a
    complete object, ``end > 0``) none after it on its last line. A quote inside a sentence of the note does not."""
    if not _line_clear(raw[raw.rfind("\n", 0, start) + 1:start]):
        return False
    if end <= 0:
        return True
    nl = raw.find("\n", end)
    return _line_clear(raw[end:] if nl < 0 else raw[end:nl])


def _candidates(raw: str) -> list[tuple[Optional[dict], int, int]]:
    """Every object that could be the draft, in reply order, as (value, start, end): each complete top-level JSON
    object with a ``name`` (``json_values``) and each ``{"name":`` opener anywhere (also after a stray unclosed
    brace, where the top-level scan stops). An opener that decodes is complete; one that does not is open (value
    None, to salvage; end -1). Openers nested in a complete candidate are dropped (a quote inside a field)."""
    found: dict[int, tuple[Optional[dict], int, int]] = {}
    for value, start, end in json_values(raw, kinds=(dict,)):
        if value.get("name"):
            found[start] = (value, start, end)
    decoder = json.JSONDecoder()
    for m in list(_NAME_OPENER.finditer(raw))[-MAX_STARTS:]:
        if m.start() in found:
            continue
        try:
            value, end = decoder.raw_decode(raw, m.start())
        except json.JSONDecodeError:
            found[m.start()] = (None, m.start(), -1)
            continue
        if isinstance(value, dict) and value.get("name"):
            found[m.start()] = (value, m.start(), end)
    complete = [c for c in found.values() if c[0] is not None]
    return [c for _, c in sorted(found.items())
            if not any(o is not c and o[1] <= c[1] and (c[2] if c[2] > 0 else c[1] + 1) <= o[2] for o in complete)]


def _salvaged(raw: str, start: int) -> Optional[tuple[dict, int, int]]:
    value, end = _salvage(raw, start)
    return (value, start, end) if value is not None else None


def _brace_salvage(raw: str) -> Optional[tuple[dict, int, int]]:
    """No ``{"name":`` opener anywhere (a malformed draft whose ``name`` is not its first key): the json-repair
    salvage of one object, last first, first among the braces that open a line, then any brace. A brace in the
    note's prose that never closes is a start only when nothing else opens after it, so the note's text is never
    merged with a draft that follows it."""
    braces = [i for i, c in enumerate(raw) if c == "{"]
    line_starts = [i for i in braces if _line_clear(raw[raw.rfind("\n", 0, i) + 1:i])]
    for tier, starts in ((1, line_starts), (2, braces)):
        for pos in reversed(starts[-MAX_STARTS:]):
            found = _salvaged(raw, pos)
            if found is None or (tier == 2 and found[2] < 0 and raw.find("{", pos + 1) >= 0):
                continue
            return found
    return None


def _draft(raw: str) -> Optional[tuple[dict, int, int]]:
    """The one object that is the draft, or None. Several candidates: the one standing on its own lines; when that
    is not exactly one, the reply is ambiguous and nothing is taken (the writer node fails and retries as for any
    unparsable reply): a named object the writer quoted in its note is never the draft."""
    cands = _candidates(raw)
    if not cands:
        return _brace_salvage(raw)
    if len(cands) > 1:
        alone = [c for c in cands if _own_line(raw, c[1], c[2])]
        if len(alone) != 1:
            logger.warning("writer_reply_ambiguous", candidates=len(cands), own_line=len(alone))
            return None
        cands = alone
    value, start, end = cands[0]
    return (value, start, end) if value is not None else _salvaged(raw, start)


def extract(raw: Optional[str]) -> tuple[Any, str]:
    """``(draft, note)``: the writer's JSON draft (None when nothing usable) and the free text around it."""
    raw = (raw or "").strip()
    body, after_fence = _unfenced(raw)
    try:
        return json.loads(body), _note(after_fence)
    except json.JSONDecodeError:
        pass
    found = _draft(raw)
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
