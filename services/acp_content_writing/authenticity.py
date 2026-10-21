"""AA-819 (AUP 2026-11) — deterministic authenticity check for T9 copy.

Anthropic's usage policy (effective 12/11/2026 on Bedrock acc3) forbids deceptive campaigns: fake
reviews and personas, concealed sponsorship, content that misrepresents its origin or authorship.
T9 pieces are published by tenants under their own brand, so the writer must never produce:

- `testimonial` — a review, rating or customer quote: an attributed quote ('"…" — Sarah, UK',
  '"…" said Tom from Leeds'), "one guest told us", star ratings ("★★★★★", "5/5", "rated 4.9"),
  "verified review", "testimonial", a review platform (TripAdvisor, Trustpilot);
- `first_person` — the customer / traveller speaking in the first person ("I booked", "my wife and
  I", "my trip with", "I'd recommend", "we would book again", "best trip of my life");
- `independence` — a claim that the piece is independent or unsponsored ("independent review",
  "unbiased", "honest review", "not sponsored", "not affiliated with", "we were not paid").

The brand's own voice ("we", "our team", "our guides", "we recommend") is never matched: "we" is
flagged only in a customer frame ("we booked with", "we travelled with"). No seed carve-out — a
testimonial is not acceptable because the source happens to contain one.

`find_issues()` maps each match to the sentence(s) it touches (hygiene.sentence_spans — a quote
and its attribution can be several spans) and returns them in the "<problem>: '<sentence>'" shape
sentence_repair.plan parses. `cut_flagged()` deletes those sentences — the fail-closed step
service.py runs when a repair could not remove them, because a held piece still reaches the tenant
(AA-770). Pure: no DB, no LLM, no I/O.
"""
from __future__ import annotations

import re
from typing import Optional

from services.acp_content_writing.hygiene import _is_cta, sentence_spans, tidy_after_cut
from services.quality import registry as qr

GATE = qr.F19_AUTHENTICITY

_CHECKED_KINDS = ("prose", "bullet", "faq_a", "faq_q", "heading")

_PROBLEMS = {
    "testimonial": ("reads as a customer review, rating or testimonial ('{m}') — the brand never publishes reviews "
                    "or customer quotes; say it in the brand's own voice or cut it"),
    "first_person": ("speaks as a customer or traveller in the first person ('{m}') — write in the brand's own "
                     "voice, or cut it"),
    "independence": ("claims to be independent, unbiased or unsponsored ('{m}') — the brand publishes this "
                     "itself; cut the claim"),
}

# ---------------------------------------------------------------- patterns

_QUOTE = r"(?:\"[^\"\n]{6,}\"|“[^”\n]{6,}”|«[^»\n]{6,}»)"
# A first name, optional initial ('Tom B.'), then what attributions carry: a place after a comma,
# 'from / of / in <Place>', a bracket, or the end of the line. 'Pha That Luang, Vientiane' has no
# such tail after its first word, so a quoted place nickname followed by a dash is not a hit.
_NAME = r"[A-Z][a-z’'-]+(?:\s[A-Z]\.)?"
_ATTR_TAIL = r"(?:\s*,\s*[A-Z][\w’'. -]{1,40}|\s+(?:from|of|in)\s+[A-Z][a-z]+|\s*\([^)\n]{2,40}\)|[ \t]*$)"
_PERSON = (r"(?:guest|travell?er|customer|client|reviewer|visitor|participant|mum|mom|dad|parent|family|couple"
           r"|hiker|trekker|cyclist)s?")
_SPEECH = (r"(?:says?|said|writes?|wrote|recalls?|recalled|remembers?|remembered|told us|tells us|explains?"
           r"|explained|adds?|added|puts? it|shared|raved)")
# The bare '<Name> from <Place> <verb>' frame has no quote to anchor it, so only verbs that are
# speech in every reading: 'Transfers from Hanoi add an hour' is a trip fact, not an attribution.
_SAY = r"(?:says|said|writes|wrote|told us|tells us|recalls|recalled|raved)"

