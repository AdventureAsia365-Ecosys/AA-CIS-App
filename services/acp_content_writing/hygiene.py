"""AA-768 (S234) — T9 text hygiene: deterministic checks and in-place fixes for what the gates
never looked at.

S233 r1 (n=20): every route blog named places outside its SCOPE ('Gobi' from the trip name,
'Erdene Zuu' from the "other trips" lines), 2 leaked the prompt's own vocabulary ("The content
seed for this journey notes that…"), 2 used social proof ("tends to stay with children
longest"), 4 repeated a nearby sentence (mostly after a sentence repair) and 2 misspelt a place in
a hashtag (#PakoTaktsang). SCOPE was a prompt line only; nothing in code checked any of it.

This module owns those rules (S234 plan C5): the scope lexicon and out-of-scope gazetteer,
`SOCIAL_PROOF_RE`, `LEAK_RE`, the hashtag fix, and the text primitives they share (`fold`,
`sentence_spans`, `is_sentence_end`, `expand_to_sentence`, `restates`, `near_verbatim`,
`tidy_after_cut`). Other modules import them —
`sentence_repair` (the splice guard), `quality_gates` (F12_scope / F13_hygiene), `atom_seed`
(sibling filter) and F9 v2 `claim_rules` — and never re-implement them.

Pure: no DB, no LLM, no I/O. `autofix()` only deletes or trims a sentence when the rule is sure
(borrowed from another trip, a prompt leak, social proof the seed does not support, a
near-verbatim repeat); everything else is reported by `find_issues()` for the gates.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

_TAG = r"\[(?:R|F):[^\]]+\]"
_TAG_STRIP_RE = re.compile(rf"\s?{_TAG}")


def fold(text: Optional[str]) -> str:
    """Accent-folded lower case: 'Khövsgöl' → 'khovsgol'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(c)).lower()


def strip_tags(text: Optional[str]) -> str:
    return _TAG_STRIP_RE.sub("", text or "")


# ---------------------------------------------------------------- proper nouns

# Capitalised words that never name a place on their own (sentence words, months, generic
# geography, nationalities that read as adjectives, people words). Folded.
COMMON_CAPS = frozenset(fold(w) for w in """
Day Days FAQ Q A The This That These Those It Its An And But Or For In On At By To From With Without Of As
If When Where What Why How Who Which Most Many Some Each Every One Two Three Four Five There Here Not No Yes
Our We You Your They Their He She His Her Them Us I My Me Let Get Come See Take Make Ask Talk Book Contact
Explore Discover Plan Start Learn Join Visit Read Just Also Then Even After Before During Between Once Only
Still Yet So Because Since While Although Though Inside Outside Above Below Near Across Along Around Beyond
January February March April May June July August September October November December
Monday Tuesday Wednesday Thursday Friday Saturday Sunday Spring Summer Autumn Fall Winter
Buddhist Buddhism Mongolian Mongolians Soviet Asian Central Siberian Tibetan Bhutanese Lao Vietnamese
Nepali Nepalese Indian Chinese Thai Khmer Burmese Hindu Hinduism Christian Muslim Islamic
National Park Province District Region Lake River Monastery Temple Dzong Valley Falls Town City Camp Ruins
Mountain Mountains Desert Steppe Forest Island Islands Bay Beach Pass Peak Hill Hills Gorge Cave Caves
Market Museum Palace Fort Village Old New Great Upper Lower North South East West Northern Southern
Eastern Western Natural Monument Reserve Sanctuary Heritage World Site Trail Route Road Street Square
Families Family Travellers Traveller Travelers Traveler Parents Children Kids Guests Visitors People Monks
Day Night Morning Afternoon Evening Note Why Location Naming Source Place Season Other Trips Moment
Moments Scope Tour Trip Journey Itinerary Guide Guides
""".split())

# 'Erdene-Zuu' / 'Uran-Togoo' are one word: a hyphen may join two capitalised parts.
_CAPW = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ’']+(?:-[A-Za-zÀ-ÖØ-öø-ÿ’']+)*-?"
_CAPWORD_RE = re.compile(rf"\b{_CAPW}(?:\s+{_CAPW})*")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def phrase_tokens(phrase: str) -> list[str]:
    """Folded tokens of a name, minus generic words: 'Erdene Zuu Monastery' → ['erdene', 'zuu']."""
    return [t for t in _TOKEN_RE.findall(fold(phrase)) if len(t) >= 3 and t not in COMMON_CAPS]


def proper_nouns(text: Optional[str], *, keep_initial: bool = False) -> set[str]:
    """Capitalised runs ('Erdene Zuu Monastery', 'Uran Togoo') in `text`. A single capitalised
    word at the start of a sentence or line is skipped — it is capitalised for grammar, not
    because it is a name — unless `keep_initial` (the allowed side of the scope lexicon, where
    over-including only means fewer flags: 'Location: Khovsgol, …')."""
    text = text or ""
    out: set[str] = set()
    for m in _CAPWORD_RE.finditer(text):
        phrase = m.group(0).strip()
        before = text[:m.start()].rstrip(" *\"'“‘(-")
        if not keep_initial and len(phrase.split()) == 1 and (not before or before[-1] in ".!?:\n#—–"):
            continue
        if phrase_tokens(phrase):
            out.add(phrase)
    return out


def lev(a: str, b: str) -> int:
    """Edit distance (Levenshtein)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _plural_pair(a: str, b: str) -> bool:
    return a == b + "s" or b == a + "s"


# ---------------------------------------------------------------- scope lexicon

SIBLING_HEADER = "Other trips that include"


def split_seed(seed: Optional[str]) -> tuple[str, str]:
    """(own text, sibling text) of a seed: the "Other trips that include this same moment" bullet
    lines are the siblings, every other line is the moment's own source."""
    own: list[str] = []
    sib: list[str] = []
    in_sib = False
    for line in (seed or "").splitlines():
        if line.startswith(SIBLING_HEADER):
            in_sib = True
            continue
        if in_sib and line.startswith("- "):
            sib.append(line[2:])
            continue
        in_sib = False
        own.append(line)
    return "\n".join(own), "\n".join(sib)


# Words of a trip's marketing name that are never a place on their own ('Bhutan Cultural Discovery',
# 'Best of Bhutan Adventure', 'Classic Bhutan Highlights Tour'). Folded. Review S234: keying the
# trip name's whole capitalised run made every trip name 'out of scope'.
TRIP_NAME_WORDS = frozenset(fold(w) for w in """
Cultural Culture Discovery Discoveries Adventure Adventures Adventurous Highlights Highlight Classic Best
Explorer Explorers Exploration Expedition Expeditions Escape Escapes Wonders Wonder Wild Wilds Wilderness
Grand Ultimate Essential Essentials Treasures Treasure Hidden Gems Secrets Secret Magic Magical Mystical
Mystic Kingdom Kingdoms Land Lands Encounter Encounters Experience Experiences Odyssey Quest Retreat
Getaway Holiday Holidays Safari Safaris Trek Treks Trekking Hike Hikes Hiking Cycling Cycle Bike Biking
Ride Rides Riding Overland Private Small Group Groups Luxury Deluxe Premium Signature Insight Insights
Panorama Glimpse Glimpses Taste Tastes Flavours Flavors Nature Wildlife Nomad Nomads Nomadic Horse Horses
Horseback Festival Festivals Teens Teen Active Soft Epic Golden Heart Soul Spirit Spirits Beauty
Beautiful Tours Trips Nights Week Weeks Thunder Dragon Dragons Untouched Remote Authentic Unique Iconic
Spectacular Majestic Ancient Sacred Spiritual Lakes Rivers Peaks Deserts Valleys Temples Monasteries
Dzongs Highlands Lowlands Countryside Coast Coastal Rural Off Beaten Path Track Tracks Loop Circuit
Combo Combined Extension Short Long Full Half Easy Gentle Moderate Challenging Introduction Intro
Overview Story Stories Footsteps Kids Child Multi Generation Generations Wellness Yoga Photography
Photo Photographic Food Foodie Culinary Cooking Wine Tea Coffee Homestay Homestays Camping Glamping
""".split())

