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

from services.acp_shared.citation_tags import TAG as _TAG
from services.acp_shared.citation_tags import strip_citation_tags
from services.acp_shared.channel_spec import STOCK_PATTERNS


def fold(text: Optional[str]) -> str:
    """Accent-folded lower case: 'Khövsgöl' → 'khovsgol'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(c)).lower()


def strip_tags(text: Optional[str]) -> str:
    """Citation tags (and a dangling, unclosed tag opener) removed — the shared AA-912 definition, so a
    sentence reads here exactly as the tenant will see it after quality_gates.strip_citation_tags."""
    return strip_citation_tags(text)


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


# AA-773 — same prefix as services/acp_shared/offered.STATUS_PREFIX (offered imports this module, so
# the constant is repeated here rather than imported).
STATUS_LINE_PREFIX = "Status:"


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
    # S234 round 3 — keys from the FULL trip's itinerary places outside the scope (capitalised only,
    # like cap_only, but never cut from the writer's TRIP line); `leg_only` ⊂ trip_keys: places a
    # place-background line mentions, out of scope only when the sentence makes them a leg of travel
    # ('a domestic airport connecting the region to Ulaanbaatar'; not 'the Je Khenpo relocates here
    # from Thimphu each winter').
    trip_keys: frozenset = frozenset()
    leg_only: frozenset = frozenset()


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
                        trip_name: Optional[str] = None, trip_places: tuple = ()) -> ScopeLexicon:
    """Allowed = every capitalised word/run of the moment's own lines (label, Location, Naming,
    Source itinerary, Place background) and of the SCOPE line, plus every 'quoted' spelling in a
    Naming line. Gazetteer = (a) proper-noun phrases of the sibling lines that are not allowed and
    (b) runs of place words of the TRIP name, split at every allowed word (`trip_name_places`;
    marketing words never count, so 'Bhutan Cultural Discovery' adds nothing when Bhutan is the
    moment's country, and 'Mongolia Gobi' on a Mongolia piece adds 'gobi'). A romanisation
    variant of an allowed token is never out of scope. Matching only against this gazetteer —
    never against "any unknown proper noun" — is what kept r1 LinkedIn at 0 hits.
    AA-773: the seed's "Status:" lines (offered / included instructions, acp_shared/offered.py) are
    not source text: 'OFFERED', 'NOT INCLUDED', 'Never' must not become allowed words."""
    own, sib = split_seed(seed)
    own = "\n".join(ln for ln in own.split("\n") if not ln.lstrip().startswith(STATUS_LINE_PREFIX))
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
    trip_keys: set[str] = set()
    leg_only: set[str] = set()
    if trip_places:
        core: set[str] = set()
        core_text = "\n".join(ln for ln in own.splitlines() if not ln.lstrip().startswith("- "))
        for p in proper_nouns(core_text, keep_initial=True) | proper_nouns(scope, keep_initial=True):
            core |= set(phrase_tokens(p))
        for line in own.splitlines():
            if line.startswith("Naming:"):
                for quoted in re.findall(r"'([^']+)'", line):
                    core |= set(phrase_tokens(quoted))
        frozen_core = frozenset(core)
        for place in trip_places:
            for part in re.split(r"[,(;/]", place or ""):
                toks = phrase_tokens(part.strip(" )"))
                if not toks or any(t in frozen_core or _near_allowed(t, frozen_core) for t in toks):
                    continue  # a place this piece covers too ('Khövsgöl Lake' on a later day)
                key = " ".join(toks)
                if key in gazetteer and key not in cap_only:
                    continue
                if all(t in frozen or _near_allowed(t, frozen) for t in toks):
                    # S234 round 4 (r3 panel §4 fix 3) — a trip place the seed's own place background
                    # names is whitelisted: 'a domestic airport connecting the region to Ulaanbaatar' and
                    # 'the Je Khenpo relocates here from Thimphu' are the seed's checked facts (7 of 7 r3
                    # trip-place hits were these). `leg_only` stays empty.
                    continue
                gazetteer.setdefault(key, " ".join(w for w in part.strip(" )").split() if fold(w) not in COMMON_CAPS))
                trip_keys.add(key)
    return ScopeLexicon(allowed=frozen, gazetteer=gazetteer, own_text=own, sibling_text=sib,
                        cap_only=frozenset(cap_only), trip_keys=frozenset(trip_keys), leg_only=frozenset(leg_only))


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


# A sentence that makes a place a leg of the trip — transport or arrival / departure wording.
_LEG_RE = re.compile(
    r"\b(?:airports?|flights?|fly|flying|flew|flown|connections?|connecting|connects?|drive|driving|drove|transfers?"
    r"|depart\w*|arriv\w*|return(?:s|ing|ed)?\s+to|by air|by road|overland|gateway|entry point|leaves? for"
    r"|set(?:s|ting)? off"
    r"|heading (?:to|for|north|south|east|west)|starts? (?:in|from)|begins? (?:in|from)|ends? (?:in|at))\b", re.I)


def places_named(lex: ScopeLexicon, text: Optional[str]) -> list[str]:
    """Display forms of the gazetteer places `text` names (accent-folded, word-bounded; a
    trip-name or trip-itinerary word only when capitalised; a `leg_only` place only when the text
    makes it a leg of travel)."""
    toks = _name_tokens(text)
    out = []
    for g, disp in sorted(lex.gazetteer.items()):
        if not _has_place(toks, g, g in lex.cap_only or g in lex.trip_keys):
            continue
        if g in lex.leg_only and not _LEG_RE.search(strip_tags(text or "")):
            continue
        out.append(disp)
    return out


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
# AA-912: is_sentence_end runs this at the char after every '.'; the shared tag body stops at the next
# bracket or newline (the old [^\]]+ scanned to the next ']' anywhere: O(n^2) on '.[F:' * n).
_TAG_AT_RE = re.compile(_TAG)
_ABBREVIATIONS = frozenset({"mt", "st", "dr", "mr", "mrs", "ms", "prof", "jr", "sr", "vs",
                            "approx", "ca", "e.g", "i.e"})
# "No." is an abbreviation only before a number ("No. 5"); "Is it hard? No." ends a sentence.
_NUMBER_ABBREVIATIONS = frozenset({"no"})
# Units: an abbreviation only when the sentence runs on in lower case or a digit ("4,000 ft. above
# the valley"); "The pass sits at 4,000 ft. The trail is steep." ends at "ft.".
_UNIT_ABBREVIATIONS = frozenset({"ft", "km", "mi", "hr", "hrs", "min", "mins"})
# The word right before the dot — a whole word: "1st." / "21st." / "4ft." are not "st" / "ft".
# AA-912: a backward scan, not search(r"(?<![A-Za-z0-9])[A-Za-z.]+$", text, 0, i), which restarted at
# offset 0 for every period (O(i) per period on plain prose, O(n^2) on a run of dots).
_WORD_DOT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.")
_NEXT_RUNS_ON_RE = re.compile(r"\s*[a-z0-9]")
_DOTTED_ACRONYM_RE = re.compile(r"(?:[A-Za-z]\.)+[A-Za-z]")
_NEXT_IS_NUMBER_RE = re.compile(r"\s*\d")


def _word_before_dot(text: str, i: int) -> str:
    """The word right before the dot at text[i] ('' when none) — a whole word: '1st.' / '4ft.' are
    not 'st' / 'ft'. Same result as searching (?<![A-Za-z0-9])[A-Za-z.]+$ in text[:i]."""
    e = i - 1 if i > 0 and text[i - 1] == "\n" else i  # '$' also matched before a final '\n'
    k = e
    while k > 0 and text[k - 1] in _WORD_DOT_CHARS:
        k -= 1
    if k > 0 and text[k - 1] in "0123456789":  # the word may only start after a '.' of the run
        p = text.find(".", k, e)
        k = e if p < 0 else p + 1
    return text[k:e]


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
        word = _word_before_dot(text, i)
        w = word.lower()
        if w in _ABBREVIATIONS or (word and _DOTTED_ACRONYM_RE.fullmatch(word)):
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


_STRICT_JACCARD = 0.5
_ADDS_NOTHING = 0.8  # share of the later sentence's copy words already in the earlier one


def adds_nothing(later: str, earlier: str, ignore: frozenset = frozenset()) -> bool:
    """≥ 80% of `later`'s copy words are already in `earlier` — the later sentence says nothing new."""
    lw, ew = copy_words(later, ignore), set(copy_words(earlier, ignore))
    return bool(lw) and sum(w in ew for w in lw) / len(lw) >= _ADDS_NOTHING


def strict_repeat(later: str, earlier: str, ignore: frozenset = frozenset()) -> bool:
    """S234 round 4 review — a REAL repeat, not two sentences that share a place phrase, the SEO
    keyword or a template ('elevation of roughly … metres above sea level'): `copies`, or
    content-word Jaccard ≥ 0.5 (both ≥ 6 content words), or `adds_nothing`. `near_verbatim`'s
    2-shared-trigram arm alone fires on r1 #17 (Lake Khövsgöl 1,645 m vs Mörön 1,283 m) and s233 #13
    (the SEO keyword only), so on its own it is an F13_hygiene warning, never blocking."""
    ca, cb = content_words(later, ignore), content_words(earlier, ignore)
    if len(ca) >= _REPEAT_MIN_WORDS and len(cb) >= _REPEAT_MIN_WORDS and _jaccard(ca, cb) >= _STRICT_JACCARD:
        return True
    return copies(later, earlier, ignore) or adds_nothing(later, earlier, ignore)


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
# Containment (S234 r2 review): the answer sentence carries a whole body sentence plus a lead-in or
# an appended clause ('…government office in Tsagaannuur, and the settlements are reached by…'), so
# its OWN share stays low. The body side then needs ≥ 6 copy words, so a short body line ('Ask us.')
# never makes a long answer a copy.
_COPY_CONTAINED_MIN_WORDS = 6

# Number words → digits for the copy test only, so 'six hundred metres' and '600 metres', 'fifty to
# sixty' and '50 to 60' compare equal (S234 r2 review, 75313b8c). Digits are kept at any length
# here ('50' would be dropped by content_words' > 2-letter rule).
_NUM_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_NUM_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
             "ninety": 90}