_TESTIMONIAL = [
    # '"…" — Sarah, UK' / '"…", - Tom B. (Sydney)'
    re.compile(rf"{_QUOTE}\s*,?\s*(?:[—–]|--?)\s*{_NAME}{_ATTR_TAIL}", re.M),
    # an attribution line of its own under the quote: '— Sarah, UK' (em / en dash only; a bullet is '- ')
    re.compile(rf"^[ \t]*[—–][ \t]*{_NAME}{_ATTR_TAIL}", re.M),
    # '"…" said Tom from Leeds' / '"…" says one recent guest'
    re.compile(rf"{_QUOTE}\s*,?\s*{_SPEECH}\s+(?:{_NAME}|(?:one|a|another|our|this|last year's)\s+"
               rf"(?:\w+\s+)?{_PERSON})\b"),
    # 'Sarah from Leeds told us' / 'one guest wrote' / 'many of our travellers say'
    re.compile(rf"\b[A-Z][a-z]+ from [A-Z][a-z]+,? {_SAY}\b"),
    re.compile(rf"\b(?i:(?:one|a|another|our|several|many|most|some|all|every)\s+(?:of\s+our\s+)?"
               rf"(?:recent\s+|past\s+|former\s+|happy\s+|returning\s+|repeat\s+|previous\s+)?{_PERSON}\s+"
               r"(?:ha(?:s|ve)\s+)?(?:said|says|say|wrote|write|writes|told us|tell us|tells us|described|describe|"
               r"describes|called|call|calls|put it|recalled|recall|recalls|shared|share|shares|raved|rave|raves|"
               r"left (?:us )?(?:a |glowing )?(?:review|note|comment)s?|rated|rate|rates|gave (?:us|it) ))"),
    # 'as one guest put it' / 'in the words of a recent traveller'
    re.compile(rf"(?i)\bas\s+(?:one|a|another|our)\s+(?:\w+\s+)?{_PERSON}\s+(?:put it|said|says|wrote|told us"
               r"|described it)\b"),
    re.compile(r"(?i)\bin the words of (?:one|a|another|our)\b"),
    # ratings and review framing
    re.compile(r"[★⭐]{2,}"),
    re.compile(r"(?i)\b(?:five|four|5|4)[- ]stars?\s+(?:reviews?|ratings?|feedback|rated)\b"),
    re.compile(r"(?i)\b(?:highly|top|best)[- ]rated\b"),
    re.compile(r"(?i)\b(?:verified|genuine|real|glowing|rave|5-star|five-star)\s+(?:guest\s+|customer\s+|traveller\s+"
               r"|traveler\s+)?(?:reviews?|reviewers?|testimonials?|feedback|ratings?)\b"),
    re.compile(r"(?i)\btestimonials?\b"),
    re.compile(r"(?i)\b(?:guest|customer|traveller|traveler|client|user)\s+reviews?\b"),
    re.compile(r"(?i)\breviews?\s+(?:say|says|praise|praises|mention|mentions|rave|raves|call|calls|describe|"
               r"describes|agree|agrees)\b"),
    re.compile(r"(?i)\b(?:trip\s?advisor|trustpilot|feefo|google reviews|yelp)\b"),
]

# Bare scores ('5/5', '4.9/5', '9/10', '4 out of 5', 'rated 4.9'): a review score, unless the same
# sentence frames it as a trek grade or a price ('graded 3/5 for difficulty', 'Difficulty: 4/5',
# '5/10 USD') — seed facts the writer may carry over. Not a fraction ('4/5 of the loop') or a measure.
_SCORES = [
    re.compile(r"(?i)\b\d(?:\.\d)?\s*/\s*(?:5|10)\b(?!\s*(?:of\b|km|kilomet|days?|hours?|nights?|metres?|meters?|%"
               r"|usd|us\$|\$|eur|€|vnd|thb|kip|lak|khr|mmk|inr|npr|btn))"),
    re.compile(r"(?i)\b\d(?:\.\d)?\s+out of\s+(?:5|five|10|ten)(?:\s+stars?)?\b"),
    re.compile(r"(?i)\brated\s+(?:\d(?:\.\d)?|five|four)\b"),
]
_GRADE_CONTEXT = re.compile(r"(?i)\b(?:difficult\w*|grade[ds]?|grading|level|fitness|challenge|challenging|strenuous"
                            r"|exertion|intensity|physical(?:ly)?)\b")