_TRIP_WORD_RE = re.compile(r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ’'\-]*")


@dataclass(frozen=True)
class ScopeLexicon:
    """`allowed`: folded tokens/phrases of the piece's own places. `gazetteer`: folded
    out-of-scope place (its tokens joined by one space) → its display form. `cap_only`: the
    gazetteer keys taken from the TRIP name — they count only when written with a capital
    ('Gobi', not 'the gobi-style dunes'). `own_text` / `sibling_text`: the seed halves."""
    allowed: frozenset
    gazetteer: dict
    own_text: str
    sibling_text: str
    cap_only: frozenset = frozenset()


def _near_allowed(token: str, allowed: frozenset) -> bool:
    """A romanisation variant of an own place ('Hovsgol' vs 'Khovsgol') is not out of scope."""
    limit = 2 if len(token) >= 7 else 1
    return any(abs(len(token) - len(a)) <= limit and lev(token, a) <= limit for a in allowed if " " not in a)


def trip_name_places(trip_name: Optional[str]) -> list[list[tuple[str, str]]]:
    """Runs of words in a trip's name that may name a place, as [(folded, display), …]: every
    capitalised word (one right after ':' included — 'Mongolia: Gobi & Khovsgol'), minus generic
    words (COMMON_CAPS), marketing words (TRIP_NAME_WORDS), numbers and words under 3 letters. A
    run breaks at any of those words and at punctuation or a lower-case word ('and', 'of'), so
    'Tiger's Nest' is one run and 'Mongolia: Gobi & Khovsgol' is three."""
    runs: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    last_end = 0
    text = trip_name or ""
    for m in _TRIP_WORD_RE.finditer(text):
        if text[last_end:m.start()].strip():
            runs.append(cur)
            cur = []
        last_end = m.end()
        for part in re.split(r"[’'\-]", m.group(0)):
            f = fold(part)
            if not f or (len(f) < 3 and f.isalpha() and cur):
                continue  # the 's' of "Tiger's" does not break the run
            if len(f) >= 3 and f.isalpha() and f not in COMMON_CAPS and f not in TRIP_NAME_WORDS:
                cur.append((f, part))
            else:
                runs.append(cur)
                cur = []
    runs.append(cur)
    return [r for r in runs if r]


def build_scope_lexicon(seed: Optional[str], scope: Optional[str] = None,
                        trip_name: Optional[str] = None) -> ScopeLexicon:
    """Allowed = every capitalised word/run of the moment's own lines (label, Location, Naming,
    Source itinerary, Place background) and of the SCOPE line, plus every 'quoted' spelling in a
    Naming line. Gazetteer = (a) proper-noun phrases of the sibling lines that are not allowed and
    (b) runs of place words of the TRIP name, split at every allowed word (`trip_name_places`;
    marketing words never count, so 'Bhutan Cultural Discovery' adds nothing when Bhutan is the
    moment's country, and 'Mongolia Gobi' on a Mongolia piece adds 'gobi'). A romanisation
    variant of an allowed token is never out of scope. Matching only against this gazetteer —
    never against "any unknown proper noun" — is what kept r1 LinkedIn at 0 hits."""
    own, sib = split_seed(seed)
    allowed: set[str] = set()
    for p in proper_nouns(own, keep_initial=True) | proper_nouns(scope, keep_initial=True):
        toks = phrase_tokens(p)
        allowed |= set(toks)
        if toks:
            allowed.add(" ".join(toks))
    for line in own.splitlines():
        if line.startswith("Naming:"):
            for quoted in re.findall(r"'([^']+)'", line):
                allowed |= set(phrase_tokens(quoted))
    frozen = frozenset(allowed)
    gazetteer: dict[str, str] = {}
    for p in proper_nouns(sib):
        toks = phrase_tokens(p)
        unknown = [t for t in toks if t not in frozen]
        if toks and unknown and not all(_near_allowed(t, frozen) for t in unknown):
            gazetteer.setdefault(" ".join(toks), " ".join(w for w in p.split() if fold(w) not in COMMON_CAPS))
    cap_only: set[str] = set()
    for run in trip_name_places(trip_name):
        # an own place inside a run splits it: 'Mongolia Gobi' on a Mongolia piece → 'gobi'
        pieces: list[list[tuple[str, str]]] = [[]]
        for tok, disp in run:
            if tok in frozen or _near_allowed(tok, frozen):
                pieces.append([])
            else:
                pieces[-1].append((tok, disp))
        for piece in pieces:
            key = " ".join(t for t, _ in piece)
            if piece and key not in gazetteer:
                gazetteer[key] = " ".join(d for _, d in piece)
                cap_only.add(key)
    return ScopeLexicon(allowed=frozen, gazetteer=gazetteer, own_text=own, sibling_text=sib,
                        cap_only=frozenset(cap_only))


_NAME_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _name_tokens(text: Optional[str]) -> list[tuple[str, bool]]:
    """(folded token, capitalised?) for the words of `text` that can be part of a place key — the
    same filter as `phrase_tokens`, so "Tiger's Nest", "Tiger’s Nest" and 'Tiger Nest' all read
    'tiger nest', and 'Erdene-Zuu' reads 'erdene zuu'."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", strip_tags(text))
                     if not unicodedata.combining(c))
    out = []
    for m in _NAME_TOKEN_RE.finditer(folded):
        t = m.group(0).lower()
        if len(t) >= 3 and t not in COMMON_CAPS:
            out.append((t, m.group(0)[0].isupper()))
    return out


def _has_place(tokens: list[tuple[str, bool]], key: str, cap_only: bool = False) -> bool:
    want = key.split()
    n = len(want)
    for i in range(len(tokens) - n + 1):
        if [t for t, _ in tokens[i:i + n]] == want and (not cap_only or tokens[i][1]):
            return True
    return False


def names_place(text: Optional[str], key: str) -> bool:
    """Does `text` name the gazetteer place `key` (case-insensitive, accent-folded, punctuation
    between its words ignored)? choose_keyword uses this on lower-case DFS keywords."""
    return _has_place(_name_tokens(text), key)


def places_named(lex: ScopeLexicon, text: Optional[str]) -> list[str]:
    """Display forms of the gazetteer places `text` names (accent-folded, word-bounded; a
    trip-name word only when capitalised)."""
    toks = _name_tokens(text)
    return [disp for g, disp in sorted(lex.gazetteer.items()) if _has_place(toks, g, g in lex.cap_only)]


def find_scope_violations(lex: ScopeLexicon, *, title: Optional[str] = None, meta: Optional[str] = None,
                          body: Optional[str] = None) -> list[dict]:
    """[{"where": "title"|"meta"|"body", "place", "sentence"}] — one per place per sentence."""
    out: list[dict] = []
    for where, text in (("title", title), ("meta", meta)):
        for place in places_named(lex, text):
            out.append({"where": where, "place": place, "sentence": strip_tags(text).strip()})
    for span in sentence_spans(body or ""):
        for place in places_named(lex, span["clean"]):
            out.append({"where": "body", "place": place, "sentence": span["clean"]})
    return out


# ---------------------------------------------------------------- sentences

_HASHTAG_RE = re.compile(r"(?<![\w&#])#([A-Za-z][A-Za-z0-9]+)")
_HASHTAG_LINE_RE = re.compile(r"(?:#[A-Za-z][A-Za-z0-9]+[\s,]*)+")
_HEADING_RE = re.compile(r"#{1,6}\s")
_SENT_END_RE = re.compile(rf"[.!?…]+[\"'”’)*]*(?:\s?{_TAG})*(?=\s+\S|\s*$)")

# Sentence boundaries (AA-769 review, S234) — the ONE rule sentence_spans, expand_to_sentence and
# sentence_repair.locate_sentence share: a mark ('.', '!', '?', '…') is an end only when whitespace,
# a citation tag or the end of the text follows it and its closers (quotes, ')' and markdown
# '*' — "**Day one.** We drive"), never "12.5 km" / "U.S."; a period after a known abbreviation
# ("Mt. Fuji", "St. Petersburg", "No. 5", "ft. above") is not one either.
_SENTENCE_END_CHARS = ".!?…"
_CLOSERS = "\"'”’)*"
# The end mark (run), its closers, and the citation tags that follow it.
_SENTENCE_TRAILER_RE = re.compile(
    rf"[{re.escape(_SENTENCE_END_CHARS)}]+[{re.escape(_CLOSERS)}]*(?:\s?{_TAG})*")
_TAG_AT_RE = re.compile(_TAG)
_ABBREVIATIONS = frozenset({"mt", "st", "dr", "mr", "mrs", "ms", "prof", "jr", "sr", "vs",
                            "approx", "ca", "e.g", "i.e"})
# "No." is an abbreviation only before a number ("No. 5"); "Is it hard? No." ends a sentence.
_NUMBER_ABBREVIATIONS = frozenset({"no"})
# Units: an abbreviation only when the sentence runs on in lower case or a digit ("4,000 ft. above
# the valley"); "The pass sits at 4,000 ft. The trail is steep." ends at "ft.".
_UNIT_ABBREVIATIONS = frozenset({"ft", "km", "mi", "hr", "hrs", "min", "mins"})
# The word right before the dot — a whole word: "1st." / "21st." / "4ft." are not "st" / "ft".
_WORD_BEFORE_DOT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z.]+$")
_NEXT_RUNS_ON_RE = re.compile(r"\s*[a-z0-9]")
_DOTTED_ACRONYM_RE = re.compile(r"(?:[A-Za-z]\.)+[A-Za-z]")
_NEXT_IS_NUMBER_RE = re.compile(r"\s*\d")


def is_sentence_end(text: str, i: int) -> bool:
    """True when text[i] ('.', '!', '?' or '…') really ends a sentence: the mark run and its closers
    must be followed by whitespace, a citation tag or the end of the text — so a decimal
    ("12.5 km", "1.5 hours") or a dotted name ("U.S.", "e.g.") never is — and a period after a
    known abbreviation ("Mt. Fuji", "St. Petersburg", "No. 5") is not either."""
    if i < 0 or i >= len(text) or text[i] not in _SENTENCE_END_CHARS:
        return False
    j = i + 1
    while j < len(text) and text[j] in _SENTENCE_END_CHARS:
        j += 1
    while j < len(text) and text[j] in _CLOSERS:
        j += 1
    if j < len(text) and not text[j].isspace() and not _TAG_AT_RE.match(text, j):
        return False
    if text[i] == "." and (i + 1 >= len(text) or text[i + 1] not in _SENTENCE_END_CHARS):
        word = _WORD_BEFORE_DOT_RE.search(text, 0, i)
        w = word.group(0).lower() if word else ""
        if w in _ABBREVIATIONS or (word and _DOTTED_ACRONYM_RE.fullmatch(word.group(0))):
            return False
        if w in _NUMBER_ABBREVIATIONS and _NEXT_IS_NUMBER_RE.match(text, i + 1):
            return False
        if w in _UNIT_ABBREVIATIONS and _NEXT_RUNS_ON_RE.match(text, i + 1):
            return False
    return True


def sentence_tail_end(text: str, pos: int) -> Optional[int]:
    """End of the sentence running through `pos`: just after the first real sentence end
    (is_sentence_end) at or after `pos`, plus its closing quotes and trailing citation tags. None
    when a newline or the end of the text comes first (a list item or heading with no end mark)."""
    i = max(pos, 0)
    while i < len(text) and text[i] != "\n":
        if text[i] in _SENTENCE_END_CHARS and is_sentence_end(text, i):
            # back to the start of the mark run ("?!", "..."), then take the whole trailer
            while i > pos and text[i - 1] in _SENTENCE_END_CHARS:
                i -= 1
            return _SENTENCE_TRAILER_RE.match(text, i).end()
        i += 1
    return None


def sentence_spans(text: str) -> list[dict]:
    """Every sentence of `text` with absolute offsets: {"start", "end", "text", "clean", "kind",
    "in_faq"}. kind is prose | bullet | faq_q | faq_a | heading | hashtags. A FAQ answer's or a
    bullet's first sentence starts after its "A: " / "- " marker, so editing it keeps the
    marker. `clean` is tag-stripped, whitespace-normalised."""
    spans: list[dict] = []
    in_faq = False
    pos = 0
    for line in text.split("\n"):
        line_start = pos
        pos += len(line) + 1
        stripped = line.strip()
        if not stripped:
            continue
        lead = len(line) - len(line.lstrip())
        if _HEADING_RE.match(stripped):
            in_faq = stripped.lstrip("#").strip().lower() == "faq"
            spans.append(_span(text, line_start + lead, line_start + len(line.rstrip()), "heading", in_faq))
            continue
        if _HASHTAG_LINE_RE.fullmatch(stripped):
            spans.append(_span(text, line_start + lead, line_start + len(line.rstrip()), "hashtags", in_faq))
            continue
        if stripped.startswith("**Q:"):
            spans.append(_span(text, line_start + lead, line_start + len(line.rstrip()), "faq_q", in_faq))
            continue
        kind = "prose"
        if stripped.startswith("A:"):
            kind, lead = "faq_a", lead + 2
        elif stripped.startswith(("- ", "* ")):
            kind, lead = "bullet", lead + 2
        while lead < len(line) and line[lead] == " ":
            lead += 1
        cur = line_start + lead
        line_end = line_start + len(line.rstrip())
        for m in _SENT_END_RE.finditer(line, lead):
            if not is_sentence_end(line, m.start()):
                continue  # "Mt. Fuji", "No. 5": not a sentence end
            end = line_start + m.end()
            if end > cur:
                spans.append(_span(text, cur, end, kind, in_faq))
            cur = end
            while cur < line_end and text[cur] == " ":
                cur += 1
        if cur < line_end:
            spans.append(_span(text, cur, line_end, kind, in_faq))
    return spans


def _span(text: str, start: int, end: int, kind: str, in_faq: bool) -> dict:
    raw = text[start:end]
    return {"start": start, "end": end, "text": raw, "clean": " ".join(strip_tags(raw).split()),
            "kind": kind, "in_faq": in_faq}


_TRAILING_JUNK_RE = re.compile(rf"(?:\s|[{re.escape(_CLOSERS)}]|{_TAG})*$")
# A line's own marker, kept when its first sentence is cut or rewritten: a FAQ answer's "A: " or a
# bullet's "- " / "* " (sentence_spans starts those sentences after it the same way).
_LINE_MARKER_RE = re.compile(r"(?:A:|[-*])[ \t]+")
_HASHTAG_TOKEN_RE = re.compile(r"#[A-Za-z][A-Za-z0-9]*")
# A FAQ question line "**Q: …?**": group 2 is the question text, which a rewrite replaces (the
# markup stays, like an answer's "A: ").
_FAQ_Q_LINE_RE = re.compile(r"([ \t]*\*\*Q:[ \t]*)(.*?\S)([ \t]*\*\*[ \t]*)")


def expand_to_sentence(text: str, start: int, end: int) -> tuple[int, int]:
    """The whole sentence around text[start:end] (an F9_brand_style phrase such as "hidden gem",
    or a located sentence), so an admin cut or rewrite (AA-769) never leaves half a sentence.
    Walks back to just after the previous real sentence end (is_sentence_end: not a decimal, not
    "Mt.") or the line start — skipping a FAQ "A: " / bullet "- " marker there — and forward to
    the sentence end plus its closing quotes and trailing citation tags. Never crosses a newline:
    a list item with no end mark stops at its line end. On a hashtag-only line (F13 misspelt
    hashtag) the span is the hashtag token(s) themselves, never the whole line; on a FAQ question
    line it is the question text inside "**Q: " … "**", never the markup."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", start)
    le = len(text) if le < 0 else le
    q = _FAQ_Q_LINE_RE.fullmatch(text, ls, le)
    if q:  # a FAQ question: the question text inside its "**Q: " … "**" markup
        return q.start(2), q.end(2)
    if _HASHTAG_LINE_RE.fullmatch(text[ls:le].strip()):
        toks = [m.span() for m in _HASHTAG_TOKEN_RE.finditer(text, ls, le)
                if m.start() < max(end, start + 1) and m.end() > start]
        return (toks[0][0], toks[-1][1]) if toks else (start, end)
    s = start
    while s > ls:
        k = s - 1
        if text[k] in _SENTENCE_END_CHARS and is_sentence_end(text, k):
            break
        if text[k] in _CLOSERS:
            j = k  # back over the closer run ("?**", ".”)") to its end mark
            while j > ls and text[j] in _CLOSERS:
                j -= 1
            if text[j] in _SENTENCE_END_CHARS and is_sentence_end(text, j):
                break
        s -= 1
    if s == ls:
        lead = len(text[ls:start]) - len(text[ls:start].lstrip(" \t"))
        marker = _LINE_MARKER_RE.match(text, ls + lead)
        if marker and marker.end() <= start:
            s = marker.end()
    lead = re.match(rf"(?:\s|{_TAG})*", text[s:start])
    s += lead.end() if lead else 0
    # A span that already ends on its sentence's mark (a located sentence + its tags) is
    # extended from that mark; anything else from its end.
    core = s + _TRAILING_JUNK_RE.search(text[s:end]).start() if end > s else end
    pos = core - 1 if core > s and text[core - 1] in _SENTENCE_END_CHARS else end
    tail = sentence_tail_end(text, pos)  # never crosses a newline
    if tail is not None:
        return s, max(tail, end)
    e = le if le >= end else end
    while e > end and text[e - 1] in " \t":
        e -= 1
    return s, e