_NUM_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000}
_DIGIT_GROUP_RE = re.compile(r"(?<=\d),(?=\d{3}\b)")


_COPY_TOKEN_RE = re.compile(r"[a-z0-9]+|[^\sa-z0-9-]")


def _numbers_to_digits(tokens: list[str]) -> list[str]:
    """['six', 'hundred', 'and', 'forty', 'five'] → ['645']. A number never runs across punctuation
    (a non-alphanumeric token) or two unit words ('five six'); a lone 'one' stays a word (a pronoun as
    often as a number); 'and' joins only inside a number ('sixteen hundred and forty-five')."""
    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t not in _NUM_UNITS and t not in _NUM_TENS and t not in _NUM_SCALES:
            out.append(t)
            i += 1
            continue
        total = current = 0
        j, used, last = i, 0, ""
        while j < n:
            w = tokens[j]
            if w in _NUM_UNITS and last not in ("unit",):
                current += _NUM_UNITS[w]
                last = "unit"
            elif w in _NUM_TENS and last not in ("unit", "tens"):
                current += _NUM_TENS[w]
                last = "tens"
            elif w in _NUM_SCALES:
                scale = _NUM_SCALES[w]
                if scale == 100:
                    current = max(current, 1) * 100
                else:
                    total += max(current, 1) * scale
                    current = 0
                last = "scale"
            elif (w == "and" and last == "scale" and j + 1 < n
                  and (tokens[j + 1] in _NUM_UNITS or tokens[j + 1] in _NUM_TENS)):
                last = "and"
            else:
                break
            used += 1
            j += 1
        out.append(t if used == 1 and t == "one" else str(total + current))
        i = j
    return out


def copy_words(sentence: str, ignore: frozenset = frozenset()) -> list[str]:
    """content_words for the copy test: number words as digits, and every number kept."""
    tokens = _numbers_to_digits(_COPY_TOKEN_RE.findall(_DIGIT_GROUP_RE.sub("", fold(strip_tags(sentence)))))
    words = [w for w in tokens if w.isdigit() or (w.isalpha() and w not in _STOP and len(w) > 2 and w not in ignore)]
    return [w[:-1] if w.endswith("s") and len(w) > 4 and not w.isdigit() else w for w in words]


def _ordered_match(ca: list[str], cb: list[str]) -> int:
    return sum(m.size for m in difflib.SequenceMatcher(None, ca, cb, autojunk=False).get_matching_blocks())