_FIRST_PERSON = [
    re.compile(r"\bI(?:\s+(?:just|recently|finally|once|first|also))?\s+(?i:booked|travell?ed\s+with|went\s+on\s+"
               r"(?:this|the|their|your)|joined\s+(?:this|the|their|your)|took\s+(?:this|the|my|our|their)\s+"
               r"(?:trip|tour|holiday|trek)|did\s+(?:this|the)\s+(?:trip|tour|trek)|signed\s+up|stayed\s+with|"
               r"was\s+(?:a|their|your)\s+(?:guest|customer|client))\b"),
    re.compile(r"\bI(?:['’]d|\s+would|\s+can|\s+cannot|\s+can['’]t)?(?:\s+(?:highly|definitely|totally|strongly|"
               r"really|wholeheartedly|100%))?\s+(?i:recommend)\b"),
    re.compile(r"(?i:\bcan(?:not|['’]t)\s+recommend\s+(?:it|them|this(?:\s+\w+)?|you|[A-Z]\w*(?:\s+[A-Z]\w*)?)"
               r"\s+enough)\b"),
    re.compile(r"(?i:\bmy\s+(?:wife|husband|partner|family|kids|children|son|daughter|friends?|girlfriend|"
               r"boyfriend|fianc[eé]e?|mum|mom|dad|parents|sister|brother)\s+and\s+)I\b"),
    re.compile(r"(?i)\bmy\s+(?:trip|tour|holiday|vacation|honeymoon|trek|stay)\s+(?:with|to|in|through|around)\b"),
    re.compile(r"(?i)\bour\s+(?:trip|tour|holiday|vacation|honeymoon|trek|stay|week)\s+with\b"),
    re.compile(r"(?i)\bwe\s+(?:booked|travell?ed|went|toured|trekked)\s+with\b"),
    re.compile(r"(?i)\bwe\s+booked\s+(?:this|our|the|a)\s+(?:trip|tour|holiday|package)\b"),
    re.compile(r"\b(?:I|[Ww]e)(?:['’]d|\s+would|\s+will|['’]ll)\s+(?i:(?:definitely\s+|absolutely\s+|100%\s+)?"
               r"(?:book|travel|go)\s+(?:with\s+\w+\s+)?again)\b"),
    re.compile(r"(?i)\bbest\s+(?:trip|holiday|vacation|tour|experience|week|decision|money)\s+(?:\w+\s+)?"
               r"(?:of|in)\s+(?:my|our)\s+(?:life|lives)\b"),
    re.compile(r"(?i)\b(?:trip|holiday|week|tour)\s+of\s+(?:my|our)\s+(?:life|lives)\b"),
    re.compile(r"(?i:\bas\s+(?:a|one)\s+(?:first[- ]time\s+|repeat\s+|solo\s+)?(?:guest|customer|client|"
               r"travell?er)),?\s+I\b"),
    re.compile(r"(?i)\bthank\s+you\s+(?:so\s+much\s+)?(?:to\s+)?(?:the\s+)?(?:whole\s+)?team\b"),
]

