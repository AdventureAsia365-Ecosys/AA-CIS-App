"""AA-1055 — keyword qualification for the keyword map (phase A of segment research) and the demand claim.

Why: an independent 3-judge panel on 90 sampled keyword|place pairs of research run R1 (S274) kept 80/86 keywords
right; the 6 wrong ones were (a) namesake place names bought without a locality — a deity or saint name plus
"temple" / "cathedral", a hotel name, an "Eagle's Nest" — whose GLOBAL search volume is about every place of that
name, so it inflates demand and can push a wrong Subject over the Blog bar; (b) a name that is not a place (a plant);
(c) a garbled phrase (a number, a type word and a verb). Root audit: docs/audits/2026-11-09-S274-session/aa1055-live/.

Three deterministic rules, no LLM and no I/O:

* `is_namesake_name(name)` — every word of the name is a generic TYPE word ("temple", "hotel", "lake", "fort"), a
  common proper noun with many namesakes ("ganesh", "immaculate", "himalaya", "eagle") or a qualifier ("old", "grand"),
  and at least one is a type word. A single distinctive word ("Zarvok Temple") clears it. Every keyword of such a place
  must carry its locality (city / region / country, from the place card, else the tour country); the plain name is
  replaced by "<name> <locality>"; a keyword that cannot be qualified is dropped.
* `not_a_place(name, what_it_is)` — a plant, animal or food, read from the place card's `what_it_is` fact ("... is a
  species of palm"); with no card, from the head noun of the name ("... tree"). Nothing is bought for it.
* `garbled_reason(keyword)` — a number glued to a word ("km5"), a brochure verb people do not type ("sightsee",
  "marvel"), or fewer than 2 meaningful words. Never applied to the plain place name.

`unqualified_words(place)` is the same namesake rule on the ranking side: for a namesake place, a bought keyword made
only of the place's own words (plus filler and generic search modifiers such as "hours", "tickets") is not claimed as
the Segment's demand, so the global "ganesh temple" row bought before this rule stops counting for every Ganesh Temple.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from services.acp_contract.ranking_reference import PLACE_KIND_NOUNS, PLACE_KINDS, keyword_words

_TIDY = re.compile(r"[^a-z0-9 ]+")
# Letters NFKD does not decompose to ASCII.
_FOLD = str.maketrans({"đ": "d", "Đ": "d", "ß": "ss", "æ": "ae", "ø": "o", "ł": "l", "œ": "oe", "þ": "th"})


def tidy(text: Optional[str]) -> str:
    """Lowercase ASCII words: accents folded ("Hội An" -> "hoi an"), everything that is not a letter or digit becomes
    a space. The tokenizer of the keyword map (segment_research_batch._tidy)."""
    folded = unicodedata.normalize("NFKD", (text or "").translate(_FOLD))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(_TIDY.sub(" ", folded.lower()).split())


# Words that carry nothing: articles, possessive "s" left by the tidy ("eagle's" -> "eagle s"), connectors.
FILLER = frozenset({"the", "a", "an", "of", "and", "s", "de", "del", "la", "le", "el", "at", "in", "on", "to"})

# What kind of place it is. Ranking's kind nouns (Japanese suffixes included) plus the landmark nouns it lacks.
TYPE_WORDS = frozenset((PLACE_KIND_NOUNS - {"japan", "japanese"}) | {
    "cathedral", "church", "chapel", "basilica", "mosque", "masjid", "pagoda", "stupa", "monastery", "kovil",
    "kovils", "devale", "vihara", "gompa", "palace", "palaces", "fort", "fortress", "citadel", "hill", "hills",
    "nest", "tower", "square", "bazaar", "cave", "caves", "tomb", "tombs", "monument", "memorial", "statue",
    "viewpoint", "resort", "resorts", "spa", "springs", "spring", "bay", "plaza", "zoo", "ruins", "gate", "wall",
    "rock", "peak", "point", "lagoon", "pond", "dam", "reservoir", "estate", "camp", "cottage", "villa", "guest",
    "homestay", "hut", "school", "college", "university", "hospital", "library", "theatre", "theater", "cinema",
    "stadium", "terminal", "pier", "jetty", "lighthouse", "garden", "gardens", "orchard", "grove",
})

# Common proper nouns and name words with many namesakes around the world: deities, saints and feasts, honorifics,
# colours and stock descriptive names, famous ranges and animals used as place names.
NAMESAKE_WORDS = frozenset({
    # Hindu / Buddhist / Jain deities and honorifics
    "ganesh", "ganesha", "ganapati", "ganapathi", "vinayaka", "vinayagar", "shiva", "siva", "shiv", "vishnu",
    "krishna", "rama", "ram", "hanuman", "kali", "durga", "lakshmi", "laxmi", "saraswati", "murugan", "brahma",
    "surya", "parvati", "devi", "mahadev", "jagannath", "buddha", "buddhist", "sri", "shri", "shree", "lord",
    "maa", "mata", "swami", "jain",
    # Christian dedications
    "immaculate", "conception", "holy", "trinity", "sacred", "heart", "saint", "st", "mary", "marys", "virgin",
    "lady", "our", "christ", "jesus", "cross", "assumption", "ascension", "resurrection", "nativity", "joseph",
    "john", "peter", "paul", "francis", "xavier", "anthony", "michael", "george", "james", "thomas", "andrew",
    "sebastian", "lucia", "catherine", "rosary", "redeemer", "saviour", "savior", "good", "shepherd", "angels",
    "angel", "all", "saints", "spirit", "family", "lourdes", "fatima", "baptist", "evangelist", "apostle",
    # Islamic dedications
    "jamia", "jama", "jami", "noor", "nur", "juma", "jummah",
    # stock descriptive names (hotels, restaurants, viewpoints)
    "royal", "golden", "silver", "white", "black", "red", "blue", "green", "crystal", "paradise", "sunset",
    "sunrise", "rainbow", "peace", "lotus", "imperial", "star", "sun", "moon", "palm", "palms", "ocean", "sea",
    "view", "heritage", "lakeside", "riverside", "hillside", "mountain", "city", "grand", "plaza", "tourist",
    "dream", "dreams", "happy", "lucky", "friendship", "liberty", "independence", "victory", "unity", "freedom",
    "central", "main", "kings", "king", "queen", "queens", "prince", "princess",
    # famous ranges and animals used as place names
    "himalaya", "himalayan", "himalayas", "everest", "alpine", "eagle", "eagles", "tiger", "tigers", "lion",
    "lions", "elephant", "elephants", "dragon", "phoenix", "falcon", "hawk", "swan",
})

# A name whose last word is one of these is a thing, not a place (used only when there is no place card).
NON_PLACE_HEADS = frozenset({
    "tree", "trees", "plant", "plants", "flower", "flowers", "orchid", "orchids", "shrub", "herb", "herbs",
    "spice", "spices", "seed", "seeds", "leaf", "leaves", "fruit", "fruits", "bird", "birds", "animal", "animals",
    "species", "dish", "dishes", "curry", "soup", "noodles", "dessert", "drink", "drinks", "cuisine", "snack",
    "snacks", "sweets", "delicacy",
})
# The place card's what_it_is fact naming a plant / animal / food ("Velk is a species of palm ...", "... is a sweet
# dish made of ..."): the first of these nouns within the 4 words after "is a" / "are", with no type word ("lake",
# "temple") before it. A fact that also names a site ("a sacred fig tree in the temple grounds") stays a place.
_NON_PLACE_NOUNS = frozenset({
    "species", "variety", "genus", "tree", "palm", "plant", "shrub", "flower", "orchid", "herb", "spice", "fruit",
    "bird", "animal", "mammal", "reptile", "fish", "dish", "food", "drink", "beverage", "dessert", "sweet", "snack",
    "delicacy", "liquor", "cuisine",
})
_IS_A = re.compile(r"\b(?:is|are|refers to)\s+(?:(?:a|an|the)\s+)?((?:[a-z-]+\s*){1,4})", re.IGNORECASE)
_SITE_WORDS = re.compile(
    r"\b(?:temple|shrine|site|sacred|pilgrimage|monument|landmark|garden|gardens|park|grounds|village|town|museum"
    r"|reserve|sanctuary|estate|farm|plantation)\b",
    re.IGNORECASE,
)

# Brochure verbs nobody types into a search box.
BROCHURE_VERBS = frozenset({
    "sightsee", "discover", "enjoy", "admire", "witness", "marvel", "wander", "immerse", "embark", "savor",
    "savour", "soak", "behold", "relish", "delight", "unwind", "indulge", "uncover",
})
_GLUED = re.compile(r"^(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]+$")
_ORDINAL = re.compile(r"^\d+(?:st|nd|rd|th)$")

# Generic search modifiers: a keyword made of a namesake place's own words plus these is still unqualified.
SEARCH_MODIFIERS = frozenset({
    "hours", "timings", "timing", "opening", "open", "time", "times", "tickets", "ticket", "entry", "entrance",
    "fee", "fees", "price", "prices", "cost", "history", "photos", "pictures", "images", "map", "location",
    "address", "near", "me", "tour", "tours", "visit", "visiting", "best", "top", "famous", "dress", "code",
    "facts", "information", "info", "guide", "website", "review", "reviews", "booking", "book", "deals", "rooms",
})


def _words(name: Optional[str]) -> list[str]:
    return [w for w in tidy(name).split() if w not in FILLER]


def is_namesake_name(name: Optional[str]) -> bool:
    """True when the name names many places around the world: only type words, namesake words and qualifiers, with
    at least one type word ("Ganesh Temple", "Immaculate Conception Cathedral", "Hotel Himalaya", "City Palace")."""
    words = _words(name)
    if not words or not any(w in TYPE_WORDS for w in words):
        return False
    return all(w in TYPE_WORDS or w in NAMESAKE_WORDS or w in PLACE_KINDS for w in words)


def not_a_place(name: Optional[str], what_it_is: Optional[str] = None) -> bool:
    """True when the "place" is a plant, animal or food. The place card's what_it_is fact decides when there is one;
    otherwise the head noun of the name."""
    if what_it_is and what_it_is.strip():
        if _SITE_WORDS.search(what_it_is):
            return False
        m = _IS_A.search(what_it_is)
        for word in (m.group(1).lower().split() if m else []):
            if word in TYPE_WORDS or word in PLACE_KINDS:
                return False
            if word in _NON_PLACE_NOUNS:
                return True
        return False
    words = _words(name)
    return bool(words) and words[-1] in NON_PLACE_HEADS


def garbled_reason(keyword: Optional[str]) -> Optional[str]:
    """Why a proposed keyword is not a real search ('glued_number' / 'brochure_verb' / 'too_short'), else None."""
    words = tidy(keyword).split()
    if any(_GLUED.match(w) and not _ORDINAL.match(w) for w in words):
        return "glued_number"
    if any(w in BROCHURE_VERBS for w in words):
        return "brochure_verb"
    meaningful = [w for w in words if w not in FILLER and not w.isdigit() and len(w) > 1]
    if len(meaningful) < 2:
        return "too_short"
    return None


@dataclass(frozen=True)
class PlaceLocation:
    """Where a place is, from its place card (locality / region / country) or the tour country, plus the card's
    what_it_is fact."""
    locality: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    what_it_is: Optional[str] = None

    def phrases(self, name: Optional[str] = None) -> list[str]:
        """The tidied locality phrases, most specific first, without empty, long (over 3 words), generic (only type
        words) or repeated ones, and without one made only of the name's own words."""
        own = set(_words(name))
        out: list[str] = []
        for raw in (self.locality, self.region, self.country):
            phrase = tidy(raw)
            words = [w for w in phrase.split() if w not in FILLER]
            if (not words or len(words) > 3 or all(w in TYPE_WORDS or w in PLACE_KINDS for w in words)
                    or set(words) <= own):
                continue
            if phrase not in out:
                out.append(phrase)
        return out


