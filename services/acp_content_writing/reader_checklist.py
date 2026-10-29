"""AA-790 round 10 (S257) — a reader-value checklist in the T9 writer prompt, built from the content seed. $0, no LLM.

Round 9 (brief grounding, S256) passed source fidelity (+0.50 against its control) but failed reader value (-0.25):
the judges asked the same questions on most pieces and the pieces did not answer them, although the seed did. "It never
says when or where in the trip the visit happens" (3 of 3 family-brand LinkedIn posts; the seed has a "Day 6 of the
trip" line); "nothing practical about the visit itself"; "the visa appears only inside an FAQ answer"; the same falls
facts repeated three times; meta lines ("This is the longest and richest section ..."). Round 10 keeps the grounding and
gives the writer the traveller's questions with the seed's own answers next to them, so the practical details reach the
piece without a single new fact.

What the block holds (all of it read from the seed and its FACTS section in code, quoted word for word):
  - When and where: the day number and name of every moment ("Day 6: Traditional School of Arts and Craft"); no day
    line at all -> "the seed gives no day number: do not give one".
  - What the traveller does at each stop: its Source itinerary lines (a thin moment is left out: its own seed note
    already limits it to one sentence). A social post gets the moment's label action instead (see social_checklist).
  - Included or a choice: the moments whose Status line says OFFERED, NOT INCLUDED.
  - Practical questions (blog only): getting there and between stops, how long and how hard, meals and nights, costs,
    visas and booking, best time to go. Each one quotes the itinerary sentences / FACTS entries that answer it, or says
    "not in the seed: leave it out, never guess it". Place background is never quoted here: it says nothing about
    what the trip does (the seed's own heading).
  - Two rules on the shape: say each fact once (no fact repeated across sections), no sentence about the piece.
A social post (150-220 words) gets the short form: one practical line (when + what the traveller does), and the
questions the seed does not answer stay out.

Mode switch: api/worker env ``T9_READER_CHECKLIST_MODE`` = ``r10`` turns it on; unset (default) or any other value is
``off`` and the writer prompt is byte for byte what it was (test_aa790_round10_reader_checklist pins the round-9
192-prompt fixture hash, with and without the grounding). The A/B harness passes the mode explicitly
(``build_user_prompt(reader_checklist=...)``), so every arm runs on one deployed image with the env unset.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from services.acp_content_writing.hygiene import split_seed
from services.acp_content_writing.trip_facts import parse_moments
from services.acp_shared.offered import OFFERED, is_status_line, status_of_line

MODE_ENV = "T9_READER_CHECKLIST_MODE"
MODE_OFF = "off"
MODE_R10 = "r10"
MODES = (MODE_OFF, MODE_R10)

QUOTE_MAX = 180            # characters of one quoted seed sentence
QUOTES_PER_TOPIC = 3       # seed sentences quoted under one practical question
DOES_PER_STOP = 2          # itinerary lines quoted for one stop

_SOURCE_PREFIX = "Source itinerary:"
_LABEL_SPLIT_RE = re.compile(r"\s+[—–]\s+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])")
_FACT_HEAD_RE = re.compile(r"^\[Fact id=[^\]]+\]\s*$")
_OFFERED_QUOTE_RE = re.compile(r"the itinerary says:\s*\"([^\"]+)\"")

# The practical questions of the blog form, in order. A sentence is quoted under every question it answers.
TOPICS: tuple[tuple[str, str, re.Pattern], ...] = (
    ("getting", "Getting there and between stops", re.compile(
        r"\b(?:bicycles?|bikes?|biking|cycl\w*|rides?|riding|drives?|driving|driven|boats?|ferry|ferries|flights?|fly"
        r"|trains?|bus|buses|walk\w*|trek\w*|hik\w*|transfers?|tarmac|roads?|tracks?|trails?|kilomet\w*|km|miles?)\b",
        re.I)),
    ("effort", "How long and how hard", re.compile(
        r"\b(?:\d+(?:\s*[–-]\s*\d+)?\s*(?:hours?|hrs?|minutes?|mins?)|half[- ]day|full[- ]day|fitness|difficult\w*"
        r"|easy|moderate|challenging|strenuous|steep\w*|demanding|physically|gentle|acclimati\w*)\b", re.I)),
    ("stay", "Meals and nights", re.compile(
        r"\b(?:breakfast|lunch|dinner|meals?|homestays?|camps?|camping|hotels?|lodges?|guest ?houses?|overnight"
        r"|nights?|sleep\w*|accommodation)\b", re.I)),
    ("costs", "Costs, visas and booking", re.compile(
        r"(?:\b(?:prices?|costs?|fees?|usd|eur|visas?|e-visas?|permits?|deposits?|book\w*)\b|[$€£]\s?\d)", re.I)),
    ("season", "Best time to go", re.compile(
        r"\b(?:seasons?|seasonal|monsoon|rain\w*|dry|weather|temperatures?|january|february|march|april|june|july"
        r"|august|september|october|november|december|best time)\b", re.I)),
)


def reader_checklist_mode(value: Optional[str] = None) -> str:
    """The mode: `value` when given (the A/B harness), else the env switch. Only 'r10' turns round 10 on; every other
    value, unset included, is 'off'."""
    raw = os.environ.get(MODE_ENV, "") if value is None else value
    return MODE_R10 if (raw or "").strip().lower() == MODE_R10 else MODE_OFF


@dataclass
class Stop:
    """One moment of the seed as the checklist reads it."""
    name: str
    action: str = ""                                   # the label's action part ('visit', 'cycle by bicycle …')
    days: list[int] = field(default_factory=list)
    does: list[str] = field(default_factory=list)      # Source itinerary texts, prefix removed
    offered: list[str] = field(default_factory=list)   # what its OFFERED Status lines quote from the itinerary
    thin: bool = False


def _quote(text: str) -> str:
    s = " ".join((text or "").split())
    if len(s) > QUOTE_MAX:
        cut = s[:QUOTE_MAX].rsplit(" ", 1)[0].rstrip(",;:—– ")
        s = cut + " …"
    return f"\"{s}\""


def _itinerary_text(line: str) -> Optional[str]:
    """The Source itinerary text of a trip line ('Source itinerary: X' or 'Day 2 of the trip: … — Source itinerary:
    X'); None for a label / Why it matters / bare Day line."""
    if _SOURCE_PREFIX not in line:
        return None
    return line.split(_SOURCE_PREFIX, 1)[1].strip() or None


def _activity(line: str) -> str:
    """What a trip line says the traveller does: its itinerary text, else the label's action part."""
    text = _itinerary_text(line)
    if text:
        return text
    parts = _LABEL_SPLIT_RE.split(line, 1)
    return (parts[1] if len(parts) > 1 else line).strip()


def _offered_by_block(seed: Optional[str]) -> list[list[str]]:
    """Per moment block (the blocks `trip_facts.parse_moments` reads, same order), the itinerary sentences its
    'Status: OFFERED, NOT INCLUDED (the itinerary says: "…")' lines quote (pure). parse_moments keeps only the status
    of a line, not the quote."""
    out: list[list[str]] = []
    for block in re.split(r"\n\s*\n", split_seed(seed)[0]):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if not lines or not _LABEL_SPLIT_RE.search(lines[0]) or lines[0].startswith(("Location:", "- ")):
            continue
        quotes = []
        for ln in lines[1:]:
            if is_status_line(ln) and status_of_line(ln) == OFFERED:
                got = _OFFERED_QUOTE_RE.search(ln)
                if got and got.group(1).strip() not in quotes:
                    quotes.append(got.group(1).strip())
        out.append(quotes)
    return out


def stops(seeds: list[str]) -> list[Stop]:
    """The seed moments of every seed text, in order (pure)."""
    out: list[Stop] = []
    for seed in seeds:
        moments = parse_moments(seed)
        offered_quotes = _offered_by_block(seed)
        if len(offered_quotes) != len(moments):  # never expected; a misaligned list must not mislabel a moment
            offered_quotes = [[] for _ in moments]
        for m, offered in zip(moments, offered_quotes):
            does: list[str] = []
            for line in m.trip_lines:
                text = _itinerary_text(line)
                if text and text not in does:
                    does.append(text)
            action = _activity(m.trip_lines[0]) if m.trip_lines else ""
            out.append(Stop(name=m.name, action=action, days=sorted(set(m.days)), does=does, offered=offered,
                            thin=m.thin))
    return out


def fact_entries(facts_text: Optional[str]) -> list[str]:
    """The FACTS entries ('<title>: <body>'), one per [Fact id=…] block (pure)."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", facts_text or ""):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        body = " ".join(ln for ln in lines if not _FACT_HEAD_RE.match(ln))
        if body:
            out.append(body)
    return out


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_RE.split(text or "") if s.strip()]


def _when_line(all_stops: list[Stop]) -> str:
    if not any(s.days for s in all_stops):
        return "the seed gives no day number: do not give one"
    by_day: dict[int, list[str]] = {}
    for s in all_stops:
        for d in s.days:
            if s.name not in by_day.setdefault(d, []):
                by_day[d].append(s.name)
    return "; ".join(f"Day {d}: {', '.join(names)}" for d, names in sorted(by_day.items()))


def _does_line(all_stops: list[Stop]) -> str:
    parts = []
    for s in all_stops:
        if s.thin or not s.does:
            continue
        parts.append(f"{s.name}: " + " ".join(_quote(t) for t in s.does[:DOES_PER_STOP]))
    return " | ".join(parts)


def topic_answers(all_stops: list[Stop], facts: list[str]) -> dict[str, list[str]]:
    """{topic key: quoted seed sentences / FACTS entries that answer it}, a sentence under every topic it answers, at
    most QUOTES_PER_TOPIC each (pure): 'A short sunset ride … a gauge of fitness levels' answers both 'getting there'
    and 'how hard', and a topic must never read "not in the seed" while the seed answers it. Only itinerary sentences
    of non-thin stops and FACTS entries; never Place background."""
    out: dict[str, list[str]] = {key: [] for key, _label, _re in TOPICS}
    items = [t for s in all_stops if not s.thin for d in s.does for t in _sentences(d)] + facts
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        for key, _label, pattern in TOPICS:
            if pattern.search(item) and len(out[key]) < QUOTES_PER_TOPIC:
                out[key].append(_quote(item))
    return out


_BLOG_HEAD = ("READER CHECKLIST (the questions a traveller asks before booking, with the CONTENT SEED's and FACTS' own "
              "answers quoted after each one. Answer every question that has an answer, in plain practical words, in "
              "the section where the reader needs it; rephrase the quotes, never add to them. A question marked \"not "
              "in the seed\" stays out of the piece: never guess it, hint at it or say that it is unknown):")
_BLOG_TAIL = ("- Say each fact once: no fact, number or description repeated in a second section or in the FAQ. Never "
              "write a sentence about the piece itself (\"this section\", \"worth reading\", \"the longest section\").")
_NOT_IN_SEED = "not in the seed: leave it out, never guess it"


def blog_checklist(all_stops: list[Stop], facts: list[str], search_questions: Optional[list[str]] = None) -> str:
    lines = [_BLOG_HEAD, f"- When and where: {_when_line(all_stops)}."]
    does = _does_line(all_stops)
    if does:
        lines.append(f"- What the traveller does at each stop: {does}")
    offered = [o for s in all_stops for o in s.offered]
    if offered:
        lines.append("- Included or a choice: everything above is part of the trip except these, which are a choice "
                     "the traveller can make: " + "; ".join(_quote(o) for o in offered))
    answers = topic_answers(all_stops, facts)
    for key, label, _re in TOPICS:
        got = answers[key]
        lines.append(f"- {label}: " + ("; ".join(got) if got else _NOT_IN_SEED))
    if facts:
        lines.append("- The FACTS entries (visa, season, price, transfer time) are what a planner looks for: give each "
                     "one used a plain sentence in the body, not only in the FAQ.")
    if search_questions:
        lines.append("- The SEARCH QUESTIONS above are what readers ask: the FAQ answers the ones the seed answers, "
                     "and the body covers the practical answers in this checklist.")
    lines.append(_BLOG_TAIL)
    return "\n".join(lines)


def social_checklist(all_stops: list[Stop]) -> str:
    """The short form. 'What the traveller does' is each moment's own label action ('visit', 'cycle by bicycle on the
    mountain road'), not its itinerary line: a one-moment post is scoped to that moment, and the itinerary line often
    lists the day's other stops ('visit the Takin Zoo, National Library, and …'), which the SCOPE line forbids."""
    when = _when_line(all_stops)
    named = [s for s in all_stops if s.action]
    actions = (_quote(named[0].action) if len(named) == 1
               else "; ".join(f"{s.name}: {_quote(s.action)}" for s in named))
    parts = [f"when it happens ({when})"]
    if actions:
        parts.append(f"what the traveller does there ({actions})")
    line = ("READER CHECKLIST (one practical line the reader can act on, from the CONTENT SEED only; rephrase the "
            "quotes, never add to them): say " + " and ".join(parts) + ".")
    offered = [o for s in all_stops for o in s.offered]
    if offered:
        line += " A choice, not part of the plan: " + "; ".join(_quote(o) for o in offered) + "."
    return line + (" Questions the seed does not answer (how long, how hard, ages, price) stay out of the post: never "
                   "guess them. Never write a sentence about the post itself.")


def checklist_block(mode: Optional[str], *, content_seed: Optional[str], channel: str,
                    route_segments: Optional[list[tuple[str, str]]] = None, facts_text: Optional[str] = None,
                    search_questions: Optional[list[str]] = None) -> str:
    """The READER CHECKLIST block for the writer prompt, or '' when the mode is off (`mode` None reads the env
    switch) or the seed has no moment block. `route_segments`: the Route walk's (atom_id, seed) pairs when the
    prompt renders them as labelled moments (blog, more than one); else `content_seed` is read."""
    if reader_checklist_mode(mode) != MODE_R10:
        return ""
    seeds = [text for _id, text in route_segments] if route_segments else [content_seed or ""]
    all_stops = stops(seeds)
    if not all_stops:
        return ""
    if channel == "blog":
        return blog_checklist(all_stops, fact_entries(facts_text), search_questions)
    return social_checklist(all_stops)


__all__ = ["MODES", "MODE_ENV", "MODE_OFF", "MODE_R10", "QUOTE_MAX", "Stop", "TOPICS", "blog_checklist",
           "checklist_block", "fact_entries", "reader_checklist_mode", "social_checklist", "stops", "topic_answers"]