_INDEPENDENCE = [
    # 'third-party' alone is ordinary advice ('avoid third-party websites'); only its review framing counts
    re.compile(r"(?i)\bthird[- ]party\s+(?:reviews?|reviewers?|opinions?|assessments?|verdicts?|rankings?|ratings?)\b"),
    re.compile(r"(?i)\b(?:independent|unbiased|impartial|neutral|unsponsored|non[- ]sponsored|"
               r"disinterested)\s+(?:travel\s+)?(?:reviews?|reviewers?|opinions?|assessments?|verdicts?|"
               r"comparisons?|rankings?|recommendations?|advice|perspectives?|blogs?|bloggers?|sites?|websites?|"
               r"look|take|guides?\s+to)\b"),
    re.compile(r"(?i)\b(?:honest|objective|candid)\s+(?:reviews?|opinions?|verdicts?|assessments?|take)\b"),
    re.compile(r"(?i)\b(?:is|was|are|were)(?:\s+not|n['’]t)\s+(?:a\s+|an\s+)?(?:sponsored|paid\s+(?:post|review|"
               r"promotion|partnership|content)|advert(?:isement)?|ad|affiliated\s+with|promotional)\b"),
    re.compile(r"(?i)\bnot\s+(?:sponsored|affiliated\s+with)\b"),
    re.compile(r"(?i)\bno\s+(?:affiliation|sponsorship|commercial\s+(?:ties|relationship|interest|links?))\b"),
    re.compile(r"(?i)\b(?:we|I)\s+(?:are|am|were|was)(?:\s+not|n['’]t)\s+(?:paid|sponsored|compensated)\b"),
    re.compile(r"(?i)\b(?:nobody|no\s+one)\s+paid\s+(?:us|me)\b"),
    re.compile(r"(?i)\bwithout\s+(?:any\s+)?(?:sponsorship|being\s+paid)\b"),
]

_FAMILIES = (("testimonial", _TESTIMONIAL), ("first_person", _FIRST_PERSON), ("independence", _INDEPENDENCE))


