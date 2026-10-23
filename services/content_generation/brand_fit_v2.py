"""AA-645 — A1 brand-fit rubric v2: the page's VOICE judged within the limits of its SOURCE.

Why (S248 offline read, root docs/audits/2026-10-23-S248-aa645-writer-decision/platform-impact-memo.md section 8): the
v1 A1 judge (`brand_fit._build_judge_prompt`, JUDGE_SYSTEM "generic travel copy that would fit any brand must score
low") never sees the source, rewards brand colour, and called the source-faithful writer-v2 pages "generic": W pages
scored 0.84 lower than today's A+ pages and 32–36 % of them would retry (16 % for A+), while the page-level Spearman
between brand fit and the panel's serious-error rate was 0.08. A retry on that feedback pushes the writer back to the
embellishment that produces invented claims.

Rubric v2 is the white-label house style (root docs/audits/2026-10-21-S246-content-quality-advisory/
adventure-asia-house-style.md, appendix "AA S1 voice guide"):

* the judge sees the source, organised by code (the same fact sheet the writer v2 reads,
  `s1_writer_v2.render_fact_sheet`);
* it scores voice only as far as the source allows: a thin source gives a short, plain page, and that is correct;
  neutral, source-faithful wording is not "generic" by itself;
* it never rewards — and its feedback never asks for — brand colour, emotion, an "angle" or claims the source lacks;
* it lists every specific the page states that the source does not support, quoting the page; code keeps only quotes
  found verbatim in the page and caps the score (one or two: 6, three or more: 4);
* code caps the score at 5 when the page names "Adventure Asia" or the old CTA "Design This Journey" and the source
  does not (a white-label page has no brand).

The reply keeps the v1 shape (`brand_fit_score` = the voice score, `cross_brand_distinct` = source specificity), so the
A1 gate (`judge_node.a1_judge_score` = brand fit alone), the AA-714 near-band median of 3 and the llm_call_log rows
work unchanged. Pure module: prompt text, source sheet, reply post-processing; no I/O, no LLM call
(`brand_fit.score_brand_fit(rubric="v2")` makes the call).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

RUBRIC_V1 = "v1"
RUBRIC_V2 = "v2"
RUBRICS = (RUBRIC_V1, RUBRIC_V2)
# The version string stored with every v2 score (llm_call_log quality_signal.rubric, generated_content
# metadata.judge.rubric). Bump it on ANY change to the prompt text or the caps below:
# tests/unit/test_aa645_brandfit_v2.py pins RUBRIC_V2_FINGERPRINT to the text, so an edit without a bump fails.
RUBRIC_V2_VERSION = "a1-voice-v2/1"
RUBRIC_V2_FINGERPRINT = "1fcafd9e2eca"

MAX_SOURCE_CHARS = 14000     # the fact sheet: golden median ~4.7k, max ~31k characters
PER_DAY_CHARS = 900          # each page day as the judge sees it (v1: 350, enough for voice, not for claims)
TOTAL_ITINERARY_CHARS = 9000
MAX_CLAIMS = 12              # unsupported specifics kept per reply
MIN_QUOTE_WORDS = 2          # a shorter quote ("Day 2") cannot be matched to one claim
CAP_UNSUPPORTED_ONE = 6.0    # one or two verified unsupported specifics
CAP_UNSUPPORTED_MANY = 4.0   # three or more
MANY_UNSUPPORTED = 3
CAP_BRAND_LEAK = 5.0
# A white-label page never names a brand. Each phrase counts only when the source does not contain it.
LEAK_PHRASES = ("Adventure Asia", "Design This Journey")

SYSTEM_V2 = """You are the voice judge for a neutral, white-label tour catalogue. Travel agencies resell each page
unchanged under their own name, so the page has no brand of its own.
You do NOT rewrite content. You judge the page's VOICE against the house style WITHIN THE LIMITS OF ITS SOURCE, list
every specific the page states that the source does not support, and give feedback the writer can act on without
adding facts. Plain, accurate wording on a thin source is correct, not a fault. Return JSON only."""

_HOUSE_STYLE = """HOUSE STYLE (a white-label master page):
- Calm, factual and specific; third person, present tense.
- Every concrete detail (activity, service, meal, transport, time, distance, altitude, view, wildlife, lodging,
  inclusion) comes from the SOURCE below. One short clause of well-known background about a place the source names is
  allowed and is not an unsupported specific.
- Length and detail follow the source: a thin source gives a short, plain page.
- No hype or superlative the source does not make (breathtaking, stunning, unforgettable, once-in-a-lifetime,
  paradise, exotic), no abstract filler ("unhurried", "rewards attention", "a different register"), no second-person
  sales voice.