def tidy_after_cut(text: str) -> str:
    """Clean up after sentences were cut (hygiene.autofix and the AA-769 admin cut / rewrite): drop
    empty FAQ answers (with their question), empty bullets, headings whose section is now empty,
    spaces before a line end, a single stray space at a line start (never a 2+ space indent),
    doubled spaces and extra blank lines. Leading / trailing blank lines go; one trailing newline
    is kept when the input had one."""
    lines = [re.sub(r"(?<=\S) {2,}", " ", re.sub(r"^ (?=\S)", "", ln.rstrip())) for ln in text.split("\n")]
    kept: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s in ("A:", "-", "*"):
            if s == "A:" and kept and kept[-1].strip().startswith("**Q:"):
                kept.pop()
            continue
        kept.append(ln)
    out: list[str] = []
    for i, ln in enumerate(kept):
        m = _HEADING_RE.match(ln.strip())
        if m:
            level = len(ln.strip()) - len(ln.strip().lstrip("#"))
            nxt = next((k.strip() for k in kept[i + 1:] if k.strip()), None)
            if nxt is None or (_HEADING_RE.match(nxt) and len(nxt) - len(nxt.lstrip("#")) <= level):
                continue
        out.append(ln)
    tidy = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
    return tidy + ("\n" if text.endswith("\n") and tidy else "")


# ---------------------------------------------------------------- repeats

_STOP = frozenset("""a an the and or but of in on at to for with from by as is are was were be been being it its
this that these those which who whom what when where why how not no so than then there here into onto over
under about after before during their his her our your you we they he she them us one also just only very more
most such each every can could will would may might should has have had do does did""".split())


def content_words(sentence: str, ignore: frozenset = frozenset()) -> list[str]:
    """Stemmed content words (stop words, ≤2-letter words and `ignore` dropped; a trailing
    plural 's' cut)."""
    words = [w for w in _TOKEN_RE.findall(fold(strip_tags(sentence)))
             if w not in _STOP and len(w) > 2 and w not in ignore]
    return [w[:-1] if w.endswith("s") and len(w) > 4 else w for w in words]


def _grams(words: list[str], n: int) -> set:
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


_REPEAT_MIN_WORDS = 6


def brand_words(brand_name: Optional[str]) -> frozenset:
    """The brand's own words — ignored by the repeat rules ('Terra Family Expeditions' is in the
    CTA and in the brand's self-description by design, S233 r1: 7 false repeats)."""
    return frozenset(_TOKEN_RE.findall(fold(brand_name)))


def restates(a: str, b: str, ignore: frozenset = frozenset()) -> bool:
    """Does sentence `a` say what `b` already says? ≥3 shared content bigrams, or 2 bigrams plus a
    shared trigram, or content-word Jaccard ≥0.5 (both ≥6 content words). Used on a repair's
    replacement against its ±4 neighbours (S233 G7: #2/#9/#14 were repairs re-stating the
    'confluence of the Pho Chhu and Mo Chhu' sentence two places earlier). Calibrated on r1: the
    4 panel repeats hit; 'Four main temples… sacred caves' vs 'main temple… sacred cave' (2
    bigrams, no trigram) does not."""
    ca, cb = content_words(a, ignore), content_words(b, ignore)
    if len(ca) < _REPEAT_MIN_WORDS or len(cb) < _REPEAT_MIN_WORDS:
        return False
    bi = len(_grams(ca, 2) & _grams(cb, 2))
    return bi >= 3 or (bi >= 2 and bool(_grams(ca, 3) & _grams(cb, 3))) or _jaccard(ca, cb) >= 0.5


def near_verbatim(a: str, b: str, ignore: frozenset = frozenset()) -> bool:
    """A stricter `restates`: ≥2 shared content trigrams or Jaccard ≥0.5 — safe to auto-delete in
    a whole body."""
    ca, cb = content_words(a, ignore), content_words(b, ignore)
    if len(ca) < _REPEAT_MIN_WORDS or len(cb) < _REPEAT_MIN_WORDS:
        return False
    return len(_grams(ca, 3) & _grams(cb, 3)) >= 2 or _jaccard(ca, cb) >= 0.5


# AA-768 (S234 round 2 fix) — a FAQ answer sentence COPIED from one body sentence: most of its
# content words appear in that sentence IN THE SAME ORDER (difflib matching blocks). Stricter than
# `restates` / `near_verbatim` on purpose: in a seed-bound blog the answer and the body both phrase
# the same seed fact, so they routinely share trigrams ('central asian steppe') and most of their
# vocabulary while answering a reader's question in other words — a normal FAQ (S233 F7 bans copies
# only). Calibrated on the S234 r1 + r2 blogs and 8 regenerated FAQs: lightly edited copies ('The
# crater is roughly 600 metres wide and between 50 to 60 metres deep, with a caldera floor…' against
# the body's 'The crater is roughly 600 metres wide and 50 to 60 metres deep, and the caldera
# floor…') reach 0.82–0.93; the same fact retold (reordered numbers, a new clause) stays ≤ 0.79.
COPY_ORDERED_SHARE = 0.8
_COPY_MIN_WORDS = 4


def copies(a: str, b: str, ignore: frozenset = frozenset()) -> bool:
    """Is sentence `a` a near-verbatim copy of sentence `b`: ≥ COPY_ORDERED_SHARE of a's content
    words appear in b in the same order. `a` needs ≥ 4 content words; a short answer such as
    'Yes, at Sod Camp.' is never a copy."""
    ca, cb = content_words(a, ignore), content_words(b, ignore)
    if len(ca) < _COPY_MIN_WORDS or not cb:
        return False
    matched = sum(m.size for m in difflib.SequenceMatcher(None, ca, cb, autojunk=False).get_matching_blocks())
    return matched / len(ca) >= COPY_ORDERED_SHARE


