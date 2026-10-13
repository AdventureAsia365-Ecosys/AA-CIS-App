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
# sentence_repair.locate_sentence share: a mark is an end only when whitespace, a citation tag or
# the end of the text follows it (never "12.5 km", "U.S."), and a period after a known
# abbreviation ("Mt. Fuji", "St. Petersburg", "No. 5") is not one either.
_SENTENCE_END_CHARS = ".!?"
_CLOSERS = "\"'”’)"
# The end mark (run), its closing quotes / brackets, and the citation tags that follow it.
_SENTENCE_TRAILER_RE = re.compile(rf"[.!?]+[{re.escape(_CLOSERS)}]*(?:\s?{_TAG})*")
_TAG_AT_RE = re.compile(_TAG)
_ABBREVIATIONS = frozenset({"mt", "st", "ft", "dr", "mr", "mrs", "ms", "prof", "jr", "sr", "vs",
                            "approx", "ca", "e.g", "i.e"})
# "No." is an abbreviation only before a number ("No. 5"); "Is it hard? No." ends a sentence.
_NUMBER_ABBREVIATIONS = frozenset({"no"})
_WORD_BEFORE_DOT_RE = re.compile(r"[A-Za-z.]+$")
_DOTTED_ACRONYM_RE = re.compile(r"(?:[A-Za-z]\.)+[A-Za-z]")
_NEXT_IS_NUMBER_RE = re.compile(r"\s*\d")


def is_sentence_end(text: str, i: int) -> bool:
    """True when text[i] ('.', '!' or '?') really ends a sentence: the mark run and its closing
    quotes must be followed by whitespace, a citation tag or the end of the text — so a decimal
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
            if line[m.start()] != "…" and not is_sentence_end(line, m.start()):
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


def expand_to_sentence(text: str, start: int, end: int) -> tuple[int, int]:
    """The whole sentence around text[start:end] (an F9_brand_style phrase such as "hidden gem",
    or a located sentence), so an admin cut or rewrite (AA-769) never leaves half a sentence.
    Walks back to just after the previous real sentence end (is_sentence_end: not a decimal, not
    "Mt.") or the line start — skipping a FAQ "A: " / bullet "- " marker there — and forward to
    the sentence end plus its closing quotes and trailing citation tags. Never crosses a newline:
    a list item with no end mark stops at its line end. On a hashtag-only line (F13 misspelt
    hashtag) the span is the hashtag token(s) themselves, never the whole line."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", start)
    le = len(text) if le < 0 else le
    if _HASHTAG_LINE_RE.fullmatch(text[ls:le].strip()):
        toks = [m.span() for m in _HASHTAG_TOKEN_RE.finditer(text, ls, le)
                if m.start() < max(end, start + 1) and m.end() > start]
        return (toks[0][0], toks[-1][1]) if toks else (start, end)
    s = start
    while s > ls:
        k = s - 1
        if text[k] in _SENTENCE_END_CHARS and is_sentence_end(text, k):
            break
        if text[k] in _CLOSERS and k >= 1 and text[k - 1] in _SENTENCE_END_CHARS and is_sentence_end(text, k - 1):
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
    lexicon: ScopeLexicon = field(init=False)

    def __post_init__(self):
        self.lexicon = build_scope_lexicon(self.seed, self.scope, self.trip_name)


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
    body_idx = [i for i, s in enumerate(spans) if s["kind"] in ("prose", "bullet", "faq_a")]
    for i in body_idx:
        s = spans[i]
        clean = s["clean"]
        if _is_cta(clean, ctx.cta):
            continue
        named = places_named(lex, clean)
        leak = LEAK_RE.search(clean)
        if (named or leak) and lex.sibling_text and _borrowed(clean, own_sh, sib_sh):
            why = f"names '{named[0]}'" if named else "leaks the source notes"
            edits[i] = ("", f"deleted a sentence borrowed from another trip ({why}): '{clean[:160]}'")
            continue
        if leak:
            fixed = _leak_fix(s["text"])
            if fixed is not None:
                edits[i] = (fixed, f"removed source-notes wording: '{clean[:160]}'")
                continue
        social = SOCIAL_PROOF_RE.search(clean)
        if social and _seed_overlap(clean, seed_words) < _SOCIAL_SUPPORT_MIN:
            edits[i] = ("", f"deleted social proof ('{social.group(0)}'): '{clean[:160]}'")
    prose = [i for i in body_idx if not spans[i]["in_faq"]]
    ignore = brand_words(ctx.brand_name)
    for a, i in enumerate(prose):
        if i in edits or _is_cta(spans[i]["clean"], ctx.cta):
            continue
        for j in prose[max(0, a - _DUP_WINDOW):a]:
            if j not in edits and near_verbatim(spans[i]["clean"], spans[j]["clean"], ignore):
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
    """What is still wrong after autofix: {"scope": [...], "hygiene": [...]}, each violation
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
    spans = sentence_spans(text)
    body = [s for s in spans if s["kind"] in ("prose", "bullet", "faq_a", "faq_q")]
    for s in body:
        m = LEAK_RE.search(s["clean"])
        if m:
            issues.append(f"leaks the source-notes wording ('{m.group(0)}'): '{s['clean'][:200]}'")
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
    return {"scope": scope, "hygiene": issues}


__all__ = [
    "COMMON_CAPS", "HygieneContext", "brand_words", "LEAK_RE", "SIBLING_HEADER", "SOCIAL_PROOF_RE", "ScopeLexicon",
    "autofix",
    "build_scope_lexicon", "content_words", "expand_to_sentence", "find_issues", "find_scope_violations",
    "is_sentence_end", "sentence_tail_end",
    "fold", "hashtag_fixes", "lev", "names_place", "near_verbatim", "phrase_tokens", "places_named", "proper_nouns",
    "restates", "sentence_spans", "split_seed", "strip_tags", "tidy_after_cut", "TRIP_NAME_WORDS", "trip_name_places",
]