def copies(a: str, b: str, ignore: frozenset = frozenset()) -> bool:
    """Is sentence `a` a near-verbatim copy of `b` (one body sentence, or two adjacent ones joined):
    ≥ COPY_ORDERED_SHARE of a's copy words appear in b in the same order, or `a` contains b — ≥
    COPY_ORDERED_SHARE of b's copy words (b ≥ 6 of them) appear in a in the same order. `a` needs ≥ 4
    copy words; a short answer such as 'Yes, at Sod Camp.' is never a copy."""
    ca, cb = copy_words(a, ignore), copy_words(b, ignore)
    if len(ca) < _COPY_MIN_WORDS or not cb:
        return False
    matched = _ordered_match(ca, cb)
    return (matched / len(ca) >= COPY_ORDERED_SHARE
            or (len(cb) >= _COPY_CONTAINED_MIN_WORDS and matched / len(cb) >= COPY_ORDERED_SHARE))


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
    # S234 round 4 — 'asks a great deal of everyone who enters it' (r3 #18, the panel's "borderline")
    # is a demand on the reader, not proof from others: not after 'of' / 'from'
    r"|(?<!\bof )(?<!\bfrom )everyone (?:who|says)|our (?:clients|guests|travell?ers) (?:say|love|tell))", re.I)

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
    near-verbatim repeat, a new near-duplicate (S234 round 3: a figure / fact told twice, a phrase
    echoed across a sentence break, a short line that adds nothing to its neighbours), or a new
    paragraph / piece opener with no referent. [] = the repair is safe to keep (S234 panel §5 fix 7:
    #19 'What they find…', duplicated sentences in #10/#19; r2 §4 fix 2: #3, #14, #16, #17)."""
    out = []
    old_rep = set(repeated_sentences(before, ignore, cta))
    out += [f"repeats an earlier sentence: '{s[:200]}'" for s in repeated_sentences(after, ignore, cta)
            if s not in old_rep]
    old_dup = {d["later"]["clean"] for d in near_duplicates(before, ignore, cta)}
    out += [f"{d['why']}: '{d['later']['clean'][:200]}'" for d in near_duplicates(after, ignore, cta)
            if d["later"]["clean"] not in old_dup and d["later"]["clean"] not in old_rep]
    old_orphans = set(orphan_lines(before, ignore, cta))
    out += [f"adds nothing to the sentences around it: '{s[:200]}'" for s in orphan_lines(after, ignore, cta)
            if s not in old_orphans]
    out += [f"opens on a pronoun with no referent: '{s[:200]}'" for s in dangling_openers(after, before)]
    # S234 round 4 (r3 panel §4 fix 1c) — a repair that leaves a broken sentence (lower-case start,
    # an unbalanced bracket or quote) is reverted
    from services.acp_content_writing.text_integrity import fragments
    old_frag = {f["text"] for f in fragments(before)}
    out += [f"{f['why']}: '{f['text'][:200]}'" for f in fragments(after) if f["text"] not in old_frag]
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- near duplicates (S234 round 3)

# A figure or fact told twice (S234 r2: 'seventy percent of Mongolia's fresh water' #14/#16, 'only a
# few hundred people' #17), a phrase echoed across a sentence break ('Uran Togoo arrives on day
# three. On day three, …' #16) — the repairs' and the writer's near-duplicates that are not verbatim.
_FACT_GRAM = 3
_ECHO_MAX = 4
_DUP_SHORT_WORDS = 12        # a fact repeat this short is deleted outright …
_DUP_CONTAINED = 0.6         # … when this share of its words is already in the earlier sentence
_ORPHAN_MAX_WORDS = 8


def _dup_spans(text: str, cta: Optional[str]) -> list[dict]:
    return [s for s in sentence_spans(text or "") if s["kind"] in ("prose", "bullet") and not s["in_faq"]
            and not _is_cta(s["clean"], cta)]


def _fact_grams(words: list[str]) -> set:
    """Copy-word 3-grams that carry a number which is not a day index ('70 percent mongolia', 'few
    100 people'; never 'day 3 …')."""
    out = set()
    for i in range(len(words) - _FACT_GRAM + 1):
        g = tuple(words[i:i + _FACT_GRAM])
        if any(w.isdigit() and not (i + k > 0 and words[i + k - 1] == "day") for k, w in enumerate(g)):
            out.add(g)
    return out


def _echo(a: list[str], b: list[str]) -> Optional[str]:
    """The run of words sentence `a` ends on and sentence `b` (right after it) opens with: ≥ 3
    words, or 2 when one is a number ('day 3')."""
    for k in range(min(_ECHO_MAX, len(a), len(b)), 1, -1):
        if a[-k:] == b[:k] and (k >= 3 or any(w.isdigit() for w in b[:k])):
            return " ".join(b[:k])
    return None


def _p5(words: list[str]) -> list[str]:
    """Content words cut to 5 letters ('surrounding' = 'surround', 'grasslands' = 'grassland')."""
    return [w[:5] for w in words]


def _restatement(text: str, spans: list[dict], j: int, ignore: frozenset) -> Optional[tuple]:
    """S234 round 4 (r3 panel §4 fix 1, #19) — a PROSE sentence that says again, in other words, what
    a sentence just before it in the same section says (not a copy, so `copies` misses it):
    - contained: every content word (≥ 2) of the later sentence is in ONE of the 3 sentences before
      it, which has more of them ('…the steppe is open and legible; the taiga is dense and enclosed.
      The steppe is open. The taiga is dense.');
    - restate: the sentence right before shares ≥ 2 content bigrams with it and holds ≥ 60% of its
      content words ('The camp sits opposite a mountain range … a mineral spring … in the area. Sod Camp
      offers a quieter rhythm, with a mountain range nearby … and a mineral spring in the area.');
    - phrase: the sentence right before shares a content bigram with it, both have ≥ 10 content words
      and it holds ≥ 45% of the later one's ('… is something children and adults register immediately.
      The dark volcanic rock, … these are things that register immediately, for adults…').
    Calibrated on the 80 stored pieces (S233 r1, S234 r1–r3): these fire only on r3 #19's three
    panel duplicates. Returns (i, kind, shared, why) or None."""
    s = spans[j]
    if s["kind"] != "prose":
        return None
    cj = _p5(content_words(s["clean"], ignore))
    if len(cj) < 2:
        return None
    for i in range(j - 1, max(-1, j - 4), -1):
        e = spans[i]
        if e["kind"] != "prose" or "#" in (text or "")[e["end"]:s["start"]]:
            break  # another section (a heading between) or a list item
        ci = _p5(content_words(e["clean"], ignore))
        si = set(ci)
        share = sum(w in si for w in cj) / len(cj)
        if share == 1.0 and len(set(cj)) < len(si):
            return i, "contained", "", "says again, in fewer words, what a sentence just before it says"
        if i != j - 1:
            continue
        shared = _grams(cj, 2) & _grams(ci, 2)
        if len(shared) >= 2 and share >= _RESTATE_SHARE:
            return i, "restate", "", "restates the sentence before it"
        if shared and share >= _PHRASE_SHARE and len(cj) >= _PHRASE_MIN_WORDS and len(ci) >= _PHRASE_MIN_WORDS:
            return i, "phrase", "", "repeats a phrase and most of the words of the sentence before it"
    return None


_RESTATE_SHARE = 0.6
_PHRASE_SHARE = 0.45
_PHRASE_MIN_WORDS = 10
# the near-duplicate kinds dedupe deletes outright (when no pronoun is orphaned)
_DELETE_KINDS = frozenset({"copy", "contained", "restate", "phrase"})


def near_duplicates(text: str, ignore: frozenset = frozenset(), cta: Optional[str] = None) -> list[dict]:
    """[{"earlier", "later" (sentence spans), "kind": copy | fact | echo | contained | restate | phrase,
    "why", "shared"}] — prose sentences outside the FAQ (the CTA excluded) that say again what an
    earlier one said:
    - copy: `copies` (≥ 80% of one sentence's copy words in the other, in order) anywhere in the body;
    - fact: a shared copy-word 3-gram carrying a number that is not a day index;
    - echo: the next sentence opens on the words the previous one ended on;
    - contained / restate / phrase (S234 round 4): `_restatement`."""
    spans = _dup_spans(text, cta)
    words = [copy_words(s["clean"], ignore) for s in spans]
    out: list[dict] = []
    seen: set[int] = set()
    for j in range(1, len(spans)):
        if len(words[j]) < 2:
            continue
        hit = None
        if j - 1 >= 0 and spans[j - 1]["end"] <= spans[j]["start"]:
            gap = (text or "")[spans[j - 1]["end"]:spans[j]["start"]]
            run = _echo(words[j - 1], words[j]) if "\n\n" not in gap else None
            if run:
                hit = (j - 1, "echo", run, f"repeats '{run}' from the end of the sentence before it")
        for i in range(j):
            if hit:
                break
            if len(words[j]) >= _COPY_MIN_WORDS and copies(spans[j]["clean"], spans[i]["clean"], ignore):
                hit = (i, "copy", "", "says again what an earlier sentence says")
                break
            shared = _fact_grams(words[j]) & _fact_grams(words[i])
            if shared:
                g = " ".join(sorted(shared)[0])
                hit = (i, "fact", g, f"repeats a figure already given earlier ('{g}')")
        if not hit:
            hit = _restatement(text, spans, j, ignore)
        if hit and j not in seen:
            seen.add(j)
            i, kind, shared, why = hit
            out.append({"earlier": spans[i], "later": spans[j], "kind": kind, "why": why, "shared": shared})
    return out


def orphan_lines(text: str, ignore: frozenset = frozenset(), cta: Optional[str] = None) -> list[str]:
    """One-sentence paragraphs of ≤ 8 words whose every content word is already in the sentence
    before or after them — a line a repair left behind ('The itinerary builds it in.' between 'The
    itinerary keeps you there…' and 'Trail Pulse builds trips around…', S234 r2 #3)."""
    spans = _dup_spans(text, cta)
    out = []
    for k, s in enumerate(spans):
        if s["kind"] != "prose":
            continue
        line_start = text.rfind("\n", 0, s["start"]) + 1
        line_end = text.find("\n", s["end"])
        line_end = len(text) if line_end < 0 else line_end
        if text[line_start:s["start"]].strip() or text[s["end"]:line_end].strip():
            continue  # not alone on its line
        cw = content_words(s["clean"], ignore)
        if len(cw) < 2 or len(s["clean"].split()) > _ORPHAN_MAX_WORDS:
            continue
        around = set()
        for t in spans[max(0, k - 1):k] + spans[k + 1:k + 2]:
            around |= {w[:5] for w in content_words(t["clean"], ignore)}
        if all(w[:5] in around for w in cw):
            out.append(s["clean"])
    return out


def _dup_safe_delete(text: str, span: dict) -> bool:
    spans = sentence_spans(text)
    k = next((n for n, s in enumerate(spans) if s["start"] == span["start"]), None)
    return k is not None and _safe_to_delete(spans, k)


_LEAD_PHRASE_RE = re.compile(r"^(?:On|By|At|In|From|For)\s+[^,.;]{1,40},\s+(?=[a-z])")


_PIECE_WIDE_MIN = 3  # a content trigram in this many sentences (headings included) is a piece-wide phrase


def _piece_wide_trigrams(text: str, ignore: frozenset = frozenset()) -> set:
    """Content trigrams found in ≥ 3 sentences or headings of the piece: the SEO keyword ('khovsgol gobi
    tour', 'tour mongolia price'), the trip name, a fact the piece tells throughout. Sharing one says
    nothing about two sentences repeating each other."""
    seen: dict = {}
    for s in sentence_spans(text or ""):
        for g in _grams(content_words(s["clean"], ignore), 3):
            seen[g] = seen.get(g, 0) + 1
    return {g for g, n in seen.items() if n >= _PIECE_WIDE_MIN}


def _own_trigrams(a: str, b: str, ignore: frozenset, common: set) -> int:
    """Shared content trigrams of `a` and `b` that are not piece-wide phrases."""
    ca, cb = content_words(a, ignore), content_words(b, ignore)
    if len(ca) < _REPEAT_MIN_WORDS or len(cb) < _REPEAT_MIN_WORDS:
        return 0
    return len((_grams(ca, 3) & _grams(cb, 3)) - common)


def _closing_zone(text: str, cta: Optional[str]) -> Optional[tuple[int, int]]:
    """(start, end) of the piece's closing section: from the heading above the CTA sentence (or, with
    no heading, the start of its paragraph) to the end of the CTA sentence. None without a CTA."""
    spans = sentence_spans(text or "")
    want = cta_norm(cta)
    k = next((n for n, s in enumerate(spans) if s["kind"] in ("prose", "bullet") and not s["in_faq"]
              and (_is_cta(s["clean"], cta) or (want and f" {want} " in f" {cta_norm(s['clean'])} "))), None)
    if k is None:
        return None
    head = next((s for s in reversed(spans[:k]) if s["kind"] == "heading"), None)
    start = head["start"] if head is not None else max(0, (text or "").rfind("\n\n", 0, spans[k]["start"]))
    return start, spans[k]["end"]


def dedupe(text: str, ignore: frozenset = frozenset(), cta: Optional[str] = None) -> tuple[str, list[str]]:
    """Fix the near-duplicates that are safe to fix in place: delete a later sentence that copies an
    earlier one, or a short one (≤ 12 copy words) that repeats a figure with ≥ 60% of its words
    already in the earlier sentence; trim an echoed opening phrase ('On day three, the route…' →
    'The route…'); delete an orphan line. A delete happens only when no pronoun is orphaned. The rest
    is left for F13_repeat. Returns (text, log)."""
    log: list[str] = []
    for _ in range(10):  # one edit per pass: offsets move after each
        changed = False
        for d in near_duplicates(text, ignore, cta):
            later, earlier = d["later"], d["earlier"]
            lw = copy_words(later["clean"], ignore)
            ew = set(copy_words(earlier["clean"], ignore))
            if d["kind"] == "echo":
                m = _LEAD_PHRASE_RE.match(later["text"])
                if m and all(w in copy_words(m.group(0), ignore) for w in d["shared"].split()):
                    rest = later["text"][m.end():]
                    text = text[:later["start"]] + rest[:1].upper() + rest[1:] + text[later["end"]:]
                    log.append(f"trimmed an echoed opening ('{m.group(0).strip(', ')}'): '{later['clean'][:160]}'")
                    changed = True
                    break
                continue
            contained = sum(w in ew for w in lw) / len(lw) if lw else 0.0
            if d["kind"] in _DELETE_KINDS or (len(lw) <= _DUP_SHORT_WORDS and contained >= _DUP_CONTAINED):
                if _dup_safe_delete(text, later):
                    after = later["end"] + 1 if text[later["end"]:later["end"] + 1] == " " else later["end"]
                    text = tidy_after_cut(text[:later["start"]] + text[after:])
                    log.append(f"deleted a sentence that {d['why']}: '{later['clean'][:160]}'")
                    changed = True
                    break
        if not changed:
            for o in orphan_lines(text, ignore, cta):
                span = next(s for s in _dup_spans(text, cta) if s["clean"] == o)
                if _dup_safe_delete(text, span):
                    text = tidy_after_cut(text[:span["start"]] + text[span["end"]:])
                    log.append(f"deleted a line that adds nothing to the sentences around it: '{o[:160]}'")
                    changed = True
                    break
        if not changed:
            # S234 round 4 (r3 #12, #17) — a closing recap that repeats an earlier sentence far away
            # (autofix's own repeat rule looks 3 sentences back only): the later copy goes when safe and
            # it is in the closing (CTA) section or adds nothing (≥ 80% of its words in the earlier one);
            # a mid-body repeat that adds a figure stays for F13_repeat ('cut it or say something new')
            zone = _closing_zone(text, cta)
            common = _piece_wide_trigrams(text, ignore)
            for r in repeated_sentences(text, ignore, cta):
                span = next((s for s in reversed(_dup_spans(text, cta)) if s["clean"] == r), None)
                if span is None:
                    continue
                prior = [t["clean"] for t in _dup_spans(text, cta) if t["end"] <= span["start"]
                         and near_verbatim(r, t["clean"], ignore)]
                closing = zone is not None and zone[0] <= span["start"] < zone[1]
                # S234 round 4 review — near_verbatim alone is not enough: s233 #13 lost the answer
                # under its keyword heading on 3 shared trigrams that are all the SEO keyword. In the
                # closing section a recap is cut on a strict repeat or on ≥ 2 shared trigrams that are
                # not piece-wide phrases (in ≥ 3 sentences: the keyword, the trip name, a fact told
                # throughout) — r3 #12 / #17's recaps (the panel's hard errors) still go. Elsewhere the
                # later sentence must add nothing.
                if closing:
                    real = any(strict_repeat(r, e, ignore) or _own_trigrams(r, e, ignore, common) >= 2
                               for e in prior)
                else:
                    real = any(adds_nothing(r, e, ignore) for e in prior)
                if real and _dup_safe_delete(text, span):
                    after = span["end"] + 1 if text[span["end"]:span["end"] + 1] == " " else span["end"]
                    text = tidy_after_cut(text[:span["start"]] + text[after:])
                    log.append(f"deleted a sentence that repeats an earlier one: '{r[:160]}'")
                    changed = True
                    break
        if not changed:
            break
    return text, log


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
                      kinds: tuple = ("prose", "bullet", "faq_a"), run: int = TEMPLATE_RUN,
                      shapes: bool = True, skip_brand_lines: bool = True) -> list[tuple[str, str]]:
    """[(sentence of `text`, the shared words)] — lines of `text` (spans of `kinds`; pass
    ("heading",) for headings) that repeat an earlier piece of the same brief (rules A/B above).
    `exclude` = source texts (own seed lines, Facts block, CTA, keyword): their words are never
    template words. A sentence carrying the SEO `keyword` is skipped — siblings of one request
    share the keyword by design (S233 #13/#16 'Khovsgol and Gobi tour Mongolia price').
    S234 round 3 — the brief check (`brief_phrases`) reuses this with `run=4`, `shapes=False` (rule A
    only) and `skip_brand_lines=False`: a brand-pack phrase pasted into the brand's own sign-off
    ('Trail Pulse builds trips around movement and energy delivered well') is exactly the target."""
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
        if skip_brand_lines and brand and brand <= set(words):
            continue  # the brand's own sign-off line ('Trail Pulse builds trips around…') is not a template
        if has_keyword(words):
            continue
        window = words + (mine[k + 1][1] if k + 1 < len(mine) else [])
        for stream in sib_stream:  # A: a contiguous run that starts in this sentence
            sm = difflib.SequenceMatcher(None, window, stream, autojunk=False)
            blk = next((b for b in sm.get_matching_blocks() if b.size >= run and b.a < len(words)
                        and novel_ok(window[b.a:b.a + b.size], 2)), None)
            if blk is not None:
                out[sp["clean"]] = " ".join(window[blk.a:blk.a + blk.size])
                break
        if sp["clean"] in out or not shapes:
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