# ---------------------------------------------------------------- leak and social proof

# The prompt's own vocabulary showing up in published text (S233: "The content seed describes
# this as a 15-day journey…", "The content seed for this journey notes that…").
LEAK_RE = re.compile(
    r"\b(?:content seed|the seed|this seed|source itinerary|place background|the itinerary calls it"
    r"|other trips that include|for context only|source notes)\b|\[moment\b|\bscope:", re.I)
_LEAK_FRAME_RE = re.compile(
    r"^(?:(?:the|this|our) (?:content )?seed(?: (?:for|of) this (?:journey|trip|route|piece))?"
    r" (?:notes|says|mentions|states|records|lists|explains|adds|confirms)(?: that)?,?"
    r"|(?:as|according to) (?:the|this) (?:content )?seed(?: (?:for|of) this (?:journey|trip|route|piece))?"
    r"(?: (?:notes|says|mentions|states|puts it))?,)\s+", re.I)
_LEAK_DESCRIBES_RE = re.compile(
    r"^(?:the|this) (?:content )?seed(?: (?:for|of) this (?:journey|trip|route|piece))? describes"
    r" (this|it|the [a-z]+(?<!s)) as\s+", re.I)

# What travellers/guests/families feel, say or remember — never sourced (S233 G3: #5 "tends to
# stay with", #16 "sit longest"). Bare 'review' is left out: it matched 'varies' style FPs.
SOCIAL_PROOF_RE = re.compile(
    r"\b(?:(?:travell?ers|guests|visitors|families|parents|kids|children|people)"
    r" (?:often |usually |always |typically |tend to )?(?:love|adore|rave|say|tell us|remember|agree)\b"
    r"|(?:most|many) (?:of )?(?:our )?(?:visitors|travell?ers|families|guests|people|parents)"
    r" (?:say|find|describe|agree|remember|love|choose|come away)"
    r"|popular with|a favou?rite (?:of|with|among)|reviews? (?:say|praise|mention)"
    r"|(?:highly|top|best|five[- ]star)[- ]rated|five[- ]star|word of mouth"
    r"|tends? to (?:stay|sit|stick|linger|remain) (?:with|in)|(?:stays?|sits?|lingers?) longest"
    r"|everyone (?:who|says)|our (?:clients|guests|travell?ers) (?:say|love|tell))", re.I)

_SOCIAL_SUPPORT_MIN = 0.30  # social-proof sentence auto-deleted only below this seed overlap


# ---------------------------------------------------------------- numbers in words (S234 round 2)

_SMALL_NUMBERS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
_TENS_WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}
# '15', 'fifteen', 'twenty-one', 'thirty' — the small numbers a day count, a day index or an age uses.
NUM_TOKEN = (r"(?:\d{1,3}|(?:twenty|thirty|forty|fifty|sixty)(?:-(?:one|two|three|four|five|six|seven|eight|nine))?"
             r"|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|eleven|twelve|ten"
             r"|one|two|three|four|five|six|seven|eight|nine)")


def small_number(token: str) -> Optional[int]:
    """'15' → 15, 'fifteen' → 15, 'twenty-one' → 21; None for anything else."""
    t = (token or "").strip().lower()
    if t.isdigit():
        return int(t)
    if t in _SMALL_NUMBERS:
        return _SMALL_NUMBERS[t]
    head, _, unit = t.partition("-")
    if head in _TENS_WORDS and (not unit or unit in _SMALL_NUMBERS):
        return _TENS_WORDS[head] + (_SMALL_NUMBERS[unit] if unit else 0)
    return None


_RANGE_SEP = r"\s*(?:–|—|-|to|-to-|through|and)\s*"
_NUM_RANGE_RE = re.compile(rf"(?<![\w.,])({NUM_TOKEN}){_RANGE_SEP}({NUM_TOKEN})(?![\w])", re.I)


def number_ranges(text: Optional[str]) -> set[tuple[int, int]]:
    """Every 'a–b' / 'a to b' / 'seven-to-sixteen' range in `text` as (a, b) values, a < b."""
    out = set()
    for m in _NUM_RANGE_RE.finditer(text or ""):
        a, b = small_number(m.group(1)), small_number(m.group(2))
        if a is not None and b is not None and a < b:
            out.add((a, b))
    return out


# ---------------------------------------------------------------- writer-instruction leaks (S234 round 2)

# The writer's brief showing through as published text (S234 panel §2 class 5): an H2 that reads
# like an instruction ("A Single Sentence on What Time Has Taken", #10; "A Sentence on Loss", #16),
# the piece describing itself ("This piece traces one route…", #18; "This guide covers days three
# through fourteen…", #9/#17; "What follows is a guide to the route…", #19).
# 'guide' and 'post' only with 'this' ('the guide will meet you…' is the tour guide, 'the post' a
# border or trading post), and a 'will' only with a self-describing verb — never an open 'will \w+'.
_SELF_VERBS = (r"traces|covers|explores|follows|looks\s+at|focuses\s+on|examines|walks\s+(?:you\s+)?through"
               r"|describes|sets\s+out|is\s+about"
               r"|will\s+(?:trace|cover|explore|follow|look\s+at|focus\s+on|examine|walk\s+(?:you\s+)?through"
               r"|describe|set\s+out|show\s+you|take\s+you\s+through)")
INSTRUCTION_LEAK_RE = re.compile(
    r"\b(?:a|one)\s+(?:single\s+|short\s+|brief\s+|final\s+)?(?:sentence|paragraph|line)\s+(?:on|about)\b"
    rf"|\b(?:this|the)\s+(?:piece|article|blog\s+post|write-up)\s+(?:{_SELF_VERBS})\b"
    rf"|\bthis\s+(?:guide|post)\s+(?:{_SELF_VERBS})\b"
    r"|\bin\s+this\s+(?:piece|post|article|guide)\b"
    r"|\bwhat\s+follows\s+is\s+(?:a|an|the)\b", re.I)
_DAY_RANGE_RE = re.compile(rf"\bdays\s+({NUM_TOKEN}){_RANGE_SEP}({NUM_TOKEN})\b", re.I)
_SCOPE_DAYS_RE = re.compile(r"\bdays?\s+(\d+)(?:\s*[–—-]\s*(\d+))?", re.I)


def scope_days(scope: Optional[str]) -> Optional[tuple[int, int]]:
    """(first, last) day of the SCOPE line: 'covers days 3–14' → (3, 14); '(day 2)' → (2, 2)."""
    m = _SCOPE_DAYS_RE.search(scope or "")
    if not m:
        return None
    first = int(m.group(1))
    return first, int(m.group(2)) if m.group(2) else first


def instruction_leaks(sentence: str, scope: Optional[str] = None) -> list[str]:
    """The writer-instruction phrases in one sentence or heading: INSTRUCTION_LEAK_RE, plus a day
    range that is exactly the SCOPE line's span ('days three through fourteen' on a days 3–14
    piece — the scope mechanics, not something a reader needs)."""
    out = [m.group(0) for m in INSTRUCTION_LEAK_RE.finditer(sentence or "")]
    span = scope_days(scope)
    if span and span[0] != span[1]:
        for m in _DAY_RANGE_RE.finditer(sentence or ""):
            if (small_number(m.group(1)), small_number(m.group(2))) == span:
                out.append(m.group(0))
    return out


_PERSON_NOUNS = (r"travell?ers?|famil(?:y|ies)|kids?|child(?:ren)?|adults?|teens?|teenagers?|youngsters?"
                 r"|explorers?|adventurers?|couples?|parents?|guests?|hikers?|professionals?|crowd")
# A range followed by a unit is a measurement ('25 to 40 minutes', 'seven to sixteen hours', '25–40 °C'),
# never an age, whatever the brand's audience line says.
_UNIT_AFTER_RE = re.compile(
    r"^[\s-]*(?:minutes?|mins?|hours?|hrs?|days?|nights?|weeks?|months?|seconds?|km|kilomet(?:re|er)s?|miles?"
    r"|metres?|meters?|m\b|feet|foot|ft|percent|per\s+cent|%|°|degrees?|kg|kilos?|litres?|liters?|people|guests"
    r"|travell?ers\s+(?:per|in\s+a)|steps?|switchbacks?|stops?|dollars?|usd|euros?)", re.I)
# ...and it is an age only in an age context: 'aged/ages 7–16', '25–40 years old / year-old', 'the
# 25–40 range', 'a 25–40 traveller', 'children (aged|between|of) seven and sixteen'.
_AGE_AFTER_RE = re.compile(
    rf"^(?:[\s-]*(?:years?|yrs?)[\s-]*olds?\b|[\s-]*year-olds?\b"
    rf"|\s+(?:age\s+)?(?:range|bracket|group|band|demographic)\b|[\s-]+(?:{_PERSON_NOUNS})\b)", re.I)
_AGE_BEFORE_RE = re.compile(
    rf"(?:\b(?:aged|ages|age)\s+|\b(?:{_PERSON_NOUNS})\s+(?:(?:aged|of|between|from)\s+)?)$", re.I)


def _audience_hits(sentence: str, want: set) -> list[re.Match]:
    out = []
    for m in _NUM_RANGE_RE.finditer(sentence or ""):
        if (small_number(m.group(1)), small_number(m.group(2))) not in want:
            continue
        after, before = sentence[m.end():], sentence[:m.start()]
        if _UNIT_AFTER_RE.match(after):
            continue
        if _AGE_AFTER_RE.match(after) or _AGE_BEFORE_RE.search(before):
            out.append(m)
    return out


def audience_leaks(sentence: str, audience: Optional[str], own_text: Optional[str]) -> list[str]:
    """The brand's Audience ranges ('25–40 years old', 'children aged 7–16') in a sentence, in
    digits or words ('seven-to-sixteen'), used as an AGE (an age word or a person noun next to it,
    no unit after it), when the moment's source text (`own_text`: its own seed lines, plus the
    Facts block when the caller has one) does not have the same range (S234 #7 'If you're a 25–40
    traveller', #2 'the seven-to-sixteen range'). 'The drive takes 25 to 40 minutes' is not one."""
    want = number_ranges(audience) - number_ranges(own_text)
    if not want:
        return []
    return [m.group(0) for m in _audience_hits(sentence, want)]