- No brand, operator or supplier name (never "Adventure Asia"), no call to action ("book now", "join us",
  "Design This Journey", "awaits").
- Day titles name the day's place(s) or main activity in sentence case; a day whose source states distance, time,
  altitude, overnight or meals may end with a short fact line.

DO NOT REWARD OR ASK FOR:
- brand colour, emotion, a "unique angle", storytelling or atmosphere the source does not support;
- more length or more detail than the source has.
Neutral wording that stays close to the source is NOT "generic". Call a passage generic only when it uses stock travel
phrases ("immerse yourself", "rich tapestry", "hidden gem", "something for everyone") where the source has its own
specifics."""

_ANCHORS = """SCORE ANCHORS
brand_fit_score = the VOICE score (pick the anchor the page matches best; 5 or 9 only between two anchors):
- 10: a calm, factual catalogue page for THIS source: specific where the source is specific, plain where it is thin;
  no hype, filler, brand, call to action or unsupported detail.
- 8: as 10, with a few soft or stock words or one flat passage; nothing unsupported.
- 7: clean and faithful, but several flat or repetitive passages or a little stock phrasing. A faithful, clean page
  on a thin source scores at least 7.
- 6: noticeable hype, superlatives or filler the source does not support, a second-person sales voice, or one or two
  unsupported specifics.