# S234 round 3 (r2 §4 fix 4) — stock lines that came back across rounds and brands, on every channel
# ('worth sitting with' ×3, 'That stop isn't filler.', 'The mountain remembers what it was.', 'The
# caldera is not a metaphor…', 'Paro Taktsang asks something of every visitor', 'Knowing them in
# advance' with no antecedent). F15_voice: one sentence repair, never a hold.
TEMPLATE_LINE_RES = tuple(re.compile(p, re.I) for p in (
    r"\bworth sitting with\b",
    r"\b(?:is|was|are|isn['’]t|wasn['’]t|aren['’]t)\s+(?:not\s+)?(?:\w+\s+)?filler\b|\bnot\s+(?:\w+\s+)?filler\b",
    r"\bthe\s+[a-z]+\s+remembers\b",
    r"\bnot\s+a\s+metaphor\b",
    r"\basks\s+something\s+of\s+(?:every|each|its|the|any)\s+(?:visitor|traveller|traveler|guest|family)s?\b",
    # only as the sentence's opener with a plural pronoun (r2 #2 'Knowing them in advance —' had no
    # antecedent); 'and knowing that in advance changes how you plan' (r1 #13, clean) is fine
    r"^knowing\s+(?:them|these|those)\s+in\s+advance\b",
))