def carries(keyword: str, phrases: list[str]) -> bool:
    """True when every word of at least one locality phrase is a word of the keyword."""
    kw = set(tidy(keyword).split())
    return any(set(p.split()) <= kw for p in phrases)


@dataclass
class Qualified:
    kept: list[str] = field(default_factory=list)
    dropped: dict[str, str] = field(default_factory=dict)   # keyword -> reason
    plain: Optional[str] = None                              # the place's own keyword (qualified when namesake)
    flag: Optional[str] = None                               # 'namesake' / 'not_a_place' / None


def qualify(name: str, keywords: list[str], plain: Optional[str], where: Optional[PlaceLocation]) -> Qualified:
    """Apply the three rules to one place's proposed keywords (already cleaned, plain name first). The kept list keeps
    the input order; a namesake plain name is replaced by "<plain> <most specific locality>" in place."""
    where = where or PlaceLocation()
    out = Qualified()
    if not_a_place(name, where.what_it_is):
        out.flag = "not_a_place"
        out.dropped = {kw: "not_a_place" for kw in keywords}
        return out
    namesake = is_namesake_name(name)
    out.flag = "namesake" if namesake else None
    phrases = where.phrases(name) if namesake else []
    for kw in keywords:
        if kw != plain and (reason := garbled_reason(kw)):
            out.dropped.setdefault(kw, f"garbled_{reason}")
            continue
        if namesake and not carries(kw, phrases):
            if kw == plain and phrases:
                qualified = f"{kw} {phrases[0]}"
                out.dropped.setdefault(kw, "unqualified")
                if qualified not in out.kept:
                    out.kept.append(qualified)
                out.plain = qualified
            else:
                out.dropped.setdefault(kw, "unqualified")
            continue
        if kw not in out.kept:
            out.kept.append(kw)
        if kw == plain:
            out.plain = kw
    return out


def unqualified_words(place: Optional[str]) -> Optional[frozenset[str]]:
    """Ranking side: for a namesake place, the words a bought keyword may consist of and still be unqualified (the
    place's own words in both tokenizations, filler, search modifiers); None for any other place. A keyword whose words
    are all inside this set is about every place of that name, so it is never claimed as this Segment's demand."""
    if not is_namesake_name(place):
        return None
    return frozenset(set(tidy(place).split()) | keyword_words(place or "") | FILLER | SEARCH_MODIFIERS)


__all__ = ["tidy", "is_namesake_name", "not_a_place", "garbled_reason", "PlaceLocation", "carries", "Qualified",
           "qualify", "unqualified_words"]