# A pronoun or demonstrative that needs the sentence before it. 'This dzong…' (a determiner and a
# noun) is fine; 'This is…', 'It was…', 'They…', 'What they find…' are not, at a piece or paragraph
# opening (S234 #19 opened on "What they find at Lake Khövsgöl is…" after a repair cut its referent).
_ANAPHOR_OPENER_RE = re.compile(
    r"^(?:it|they|them|what\s+(?:they|it|this|that|these|those)|such|"
    r"(?:this|that|these|those)(?=\s*(?:[,.;:!?—–]|$|\s+(?:is|was|are|were|has|had|have|'s|’s|means|meant|makes"
    r"|made|matters|explains|shows|gives|sets|changes|will|would|can|could|may|might|does|did|isn't|wasn't"
    r"|aren't|is\s+not|was\s+not|alone|too|also|all|one|kind|sort|detail|matters)\b)))\b", re.I)


def dangling_opener(sentence: str) -> bool:
    """Does `sentence` open on a pronoun that needs an earlier sentence ('It…', 'This is…',
    'What they find…')?"""
    return bool(_ANAPHOR_OPENER_RE.match(strip_tags(sentence or "").strip().lstrip("\"'“‘*")))


def _openers(text: str) -> list[str]:
    """The piece's first prose sentence and the first sentence of every later prose paragraph
    (clean text), in order — what a reader meets without the sentence before it."""
    out, prev_end, prev_kind = [], None, None
    for s in sentence_spans(text or ""):
        kind, after_q = s["kind"], prev_kind == "faq_q"
        prev_kind = kind
        if kind not in ("prose", "bullet", "faq_a"):
            prev_end = None
            continue
        gap = text[prev_end:s["start"]] if prev_end is not None else "\n\n"
        # an FAQ answer's first sentence has its question right above it as the referent
        # ('Q: How deep is Lake Khövsgöl? A: It reaches 262 metres…') — not an opener
        if ("\n\n" in gap or prev_end is None) and not (kind == "faq_a" and after_q):
            out.append(s["clean"])
        prev_end = s["end"]
    return out


def dangling_openers(text: str, before: Optional[str] = None) -> list[str]:
    """Paragraph / piece openers of `text` that need a sentence before them. With `before` (the
    text before a repair), only the ones `before` did not already have — the repair made them."""
    had = {o for o in _openers(before) if dangling_opener(o)} if before is not None else set()
    return [o for o in _openers(text) if dangling_opener(o) and o not in had]


def piece_opens_dangling(text: str) -> Optional[str]:
    """The piece's very first prose sentence when it opens on a pronoun with no referent."""
    ops = _openers(text)
    return ops[0] if ops and dangling_opener(ops[0]) else None


# At the very start of a piece only 'What they / this / these… ' is certainly anaphora (S234 #19
# 'What they find at Lake Khövsgöl is…', left by a repair). 'It takes four hours…', 'It is 5 a.m. in
# Paro', 'This is Punakha Dzong…', 'They call it…' are ordinary hooks with an impersonal or
# presentational subject — a warning, never a blocking leak.
_CERTAIN_ANAPHOR_RE = re.compile(r"^what\s+(?:they|this|that|these|those)\b", re.I)


def certain_anaphor(sentence: str) -> bool:
    return bool(_CERTAIN_ANAPHOR_RE.match(strip_tags(sentence or "").strip().lstrip("\"'“‘*")))


def repeated_sentences(text: str, ignore: frozenset = frozenset(), cta: Optional[str] = None) -> list[str]:
    """Prose sentences that near-verbatim repeat an earlier one (the F13 repeat rule)."""
    prose = [s for s in sentence_spans(text or "") if s["kind"] in ("prose", "bullet") and not s["in_faq"]]
    out = []
    for a, s in enumerate(prose):
        if _is_cta(s["clean"], cta):
            continue
        if any(near_verbatim(s["clean"], t["clean"], ignore) for t in prose[:a]):
            out.append(s["clean"])
    return out


def integrity_problems(before: str, after: str, ignore: frozenset = frozenset(),
                       cta: Optional[str] = None) -> list[str]:
    """What a repair (sentence splice, a regenerated section, a rewritten first line) broke: a new
    near-verbatim repeat, or a new paragraph / piece opener with no referent. [] = the repair is
    safe to keep (S234 panel §5 fix 7: #19 'What they find…', duplicated sentences in #10/#19)."""
    out = []
    old_rep = set(repeated_sentences(before, ignore, cta))
    out += [f"repeats an earlier sentence: '{s[:200]}'" for s in repeated_sentences(after, ignore, cta)
            if s not in old_rep]
    out += [f"opens on a pronoun with no referent: '{s[:200]}'" for s in dangling_openers(after, before)]
    return out


# ---------------------------------------------------------------- trip name and trip length (S234 round 2)

_TRIP_DAYS_RE = re.compile(rf"\b({NUM_TOKEN})[\s-]*(?:days?|d)\b", re.I)


def trip_length(trip_name: Optional[str]) -> Optional[int]:
    """The trip's day count from its name: 'Mongolia: … — 15 Days' → 15, 'Fifteen-Day …' → 15."""
    m = _TRIP_DAYS_RE.search(trip_name or "")
    return small_number(m.group(1)) if m else None


def scope_is_partial(scope: Optional[str], trip_name: Optional[str]) -> bool:
    """Does the SCOPE line cover only part of the trip? A one-moment scope always does; a span does
    unless it runs from day 1 to the trip's last day. No SCOPE line → False (nothing to hold to)."""
    if not scope:
        return False
    span, total = scope_days(scope), trip_length(trip_name)
    if span is None or total is None:
        return True
    return not (span[0] <= 1 and span[1] >= total)


# Generic geography words that follow a place in a trip name ('Gobi Desert', 'Halong Bay').
_GEO_WORDS = ("Desert|Lake|Lakes|River|Rivers|Valley|Valleys|Mountains?|Range|Steppe|Forest|Island|Islands|Bay"
              "|Coast|Highlands|Region|Province|National Park|Park|Falls|Delta|Plateau|Peninsula")
_DURATION_RE = re.compile(
    rf"\s*(?:[—–-]|\(|,)?\s*\b{NUM_TOKEN}[\s-]*(?:days?|d)(?:\s*(?:/|and|&)\s*{NUM_TOKEN}[\s-]*(?:nights?|n))?\b\)?",
    re.I)


def scoped_trip_name(trip_name: Optional[str], scope: Optional[str], lexicon: Optional[ScopeLexicon]) -> Optional[str]:
    """The TRIP line the writer gets (S234 panel §5 fix 4). When the SCOPE covers only part of the
    trip, drop the trip's duration ('— 15 Days', 'Fifteen-Day') and the places of its name that
    are outside the scope ('and Gobi Desert'): 'Mongolia: Khövsgöl Lake and Gobi Desert — 15 Days'
    → 'Mongolia: Khövsgöl Lake'. The full name otherwise. None when nothing is left."""
    if not trip_name or not scope_is_partial(scope, trip_name):
        return trip_name
    name = _DURATION_RE.sub("", trip_name)
    cut_inside_phrase = False
    if lexicon is not None:
        for key in lexicon.cap_only:
            disp = lexicon.gazetteer.get(key) or ""
            if not disp:
                continue
            words = r"[\s-]+".join(re.escape(w) for w in disp.split())
            generic = rf"(?:\s+(?:{_GEO_WORDS}))*"
            pat = re.compile(rf"(?:\s*(?:,|&|\+|/|\band\b|\bor\b))?\s*\b{words}\b{generic}", re.I)
            for m in pat.finditer(name):
                # 'Laos to Vietnam by bicycle', 'Angkor to the Mekong Delta': the place is half of a
                # route phrase — cutting it leaves 'Laos to by bicycle'
                if _ROUTE_CONNECTOR_BEFORE_RE.search(name[:m.start()]):
                    cut_inside_phrase = True
            name = pat.sub("", name)
    name = re.sub(r"\s{2,}", " ", name)
    name = re.sub(r"^[\s:,&/+—–-]+|[\s:,&/+—–-]+$", "", name)
    name = re.sub(r"\s+(?:and|or|&)$", "", name, flags=re.I).strip()
    if len(name) < 3 or cut_inside_phrase or _broken_trip_name(name):
        # never a half-cut name in the writer's TRIP line (S234 round 2 review): no TRIP line instead
        return None
    return name


_ROUTE_CONNECTOR_BEFORE_RE = re.compile(r"\b(?:to|from|via|through|into|towards?|the|of)\s*$", re.I)
_DOUBLE_CONNECTOR_RE = re.compile(r"\b(?:to|from|of|and|or|via)\s+(?:to|from|of|and|or|via|by|with|in)\b", re.I)
_DANGLING_CONNECTOR_RE = re.compile(r"\b(?:to|the|of|and|or|from|via|through|by|with|in|&)\s*(?:[:,;—–(-]|$)", re.I)


def _broken_trip_name(name: str) -> bool:
    """A cut that left the name malformed: a connector before the end or punctuation ('Cambodia:
    Angkor to the'), two connectors in a row ('Laos to by bicycle'), ':,' ('Vietnam:, Halong Bay'),
    or a stray one- or two-letter capitalised token left from a multi-word place ('… Halong Bay An'
    from 'Hoi An')."""
    if (_DANGLING_CONNECTOR_RE.search(name) or _DOUBLE_CONNECTOR_RE.search(name)
            or re.search(r"[:,;]\s*[:,;]", name)):
        return True
    return any(len(tok) <= 2 and tok[:1].isupper() for tok in re.findall(r"[^\W\d_]+", name)[1:])


def _trip_length_alt(total: int) -> str:
    """Regex alternation for a day count in digits or words: 15 → '15|fifteen'."""
    words = [w for w, v in _SMALL_NUMBERS.items() if v == total]
    if 20 < total < 70:
        tens = max((t for t, v in _TENS_WORDS.items() if v <= total), key=_TENS_WORDS.get)
        unit = total - _TENS_WORDS[tens]
        words.append(tens + (f"-{next(w for w, v in _SMALL_NUMBERS.items() if v == unit)}" if unit else ""))
    return "|".join([str(total)] + [re.escape(w) for w in words])