def template_lines(text: str) -> list[tuple[str, str]]:
    """[(stock line, its sentence)] for TEMPLATE_LINE_RES, prose / bullets / FAQ answers."""
    out = []
    for sp in sentence_spans(text or ""):
        if sp["kind"] not in ("prose", "bullet", "faq_a"):
            continue
        for rx in TEMPLATE_LINE_RES:
            m = rx.search(sp["clean"])
            if m:
                out.append((" ".join(m.group(0).split()), sp["clean"]))
                break
    return out


def stock_line_items(text: str) -> list[dict]:
    """AA-780 (S246) — one cut item per channel_spec.STOCK_PATTERNS match in a prose / bullet sentence:
    {"start", "end", "sentence", "phrase"}, the item spanning every sentence the match touches ("Not as X.
    Not as Y."), one item per sentence set. The same list F15_voice blocks on attempt 1 only; the T9 loop
    cuts what is left of it after its last attempt (service._cut_stock_lines, through
    sentence_repair.delete_flagged — never the hook, never the CTA, no orphaned pronoun)."""
    text = text or ""
    spans = [sp for sp in sentence_spans(text) if sp["kind"] in ("prose", "bullet")]
    hits: list[tuple[int, int, str]] = []  # (first span index, last span index, phrase)
    for pattern in STOCK_PATTERNS:
        for m in pattern.finditer(text):
            idx = [i for i, sp in enumerate(spans) if sp["start"] < m.end() and m.start() < sp["end"]]
            if idx:  # a heading / hashtag line / FAQ question is never cut here
                hits.append((idx[0], idx[-1], " ".join(m.group(0).split())))
    merged: list[list] = []  # overlapping hits ("Not as filler." in "Not as filler. Not as …") → one item
    for i, j, phrase in sorted(hits):
        if merged and i <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], j)
            if phrase not in merged[-1][2]:
                merged[-1][2].append(phrase)
        else:
            merged.append([i, j, [phrase]])
    return [{"start": spans[i]["start"], "end": spans[j]["end"],
             "sentence": " ".join(sp["clean"] for sp in spans[i:j + 1]), "phrase": "; ".join(phrases)}
            for i, j, phrases in merged]


BRIEF_RUN = 5  # 4 fired on generic runs ('in a mountain kingdom', 'steppe gives way to') on clean pieces
_BRIEF_PACK_FIELDS = ("core_idea", "style_guide", "good_examples", "customer_mindset", "brand_type")
_BRIEF_ANGLE_FIELDS = ("why_it_works", "best_final_style", "formula_fit")


def brief_texts(brand_pack: Optional[dict], angle: Optional[dict] = None) -> dict[str, list[str]]:
    """{"pack": the brand pack's text fields, "angle": the chosen angle's brief fields} — never the
    angle's name (the blog H2 is the angle name by design) and never the voice adjectives."""
    out: dict[str, list[str]] = {"pack": [], "angle": []}
    for key, src, fields in (("pack", brand_pack or {}, _BRIEF_PACK_FIELDS),
                             ("angle", angle or {}, _BRIEF_ANGLE_FIELDS)):
        for f in fields:
            v = src.get(f)
            if isinstance(v, str) and v.strip():
                out[key].append(v)
    return out


def brief_phrases(text: str, briefs: dict, *, exclude: list[str] = (), brand_name: Optional[str] = None,
                  keyword: Optional[str] = None) -> list[tuple[str, str]]:
    """[(sentence, the shared words)] — a run of ≥ 5 words copied from the brand pack (anywhere) or
    from the angle brief into a sentence that speaks as the brand, at least half of its content
    words not in the source (seed, Facts, CTA): S234 r2 #3/#11 'Trail Pulse builds trips around
    movement and energy delivered well' (the angle's 'core idea of movement and energy delivered
    well'), #5/#17 'Terra … plans active, educational journeys for families who want…' (core idea).
    An angle phrase in a sentence that does not name the brand is the writer following its brief
    ('That choice was inseparable from the institution's purpose', S234 r1 #5, panel-clean) — not
    flagged. The sibling n-gram rule A, on brief text instead of siblings, brand lines included."""
    kw = dict(exclude=exclude, brand_name=brand_name, keyword=keyword, run=BRIEF_RUN, shapes=False,
              skip_brand_lines=False)
    out = dict(sibling_templates(text, briefs.get("pack") or [], **kw))
    brand = brand_words(brand_name)
    for sentence, run in sibling_templates(text, briefs.get("angle") or [], **kw):
        if brand and brand <= set(_WORD_RE.findall(fold(sentence))):
            out.setdefault(sentence, run)
    return list(out.items())


# ---------------------------------------------------------------- CTA literal (S234 round 3)

_FAQ_HEADING_RE = re.compile(r"(?im)^#{1,6}[ \t]*faq\b.*$")
# AA-912: the old (?:\n[ \t]*(?:#T[\s,]*)+[ \t]*)+\s*$ backtracked exponentially ('\n#A0' x24 + '!' = 3.4 s).
# Same language, checked line by line: the block's first line is [ \t]*#T…, every later line only
# tags / ',' / whitespace (blank lines included), up to the end of the text.
_HASHTAG_TAIL_FIRST_RE = re.compile(
    r"[ \t]*+#[A-Za-z][A-Za-z0-9]++(?:[^\S\n]|,)*+(?:#[A-Za-z][A-Za-z0-9]++(?:[^\S\n]|,)*+)*+")
_HASHTAG_TAIL_LINE_RE = re.compile(
    r"(?:[^\S\n]|,)*+(?:#[A-Za-z][A-Za-z0-9]++(?:[^\S\n]|,)*+)*+")


def _hashtag_tail_start(body: str) -> Optional[int]:
    """Offset of the '\\n' that opens the trailing hashtag block (hashtag lines, blank lines and
    ',' / whitespace between tags, to the end), or None — walking the lines back from the end."""
    lines = body.split("\n")
    pos, start = len(body), None
    for k in range(len(lines) - 1, 0, -1):
        pos -= len(lines[k]) + 1  # the '\n' before line k
        if not _HASHTAG_TAIL_LINE_RE.fullmatch(lines[k]):
            break
        if _HASHTAG_TAIL_FIRST_RE.fullmatch(lines[k]):
            start = pos
    return start


def cta_norm(text: Optional[str]) -> str:
    """Accent-folded words only, '&' read as 'and': 'Talk to an Atlas & Hearth travel designer.' and
    'talk to an atlas and hearth travel designer' compare equal."""
    t = fold(strip_tags(text or "")).replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", t))