def attribution_spans(text: str) -> list[tuple[int, int]]:
    """AA-778 — (start, end) of each dash attribution ('"…" <em-dash> Sarah, UK', or '<em-dash> Sarah, UK' on a
    line of its own): the first two _TESTIMONIAL patterns need the dash. hygiene.fix_dashes runs before
    the gates and keeps these dashes, so F19 still flags the sentence and cut_flagged removes it whole."""
    return [m.span() for p in _TESTIMONIAL[:2] for m in p.finditer(text or "") if m.group(0).strip()]


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence (or line) a match sits in — enough context to tell a trek grade from a review score."""
    lo = max(text.rfind(c, 0, start) for c in (".", "!", "?", "\n")) + 1
    hi = min([i for i in (text.find(c, end) for c in (".", "!", "?", "\n")) if i != -1] or [len(text)])
    return text[lo:hi]


def _matches(text: str) -> list[tuple[str, re.Match]]:
    text = text or ""
    out: list[tuple[str, re.Match]] = []
    for kind, patterns in _FAMILIES:
        for pattern in patterns:
            out += [(kind, m) for m in pattern.finditer(text) if m.group(0).strip()]
    for pattern in _SCORES:
        out += [("testimonial", m) for m in pattern.finditer(text) if m.group(0).strip()
                and not _GRADE_CONTEXT.search(_sentence_around(text, m.start(), m.end()))]
    return out


# AA-819 follow-up (S237) — the tenant's own CTA line is exempt from F19 (flagged() above skips it:
# it is the tenant's configured text, published word for word). The portal still warns the tenant
# when the CTA itself claims reviews or ratings, and AA admins get a bell notification. The CTA rule
# is the testimonial family + review scores above, plus the short review hooks a CTA uses that body
# prose would not be flagged for ('Read our reviews', '5-star', 'loved by 2,000 travellers').
_CTA_AUDIENCE = (r"(?:\d[\d,.]*\+?\s+|our\s+|thousands\s+of\s+|hundreds\s+of\s+|happy\s+|real\s+|past\s+)?"
                 r"(?:guests|travell?ers|customers|clients)")
_CTA_REVIEW_EXTRA = [
    re.compile(r"(?i)\breviews\b"),
    re.compile(r"(?i)\breviewers?\b"),
    # 'reviewed by our guests' — not 'get your itinerary reviewed by a local expert' (a service)
    re.compile(rf"(?i)\breviewed\s+by\s+{_CTA_AUDIENCE}\b"),
    # 'rated' only as a claim ('rated us 5 stars', 'Rated excellent', 'Rated #1', 'guests rated'),
    # not 'Rated trips for families' or 'rated PG'; bare 'rated 4.9' is in _SCORES
    re.compile(r"(?i)\brated\s+(?:us|it|them|excellent|outstanding|exceptional|superb|amazing|perfect|best|top"
               r"|highly|the\s+best|number\s+one|no\.?\s*1\b|#\s*1\b|by\b)"),
    re.compile(r"(?i)\b(?:who|they|guests|travell?ers|customers|clients)\s+(?:have\s+|has\s+)?rated\b"),
    # '98% of our guests recommend us'
    re.compile(rf"(?i)\b\d{{1,3}}(?:\.\d)?\s*%\s+of\s+{_CTA_AUDIENCE}\s+(?:would\s+)?(?:recommend|rate|rated"
               r"|love|loved|return|come\s+back|book\s+again)\b"),
    # '5-star' / 'five stars' — not a hotel class ('a five-star hotel', '5-star luxury stay')
    re.compile(r"(?i)\b(?:five|5)[- ]stars?\b(?!\s+(?:hotels?|resorts?|lodges?|luxury|accommodation|stays?|camps?"
               r"|cruises?|boats?|villas?|service))"),
    re.compile(r"(?i)\bwhat\s+(?:our|other|past|real|happy|recent)\s+(?:guests|travell?ers|customers|clients)\s+"
               r"(?:say|said|think|thought|wrote|write)\b"),
    re.compile(rf"(?i)\b(?:loved|recommended|praised|trusted)\s+by\s+{_CTA_AUDIENCE}\b"),
    # TripAdvisor's review-based award ('Travellers' Choice 2025 winner')
    re.compile(r"(?i)\btravell?ers['\u2019]?\s+choice\b"),
]
# 'rating(s)' is a claim unless the sentence frames it as a trek grade ('trek difficulty ratings')
_CTA_RATING = re.compile(r"(?i)\bratings?\b")


def cta_review_claims(cta: Optional[str]) -> list[str]:
    """The phrases in a tenant CTA that claim reviews, ratings or testimonials ('five-star reviews',
    'Rated 4.9', 'TripAdvisor', 'what our guests say'), in text order, overlaps collapsed to the
    widest match. Empty for an ordinary CTA ('Talk to a travel designer', 'Book your trek').
    Deterministic, no LLM — the portal warns on it, it never blocks or edits the CTA."""
    text = " ".join((cta or "").split())
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    for pattern in (*_TESTIMONIAL, *_CTA_REVIEW_EXTRA):
        spans += [(m.start(), m.end()) for m in pattern.finditer(text) if m.group(0).strip()]
    for pattern in (*_SCORES, _CTA_RATING):
        spans += [(m.start(), m.end()) for m in pattern.finditer(text) if m.group(0).strip()
                  and not _GRADE_CONTEXT.search(_sentence_around(text, m.start(), m.end()))]
    kept: list[list[int]] = []
    for start, end in sorted(spans, key=lambda s: (s[0], -s[1])):
        if kept and start < kept[-1][1]:  # overlaps the previous phrase: widen it
            kept[-1][1] = max(kept[-1][1], end)
        else:
            kept.append([start, end])
    return list(dict.fromkeys(text[a:b].strip() for a, b in kept))


def field_hits(value: Optional[str]) -> list[tuple[str, str]]:
    """(kind, matched text) in a one-line field (the SEO title or meta)."""
    return [(kind, m.group(0).strip()) for kind, m in _matches(value or "")]


def flagged(content_text: str, cta: Optional[str] = None) -> list[dict]:
    """Every flagged sentence group of the body: {"start", "end", "sentence", "kind", "match"}, in text
    order, one per group. A group runs from the first to the last sentence span a match touches (a
    quote split by the sentence splitter, plus its attribution line). The CTA line is the tenant's own
    configured text and is never flagged."""
    text = content_text or ""
    spans = [s for s in sentence_spans(text) if s["kind"] in _CHECKED_KINDS]
    groups: list[dict] = []
    for kind, m in sorted(_matches(text), key=lambda km: km[1].start()):
        hit = [s for s in spans if s["start"] < max(m.end(), m.start() + 1) and m.start() < s["end"]]
        if not hit:
            continue
        start, end = hit[0]["start"], hit[-1]["end"]
        sentence = " ".join(s["clean"] for s in hit)
        if any(_is_cta(s["clean"], cta) for s in hit):
            continue
        same = next((g for g in groups if g["start"] < end and start < g["end"]), None)
        if same is not None:  # overlapping groups merge (two hits in one quote)
            if start < same["start"] or end > same["end"]:
                spans_in = [s for s in spans if min(start, same["start"]) <= s["start"]
                            and s["end"] <= max(end, same["end"])]
                same.update(start=min(start, same["start"]), end=max(end, same["end"]),
                            sentence=" ".join(s["clean"] for s in spans_in))
            continue
        groups.append({"start": start, "end": end, "sentence": sentence, "kind": kind,
                       "match": " ".join(m.group(0).split())})
    return sorted(groups, key=lambda g: g["start"])


def find_issues(content_text: str, cta: Optional[str] = None, *, seo_title: Optional[str] = None,
                meta_description: Optional[str] = None) -> list[str]:
    """Violations in the "<problem>: '<sentence>'" shape; a title / meta hit starts with "title " /
    "meta " (the SEO fit owns those, like F12/F17)."""
    out = [f"{_PROBLEMS[g['kind']].format(m=g['match'][:60])}: '{g['sentence'][:200]}'"
           for g in flagged(content_text, cta)]
    for field, value in (("title", seo_title), ("meta", meta_description)):
        for kind, match in field_hits(value):
            out.append(f"{field} {_PROBLEMS[kind].format(m=match[:60])}: '{(value or '')[:200]}'")
    return list(dict.fromkeys(out))


def cut_flagged(content_text: str, cta: Optional[str] = None) -> tuple[str, list[str]]:
    """Delete every flagged sentence group from the body (the hook included — the AUP rule wins over
    the piece's shape; the CTA is never cut) and tidy the joins. Returns (text, the cut sentences)."""
    groups = flagged(content_text, cta)
    if not groups:
        return content_text, []
    text = content_text
    ranges = sorted(_with_faq_answers(content_text, [(g["start"], g["end"]) for g in groups]))
    merged: list[list[int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        end = end + 1 if text[end:end + 1] == " " else end
        text = text[:start] + text[end:]
    return tidy_after_cut(text), [g["sentence"] for g in groups]


def _with_faq_answers(content_text: str, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """A cut FAQ question takes its answer with it: an answer left under no question (or under the
    previous pair's) is a broken FAQ. tidy_after_cut then drops the empty "A:" line. (An answer cut
    to nothing already takes its question — tidy_after_cut.)"""
    spans = sentence_spans(content_text)
    out = list(ranges)
    for i, s in enumerate(spans):
        if s["kind"] != "faq_q" or not any(a < s["end"] and s["start"] < b for a, b in ranges):
            continue
        answer = []
        for nxt in spans[i + 1:]:
            if nxt["kind"] != "faq_a":
                break
            answer.append(nxt)
        if answer:
            out.append((answer[0]["start"], answer[-1]["end"]))
    return out


__all__ = ["GATE", "attribution_spans", "cta_review_claims", "cut_flagged", "field_hits", "find_issues", "flagged"]