def _trip_length_re(total: int) -> re.Pattern:
    return re.compile(rf"(?<![\w-])(?:{_trip_length_alt(total)})[\s-]+(?:days?|nights?)\b", re.I)


_JOURNEY_END_RE = re.compile(
    r"\b(?:the|this|our|your)\s+(?:whole\s+|full\s+)?(?:journey|trip|route|tour|itinerary|expedition)\s+"
    r"(?:then\s+|finally\s+)?(?:concludes|ends|closes|finishes|wraps\s+up|comes\s+to\s+(?:an|its)\s+end)\b", re.I)


def trip_length_mentions(sentence: str, ctx: "HygieneContext") -> list[str]:
    """The whole trip's length ('fifteen-day', '15 days') in a sentence of a piece that covers only
    part of the trip, when the moment's own source lines do not state it (S234: 4/4 Terra blogs)."""
    total = trip_length(ctx.trip_name)
    if total is None or not scope_is_partial(ctx.scope, ctx.trip_name):
        return []
    rx = _trip_length_re(total)
    if rx.search(ctx.lexicon.own_text or ""):
        return []
    # the trip's own name quoted in full ('the Bhutan Cultural Discovery - 8 Days itinerary') is the
    # trip-name rule's business (autofix), not a length claim
    clean = re.sub(re.escape(ctx.trip_name or ""), " ", strip_tags(sentence), flags=re.I) if ctx.trip_name else sentence
    return [m.group(0) for m in rx.finditer(clean)]


def journey_end_claims(sentence: str, ctx: "HygieneContext") -> list[str]:
    """'The journey concludes / the route closes …' in a piece whose SCOPE stops before the trip's
    last day (S234 #17 'The route closes, on day fourteen', #18 'The journey concludes with…')."""
    if not scope_is_partial(ctx.scope, ctx.trip_name):
        return []
    span, total = scope_days(ctx.scope), trip_length(ctx.trip_name)
    if span is not None and total is not None and span[1] >= total:
        return []
    return [m.group(0) for m in _JOURNEY_END_RE.finditer(strip_tags(sentence))]


# ---------------------------------------------------------------- contrast frames + sibling templates (S234 round 2)

# "not X, it is Y" family (S234 panel §2 class 1: 18/20 pieces). Each pattern is one frame; a
# sentence pair "That's not filler. It's a structural choice." is one frame too.
CONTRAST_FRAME_RES = tuple(re.compile(p, re.I) for p in (
    r"\b(?:is|was|are|were)\s+not\b[^.!?;]{1,80}?[,;:—–]\s*(?:it|this|that|they)(?:['’]s|['’]re|\s+is|\s+was"
    r"|\s+are|\s+were)\b",
    r"\b(?:is|was|are|were)n['’]t\b[^.!?;]{1,80}?[,;:—–]\s*(?:it|this|that|they)(?:['’]s|['’]re|\s+is|\s+was"
    r"|\s+are|\s+were)\b",
    r"(?:\bnot\b|n['’]t\b)[^.!?\n]{0,80}[.!?][\"'”’]?[ \t]+(?:It|This|That|They)(?:['’]s|['’]re|\s+is|\s+was|\s+are"
    r"|\s+were)\b",
    r"\bnot\s+(?:just|only|merely|simply)\b[^.!?;]{1,80}?\bbut\b",
    r"\bless\s+(?:a\s+|an\s+|about\s+)?\w+(?:\s+\w+){0,4}\s+than\b",
    r"\b(?:this|that|it)\s+is\s+not\s+(?:a\s+)?metaphor\b",
    r"\bnot\s+(?:a\s+|an\s+|the\s+)?\w+(?:\s+\w+){0,3}\s+but\s+(?!also\b)\w+",
    r"[—–,;]\s*not\s+(?:just|only|merely|simply)\s+\w+",
))
CONTRAST_WORDS_PER_FRAME = 150


def contrast_frames(text: str) -> list[tuple[int, int, str, int]]:
    """(start, end, matched text, pattern index) of every contrast frame, non-overlapping, in text
    order."""
    clean = text or ""
    hits: list[tuple[int, int, str, int]] = []
    for k, rx in enumerate(CONTRAST_FRAME_RES):
        for m in rx.finditer(clean):
            if not any(m.start() < e and s < m.end() for s, e, _, _ in hits):
                hits.append((m.start(), m.end(), m.group(0), k))
    return sorted(hits)