def has_cta_literal(text: Optional[str], cta: Optional[str]) -> bool:
    """Is the configured CTA line in the prose, word for word (whitespace / punctuation / case
    normalised), before any FAQ heading and outside hashtag lines?"""
    want = cta_norm(cta)
    if not want:
        return False
    body = text or ""
    m = _FAQ_HEADING_RE.search(body)
    body = body[:m.start()] if m else body
    body = "\n".join(ln for ln in body.split("\n") if not _HASHTAG_LINE_RE.fullmatch(ln.strip() or "-"))
    return f" {want} " in f" {cta_norm(body)} "


# an invitation verb ('ask', 'speak to', 'get in touch with' …) a few words before the brand's first
# word: the shape of a reworded CTA ('Speak to Terra Family Expeditions about how this journey is
# structured…', 'If you want to understand Day 4, ask WildKind Travel for the complete itinerary.')
_INVITE_VERBS = (r"ask|speak|talk|contact|reach\s+out|get\s+in\s+touch|enquire|inquire|write|email|call|message"
                 r"|book|request|get|plan")
CTA_WINDOW = 3  # the closing prose sentences searched for a reworded CTA


def _cta_brand_word(cta: Optional[str]) -> Optional[str]:
    """The first capitalised word of the CTA after its verb ('Terra', 'WildKind', 'Atlas', 'Trail')."""
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][\w’'-]*", cta or "")
    return next((w for w in words[1:] if w[0].isupper()), None)


def _is_cta_invite(sentence: str, cta: Optional[str]) -> bool:
    """A sentence that invites the reader to contact the brand: an invitation verb, then at most four
    words, then the CTA's brand word. The brand's own line ('Terra Family Expeditions plans journeys
    for families…') has no invitation before the name and is not one."""
    brand = _cta_brand_word(cta)
    if not brand:
        return False
    return bool(re.search(rf"\b(?:{_INVITE_VERBS})\b(?:\s+[\w’'&-]+){{0,4}}?\s+{re.escape(brand)}\b",
                          strip_tags(sentence or ""), re.I))


def ensure_cta(text: str, cta: Optional[str]) -> tuple[str, list[str]]:
    """S234 r2 #19 paraphrased the configured CTA ('Speak to Terra Family Expeditions about how this
    journey is structured…') and `check_cta` passed it. The literal line is restored in code, and the
    piece ends with ONE CTA: the last of the closing CTA_WINDOW prose sentences that invites the
    reader to contact the brand (`_is_cta_invite`) — or, failing that, a closing sentence that is the
    CTA reworded (≥ 60% of its words) — is replaced by it in place. Only when no such sentence exists
    is the line added: as its own paragraph right before '## FAQ' (blog) or the hashtags (social),
    and before a closing question paragraph (a post's engagement question stays last). Returns
    (text, log)."""
    line = " ".join((cta or "").split())
    if not line or has_cta_literal(text, cta):
        return text, []
    if line[-1] not in ".!?":
        line += "."
    body = text or ""
    faq = _FAQ_HEADING_RE.search(body)
    end = faq.start() if faq else len(body)
    tail = _hashtag_tail_start(body[:end]) if not faq else None
    if tail is not None:
        end = tail
    spans = [s for s in sentence_spans(body) if s["kind"] == "prose" and s["end"] <= end and not s["in_faq"]]
    closing = spans[-CTA_WINDOW:]
    invites = [s for s in closing if _is_cta_invite(s["clean"], cta)]
    # the brand's own line before the closing sentence ('Terra Family Expeditions plans journeys for
    # families who want…', r2 #19) shares the CTA's words too, so the ≥ 60% test is for the last one only
    target = invites[-1] if invites else (spans[-1] if spans and _is_cta(spans[-1]["clean"], cta) else None)
    if target is not None:
        new = body[:target["start"]] + line + body[target["end"]:]
        return new, [f"restored the configured CTA line in place of a reworded one: '{target['clean'][:160]}'"]
    last = spans[-1] if spans else None
    if last is not None and last["clean"].endswith("?"):
        para = body.rfind("\n\n", 0, last["start"])
        if para > 0 and body[:para].strip():
            end = para
    head = body[:end].rstrip()
    rest = body[end:].lstrip("\n")
    new = f"{head}\n\n{line}\n\n{rest}" if rest else f"{head}\n\n{line}\n"
    if not (text or "").endswith("\n"):
        new = new.rstrip("\n")
    return new, ["added the configured CTA line, which the piece did not have word for word"]


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
    """Hashtag-only lines go to the end as one line, capped at `hashtags_max` (channel spec), each tag
    once. AA-780 (S246 panel t9l-002 / t9l-014 shipped "#Bhutan #PunakhaDzong #PunakhaDzong"): a repeated
    tag (case-insensitive, the first spelling kept) also takes the rewrite branch — before, a tag line
    already last and under the cap was returned as written."""
    if hashtags_max is None:
        return text, []
    lines = text.rstrip("\n").split("\n")
    tag_lines = [i for i, ln in enumerate(lines) if ln.strip() and _HASHTAG_LINE_RE.fullmatch(ln.strip())]
    if not tag_lines:
        return text, []
    raw = [t for i in tag_lines for t in re.findall(r"#[A-Za-z][A-Za-z0-9]+", lines[i])]
    first: dict[str, str] = {}
    for t in raw:  # first spelling of each tag, in order
        first.setdefault(t.lower(), t)
    tags = list(first.values())
    dupes = len(raw) - len(tags)
    body = [ln for i, ln in enumerate(lines) if i not in set(tag_lines)]
    while body and not body[-1].strip():
        body.pop()
    kept = tags[:hashtags_max]
    log = []
    if dupes:
        log.append(f"removed {dupes} repeated hashtag(s)")
    if len(tags) > len(kept):
        log.append(f"kept {len(kept)} of {len(tags)} hashtags (channel cap {hashtags_max})")
    if tag_lines != [len(lines) - 1] or len(tags) > len(kept) or dupes:
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
    text = text.lstrip()  # a leading blank line is not the hook line (S234 r3 #3)
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
    # S234 round 3 — places of the FULL trip outside the scoped days (the trip's itinerary atoms,
    # service._trip_places_outside): out-of-scope gazetteer entries even when a place-background
    # line mentions them ('a domestic airport connecting the region to Ulaanbaatar').
    trip_places: tuple = ()
    lexicon: ScopeLexicon = field(init=False)

    def __post_init__(self):
        self.lexicon = build_scope_lexicon(self.seed, self.scope, self.trip_name, trip_places=self.trip_places)

    @property
    def moments(self) -> list:
        """The seed's moment blocks (trip_facts.parse_moments), parsed once."""
        if not hasattr(self, "_moments"):
            from services.acp_content_writing.trip_facts import parse_moments
            self._moments = parse_moments(self.seed)
        return self._moments

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


# ---------------------------------------------------------------- em-dashes (AA-778)

# chị Thư bans the em-dash in tenant copy; S234 r1–r4 had 182–200 per 20 pieces (14.2 per 1,000 words)
# and the panel scored AI tells 4.75/10. A dash here is an em-dash (spaced or not) or an en-dash with a
# space on both sides. A bare en-dash ('3–5 days', '1637–1638') and a hyphen ('Uran Togoo-Tulga Uul')
# are never dashes. Code uses the escapes (— em, – en) so a search for the character finds
# only text.
_EM, _EN = "—", "–"
# AA-912: a match starts only where a blank run starts (the old leading [ \t]* was retried from every
# blank: O(n^2) on a long run of spaces/tabs); '—[ \t]*' alone covers a dash right after the previous
# match ('a — \t— b'). Same spans as before (fuzzed).
_DASH_RE = re.compile(r"(?<![ \t])[ \t]*+—[ \t]*|—[ \t]*|(?<![ \t])[ \t]++–[ \t]+")
_RANGE_END_RE = re.compile(r"[ \t]*[$€£¥]?\d")
# The F2 residue guard (quality_gates._BANNED_PATTERNS_SEED): an em-dash with the word on each side. F2
# exempts a match found verbatim in the source, and every seed has em-dashes in its own labels ('Uran
# Togoo <em-dash> trek into …'), so a bare em-dash would always be exempt; the word-dash-word phrase is
# exempt only when the source has that exact phrase.
EM_DASH_PATTERN = r"(?:[^\s—]+[ \t]*)?—(?:[ \t]*[^\s—]+)?"
_QUOTED_RE = re.compile(r"\"[^\"\n]{1,200}\"|“[^”\n]{1,200}”")
# X in '… <dash> X' that continues the sentence: joined with a comma ('and it repays both', 'not as a
# performance', 'which gives children …').
_DASH_COMMA_OPENERS = frozenset("""and but or nor so yet not which who whom whose where when while whereas
because before after until though although unless including especially particularly meaning making giving
leaving allowing letting creating adding suggesting""".split())
# X that opens on a subject pronoun is a clause of its own ('it is two', 'they're the shape …').
_DASH_SUBJECTS = frozenset("it it's they they're we we're you you're he she i".split())
_DASH_FINITE = frozenset("""is are was were has have had does do did can could will would may might must should
shall isn't aren't wasn't weren't hasn't haven't hadn't doesn't don't didn't can't cannot won't wouldn't
couldn't shouldn't it's that's there's they're""".split())
# a finite verb only counts before these: 'figures that give …' is a noun phrase and 'what is given
# time, what …' a list, not a clause
_DASH_CLAUSE_STOPS = frozenset("that which who whom whose where when while what how as than".split())
_DASH_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ’']+|[,;:()]")
_DASH_END_JUNK = " \t.!?…\"'”’)*"


def _dash_words(x: str) -> list[str]:
    return [w.lower().replace("’", "'") for w in _DASH_WORD_RE.findall(strip_tags(x))]


def _dash_clause(x: str) -> bool:
    """X can stand as a sentence: it opens on a subject pronoun, or has a finite verb in its first 5
    words, before any comma, bracket or relative / comparative word and after at most one article
    ('the herds are …'; 'the moment the route has …' is a noun with a reduced relative). Conservative
    on purpose: a miss gives a colon (always grammatical), a false hit gives a fragment."""
    words = _dash_words(x)
    if len(words) > 2 and words[1] == ",":  # 'instead, the animals are used …'
        words = words[2:]
    if len(words) < 2:
        return False
    if words[0] in _DASH_SUBJECTS:
        return True
    articles = 0
    for w in words[:5]:
        if w in _DASH_CLAUSE_STOPS or w in ",;:()":
            return False
        articles += w in ("the", "a", "an")
        if w in _DASH_FINITE:
            return articles < 2
    return False


def _dash_trailing(head: str, x: str) -> str:
    """The separator for a single dash before X (the rest of the sentence): ', ' when X continues the
    sentence (a conjunction, 'not', 'rather than', a relative word, a linking -ing word such as
    'making', or an -ly / -ed word with no finite verb after it); '. ' when X is a clause of its own and
    the text before the dash has a finite verb or is a bare 'Yes' / 'No' (the caller capitalises X;
    'The courtyards, the temples <dash> these are …' has none and would leave a fragment); otherwise
    ': ' (', ' when the sentence already has a colon). A colon is the fallback because it is never
    wrong: 'cameras stay at the checkpoint, knowing the rules …' would be a comma splice."""
    words = _dash_words(x)
    first = words[0] if words else ""
    if first in _DASH_COMMA_OPENERS or words[:2] in (["rather", "than"], ["instead", "of"]):
        return ", "
    if (len(first) > 4 and first.endswith(("ly", "ed")) and words[1:2] != [","]
            and not any(w in _DASH_FINITE for w in words)):
        return ", "
    head_words = _dash_words(head)
    if _dash_clause(x) and (any(w in _DASH_FINITE for w in head_words) or head_words in (["yes"], ["no"])):
        return ". "
    return ", " if ":" in strip_tags(head) else ": "


def _cap_first(s: str) -> str:
    """`s` with its first letter upper-case (past opening quotes, brackets and markdown)."""
    for k, ch in enumerate(s):
        if ch.isalpha():
            return s[:k] + ch.upper() + s[k + 1:]
        if not (ch in "\"'“‘*_(" or ch.isspace()):
            return s
    return s


def _protected_ranges(text: str, ctx: Optional[HygieneContext]) -> list[tuple[int, int]]:
    """Where a dash is kept: the TRIP name written verbatim ('Ho Chi Minh Trail <em-dash> 14 Days'), and
    a quoted span ("…" / “…”) that is word for word in the seed, the Facts block or the trip name. The
    F2 residue guard exempts the same text (its source holds the seed, Facts, trip name and CTA).
    Also, with or without `ctx`, the dash of a testimonial attribution ('"…" <em-dash> Sarah, UK'): F19
    needs it to flag the invented quote, which is then cut whole (authenticity.attribution_spans)."""
    from services.acp_content_writing.authenticity import attribution_spans  # authenticity imports hygiene
    out: list[tuple[int, int]] = attribution_spans(text)
    if ctx is None:
        return out
    trip = (ctx.trip_name or "").strip()
    if trip and (_EM in trip or _EN in trip):
        out += [m.span() for m in re.finditer(re.escape(trip), text, re.I)]
    source = fold(f"{ctx.seed or ''}\n{ctx.facts or ''}\n{trip}")
    for m in _QUOTED_RE.finditer(text):
        inner = m.group(0)[1:-1].strip()
        if (_EM in inner or _EN in inner) and fold(inner) in source:
            out.append(m.span())
    return out


def _dash_separators(raw: str, kind: str, dashes: list[re.Match]) -> list[tuple[re.Match, str]]:
    """(dash, replacement) for the unprotected dashes of one sentence, in order."""
    if kind == "heading":
        return [(m, ": " if k == 0 else ", ") for k, m in enumerate(dashes)]
    seps: list[tuple[re.Match, str]] = []
    k = 0
    while k < len(dashes):
        m = dashes[k]
        head = raw[:m.start()]
        if not strip_tags(head).strip(" \t*_#-"):  # a dash that opens the line or the answer
            seps.append((m, "" if not head.strip() or head.endswith(("*", "_")) else " "))
            k += 1
            continue
        if k + 1 < len(dashes):  # a pair: '<dash> X <dash>' is a parenthesis
            n = dashes[k + 1]
            inner = raw[m.end():n.start()]
            # brackets when X has its own commas, or when 'and' / 'or' follows ('the Je Khenpo, the Chief
            # Abbot, and the monks' would read as three people)
            if (("," in strip_tags(inner) or re.match(r"(?:and|or)\b", raw[n.end():], re.I))
                    and "(" not in inner and ")" not in inner):
                # S238 (T2): an opening phrase with no finite verb, then the main clause after the pair,
                # keeps the comma the dash stood for ('Built in 1727 (no metal beams, no nails), it
                # stands …'); only when the clause opens on its subject ('Its walls (thick, white) are …'
                # has none)
                close = ") "
                tail = _dash_words(raw[n.end():])
                if (tail and tail[0] in _DASH_SUBJECTS | {"this", "there"}
                        and not any(w in _DASH_FINITE for w in _dash_words(head))):
                    close = "), "
                seps += [(m, " ("), (n, close)]
            else:
                seps += [(m, ", "), (n, ", ")]
            k += 2
            continue
        x = raw[m.end():]
        seps.append((m, _dash_trailing(head, x) if strip_tags(x).strip(_DASH_END_JUNK) else ""))
        k += 1
    return seps


def _fix_dash_span(raw: str, kind: str, keep: list[tuple[int, int]]) -> tuple[str, int]:
    """One sentence (or heading / FAQ question line) with its dashes replaced. `keep` are protected
    (start, end) ranges relative to `raw`. Returns (new text, dashes replaced)."""
    found = [m for m in _DASH_RE.finditer(raw) if not any(s <= m.start() < e for s, e in keep)]
    if not found:
        return raw, 0
    # A dash between two numbers is a range ('1637<em-dash>1638', '20,000 <en-dash> 24,000', '8:00 - 10:00',
    # '$1,200 <em-dash> $1,500', '10% <em-dash> 20%'), never a clause break: a colon or comma there would change
    # the fact. An em-dash becomes an unspaced en-dash; a spaced en-dash is left exactly as written.
    ranges = [m for m in found if raw[:m.start()].rstrip()[-1:] in tuple("0123456789%")
              and _RANGE_END_RE.match(raw, m.end())]
    seps = [(m, _EN if _EM in m.group(0) else m.group(0)) for m in ranges]
    seps += _dash_separators(raw, kind, [m for m in found if m not in ranges])
    seps.sort(key=lambda p: p[0].start())
    out, pos = [], 0
    for m, sep in seps:
        before = raw[pos:m.start()]
        out.append(before.rstrip(",;") if sep.strip() in (",", ":", ".", "(") else before)
        out.append(sep)
        pos = m.end()
        if sep == ". ":
            raw = raw[:pos] + _cap_first(raw[pos:])
    out.append(raw[pos:])
    new = re.sub(r",[ \t]*,", ",", "".join(out))
    new = re.sub(r"[,:][ \t]*(?=[.!?…](?:\s|$|[\"'”’)*]))", "", new)
    return new, len(seps)


def fix_dashes(text: str, ctx: Optional[HygieneContext] = None) -> tuple[str, list[str]]:
    """AA-778 — replace every em-dash (and spaced en-dash) the writer used as a dash, sentence by
    sentence, deterministically:
    - a pair '… <dash> X <dash> …' is a parenthesis: ', X, ', or '(X)' when X has its own commas;
    - a single '… <dash> X.' becomes ', X.' when X continues the sentence (a conjunction, 'not', a
      relative word, an -ing or -ly word), '. X.' (capitalised) when X is a clause of its own ('it is
      two'), otherwise ': X.' (', X.' when the sentence already has a colon);
    - in a heading, ': ' (then ', ');
    - between two digits, an en-dash (a number range).
    Kept: the TRIP name written verbatim, a quoted span copied from the source and a testimonial
    attribution dash, so F19 still sees it (_protected_ranges; only the attribution is kept without
    `ctx`, e.g. an admin's sentence in sentence_repair.sanitize_replacement).
    Bare en-dashes ('3–5 days') and hyphens are never touched. autofix runs it after every sentence
    deletion, so the repeat and borrowed-sentence rules compare what the writer wrote; the gates
    (contrast frames, F2) read the result. Returns (text, log)."""
    text = text or ""
    if _EM not in text and not re.search(r"[ \t]–[ \t]", text):
        return text, []
    keep = _protected_ranges(text, ctx)
    total = 0
    for s in reversed(sentence_spans(text)):
        if s["kind"] == "hashtags" or (_EM not in s["text"] and _EN not in s["text"]):
            continue
        rel = [(max(a, s["start"]) - s["start"], min(b, s["end"]) - s["start"]) for a, b in keep
               if a < s["end"] and s["start"] < b]
        new, n = _fix_dash_span(s["text"], s["kind"], rel)
        if n:
            text = text[:s["start"]] + new + text[s["end"]:]
            total += n
    return text, ([f"replaced {total} em-dash(es) with a comma, colon, full stop or brackets"] if total else [])


def autofix(content_text: str, ctx: HygieneContext) -> tuple[str, list[str]]:
    """Fix in place what is safe to fix, in this order: the TRIP name when it names an
    out-of-scope place → 'this journey'; delete sentences borrowed from another trip that name an
    out-of-scope place or leak; trim a leaked frame ('The content seed notes that …'); delete
    social proof the seed does not support; delete the later of two near-verbatim sentences within
    3 of each other; replace the em-dashes (fix_dashes, AA-778); fix a hashtag one edit away from a
    seed place; hashtag cap / last line and first-line split when the channel spec gives them. The
    CTA sentence, headings and FAQ questions are never deleted. Returns (text, log of every edit)."""
    lex = ctx.lexicon
    # S234 round 4 (r3 #3) — blank lines before the first line hid a 283-character hook from the
    # first-line split below (it read an empty first line)
    text, log = (content_text or "").lstrip(), []
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

    # S234 round 3 — near-duplicates the window rule above does not see (a figure told twice, an echoed
    # opening, an orphan line) and a moment repeated under another moment's heading
    text, more = dedupe(text, ignore, ctx.cta)
    log += more
    from services.acp_content_writing.trip_facts import fix_misplaced
    text, more = fix_misplaced(text, ctx.moments, ctx.cta)
    log += more
    # AA-778 — after every deletion above (they compare the sentences as written), before the gates
    text, more = fix_dashes(text, ctx)
    log += more

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
    # S234 round 3 — what dedupe / fix_misplaced could not fix safely: blocking, sentence-repairable
    repeats: list[str] = []
    for d in near_duplicates(text, ignore, ctx.cta):
        repeats.append(f"{d['why']} — cut it or say something new: '{d['later']['clean'][:200]}'")
    repeats += [f"adds nothing to the sentences around it — cut it: '{o[:200]}'"
                for o in orphan_lines(text, ignore, ctx.cta)]
    from services.acp_content_writing.trip_facts import misplaced_sentences
    repeats += [f"repeats a moment under another moment's heading — cut it or move its fact: '{m[:200]}'"
                for m in misplaced_sentences(text, ctx.moments, ctx.cta)]
    repeated = {r.rsplit(": '", 1)[-1] for r in repeats}
    for a, s in enumerate(prose):
        if _is_cta(s["clean"], ctx.cta):
            continue
        earlier = [t["clean"] for t in prose[:a] if near_verbatim(s["clean"], t["clean"], ignore)]
        if earlier and f"{s['clean'][:200]}'" not in repeated:
            if any(strict_repeat(s["clean"], e, ignore) for e in earlier):
                # S234 round 4 — a real repeat is blocking (F13_repeat): the final pass deletes the
                # later copy when that is safe, so one left here is visible (r3 #12, #17 recaps)
                repeats.append(f"repeats an earlier sentence — cut it or say something new: '{s['clean'][:200]}'")
            else:
                # 2 shared trigrams only (a place phrase, the keyword, a template): a warning, as
                # before round 4 (review: r1 #17 'elevation of roughly … metres above sea level')
                issues.append(f"repeats an earlier sentence: '{s['clean'][:200]}'")
    # S234 round 4 (r3 panel §4 fix 1) — one CTA, no fragment: the same function the final pass uses
    from services.acp_content_writing.text_integrity import integrity_findings
    repeats += integrity_findings(text, ctx.cta)
    from services.acp_content_writing.text_integrity import unbalanced_findings
    issues += unbalanced_findings(text)
    for old, new in hashtag_fixes(text, lex, ctx.brand_name).items():
        issues.append(f"misspelt hashtag '{old}' (did you mean '{new}'?): '{old}'")
    if ctx.hashtags_max is not None:
        n = len(set(_HASHTAG_RE.findall(text)))
        if n > ctx.hashtags_max:
            issues.append(f"{n} hashtags, more than the channel's {ctx.hashtags_max}: '{n} hashtags'")
    return {"scope": scope, "hygiene": issues, "leak": list(dict.fromkeys(leaks)),
            "repeat": list(dict.fromkeys(repeats))}


__all__ = [
    "COMMON_CAPS", "HygieneContext", "brand_words", "LEAK_RE", "SIBLING_HEADER", "SOCIAL_PROOF_RE", "ScopeLexicon",
    "autofix", "EM_DASH_PATTERN", "fix_dashes",
    "build_scope_lexicon", "content_words", "copies", "copy_words", "expand_to_sentence", "find_issues",
    "find_scope_violations",
    "is_sentence_end", "sentence_tail_end",
    "fold", "hashtag_fixes", "lev", "names_place", "near_verbatim", "phrase_tokens", "places_named", "proper_nouns",
    "restates", "sentence_spans", "split_seed", "stock_line_items", "strip_tags", "tidy_after_cut", "TRIP_NAME_WORDS",
    "trip_name_places",
]