- 4: three or more unsupported specifics, a brand / operator name or a call to action, or it reads as advertising.
- 2: mostly advertising or invented.
cross_brand_distinct = SOURCE SPECIFICITY (1-10): how fully the page uses the SOURCE's own specifics where they fit
(named places, figures, meals, overnights, sold activities) instead of stock phrases. Judge only against what the
source has: a thin source fully used is 10; a rich source reduced to generalities is 4 or lower."""

_REPLY = """Fill the keys in this order:
- unsupported_specifics: every specific the page states that the SOURCE does not support (an activity, service, meal,
  transport mode, time, distance, altitude, view, wildlife sighting, lodging feature, inclusion or superlative claim).
  Background clauses about a named place do not count; a detail the source states in other words is supported. Each
  item: {"quote": "<the page's words, copied exactly, 3-20 words>", "why": "<what the source says instead, or that it
  says nothing about it>"}. [] when there are none.
- brand_fit_score, cross_brand_distinct: integers, per the anchors.
- feedback: concrete changes to the VOICE that stay within the source (remove hype, filler, unsupported details, brand
  names); never ask for added facts, colour, emotion or a brand angle. "" when nothing needs changing.

Return JSON ONLY, no markdown, exactly:
""" + ('{"unsupported_specifics": [{"quote": "<string>", "why": "<string>"}], "brand_fit_score": <int>, '
       '"cross_brand_distinct": <int>, "feedback": "<string>"}')


def fingerprint() -> str:
    """12 hex chars over every text that shapes a v2 verdict (prompt parts + caps). Pinned by a unit test."""
    caps = f"{CAP_UNSUPPORTED_ONE}|{CAP_UNSUPPORTED_MANY}|{MANY_UNSUPPORTED}|{CAP_BRAND_LEAK}|{LEAK_PHRASES}"
    body = "\x1e".join([SYSTEM_V2, _HOUSE_STYLE, _ANCHORS, _REPLY, caps, str(PER_DAY_CHARS), str(MAX_SOURCE_CHARS)])
    return hashlib.sha256(body.encode()).hexdigest()[:12]


# ------------------------------------------------------------------ source

def source_text(tour: Optional[dict]) -> str:
    """Every source field as one text (leak check)."""
    from services.quality.s1_facts import source_fields
    return "\n".join(source_fields(tour or {}).values())


def source_sheet(tour: Optional[dict], tour_agency: Optional[dict] = None) -> str:
    """The source as the judge reads it: the writer v2 fact sheet (built by code, every value verbatim from the
    source), cut to MAX_SOURCE_CHARS. "" when there is no source tour."""
    if not tour:
        return ""
    from services.content_generation import s1_writer_v2 as w
    from services.quality.s1_facts import build_fact_table

    t = w.with_agency_fields(tour, tour_agency)
    facts = build_fact_table(t)
    sheet = w.render_fact_sheet(t, facts, w.day_plan(t, facts))
    if len(sheet) > MAX_SOURCE_CHARS:
        sheet = sheet[:MAX_SOURCE_CHARS].rstrip() + "\n… [source shortened for length]"
    return sheet


# ------------------------------------------------------------------ page

PAGE_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries", "seo_title", "seo_meta")


def page_text(generated: dict) -> str:
    """Every page field as one text (quote verification and leak check), itineraries in full."""
    from services.quality.s1_facts import field_text
    return "\n".join(field_text((generated or {}).get(f)) for f in PAGE_FIELDS)


def build_user_prompt(generated: dict, sheet: str) -> str:
    from services.content_generation.brand_fit import itinerary_digest

    highlights = generated.get("highlights") or []
    if isinstance(highlights, str):
        highlights = [highlights]
    hl = "\n".join(f"- {h}" for h in highlights) or "(none)"
    itin = itinerary_digest(generated.get("itineraries"), per_day=PER_DAY_CHARS, total=TOTAL_ITINERARY_CHARS)
    return f"""Judge this catalogue page's voice against the house style, within the limits of its source.

{_HOUSE_STYLE}

SOURCE (the supplier's text, organised by code; the only facts the page may state):
{sheet or "(no source available)"}

PAGE TO JUDGE:
NAME: {generated.get("name")}
SUBTITLE: {generated.get("subtitle")}
SUMMARY: {generated.get("summary")}
HIGHLIGHTS:
{hl}
ITINERARIES (every day; long days shortened with "…"):
{itin}
SEO_TITLE: {generated.get("seo_title")}
SEO_META: {generated.get("seo_meta")}

{_ANCHORS}

{_REPLY}"""


# ------------------------------------------------------------------ reply

def _norm(text: str) -> str:
    from services.content_generation.brand_fit import _normalise_quote
    return _normalise_quote(text)


_QUOTE_TRIM = " \t\n\"'.,;:!?-…"


def quote_in_page(quote: str, page: str) -> bool:
    """True when `quote` (after quote / dash / case folding, edge punctuation and a trailing "…" dropped) is a
    verbatim passage of the page of at least MIN_QUOTE_WORDS words. A paraphrase or an invented quote is False."""
    q = _norm(quote).strip(_QUOTE_TRIM)
    while q.endswith("..."):
        q = q[:-3].strip(_QUOTE_TRIM)
    return len(q.split()) >= MIN_QUOTE_WORDS and q in _norm(page)


def brand_leaks(page: str, source: str) -> list[str]:
    """LEAK_PHRASES the page contains (whole words, any case) and the source does not."""
    out = []
    for phrase in LEAK_PHRASES:
        pat = re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, phrase.split())) + r"(?!\w)", re.I)
        if pat.search(page or "") and not pat.search(source or ""):
            out.append(phrase)
    return out


def _claims(value) -> list[dict]:
    from services.content_generation.brand_fit import _coerce_text

    items = value if isinstance(value, list) else []
    out = []
    for it in items:
        if isinstance(it, dict):
            q, why = _coerce_text(it.get("quote")), _coerce_text(it.get("why"))
        else:
            q, why = _coerce_text(it), ""
        if q:
            out.append({"quote": q[:300], "why": why[:300]})
    return out


@dataclass(frozen=True)
class V2Verdict:
    brand_fit: float                       # after the code caps (what the A1 gate reads)
    raw_brand_fit: float                   # the model's own voice score
    distinct: float                        # source specificity
    feedback: str
    unsupported: tuple = ()                # verified claims [{quote, why}], at most MAX_CLAIMS
    unverified: int = 0                    # claims whose quote is not in the page (dropped)
    leaks: tuple = ()
    caps: tuple = field(default_factory=tuple)   # ("unsupported" | "unsupported_many" | "brand_leak", ...)


def apply_reply(reply: dict, generated: dict, source: str) -> V2Verdict:
    """The verdict of one v2 judge reply: scores coerced to [0, 10], claims kept only when quoted from the page, the
    code caps applied (unsupported specifics, brand leak). `source` = `source_text(tour)`."""
    from services.content_generation.brand_fit import _coerce_score, _coerce_text

    page = page_text(generated)
    raw = _coerce_score(reply.get("brand_fit_score"))
    distinct = _coerce_score(reply.get("cross_brand_distinct"))
    claims = _claims(reply.get("unsupported_specifics"))
    verified = [c for c in claims if quote_in_page(c["quote"], page)]
    leaks = brand_leaks(page, source)
    score, caps = raw, []
    if len(verified) >= MANY_UNSUPPORTED:
        score, caps = min(score, CAP_UNSUPPORTED_MANY), ["unsupported_many"]
    elif verified:
        score, caps = min(score, CAP_UNSUPPORTED_ONE), ["unsupported"]
    if leaks:
        score = min(score, CAP_BRAND_LEAK)
        caps.append("brand_leak")
    return V2Verdict(brand_fit=score, raw_brand_fit=raw, distinct=distinct,
                     feedback=_coerce_text(reply.get("feedback")), unsupported=tuple(verified[:MAX_CLAIMS]),
                     unverified=len(claims) - len(verified), leaks=tuple(leaks), caps=tuple(caps))