def contrast_excess(text: str) -> tuple[int, int, list[tuple[int, int, str, int]]]:
    """(frames, allowed, frames) — allowed is one frame per CONTRAST_WORDS_PER_FRAME words (at least
    one). A piece is over the line when frames > allowed (S234 panel §5 fix 6: '>1 per 150 words')."""
    frames = contrast_frames(text)
    words = len(strip_tags(text or "").split())
    allowed = max(1, words // CONTRAST_WORDS_PER_FRAME)
    return len(frames), allowed, frames


# Cross-piece templates (S234 panel §2 class 8). The plan said "a shared 8-gram"; on the 20 stored
# pieces no target pair shares one ('That's not filler. It's a structural choice.' / 'That's not
# downtime filler. It's a deliberate call.'; 'a well-prepared family arrives knowing exactly what
# to expect' / '… arrives already knowing what to expect'), while shared seed facts do. So a line is
# a template when it shares with an earlier sibling (A) a contiguous run of TEMPLATE_RUN words, or
# (B) a sentence (or two-sentence window) at SequenceMatcher ratio ≥ TEMPLATE_RATIO with
# TEMPLATE_RUN matched words — and in both cases at least a third of the matched content words are
# NOT source words (seed, CTA, brand name, number words): a repeated seed fact is not a template. A
# sentence naming the brand (its sign-off / self-description) is skipped.
TEMPLATE_RUN = 5
TEMPLATE_RATIO = 0.6
_WORD_RE = re.compile(r"[a-z0-9]+")
_NUMBER_WORDS = frozenset(set(_SMALL_NUMBERS) | set(_TENS_WORDS) | {
    "hundred", "thousand", "million", "mid", "century", "centuries", "first", "second", "third", "fourth", "fifth",
    "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth",
    "sixteenth", "seventeenth", "eighteenth", "nineteenth", "twentieth"})


def _stem(w: str) -> str:
    return w[:-1] if w.endswith("s") and len(w) > 4 else w


def _template_units(text: str, kinds: tuple) -> list[tuple[dict, list[str]]]:
    return [(sp, _WORD_RE.findall(fold(sp["clean"]))) for sp in sentence_spans(text or "") if sp["kind"] in kinds]


def sibling_templates(text: str, siblings: list[str], *, exclude: list[str] = (),
                      brand_name: Optional[str] = None, keyword: Optional[str] = None,
                      kinds: tuple = ("prose", "bullet", "faq_a")) -> list[tuple[str, str]]:
    """[(sentence of `text`, the shared words)] — lines of `text` (spans of `kinds`; pass
    ("heading",) for headings) that repeat an earlier piece of the same brief (rules A/B above).
    `exclude` = source texts (own seed lines, Facts block, CTA, keyword): their words are never
    template words. A sentence carrying the SEO `keyword` is skipped — siblings of one request
    share the keyword by design (S233 #13/#16 'Khovsgol and Gobi tour Mongolia price')."""
    import difflib

    if not siblings:
        return []
    source = set(_NUMBER_WORDS) | brand_words(brand_name)
    for e in list(exclude) + [keyword or ""]:
        source |= {_stem(w) for w in _WORD_RE.findall(fold(strip_tags(e or "")))}
    kw_words = _WORD_RE.findall(fold(keyword or ""))

    def has_keyword(words: list[str]) -> bool:
        n = len(kw_words)
        return n > 0 and any(words[k:k + n] == kw_words for k in range(len(words) - n + 1))

    def novel_ok(words: list[str], min_novel: int) -> bool:
        # S234 round 2 review: at least HALF the matched content words must be new (was a third —
        # a seed fact paraphrased the same way twice, 'A mineral spring in the vicinity carries a
        # long local…', passed at 2 of 6)
        content = [_stem(w) for w in words if w not in _STOP and len(w) > 2]
        novel = [w for w in content if w not in source]
        return len(novel) >= min_novel and 2 * len(novel) >= len(content)

    mine = _template_units(text, kinds)
    sib_units: list[list[str]] = []
    sib_stream: list[list[str]] = []
    for sib in siblings:
        units = _template_units(sib, kinds)
        sib_units += [u for _, u in units]
        sib_units += [units[k][1] + units[k + 1][1] for k in range(len(units) - 1)]
        sib_stream.append([w for _, u in units for w in u])
    out: dict[str, str] = {}
    brand = brand_words(brand_name)
    for k, (sp, words) in enumerate(mine):
        if len(words) < 4 or sp["clean"] in out:
            continue
        if brand and brand <= set(words):
            continue  # the brand's own sign-off line ('Trail Pulse builds trips around…') is not a template
        if has_keyword(words):
            continue
        window = words + (mine[k + 1][1] if k + 1 < len(mine) else [])
        for stream in sib_stream:  # A: a contiguous run that starts in this sentence
            sm = difflib.SequenceMatcher(None, window, stream, autojunk=False)
            blk = next((b for b in sm.get_matching_blocks() if b.size >= TEMPLATE_RUN and b.a < len(words)
                        and novel_ok(window[b.a:b.a + b.size], 2)), None)
            if blk is not None:
                out[sp["clean"]] = " ".join(window[blk.a:blk.a + blk.size])
                break
        if sp["clean"] in out:
            continue
        for unit in sib_units:  # B: the same sentence shape with a word or two changed
            for mine_unit in (words, window):
                sm = difflib.SequenceMatcher(None, mine_unit, unit, autojunk=False)
                if sm.real_quick_ratio() >= TEMPLATE_RATIO and sm.ratio() >= TEMPLATE_RATIO:
                    break
            else:
                continue
            blocks = [b for b in sm.get_matching_blocks() if b.size]
            matched = [mine_unit[b.a + i] for b in blocks for i in range(b.size)]
            # the words matched inside THIS sentence decide (a window must not lend its next sentence)
            own = [mine_unit[b.a + i] for b in blocks for i in range(b.size) if b.a + i < len(words)]
            if len(matched) >= TEMPLATE_RUN and len(own) >= 3 and novel_ok(own, 1):
                out[sp["clean"]] = " ".join(matched)
                break
    return list(out.items())


def _seed_overlap(sentence: str, seed_words: set) -> float:
    words = content_words(sentence)
    return sum(w in seed_words for w in words) / len(words) if words else 0.0


# ---------------------------------------------------------------- hashtags

def _tag_parts(tag: str) -> list[str]:
    return re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+(?![a-z])|\d+", tag)


def _seed_name_words(lex: ScopeLexicon) -> dict[str, str]:
    """Folded → display word for every word of the seed's own proper nouns (≥4 letters)."""
    words: dict[str, str] = {}
    for p in proper_nouns(lex.own_text):
        for w in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", p):
            f = fold(w)
            if len(f) >= 4 and f not in COMMON_CAPS:
                words.setdefault(f, f.capitalize())
    return words


def hashtag_fixes(text: str, lex: ScopeLexicon, brand_name: Optional[str] = None) -> dict[str, str]:
    """{'#PakoTaktsang': '#ParoTaktsang'} — a hashtag part one edit away from a seed place word
    is a misspelling. Plurals ('TigersNest') and the brand's own words ('TrailPulse') are left
    alone; distance 2 is never auto-fixed (S234 C3: it would touch TigersNest/TigerNest)."""
    names = _seed_name_words(lex)
    brand = {fold(w) for w in re.findall(r"[A-Za-z]+", brand_name or "")}
    fixes: dict[str, str] = {}
    for tag in dict.fromkeys(_HASHTAG_RE.findall(text or "")):
        parts, changed = _tag_parts(tag), False
        for i, part in enumerate(parts):
            p = fold(part)
            if len(p) < 4 or p in names or p in brand:
                continue
            near = [w for w in names if abs(len(w) - len(p)) <= 1 and lev(w, p) == 1 and not _plural_pair(w, p)]
            if len(near) == 1:
                parts[i] = names[near[0]] if part[0].isupper() else near[0]
                changed = True
        if changed:
            fixes[f"#{tag}"] = "#" + "".join(parts)
    return fixes


def _apply_hashtag_layout(text: str, hashtags_max: Optional[int]) -> tuple[str, list[str]]:
    """Hashtag-only lines go to the end as one line, capped at `hashtags_max` (channel spec)."""
    if hashtags_max is None:
        return text, []
    lines = text.rstrip("\n").split("\n")
    tag_lines = [i for i, ln in enumerate(lines) if ln.strip() and _HASHTAG_LINE_RE.fullmatch(ln.strip())]
    if not tag_lines:
        return text, []
    tags = list(dict.fromkeys(t for i in tag_lines for t in re.findall(r"#[A-Za-z][A-Za-z0-9]+", lines[i])))
    body = [ln for i, ln in enumerate(lines) if i not in set(tag_lines)]
    while body and not body[-1].strip():
        body.pop()
    kept = tags[:hashtags_max]
    log = []
    if len(tags) > len(kept):
        log.append(f"kept {len(kept)} of {len(tags)} hashtags (channel cap {hashtags_max})")
    if tag_lines != [len(lines) - 1] or len(tags) > len(kept):
        new = "\n".join(body) + ("\n\n" + " ".join(kept) if kept else "")
        if not log:
            log.append("moved the hashtags to the last line")
        return new + ("\n" if text.endswith("\n") else ""), log
    return text, []


def _split_first_line(text: str, first_line_max: Optional[int]) -> tuple[str, list[str]]:
    """Social hook: when the first line is longer than the channel's `first_line_max`, break it
    after its first sentence (if that sentence fits)."""
    if not first_line_max:
        return text, []
    head, sep, rest = text.partition("\n")
    if len(head) <= first_line_max:
        return text, []
    m = _SENT_END_RE.search(head)
    if not m or m.end() > first_line_max or m.end() >= len(head.rstrip()):
        return text, []
    first, remainder = head[:m.end()].rstrip(), head[m.end():].strip()
    return f"{first}\n\n{remainder}{sep}{rest}", ["split the first line after its first sentence"]


# ---------------------------------------------------------------- autofix + issues

@dataclass
class HygieneContext:
    """What the rules need about one piece. `hashtags_max` / `first_line_max` come from the
    channel spec (AA-771, PR3); None leaves hashtag layout and the first line alone."""
    seed: str
    scope: Optional[str] = None
    trip_name: Optional[str] = None
    brand_name: Optional[str] = None
    cta: Optional[str] = None
    hashtags_max: Optional[int] = None
    first_line_max: Optional[int] = None
    # S234 round 2 — the brand pack's audience line (customer_segment): its number ranges are never
    # a fact about the trip ('25–40', 'children aged 7–16').
    audience: Optional[str] = None
    # S234 round 2 review — the piece's Facts block: a range stated there ('25–40 °C') is a source
    # fact, never the audience leaking.
    facts: Optional[str] = None
    lexicon: ScopeLexicon = field(init=False)

    def __post_init__(self):
        self.lexicon = build_scope_lexicon(self.seed, self.scope, self.trip_name)

    @property
    def source_text(self) -> str:
        """The moment's own seed lines plus the Facts block — what a number may come from."""
        return f"{self.lexicon.own_text or ''}\n{self.facts or ''}"


_CTA_PROTECT = 0.6  # a sentence holding ≥60% of the CTA's content words is the CTA — never deleted


def _is_cta(sentence: str, cta: Optional[str]) -> bool:
    cw = set(content_words(cta or ""))
    return bool(cw) and len(cw & set(content_words(sentence))) / len(cw) >= _CTA_PROTECT


def _shingles(s: str, n: int = 3) -> set:
    return _grams(_TOKEN_RE.findall(fold(strip_tags(s))), n)


def _borrowed(sentence: str, own_sh: set, sib_sh: set) -> bool:
    """More of the sentence comes from another trip's line than from this moment's own text."""
    sh = _shingles(sentence)
    a, b = len(sh & sib_sh), len(sh & own_sh)
    return a >= 3 and a > 2 * b


_DUP_WINDOW = 3


def _leak_fix(sentence: str) -> Optional[str]:
    """The sentence without its leaked frame, or None when it cannot be trimmed cleanly."""
    m = _LEAK_DESCRIBES_RE.match(sentence)
    if m:
        subj = m.group(1)
        rest = sentence[m.end():]
        return subj[0].upper() + subj[1:] + " is " + rest
    m = _LEAK_FRAME_RE.match(sentence)
    if m:
        rest = sentence[m.end():]
        if rest and not LEAK_RE.search(rest):
            return rest[0].upper() + rest[1:]
    return None


def _fix_heading(raw: str, scope: Optional[str]) -> str:
    """'## A Single Sentence on What Time Has Taken' → '## What Time Has Taken'; '## The Monastery
    Ruins: A Sentence on Loss' → '## The Monastery Ruins: Loss'. A heading with nothing left goes
    (tidy_after_cut drops the empty line)."""
    m = re.match(r"(\s*#{1,6}\s+)(.*)$", raw)
    if not m:
        return raw
    marks, body = m.group(1), m.group(2)
    for leak in instruction_leaks(body, scope):
        body = body.replace(leak, " ", 1)
    body = re.sub(r"\s{2,}", " ", body).strip(" :—–-,")
    body = re.sub(r"\s+([:,])", r"\1", body)
    if not re.search(r"[A-Za-z]{3,}", body):
        return ""
    return marks + body[0].upper() + body[1:]


def _trim_audience(text: str, leak: str) -> Optional[str]:
    """Only two shapes are trimmed, both still grammatical without the range: an age adjective
    after 'a/an' before a person noun ('If you're a 25–40 traveller who…' → 'If you're a traveller
    who…'; 'an 18–35-year-old explorer' → 'an explorer'), and an 'aged/ages X–Y' postmodifier right
    after a person noun ('children aged 7–16 who' → 'children who'). Anything else ('children
    between seven and sixteen is…', 'the seven-to-sixteen range') → None: the caller deletes the
    sentence when that is safe, else F13_leak blocks — never a half-cut sentence."""
    rng = re.escape(leak)
    years = r"(?:[\s-]+years?[\s-]+olds?|[\s-]+year-olds?)?"
    m = re.search(rf"\b(a|an)\s+{rng}{years}[\s-]+(?=({_PERSON_NOUNS})\b)", text, re.I)
    if m:
        art = "an" if m.group(2)[:1].lower() in "aeiou" else "a"
        art = art.capitalize() if m.group(1)[:1].isupper() else art
        return text[:m.start()] + art + " " + text[m.end():]
    m = re.search(rf"\b(?:{_PERSON_NOUNS})(\s+(?:aged|ages)\s+{rng}{years})(?![\w-])", text, re.I)
    if m:
        return text[:m.start(1)] + text[m.end(1):]
    return None


def _next_body(spans: list[dict], i: int) -> Optional[dict]:
    return next((s for s in spans[i + 1:] if s["kind"] in ("prose", "bullet", "faq_a")), None)


def _safe_to_delete(spans: list[dict], i: int) -> bool:
    """Deleting sentence i leaves no pronoun without its referent: the next sentence does not open
    on 'It…', 'This is…', 'They…' (S234 #19)."""
    nxt = _next_body(spans, i)
    return nxt is None or not dangling_opener(nxt["clean"])


def _round2_fix(spans: list[dict], i: int, ctx: HygieneContext,
                trim_only: bool = False) -> Optional[tuple[str, str]]:
    """S234 round 2 fixes of one body sentence: drop the whole trip's length used as an adjective
    ('the full fifteen-day Mongolia itinerary' → 'the full Mongolia itinerary'); delete a
    sentence that describes the piece itself ('This guide covers days three through fourteen…');
    trim or delete the brand's audience range. A delete happens only when it leaves no dangling
    pronoun; otherwise the sentence is left for F13_leak (blocking). None = nothing to do."""
    s = spans[i]
    text, logs = s["text"], []
    total = trip_length(ctx.trip_name)
    if total is not None and trip_length_mentions(text, ctx):
        adj = re.compile(rf"(?<![\w-])(?:{_trip_length_alt(total)})-day\s+(?=[A-Za-zÀ-ÿ'’])", re.I)
        new = adj.sub("", text)
        if new != text:
            logs.append(f"dropped the whole trip's length from a part-of-trip piece: '{s['clean'][:160]}'")
            text = new
    if trim_only:
        return (text, "; ".join(logs)) if logs else None
    if instruction_leaks(strip_tags(text), ctx.scope):
        if _safe_to_delete(spans, i):
            return "", f"deleted writer-instruction wording: '{s['clean'][:160]}'"
        return (text, "; ".join(logs)) if logs else None
    for leak in audience_leaks(strip_tags(text), ctx.audience, ctx.source_text):
        trimmed = _trim_audience(text, leak)
        if trimmed is not None:
            text = trimmed
            logs.append(f"removed the brand's audience range '{leak}': '{s['clean'][:160]}'")
        elif _safe_to_delete(spans, i):
            return "", f"deleted the brand's audience range '{leak}': '{s['clean'][:160]}'"
    return (text, "; ".join(logs)) if logs else None


def autofix(content_text: str, ctx: HygieneContext) -> tuple[str, list[str]]:
    """Fix in place what is safe to fix, in this order: the TRIP name when it names an
    out-of-scope place → 'this journey'; delete sentences borrowed from another trip that name an
    out-of-scope place or leak; trim a leaked frame ('The content seed notes that …'); delete
    social proof the seed does not support; delete the later of two near-verbatim sentences within
    3 of each other; fix a hashtag one edit away from a seed place; hashtag cap / last line and
    first-line split when the channel spec gives them. The CTA sentence, headings and FAQ questions
    are never deleted. Returns (text, log of every edit)."""
    lex = ctx.lexicon
    text, log = content_text or "", []
    trip = (ctx.trip_name or "").strip()
    if trip and len(trip) > 8 and places_named(lex, trip):
        # "the *Mongolia: … — 15 Days* itinerary" → "this journey" (S233 r1 #5), not "the this journey itinerary".
        pattern = re.compile(rf"(?:\b(the|The) )?[*_]?{re.escape(trip)}[*_]?"
                             r"(?: (?:itinerary|trip|journey|tour|route|programme|program)\b)?", re.I)
        if pattern.search(text):
            text = pattern.sub(lambda m: "This journey" if m.group(1) == "The" else "this journey", text)
            log.append(f"replaced the trip name '{trip}' with 'this journey'")

    own_sh, sib_sh = _shingles(lex.own_text), _shingles(lex.sibling_text)
    seed_words = set(content_words(lex.own_text))
    spans = sentence_spans(text)
    edits: dict[int, tuple[str, str]] = {}  # span index → (replacement, log line)
    for i, s in enumerate(spans):  # S234 round 2 — an instruction-style heading keeps its topic only
        if s["kind"] == "heading" and instruction_leaks(s["clean"], ctx.scope):
            fixed = _fix_heading(s["text"], ctx.scope)
            edits[i] = (fixed, f"removed writer-instruction wording from a heading: '{s['clean'][:160]}'")
    body_idx = [i for i, s in enumerate(spans) if s["kind"] in ("prose", "bullet", "faq_a")]
    for i in body_idx:
        s = spans[i]
        clean = s["clean"]
        if _is_cta(clean, ctx.cta):
            # the CTA is never deleted, but the whole trip's length can go ('the full fifteen-day …')
            fixed_r2 = _round2_fix(spans, i, ctx, trim_only=True)
            if fixed_r2 is not None:
                edits[i] = fixed_r2
            continue
        work, r2_logs = s["text"], []
        fixed_r2 = _round2_fix(spans, i, ctx)
        if fixed_r2 is not None:
            if not fixed_r2[0]:  # the sentence goes — nothing left for the checks below
                edits[i] = fixed_r2
                continue
            # a trim ('fifteen-day', an audience range) — the sentence still runs through the
            # borrowed / source-notes / social-proof checks below (S233 #13: 'The content seed
            # describes this as a 15-day journey…' needs both fixes)
            work, r2_logs = fixed_r2[0], [fixed_r2[1]]
            clean = " ".join(strip_tags(work).split())
        named = places_named(lex, clean)
        leak = LEAK_RE.search(clean)
        if (named or leak) and lex.sibling_text and _borrowed(clean, own_sh, sib_sh):
            why = f"names '{named[0]}'" if named else "leaks the source notes"
            edits[i] = ("", f"deleted a sentence borrowed from another trip ({why}): '{s['clean'][:160]}'")
            continue
        if leak:
            fixed = _leak_fix(work)
            if fixed is not None:
                edits[i] = (fixed, "; ".join(r2_logs + [f"removed source-notes wording: '{s['clean'][:160]}'"]))
                continue
        social = SOCIAL_PROOF_RE.search(clean)
        if social and _seed_overlap(clean, seed_words) < _SOCIAL_SUPPORT_MIN:
            edits[i] = ("", f"deleted social proof ('{social.group(0)}'): '{s['clean'][:160]}'")
            continue
        if r2_logs:
            edits[i] = (work, r2_logs[0])
    prose = [i for i in body_idx if not spans[i]["in_faq"]]
    ignore = brand_words(ctx.brand_name)

    def deleted(k: int) -> bool:  # a trimmed sentence is still there and can still repeat
        return k in edits and not edits[k][0]

    for a, i in enumerate(prose):
        if deleted(i) or _is_cta(spans[i]["clean"], ctx.cta):
            continue
        for j in prose[max(0, a - _DUP_WINDOW):a]:
            if not deleted(j) and near_verbatim(spans[i]["clean"], spans[j]["clean"], ignore):
                edits[i] = ("", f"deleted a repeat of a nearby sentence: '{spans[i]['clean'][:160]}'")
                break
    if edits:
        for i in sorted(edits, reverse=True):
            s = spans[i]
            new, line = edits[i]
            if new:
                text = text[:s["start"]] + new + text[s["end"]:]
            else:
                after = s["end"] + 1 if text[s["end"]:s["end"] + 1] == " " else s["end"]
                text = text[:s["start"]] + text[after:]
            log.append(line)
        text = tidy_after_cut(text)

    for old, new in hashtag_fixes(text, lex, ctx.brand_name).items():
        text = re.sub(rf"{re.escape(old)}(?![A-Za-z0-9])", new, text)
        log.append(f"fixed hashtag {old} → {new}")
    text, more = _apply_hashtag_layout(text, ctx.hashtags_max)
    log += more
    text, more = _split_first_line(text, ctx.first_line_max)
    log += more
    return text, log


def find_issues(content_text: str, ctx: HygieneContext, *, seo_title: Optional[str] = None,
                meta_description: Optional[str] = None) -> dict[str, list[str]]:
    """What is still wrong after autofix: {"scope": [...], "hygiene": [...], "leak": [...]}, each violation
    quoting its sentence as "<problem>: '<sentence>'" (the shape sentence_repair.plan parses).
    Title/meta scope violations start with "title " / "meta " — the SEO fit fixes those, not a
    sentence repair."""
    lex = ctx.lexicon
    text = content_text or ""
    scope = []
    for v in find_scope_violations(lex, title=seo_title, meta=meta_description, body=text):
        prefix = "" if v["where"] == "body" else f"{v['where']} "
        scope.append(f"{prefix}names '{v['place']}', outside this piece's scope: '{v['sentence'][:200]}'")
    issues: list[str] = []
    leaks: list[str] = []
    spans = sentence_spans(text)
    body = [s for s in spans if s["kind"] in ("prose", "bullet", "faq_a", "faq_q")]
    for s in spans:  # S234 round 2 — headings too ('A Sentence on Loss')
        if s["kind"] not in ("prose", "bullet", "faq_a", "faq_q", "heading"):
            continue
        for leak in instruction_leaks(s["clean"], ctx.scope):
            leaks.append(f"leaks the writer's instructions ('{leak}'): '{s['clean'][:200]}'")
        for leak in audience_leaks(s["clean"], ctx.audience, ctx.source_text):
            leaks.append(f"leaks the brand's audience definition ('{leak}'), not a fact about this trip: "
                         f"'{s['clean'][:200]}'")
        if s["kind"] == "heading":
            continue
        for m in trip_length_mentions(s["clean"], ctx):
            scope.append(f"states the whole trip's length ('{m}'), but this piece covers only part of the trip: "
                         f"'{s['clean'][:200]}'")
        for m in journey_end_claims(s["clean"], ctx):
            scope.append(f"says '{m}', but the trip goes on after this piece's last day: '{s['clean'][:200]}'")
    opener = piece_opens_dangling(text)
    if opener and certain_anaphor(opener):
        leaks.append(f"opens on a pronoun with no referent: '{opener[:200]}'")
    elif opener:
        issues.append(f"may open on a pronoun with no referent: '{opener[:200]}'")
    for s in body:
        m = LEAK_RE.search(s["clean"])
        if m:
            leaks.append(f"leaks the source-notes wording ('{m.group(0)}'): '{s['clean'][:200]}'")
        m = SOCIAL_PROOF_RE.search(s["clean"])
        if m and not _is_cta(s["clean"], ctx.cta):
            issues.append(f"social proof ('{m.group(0)}') the source does not state: '{s['clean'][:200]}'")
    prose = [s for s in body if s["kind"] in ("prose", "bullet") and not s["in_faq"]]
    ignore = brand_words(ctx.brand_name)
    for a, s in enumerate(prose):
        if _is_cta(s["clean"], ctx.cta):
            continue
        if any(near_verbatim(s["clean"], t["clean"], ignore) for t in prose[:a]):
            issues.append(f"repeats an earlier sentence: '{s['clean'][:200]}'")
    for old, new in hashtag_fixes(text, lex, ctx.brand_name).items():
        issues.append(f"misspelt hashtag '{old}' (did you mean '{new}'?): '{old}'")
    if ctx.hashtags_max is not None:
        n = len(set(_HASHTAG_RE.findall(text)))
        if n > ctx.hashtags_max:
            issues.append(f"{n} hashtags, more than the channel's {ctx.hashtags_max}: '{n} hashtags'")
    return {"scope": scope, "hygiene": issues, "leak": list(dict.fromkeys(leaks))}


__all__ = [
    "COMMON_CAPS", "HygieneContext", "brand_words", "LEAK_RE", "SIBLING_HEADER", "SOCIAL_PROOF_RE", "ScopeLexicon",
    "autofix",
    "build_scope_lexicon", "content_words", "copies", "expand_to_sentence", "find_issues", "find_scope_violations",
    "is_sentence_end", "sentence_tail_end",
    "fold", "hashtag_fixes", "lev", "names_place", "near_verbatim", "phrase_tokens", "places_named", "proper_nouns",
    "restates", "sentence_spans", "split_seed", "strip_tags", "tidy_after_cut", "TRIP_NAME_WORDS", "trip_name_places",
]
