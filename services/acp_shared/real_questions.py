"""services.acp_shared.real_questions — AA-779 (S245) real-user question source chain for blogs.

A blog (T8 angles, T9 FAQ) used to read People Also Ask (PAA) only from the tour-level SERP
(`silver_aa_internal.seo_context`, one call on the tour title). Google often shows no PAA box for a
long, rare tour title, so the S245 checkpoint had none, while the questions of the blog's own places
were already in the DB. This module reads them, in a fixed order, at $0 (DB reads only):

  1. `segment_match` — PAA questions already landed on the piece's Segments by embedding match
     (`acp_contract.atom_matches`, kind 'question'; they passed the A3 Jev scope/landing gates).
  2. `place_paa`     — PAA bought for a keyword of one of the piece's places
     (`acp_contract.search_demand.people_also_ask`, any market, English questions only).
  2b. `serp_topup`   — PAA the admin-only SERP top-up (AA-779 B, click depth 2) bought for
     '<place> <country>' (`search_demand`, keyword `topup_keyword`, its own market `TOPUP_MARKET`
     that no other reader uses, so it never mixes with the Segment research cache).
  Off-meaning questions (about a place name as a word: 'What is a moron?' for Mörön) are dropped
  from every tier, using the piece's own place words (and their spelling variants: 'Khuvsgul').
  The FAQ's scope filter (quality_gates.paa_for_scope) reads `own_place_forms` / `own_place_named`
  (exact own names, written as names), `variant_place_question` (romanisation variants and exonyms,
  with the other-place / comparison / harm checks), `harm_question` (war, politics, diet: never a FAQ
  question) and `country_travel_question` (practical travel questions about the scope's country) from here.
  3. `tour_paa`      — the tour-level PAA the request snapshot holds (the only source before).

Later sources plug in as more `QuestionSource` objects in `DB_SOURCES` (before the tour tier):
AA-779 (B) the admin SERP top-up (`serp_topup`, reader `SerpTopupSource`, buyer
`question_topup.py`) and (C) a DataForSEO Autocomplete fallback (`autocomplete`, not built). Those
buy data in an admin job / script only; this module never calls
DataForSEO (AA-646: no tenant action buys DFS data). A question an LLM writes is never "real"
(`SOURCE_WRITER` only labels a FAQ question that matches no real one).

The source of each question is admin-only data (`content_piece.writer_input`); the tenant never sees
it (the request snapshot the tenant API returns carries question texts only).
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, Sequence

import structlog

from services.acp_content_writing.hygiene import COMMON_CAPS, content_words
from services.acp_contract.ranking_reference import PLACE_KIND_NOUNS, PLACE_KINDS, keyword_words

logger = structlog.get_logger()

SOURCE_SEGMENT = "segment_match"
SOURCE_PLACE = "place_paa"
SOURCE_TOUR = "tour_paa"
# AA-779 (B) admin SERP top-up (reader: SerpTopupSource); (C) reserved, no reader yet.
SOURCE_SERP_TOPUP = "serp_topup"
SOURCE_AUTOCOMPLETE = "autocomplete"
REAL_SOURCES = (SOURCE_SEGMENT, SOURCE_PLACE, SOURCE_SERP_TOPUP, SOURCE_AUTOCOMPLETE, SOURCE_TOUR)
# A FAQ question that matches no real question (the writer or write_faq made it up).
SOURCE_WRITER = "writer"

PER_SOURCE_MAX = 12  # raw questions one source may add
CHAIN_MAX = 30       # raw questions in the job payload (paa_for_scope keeps at most 6 of them)
# A Segment whose questions_count cache is NULL (invalidated, not recomputed yet): matches newer than
# this before its last computation still count (a full landing pass takes minutes, not a day).
STALE_MATCH_SLACK = "1 day"


def fold(text: Optional[str]) -> str:
    """Accent-folded lower case: 'Khövsgöl' → 'khovsgol', 'Đà Nẵng' → 'da nang' (đ has no NFKD split)."""
    return deaccent(text).lower()


def question_key(question: str) -> str:
    """The dedupe / match key: folded, whitespace collapsed, end punctuation cut."""
    return " ".join(fold(question).split()).rstrip(" ?.!")


# ---------------------------------------------------------------- language + meaning

_ENGLISH_OPENERS = frozenset("""what where when why how who whom whose which is are was were can could do does did
should would will shall may might has have had am isn aren wasn don doesn didn
""".split())
# Words of a German / French / Dutch question that an English one does not use ('Was ist …?').
_FOREIGN_WORDS = frozenset("""ist sind und der das het een wat hoe waar est sont les une quel quelle quels pour wie
kann nicht mit ein eine zum zur dans avec welke kunnen moet gibt kostet bedeutet heisst
""".split())
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def looks_english(question: str) -> bool:
    """A PAA question in English starts with a question word or an auxiliary and has no German /
    French / Dutch function word (DE/FR/NL markets may return questions in their language)."""
    tokens = _TOKEN_RE.findall(fold(question))
    return bool(tokens) and tokens[0] in _ENGLISH_OPENERS and not (set(tokens) & _FOREIGN_WORDS)


# A question about the place NAME as a word (its meaning, spelling, an insult it sounds like) rather
# than the place: the DFS keyword of the town Mörön is 'moron', and its SERP asks about the insult.
_WORD_SENSE_RE = re.compile(
    r"\b(?:mean(?!\s+(?:annual|daily|monthly|yearly|temperatures?|depth|elevation|altitude|height|rainfall))"
    r"|means|meaning|meanings|definition|define|defined|synonyms?|antonyms?|slang|insult(?:s|ing)?"
    r"|swear\s+word|curse\s+word|bad\s+word|idiot|idiots|imbecile|stupid|spell(?:ed|ing)?|spelt"
    r"|pronounce[ds]?|pronunciation|etymology|another\s+word|the\s+word|a\s+word|the\s+term)\b")
# S245 r7 review — the later word cues ('a British word', 'word for', translation, dictionary,
# rhyme, slur) only count right next to one of the piece's own place names (see `_name_word_cue`):
# on their own they also fire on real traveller questions ('How do you say hello in Mongolian?',
# 'What is the Mongolian word for hello?', 'Is English translation available on Mongolia tours?').

# Languages a 'What is <name> in <language>?' question asks a translation into.
_LANGUAGES = (
    "english|german|french|spanish|italian|portuguese|dutch|latin|greek|russian|polish|swedish|norwegian|danish"
    "|finnish|turkish|arabic|hebrew|hindi|urdu|chinese|mandarin|cantonese|japanese|korean|vietnamese|thai|malay"
    "|indonesian|tagalog|mongolian|tibetan|nepali|persian|farsi|swahili|sign\\s+language")
# A question that names the place for a traveller: a lowercase name with none of these is the word,
# not the place ('Is moron offensive in the UK?' has none; 'how far is moron from the lake?' has one).
_TRAVEL_CONTEXT = frozenset("""
visit visiting visited travel travelling traveling trip trips tour tours town city village capital province
region district country airport airports flight flights fly flying drive driving bus train road roads
hotel hotels stay stays camp camps lodge guesthouse weather climate winter summer spring autumn season
temperature temperatures cold hot warm snow rain far distance km kilometres kilometers miles located
location where population lake lakes river rivers mountain mountains valley park national near nearby
reach worth known famous things eat food restaurant museum monastery temple market safe safety deep
sights attractions frozen freeze freezes swim swimming hike hiking trek trekking open tourist tourists
tourism month months best time volcano volcanoes climb climbing steps pack packing expensive cheap cost
costs price prices spend spending festival festivals people language languages cruise cruises dangerous
crowded
""".split())
# Common English words that also make up place names ('Ha Long', 'Great Wall', 'Tiger's Nest', 'Golden
# Triangle'): never read as 'the place name used as a word' — a question using the word is ordinary
# English ('How long should I spend in Hanoi?'). 'moron' is deliberately not here: it is the case the
# test exists for (a rare word whose own sense Google answers).
_COMMON_ENGLISH = frozenset("""
long great golden gold white black blue green yellow orange pink purple brown grey gray dark light bright
rock rocks wall walls nest tiger tigers flaming fire water stone stones sand sun moon star stars sky sea
cliff cliffs old new high low big little small tall deep grand royal holy sacred happy free fine nice best
good north south east west upper lower central middle inner outer first second third last hidden lost
dragon elephant monkey horse eagle lion snake bird fish cat dog white crystal silver jade pearl iron
paradise heaven garden gardens bridge gate gates tower towers castle forest jungle desert island islands
bay beach peak point head cape spring springs wells well field fields stream wood woods green mount
crown king queen prince temple palace hall house home church market square street road way trail
triangle circle cross corner line view rise heights valley falls river lake city town village
""".split())
_NAME_TOKEN_RE = re.compile(r"[^\W\d_]+")


def _lowercase_only_use(question: str, name: str) -> bool:
    """The name occurs in the question, and only as a lowercase word after the first one —
    Google PAA capitalises a place ('Does Mörön have an airport?') but not the common word
    ('Is moron a British word?'). A first-word occurrence says nothing (always capitalised)."""
    seen = False
    for i, tok in enumerate(_NAME_TOKEN_RE.findall(question or "")):
        if fold(tok) != name:
            continue
        if i == 0 or tok[0].isupper():
            return False
        seen = True
    return seen


def _name_word_cue(q: str, n: str) -> bool:
    """A word cue right next to the (escaped, folded) place name `n` in the folded question `q`:
    'Is moron a British word?', 'Is moron a slur?', 'word for moron', 'translation of moron',
    'what rhymes with moron'."""
    return bool(
        re.search(rf"\b{n}\s+(?:is\s+)?(?:an?|the)\s+(?:\w+\s+){{0,2}}(?:words?|slur|term)\b", q)
        or re.search(rf"\b{n}\s+(?:is\s+)?(?:an?\s+)?(?:slur|derogatory)\b", q)
        or re.search(rf"\bwords?\s+for\s+(?:an?\s+|the\s+)?{n}\b", q)
        or re.search(rf"\btranslat\w*\s+(?:of\s+|for\s+)?(?:the\s+(?:word|name)\s+)?{n}\b", q)
        or re.search(rf"\b{n}\s+translat\w*", q)
        or re.search(rf"\b(?:rhymes?\s+with|dictionary\s+(?:definition|meaning)\s+of)\s+{n}\b", q)
        or re.search(rf"\b{n}\s+(?:rhymes?\s+with|in\s+the\s+dictionary)\b", q))


def _single_word(name: str) -> bool:
    return len(name) >= 4 and name not in _PLACE_GENERIC and name not in _COMMON_ENGLISH


def single_word_places(places: Iterable[str], countries: Iterable[str] = ()) -> set[str]:
    """The places whose whole name folds to ONE word ('Mörön' → 'moron'), the only names the
    lowercase-word test may read: a token of a longer name ('long' of 'Ha Long Bay', 'wall' of
    'Great Wall', 'khovsgol' of 'Lake Khövsgöl') is never one. Not a country, not a kind of
    place, not a common English word."""
    skip = {w for c in countries for w in _TOKEN_RE.findall(fold(c))}
    out: set[str] = set()
    for p in places:
        tokens = _TOKEN_RE.findall(fold(p))
        if len(tokens) == 1 and tokens[0] not in skip and _single_word(tokens[0]):
            out.add(tokens[0])
    return out


def is_off_meaning(question: str, names: Iterable[str] = (), lowercase_names: Iterable[str] = (),
                   countries: Iterable[str] = ()) -> bool:
    """True when the question is about a word, not a place: a word-sense cue ('Is moron a bad
    word?', 'What does it mean if someone calls you a moron?'), or — for `names`, the folded name
    tokens of the piece's own places — the name used as a word: a common noun ('What is a moron?',
    'Is moron offensive?', 'Why do people call someone a moron?'), with a word cue next to it ('Is
    moron a British word?', 'Is moron a slur?'), asked in another language ('What is moron in
    German?'), spelt / said / translated ('How do you spell moron?'). Real travel questions stay:
    'Does Mörön have an airport?', 'How far is Moron from Lake Khovsgol?', 'Is it rude to refuse
    tea in a ger?' (rude / offensive count only right after the name), 'How do you say hello in
    Mongolian?' (no place name next to the cue). A common English word ('long' of Ha Long Bay) is
    never read as a name.

    The lowercase test ('Is moron still used today?') reads only `lowercase_names` — folded
    single-word place names (`single_word_places`; default none). It fires only when such a name
    is written lowercase (never capitalised, never first), the question names no other place word
    of `names`/`lowercase_names` and no country of `countries` (folded country names), and has no
    travel word. The autocomplete tier passes none: its text is all lowercase."""
    q = fold(question)
    if _WORD_SENSE_RE.search(q):
        return True
    q_words = set(_TOKEN_RE.findall(q))
    names = {n for n in names if n and n not in _COMMON_ENGLISH}
    lower_ok = {n for n in lowercase_names if n and _single_word(n)}
    # S245 (20/10) — a spelling variant of an own name ('Khuvsgul' for Khövsgöl) is that name here
    # too, so 'Is Khuvsgul a British word?' is caught like the exact spelling.
    names |= {v for v in name_variants_in(question, names) if v not in _COMMON_ENGLISH}
    lower_ok |= {v for v in name_variants_in(question, lower_ok) if _single_word(v)}
    country_words = {w for c in countries for w in _TOKEN_RE.findall(fold(c))}
    for name in lower_ok:
        if (_lowercase_only_use(question, name)
                and not (q_words - {name}) & (_TRAVEL_CONTEXT | names | lower_ok | country_words)):
            return True
    for name in names | lower_ok:
        n = re.escape(name)
        if (re.search(rf"\b(?:a|an)\s+{n}\s*(?:\?|$|\band\b|\bor\b|\bvs\b)", q)
                or re.search(rf"\b(?:a|an)\s+(?:total|complete|real|absolute|utter|such)\s+{n}\b", q)
                or re.search(rf"\b{n}\s+(?:is\s+|so\s+)?(?:offensive|rude)\b", q)
                or re.search(rf"\b(?:call|calls|calling|called)\s+(?:you|someone|somebody|him|her|them|me)"
                             rf"\s+an?\s+{n}\b", q)
                or re.search(rf"\b{n}\s+in\s+(?:{_LANGUAGES})\b", q)
                or re.search(rf"\b(?:say|spell|write|translate)\s+(?:the\s+name\s+)?{n}\b", q)
                or re.search(rf"\b{n}\s+come\s+from\b", q)
                or _name_word_cue(q, n)):
            return True
    return False


# ---------------------------------------------------------------- spelling variants + country level (S245, 20/10)
# Live 20/10: the SERP top-up stored good questions for 'khovsgol lake mongolia' ('Can you swim in Lake
# Khuvsgul?', 'What is the most famous lake in Mongolia?') and the scope filter kept 0 of 12: Google spells
# the lake Khuvsgul / Hovsgol, and the country-level ones name no place of the moment.
# Review round 1 (S245, 20/10): a kept question can reach a customer-facing FAQ, so precision comes first.
# A spelling variant is a romanisation of the same name (diacritics, kh/h, o/u/ö/ü, doubled letters, one
# inserted vowel, words joined or split) or a listed exonym — never a free edit distance, which matched
# other real places one consonant apart (Karakoram for Karakorum, Darkhan for Darkhad, Chengde for
# Chengdu). A country-level question needs the country written as a name, a travel topic, and a tie to
# the moment.

# A variant needs a name (and the question word) of at least this many folded letters: short names
# ('Hue', 'Paro', 'Mörön') match only exactly or through the alias table.
VARIANT_MIN_LEN = 6


def deaccent(text: Optional[str]) -> str:
    """Diacritics off, case kept: 'Huế' → 'Hue', 'Đà Nẵng' → 'Da Nang' (NFKD leaves đ / Đ whole)."""
    t = (text or "").replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))


def joined_name(text: Optional[str]) -> str:
    """The folded letters of a name with spaces and hyphens removed: 'Ha Long' → 'halong'."""
    return "".join(_TOKEN_RE.findall(fold(text)))


def name_skeleton(word: str) -> str:
    """The romanisation key of a place name: folded and joined ('Khövsgöl' → 'khovsgol', 'Hoi An' →
    'hoian'), 'kh' → 'h' (romanisations drop it: Hovsgol), u → o (ö / ü / o / u are one vowel pair in
    Latin letters: Khuvsgul), doubled letters once ('Ulaanbaatar' → 'olanbatar')."""
    w = joined_name(word).replace("kh", "h").replace("u", "o")
    return re.sub(r"(.)\1+", r"\1", w)


# Exonyms and older romanisations Google uses for a place (folded, words joined); one group = one place.
# Matched only as a capitalised question word (Google capitalises a place).
_PLACE_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("ulaanbaatar", "ulanbator", "ulaanbator", "ulanbatar"),
    ("kharkhorin", "karakorum", "kharkhorum", "kharakhorum"),
    ("moron", "muren", "murun"),
    ("luangprabang", "louangphabang", "louangprabang"),
    ("thimphu", "thimpu"),
    ("yangon", "rangoon"),
    # 'Ho Chi Minh' alone in a question is the person: `person_question` drops it first (review round 3)
    ("hochiminh", "hochiminhcity", "saigon"),
    ("siemreap", "siemriep"),
    ("kathmandu", "katmandu"),
    ("myanmar", "burma"),
    ("laos", "laopdr"),
)
_PLACE_ALIASES: dict[str, frozenset] = {}
for _group in _PLACE_ALIAS_GROUPS:
    for _key in _group:
        _PLACE_ALIASES[_key] = _PLACE_ALIASES.get(_key, frozenset()) | frozenset(_group)
_VOWELS = frozenset("aeio")  # after the skeleton's u → o


def _inserted_vowel(longer: str, shorter: str) -> bool:
    """`longer` is `shorter` with one vowel added inside the word ('hovsogol' / 'hovsgol': Khuvsugul)."""
    if len(longer) != len(shorter) + 1:
        return False
    return any(longer[i] in _VOWELS and longer[:i] + longer[i + 1:] == shorter for i in range(1, len(longer) - 1))


# Words that make a listed exonym another thing (S245 review round 2): 'Karakorum' is also a spelling of
# the Karakoram range ('How high is the Karakorum range?', 'Is K2 in the Karakorum?'), 'Rangoon' a crab
# dish ('Is crab Rangoon healthy?'). Folded alias key → words that, anywhere in the question, deny it.
_ALIAS_HOMONYMS: dict[str, frozenset] = {
    "karakorum": frozenset("range ranges mountain mountains highway peak peaks glacier glaciers pass k".split()),
    "kharakhorum": frozenset("range ranges mountain mountains highway peak peaks glacier glaciers pass k".split()),
    "rangoon": frozenset("crab crabs cheese wonton wontons recipe recipes calories sauce dip".split()),
    "burma": frozenset("shave".split()),
    # review round 3 — Saigon cinnamon (a spice), Saigon beer, Miss Saigon (a musical)
    "saigon": frozenset("cinnamon cassia spice spices powder beer miss musical".split()),
}


def place_variant_kind(word: str, name: str, *, capitalised: bool = True) -> Optional[str]:
    """How `word` (a question word, or a run of words) spells the place name `name`: 'exact' (equal
    after folding and joining: 'Hoian' / 'Hoi An'), 'skeleton' (both ≥ VARIANT_MIN_LEN letters, the
    same `name_skeleton`: Khuvsgul, Hovsgol, Khovsgol, Khövsgöl are one; only for a `capitalised`
    word also one inserted vowel: 'Khuvsugul'), 'alias' (a listed exonym of a `capitalised` word,
    `_PLACE_ALIAS_GROUPS`: Ulan Bator, Karakorum, Muren), or None. Never a consonant change:
    Karakoram is not Karakorum, Darkhan is not Darkhad, Mandala is not Mandalay."""
    w, n = joined_name(word), joined_name(name)
    if not w or not n:
        return None
    if w == n:
        return "exact"
    aliases = _PLACE_ALIASES.get(n, frozenset())
    if len(w) >= VARIANT_MIN_LEN and len(n) >= VARIANT_MIN_LEN:
        sw, sn = name_skeleton(w), name_skeleton(n)
        if sw == sn or (capitalised and (_inserted_vowel(sw, sn) or _inserted_vowel(sn, sw))):
            return "skeleton"
    if not capitalised:
        return None
    if w in aliases:
        return "alias"
    if len(w) < VARIANT_MIN_LEN:
        return None
    sw = name_skeleton(w)
    keys = {name_skeleton(a) for a in aliases if len(a) >= VARIANT_MIN_LEN}
    if sw in keys or any(_inserted_vowel(sw, k) or _inserted_vowel(k, sw) for k in keys):
        return "alias"
    return None


def place_variant_match(word: str, name: str, *, capitalised: bool = True) -> bool:
    """Is `word` a spelling of the place name `name` (`place_variant_kind`)?"""
    return place_variant_kind(word, name, capitalised=capitalised) is not None


def _question_word_runs(question: Optional[str]) -> list[tuple[str, bool]]:
    """(folded surface, capitalised) for every word of the question and every run of 2–3 adjacent
    capitalised words ('Ha Long', 'Ulan Bator'), diacritics folded."""
    text = deaccent(question)
    words = [(m.group(0), m.start(), m.end()) for m in _NAME_TOKEN_RE.finditer(text)]
    out: list[tuple[str, bool]] = []
    for i, (w, _, _) in enumerate(words):
        out.append((fold(w), w[0].isupper()))
        if not w[0].isupper():
            continue
        parts = [w]
        j = i
        while j + 1 < len(words) and len(parts) < 3:
            nxt, start, _ = words[j + 1]
            if not nxt[0].isupper() or text[words[j][2]:start].strip(" -"):
                break
            parts.append(nxt)
            j += 1
            out.append((fold(" ".join(parts)), True))
    return out


def variant_kinds(question: str, names: Iterable[str]) -> dict[str, str]:
    """Folded words (or word runs: 'ha long') of `question` that are a spelling variant — not the
    exact single word — of one of the place names `names` (folded words or joined names), each with
    its kind ('skeleton' or 'alias', `place_variant_kind`; 'skeleton' wins). A country or nationality
    word is never a place variant ('Cambodian' is not Cambodia's own place)."""
    targets = {joined_name(n) for n in names if n}
    targets = {t for t in targets if t and t not in ALL_COUNTRY_WORDS}
    if not targets:
        return {}
    out: dict[str, str] = {}
    for surface, capitalised in _question_word_runs(question):
        key = joined_name(surface)
        if not key or key in ALL_COUNTRY_WORDS or (" " not in surface and key in targets):
            continue
        kinds = {place_variant_kind(key, t, capitalised=capitalised) for t in targets} - {None}
        if kinds:
            out[surface] = "skeleton" if kinds & {"exact", "skeleton"} else "alias"
    return out


def name_variants_in(question: str, names: Iterable[str]) -> set[str]:
    """The surfaces of `variant_kinds` ('khuvsgul' for 'khovsgol', 'halong' for 'ha long', 'ulan
    bator' for 'ulaanbaatar')."""
    return set(variant_kinds(question, names))


def names_own_place(question: str, names: Iterable[str]) -> bool:
    """Does `question` name one of the folded place words `names`, exactly or as a spelling
    variant (`name_variants_in`)?"""
    names = {n for n in names if n}
    return bool(set(_TOKEN_RE.findall(fold(question))) & names) or bool(name_variants_in(question, names))


# Folded country word (as `_seed_countries` / a Location line gives it) → its folded demonyms: a
# question about 'Mongolian gers' or 'Lao food' is about the country too.
DEMONYMS: dict[str, tuple[str, ...]] = {
    "mongolia": ("mongolian", "mongolians"), "laos": ("lao", "laotian", "laotians"), "lao": ("laotian",),
    "bhutan": ("bhutanese",), "vietnam": ("vietnamese",), "cambodia": ("cambodian", "cambodians", "khmer"),
    "thailand": ("thai",), "myanmar": ("burmese",), "burma": ("burmese",), "nepal": ("nepali", "nepalese"),
    "tibet": ("tibetan", "tibetans"), "china": ("chinese",), "india": ("indian", "indians"),
    "japan": ("japanese",), "korea": ("korean", "koreans"), "indonesia": ("indonesian", "indonesians"),
    "malaysia": ("malaysian", "malaysians"), "philippines": ("filipino", "filipinos", "philippine"),
    "lanka": ("lankan", "lankans"), "uzbekistan": ("uzbek", "uzbeks"), "kazakhstan": ("kazakh", "kazakhs"),
    "kyrgyzstan": ("kyrgyz",),
}
# Other countries and nationalities a question may name (folded): never the scope, never a place
# variant. Read in lowercase too ('Is Mongolia as safe as japan?').
_OTHER_COUNTRY_WORDS = frozenset("""
russia russian russians america american americans usa canada canadian canadians britain british brits
england australia australian australians europe european europeans france german germans germany italy
italian italians spain singapore singaporean taiwan taiwanese siberia siberian kazakh korea korean north
south mexico brazil israel israeli israelis dubai uae
""".split()) - {"north", "south"}
ALL_COUNTRY_WORDS = (frozenset(DEMONYMS) | {d for ds in DEMONYMS.values() for d in ds} | _OTHER_COUNTRY_WORDS
                     | {"vietnam", "burma", "myanmar", "laos"})
# Nationalities a traveller question may name about itself — only in a traveller frame ('… for American
# tourists', '… for Americans'); anywhere else a nationality makes the question a comparison.
_NATIONALITY_WORDS = (frozenset({d for ds in DEMONYMS.values() for d in ds}) | frozenset("""
american americans british brit brits english australian australians canadian canadians european europeans
german germans french dutch italian italians spanish swiss israeli israelis russian russians singaporean
taiwanese us uk usa
""".split()))
_TRAVELLER_NOUNS = frozenset("""
tourist tourists traveller travellers traveler travelers visitor visitors citizen citizens passport passports
national nationals backpacker backpackers expat expats holder holders
""".split())


def _scope_countries(countries: Iterable[str]) -> tuple[set[str], set[str]]:
    """(folded country names with their aliases, folded demonyms) of the scope's countries
    ('viet' + 'nam' of 'Viet Nam' → 'vietnam'; 'myanmar' ↔ 'burma')."""
    words = {w for c in countries for w in _TOKEN_RE.findall(fold(c))}
    if {"viet", "nam"} <= words:
        words = (words - {"viet", "nam"}) | {"vietnam"}
    if {"lao", "pdr"} <= words:
        words = (words - {"lao", "pdr"}) | {"laos"}
    words |= {a for w in words for a in _PLACE_ALIASES.get(w, ())}
    return words, {d for w in words for d in DEMONYMS.get(w, ())}


def country_terms(countries: Iterable[str]) -> set[str]:
    """Folded words of the scope's countries (and their aliases) plus their demonyms ('mongolia',
    'mongolian', …)."""
    names, demonyms = _scope_countries(countries)
    return names | demonyms


# Words after a demonym that make it the country as a destination ('Mongolian gers', 'Lao food'); a
# homonym noun near it never does ('Mongolian beef', 'Burmese python', 'Tibetan mastiff').
_DEMONYM_TRAVEL_NOUNS = frozenset("""
food foods cuisine dish dishes meal meals breakfast restaurant restaurants people culture customs custom
traditions tradition etiquette hospitality ger gers yurt yurts homestay homestays camp camps tourists
tourism visa visas currency money weather climate festival festivals language nomad nomads nomadic
countryside village villages tea
""".split())
_DEMONYM_HOMONYMS = frozenset("""
beef bbq barbecue grill grilled hot pot gerbil gerbils spot spots death worm worms python pythons cat cats
kitten kittens mastiff mastiffs terrier terriers spaniel spaniels rug rugs script alphabet chicken lamb
noodles noodle dog dogs horse horses sheep
""".split())
_TRAVEL_FRAME_RE = re.compile(
    r"\b(?:you|your|i|we|my|our|visit\w*|travel\w*|trips?|tours?|touris\w*|holidays?|vacations?|backpack\w*"
    r"|famous|known|best|top|beautiful|popular|must|worth|explore|see)\b")
# General practical travel topics: a country-level question on one of these is in scope for any moment.
_PRACTICAL_RE = re.compile(
    r"\b(?:best\s+(?:time|month|months|season|seasons)|when\s+(?:to|should\s+(?:i|you|we))\s+(?:visit|go|travel)"
    r"|(?:time|month|season)s?\s+(?:of\s+(?:the\s+)?year\s+)?to\s+(?:visit|go|travel)"
    r"|weather|climate|temperatures?|rain|rains|rainy|snow|snows|monsoon|cold|warm|hot\s+(?:in|during)|freezing"
    r"|winter|summer"
    r"|visas?|passports?|currency|cash|atms?|credit\s+cards?|cards|tipping|tip"
    r"|food|foods|dish|dishes|cuisine|restaurants?|tap\s+water|drinking\s+water"
    r"|pack|packing|wear|clothes|clothing|worth|(?:known|famous)\s+for"
    r"|good\s+(?:place|country|destination)\s+(?:to|for)\s+(?:visit|travel|tourists?|holidays?|vacations?)"
    r"|how\s+many\s+days|how\s+long\s+(?:should|do|does|to|is\s+enough)"
    r"|speak\s+english|english\s+(?:is\s+)?(?:\w+\s+)?spoken|english\s+speaking"
    r"|toilets?|bathrooms?|showers?|wi\s+fi|wifi|internet|sim|electricity|plugs?|sockets?"
    r"|get(?:ting)?\s+around|transport\w*)\b")
# Price words (review round 3): 'cheap' / 'expensive' / 'money' say nothing about what is cheap, so they
# count only for the country itself ('Is Mongolia expensive?', 'How cheap is Laos?') or a travel subject
# ('Is food cheap in Laos?', 'Is Vietnam cheap to visit?'), and 'money' only for the traveller's own ('How
# much money do I need …?') — never 'Are drugs / guns / iPhones cheap in …', 'Is money laundering common …'.
_COST_RE = re.compile(r"\b(?:expensive|cheap|cheaper|pricey|affordable|afford\w*|budget|money|costs?|prices?)\b")
_COST_SUBJECT_RE = re.compile(
    r"\b(?:trips?|tours?|travel\w*|visit\w*|holidays?|vacations?|touris\w*|backpack\w*|food|foods|meals?|eat"
    r"|eating|drinks?|beer|hotels?|hostels?|accommodation|guesthouses?|homestays?|stay|staying|flights?|transport"
    r"|taxis?|buses|trains?|souvenirs?|shopping|restaurants?|street\s+food|tickets?|entrance\s+fees?)\b")
_MONEY_FRAME_RE = re.compile(
    r"\bmoney\b.*\b(?:i|you|we)\s+(?:need|bring|take)\b|\b(?:bring|take|exchange|change|withdraw|carry)\s+money\b"
    r"|\bhow\s+much\s+money\b")
# The cost of living there, not of a trip ('Is electricity expensive in Vietnam?')
_LIVING_COST_RE = re.compile(r"\b(?:electricity|utilit\w*|rent|rents|bills?|groceries|healthcare|insurance|taxes)\b")
_SAFETY_RE = re.compile(r"\b(?:safe|safety|dangerous|friendly|welcoming)\b")
_SAFETY_FRAME_RE = re.compile(
    r"\b(?:touris\w*|travel\w*|visit\w*|you|i|we|foreigners?|solo|backpack\w*|citizens?|americans|brits)\b")
# Review round 3: who is safe / friendly must be the country, its people, the locals or 'it' ('Is Vietnam
# friendly to foreigners?', 'Are Vietnamese people friendly?', 'Is it safe to travel to Vietnam?') — never
# 'Are girls in Vietnam friendly to foreigners?'. {c}: the scope's country names and demonyms.
_SAFETY_SUBJECT_TMPL = (
    r"\b(?:is|are|isn\s+t|aren\s+t)\s+(?:it|the\s+)?(?:{c}|locals?|people|the\s+locals|it)(?:\s+(?:{c}|people|locals))?"
    r"(?:\s+(?:in|of)\s+(?:{c}))?\s+(?:\w+\s+)?(?:safe|safer|dangerous|friendly|welcoming)\b"
    r"|\bhow\s+(?:safe|dangerous|friendly|welcoming)\s+(?:is|are)\s+(?:it|the\s+)?(?:{c}|locals?|people|it)\b"
    r"|\bsafe\s+(?:to|for)\s+(?:travel|visit|go|tourists?|travell?ers?|foreigners|backpack\w*|solo)\b"
    r"|\bsafety\b")
_EAT_RE = re.compile(r"\b(?:eat|eating)\b")
_EAT_FRAME_RE = re.compile(r"\b(?:you|i|we|touris\w*|travel\w*|visitors?)\b")
# Not travel: people, politics, economy, demographics, history trivia.
_COUNTRY_DENY_RE = re.compile(
    r"^(?:did|why\s+did)\b"
    r"|\b(?:who\s+(?:is|was|are|were|led|leads|wrote|founded|built|ruled|rules|invented|owns|won)"
    r"|presidents?|prime\s+minister|government|politic\w*|communis\w*|socialis\w*|democra\w*|elections?"
    r"|parliament|kings?|queens?|monarch\w*|dictator\w*|military|army|armies|wars?|warriors?|empire"
    r"|emperors?|conquer\w*|invad\w*|invasions?|genocide|independence"
    r"|gdp|econom\w*|invest\w*|poverty|poor|rich|richest|wealth\w*|income|salary|salaries|wages?|unemploy\w*"
    r"|exports?|imports?|industr\w*|mining|population|populat\w*|percent\w*|census|minorit\w*|ethnic\w*"
    r"|religio\w*|buddhis\w*|muslims?|christian\w*|islam\w*|atheis\w*"
    r"|actors?|actress\w*|singers?|wrestlers?|wrestling|sumo|celebrit\w*|footballers?|athletes?|models?"
    r"|person|famous\s+people|girlfriends?|wife|wives|husbands?|marry|married|marriage|dating|history|historical"
    # review round 2 — past tense and history ('What did soldiers wear in Vietnam?', 'What currency did
    # Mongolia use before the tugrik?', 'What is Cambodia known for historically?')
    r"|did|was|were|used\s+to|soldiers?|troops|veterans?|centur\w*|historic\w*|ancient|dynast\w*|medieval"
    r"|colonial\w*|ancestors?"
    # living, retiring or working there, the economy, names, sport and music — not a traveller's question
    # ('Is Mongolia expensive to live in?', 'Is Laos cheap to retire?', 'How does China make money?')
    r"|live\s+in|living\s+in|move\s+to|moving\s+to|relocat\w*|retire\w*|refugees?|asylum|citizenship"
    r"|criticiz\w*|criticis\w*|make\s+money|makes\s+money|earn\w*|deficit|debt|budget\s+deficit"
    r"|so\s+(?:cheap|expensive)|family\s+names?|surnames?|names?|band|bands|songs?|albums?|movies?|films?"
    r"|football|soccer|teams?|league|olympic\w*"
    # review round 3 — goods, property, study: what a price word asks about when it is not travel ('Is land
    # cheap in Mongolia?', 'Are cars expensive in Mongolia?', 'Is it cheap to study in Vietnam?')
    r"|cars|property|properties|land|real\s+estate|houses|housing|apartments?|condos?|guns?|firearms?|weapons?"
    r"|iphones?|laptops?|electronics|gold|jewel\w*|study|studying|students?|universit\w*|tuition|schools?"
    r"|babies|baby|adopt\w*|labou?r|workers?)\b")
# Off-topic shapes: comparisons with somewhere else ('What US state is most like Mongolia?', 'Is Mongolia
# as safe as the UK?', 'Is Lao food spicier than Thai food?'). World superlatives: `is_world_question`.
_COMPARISON_RE = re.compile(
    r"\b(?:(?:most|more|least)\s+(?:like|similar)|similar\s+to|compar\w*|versus|vs|equivalent|resembl\w*"
    r"|difference\s+between|differ\w*\s+from|same\s+as|influenced\s+by|\w+er\s+than|(?:more|less)\s+\w+\s+than"
    r"|as\s+\w+\s+as|(?:us|u\s+s|american|which|what)\s+states?|inner\s+mongolia)\b")
# Words a country-level question shares with a seed without tying it to the moment.
_COUNTRY_GENERIC = frozenset("""
country countrie nation national world people person live living life thing place many much most best good
great way know known get make have year day time like visit visiting tour trip travel tourist famou top
beautiful popular worth see explore percent province region district area capital part
""".split())
# Capitalised words that name no place: question openers (first word only), months and days, events,
# payment and app brands, the English language, kinds of place ('Lake', 'National Park').
_OPENERS = _ENGLISH_OPENERS | frozenset("""
best top things tips places good cheap most traveling travelling travel visiting visit must popular famous
local typical any planning going getting the a an in on for to if after before during whats
""".split())
_NEUTRAL_CAPS = frozenset("""
january february march april may june july august september october november december monday tuesday
wednesday thursday friday saturday sunday spring summer autumn fall winter i festival naadam tsagaan sar
tsechu losar tet songkran boun thingyan thadingyut visa mastercard amex grab uber google whatsapp airbnb
wifi unesco atm atms sim usd gps id covid foreign foreigner foreigners western westerner westerners
""".split())
_NEUTRAL_PHRASES_RE = re.compile(r"\b(?:Northern\s+Lights|Milky\s+Way|Lunar\s+New\s+Year|New\s+Year|Pi\s+Mai)\b")
_KIND_CAPS = (PLACE_KIND_NOUNS - {"japan", "japanese"}) | frozenset("""
desert deserts steppe steppes monastery monasteries dzong dzongs bay province national park parks cave caves
hill hills islands palace pagoda pagodas dune dunes glacier highlands plateau square lakes rivers town old
ancient nuur gol uul khiid aimag soum
""".split())
_NAME_EDGE_KINDS = _KIND_CAPS | frozenset("bay lake city town province district region".split())


def scope_name_key(phrase: str) -> str:
    """The joined name key of a multi-word place phrase, kinds of place cut from its ends: 'Ha Long
    Bay' → 'halong', 'Hoi An' → 'hoian', 'Luang Prabang' → 'luangprabang'; '' for a one-word name."""
    tokens = _TOKEN_RE.findall(fold(phrase))
    while tokens and tokens[0] in _NAME_EDGE_KINDS:
        tokens = tokens[1:]
    while tokens and tokens[-1] in _NAME_EDGE_KINDS:
        tokens = tokens[:-1]
    return "".join(tokens) if len(tokens) >= 2 else ""


# ---------------------------------------------------------------- own place names (review round 2)
# The exact own-place test reads only the moment's real place names: the Location fields and the label
# (canonical), and a name written mid-line only when it carries a kind of landmark ('Gandan Monastery',
# 'Turtle Rock', 'Great Wall') — never a person or a thing the seed also names ('Dalai Lama', 'Nguyễn',
# 'Mongol Empire'), never a line opener ('Trek …', 'Watch …'). Before, any capitalised seed token was an
# own place ('Is Tiger Woods still playing golf?' for Tiger's Nest, 'Is Wall Street open today?' for the
# Great Wall), and so was a lowercase Location token ('Can I bring my cat to Japan?' for Can Tho).
_LANDMARK_KINDS = _KIND_CAPS | frozenset("""
wall walls rock rocks nest gate gates tower towers stupa stupas chorten chortens minaret minarets mosque mosques
fortress citadel tomb tombs mausoleum canyon cliff cliffs springs waterfall waterfalls square lagoon
""".split())
_POSSESSIVE_RE = re.compile(r"[’']s\b")
# Qualifiers among the kind words (never a kind of place on their own: 'Ancient Town', 'Old Quarter').
_PLACE_QUALIFIER_WORDS = frozenset("ancient old new great grand upper lower national central".split())


def _name_seq(text: Optional[str]) -> list[str]:
    """Folded name words of `text`, a possessive 's cut ('Tiger's Nest' → ['tiger', 'nest'])."""
    return [fold(w) for w in _NAME_TOKEN_RE.findall(_POSSESSIVE_RE.sub("", deaccent(text)))]


def _distinct_word(w: str) -> bool:
    return (w not in _COMMON_ENGLISH and w not in PLACE_KINDS and w not in _NAME_EDGE_KINDS
            and w not in ALL_COUNTRY_WORDS)


def own_place_forms(canonical: Iterable[str], landmarks: Iterable[str] = ()) -> frozenset:
    """The folded word sequences that name one of the moment's own places in a question.
    `canonical` (Location fields, label): the whole name ('ha long bay'), its core without kinds of
    place at the ends ('ha long', 'khovsgol' of 'Khövsgöl Province') when that core has a distinctive
    word, the core written as one word ('halong'), and a distinctive core word of ≥ 5 letters alone
    ('terelj' of 'Gorkhi-Terelj', 'prabang'; never 'long', 'reap', 'hoi', 'can'). `landmarks` (other
    names of the seed): only a whole name of ≥ 2 words with a kind of landmark ('great wall', 'gandan
    monastery')."""
    forms: set[tuple[str, ...]] = set()
    for phrase in canonical:
        toks = tuple(_name_seq(phrase))
        if not toks:
            continue
        if set(toks) & _PERSON_TOKENS and _PERSON_NAMES_RE.search(" ".join(toks)):
            # named after a person ('Ho Chi Minh City', 'Genghis Khan Equestrian Statue'): only the whole
            # name, never its core or a word of it ('ho chi minh', 'genghis' are the person; review round 3)
            if len(toks) > 1:
                forms.add(toks)
                forms.add(("".join(toks),))
            continue
        if len(toks) > 1 or _distinct_word(toks[0]):
            forms.add(toks)
        core = list(toks)
        while core and core[0] in _NAME_EDGE_KINDS:
            core = core[1:]
        while core and core[-1] in _NAME_EDGE_KINDS:
            core = core[:-1]
        if core and any(_distinct_word(t) for t in core) and len("".join(core)) >= 3:
            forms.add(tuple(core))
            if len(core) > 1:
                forms.add(("".join(core),))  # written as one word: 'Halong', 'Hoian', 'Danang'
                forms |= {(t,) for t in core if len(t) >= 5 and _distinct_word(t)}
    for phrase in landmarks:
        toks = tuple(_name_seq(phrase))
        if len(toks) >= 2 and set(toks) & _LANDMARK_KINDS and not set(toks) <= _LANDMARK_KINDS:
            forms.add(toks)
    return frozenset(f for f in forms if "".join(f) not in ALL_COUNTRY_WORDS)


def place_kind_words(forms: Iterable[tuple]) -> set[str]:
    """The kinds of place the moment's own names carry ('lake' of 'Khövsgöl Lake', 'monastery', 'wall'
    of 'Great Wall'), as content_words stems: what ties a country-level question to the moment ('What
    is the most famous lake in Mongolia?' for a lake, never 'the most famous tower' for a wall)."""
    kinds = _LANDMARK_KINDS | _NAME_EDGE_KINDS | PLACE_KIND_NOUNS
    out: set[str] = set()
    for form in forms:
        for t in form:
            if t in kinds and t not in _PLACE_QUALIFIER_WORDS:
                out.add(t[:-1] if t.endswith("s") and len(t) > 4 else t)
    return out


def own_place_named(question: str, forms: Iterable[tuple]) -> set[str]:
    """The own-place forms (`own_place_forms`) `question` names, written as a name: every word of the
    match that is not a kind of place capitalised ('Is Hue worth visiting?', 'the Great Wall'), and not
    a question opener in first position ('Can …' is never Can Tho). A lowercase word counts only as a
    whole one-word name of ≥ 6 letters that is no English word we know, in a question with a travel
    word ('how deep is lake khovsgol'): never 'What is hue in color theory?', 'Who are the hoi polloi?',
    'Why don't …' for Don Det."""
    text = _POSSESSIVE_RE.sub("", deaccent(question))
    raw = _NAME_TOKEN_RE.findall(text)
    words = [fold(w) for w in raw]
    travel = None
    # a word of a longer name whose other words are not kinds of place ('floating' of 'Cai Rang Floating
    # Market', 'terelj' of 'Gorkhi-Terelj') never counts in lowercase; 'khovsgol' of 'Khövsgöl Lake' does
    partial = {t for f in forms if len(f) > 1 for t in f if any(o != t and o not in _LANDMARK_KINDS for o in f)}
    out: set[str] = set()
    for form in forms:
        n = len(form)
        for i in range(len(words) - n + 1):
            if tuple(words[i:i + n]) != form:
                continue
            if i == 0 and words[0] in _OPENERS:
                continue
            if all(raw[i + k][0].isupper() for k in range(n) if form[k] not in _LANDMARK_KINDS):
                out.add(" ".join(form))
                break
            if n == 1 and len(form[0]) >= 6 and _distinct_word(form[0]) and form[0] not in partial:
                travel = _travel_context(question) if travel is None else travel
                if travel:
                    out.add(form[0])
                    break
    return out


# Never a FAQ question, whatever place it names (all paths; S245 review round 2): war and death, politics,
# celebrities and romance, diet / health / leftovers of a cuisine ('When did Saigon fall?', 'Is Chinese
# food bad for weight loss?', 'How long does Vietnamese food last in the fridge?'). A word of the seed's
# own names does not count ('War Remnants Museum', 'Killing Fields').
_HARM_DENY_RE = re.compile(
    r"\b(?:wars?|battles?|bomb\w*|died|dies|deaths?|dead|killed|kill|killing|massacres?|genocide|soldiers?|troops"
    r"|veterans?|die|invad\w*|invasions?|occupation|fall\s+of|did\s+(?:\w+\s+){1,3}fall|executed|execution"
    r"|presidents?|prime\s+minister|government|politic\w*|communis\w*|elections?|dictator\w*|protests?|exiled?"
    r"|dalai\s+lama|actors?|actress\w*|singers?|rappers?|celebrit\w*|married|marry|marriage|divorc\w*"
    r"|girlfriends?|boyfriends?|wife|husband|dating|net\s+worth"
    r"|calories|calorie|diet|diets|dieting|weight\s+loss|lose\s+weight|healthy|unhealthy|fridge|refrigerat\w*"
    r"|leftovers?|left\s+out|reheat\w*|microwave|sleepy|headaches?|msg|pregnan\w*|diabet\w*|cholesterol"
    r"|sodium|carbs?|keto|fattening|recipes?|takeout|take\s+out|takeaway|delivery|near\s+me|buffets?"
    # a name as a name, media and entertainment ('Is Paro a good name for a girl?', 'Is Star Trek on Netflix?')
    r"|baby\s+names?|last\s+name|first\s+name|surnames?|good\s+name|names?\s+for\s+(?:an?\s+)?\w+"
    r"|bands?|albums?|songs?|lyrics|movies?|films?|netflix|episodes?|tv\s+(?:show|series)|video\s+games?"
    # review round 3 — coups and unrest, crime, drugs, sex work, disputed land, censorship, medical tourism,
    # a person's body or grave, dog meat ('Is Yangon under martial law?', 'Can you buy weed in Vang Vieng?',
    # 'Where are the massage parlors in Hanoi?', 'Who owns Kashmir?', 'What happened at Tiananmen Square?',
    # 'Is dental work cheap in Vietnam?', 'Are girls in Vietnam friendly to foreigners?')
    r"|coups?|junta|martial\s+law|curfews?|riots?|rioting|uprisings?|crackdown|revolution\w*|tanks?"
    r"|terror\w*|murder\w*|kidnap\w*|rape|raped|rapes|rapist\w*|crime\s+rates?|police|shootings?|gunm[ae]n"
    r"|traffick\w*|smuggl\w*|launder\w*|brib\w*|corrupt\w*|censor\w*|disputed?|occupied"
    r"|self\s+immolat\w*|set\s+(?:themselves|himself|herself)\s+on\s+fire"
    r"|what\s+happened\s+(?:at|in|to|on|during)|who\s+(?:owns|controls|rules|governs|runs)"
    r"|drugs?|weed|cannabis|marijuana|opium|cocaine|heroin|meth|methamphetamine|ecstasy|mdma|ketamine|kratom"
    r"|narcotics?|magic\s+mushrooms?|happy\s+(?:pizzas?|shakes?)"
    r"|prostitut\w*|brothels?|red\s+light|massage\s+parlou?rs?|happy\s+endings?|sex|sexy|sexual\w*|hookers?"
    r"|escort\s+(?:services?|girls?|agenc\w*)|strip\s+clubs?|ladyboys?|bar\s+girls?|go\s+go\s+bars?|brides?"
    r"|(?:girls?|women|woman|ladies|lady|men|guys)\s+(?:in|from)|single\s+(?:men|males?|guys|women|girls)"
    r"|dental|dentists?|dentistry|teeth|implants?|veneers?|surger(?:y|ies)|surgeons?|plastic\s+surgery"
    r"|cosmetic\w*|botox|ivf|surrogac\w*|medical\s+tourism|have\s+a\s+baby|give\s+birth|giving\s+birth"
    r"|childbirth|abortions?|hair\s+transplants?"
    r"|buried|burial\s+place|embalm\w*|(?:dogs?|cats?)\s+meat|eat\w*\s+(?:\w+\s+){0,2}?(?:dogs?|cats?|snakes?|bats?"
    r"|rats?|monkeys?|pangolins?)\b)\b")


def harm_question(question: str, exempt: Iterable[str] = ()) -> bool:
    """A question `_HARM_DENY_RE` names (folded), unless every hit is a word of `exempt` (the seed's own
    name words: 'Is the War Remnants Museum worth visiting?' for that museum)."""
    q = " ".join(_TOKEN_RE.findall(fold(_normalise_question(question))))
    ok = {e for e in exempt if e}
    return any(not set(m.group(0).split()) <= ok for m in _HARM_DENY_RE.finditer(q))


def _travel_context(question: str) -> bool:
    q = " ".join(_TOKEN_RE.findall(fold(question)))
    return bool(set(q.split()) & _TRAVEL_CONTEXT) or bool(_PRACTICAL_RE.search(q) or _COST_RE.search(q))


# Facts about a place itself a one-word own name may be asked with ('How high is Lhasa?', 'How old is Hue?').
_PLACE_FACT_WORDS = frozenset("high altitude elevation height old big large size built founded".split())


def travel_context(question: str) -> bool:
    """Does `question` carry a travel word (`_TRAVEL_CONTEXT`), a practical travel topic or a fact about
    a place itself (high, old, built …)? What a question naming an own place by a one-word name needs
    (review round 3: 'Is Java hard to learn?' has none)."""
    return _travel_context(question) or bool(set(_TOKEN_RE.findall(fold(question))) & _PLACE_FACT_WORDS)


# ---------------------------------------------------------------- people and homonyms (review round 3)
# A place named after a person ('Ho Chi Minh City', 'Chinggis Khaan Statue Complex'): the person's name
# alone is the person ('Where is Ho Chi Minh buried?', 'How many children did Genghis Khan have?'). It
# names the place only with a kind of place right after it ('Ho Chi Minh City', 'the Genghis Khan
# statue'), and 'Ho Chi Minh' also after a place preposition ('things to do in Ho Chi Minh').
_PERSON_NAMES_RE = re.compile(
    r"\b(?:ho\s+chi\s+minh|(?:genghis|chinggis|chingis|jenghis|genghiz|chingghis)\s+kha+n|kublai\s+kha+n"
    r"|aung\s+san(?:\s+suu\s+kyi)?|suu\s+kyi|su(?:k)?hbaatar|sukhe\s+bator|mao(?:\s+zedong|\s+tse\s+tung)?"
    r"|pol\s+pot)\b")
_PERSON_PLACE_KINDS = frozenset("""
city mausoleum museum statue statues square complex trail airport memorial house residence park monument
temple palace stilt market street road bridge international equestrian tomb shrine avenue boulevard theme
province aimag district stadium
""".split())
_PERSON_PLACE_FRAME = frozenset("in to from near around visit visiting reach reaching leave leaving between".split())
_PERSON_TOKENS = frozenset("""
ho chi minh genghis chinggis chingis jenghis genghiz chingghis khan khaan kublai aung san suu kyi sukhbaatar
suhbaatar sukhe bator mao zedong tse tung pol pot
""".split())


def person_question(question: str) -> bool:
    """`question` names a person a place is named after, not the place: 'Was Ho Chi Minh a good
    leader?', "Is Ho Chi Minh's body real?", 'How tall was Genghis Khan?' — but not 'Is Ho Chi Minh
    City worth visiting?', 'How tall is the Genghis Khan statue?' or 'things to do in Ho Chi Minh'."""
    q = " ".join(_TOKEN_RE.findall(fold(_normalise_question(question))))
    for m in _PERSON_NAMES_RE.finditer(q):
        after = q[m.end():].split()
        if after and after[0] == "s":
            after = after[1:]
        if after and after[0] in _PERSON_PLACE_KINDS:
            continue
        before = q[:m.start()].split()
        if m.group(0) == "ho chi minh" and before and before[-1] in _PERSON_PLACE_FRAME:
            continue
        return True
    return False


# A place name that is also a product, brand, breed or programming language ('Great Wall cars', 'Lhasa
# apso', 'Jodhpur boots', 'Saigon cinnamon', 'Kathmandu clothing', 'learn Java'): one of these right after
# the name (or the verb right before it) makes the question about the thing, not the place.
_NAME_HOMONYM_NEXT = frozenset("""
car cars ute utes suv suvs truck trucks pickup pickups motor motors haval brand brands clothing clothes jacket
jackets boot boots breeches jodhpurs apso apsos terrier terriers spaniel spaniels cat cats dog dogs puppy
puppies breed breeds camera cameras film films lens lenses printer printers bank banks cinnamon cassia spice
spices rug rugs programming code script jdk syndrome virus flu fever shares stock stocks
""".split())
_NAME_HOMONYM_BEFORE = frozenset("learn learning install installing download downloading code coding".split())
# A capitalised kind of place after an own name that the moment's own names do not carry is another place
# ('Mandalay Bay' — a Las Vegas resort — for Mandalay Hill).
_OTHER_PLACE_NEXT = frozenset("""
bay hotel hotels casino casinos resort resorts mall county state university college stadium arena airlines
airways
""".split())
_HOMONYM_THEME_RE = re.compile(
    r"\b(?:hypoallergenic|shedding|grooming|(?:hard|easy|difficult)\s+to\s+learn|programming|coding|developers?"
    r"|javascript|warranty|horsepower|mpg)\b")


def name_homonym(question: str, names: Iterable[str], own_words: Iterable[str] = ()) -> bool:
    """`question` uses one of the folded own name words `names` as a product, brand, breed or other
    place: a homonym noun right after it ('Are Great Wall cars reliable?', 'Is a Lhasa apso
    hypoallergenic?', 'Is Kathmandu a good clothing brand?' — two words on), a verb before it ('How long
    does it take to learn Java?'), a capitalised kind of place the own names do not carry ('Does Mandalay
    Bay have a lazy river?' for Mandalay Hill; `own_words`: words of the own names), or a homonym theme
    anywhere (hypoallergenic, programming, 'hard to learn')."""
    names = {n for n in names if n}
    if not names:
        return False
    own = {w for w in own_words if w}
    text = _normalise_question(question)
    raw = _NAME_TOKEN_RE.findall(text)
    words = [fold(w) for w in raw]
    hit = False
    for i, w in enumerate(words):
        if w not in names:
            continue
        hit = True
        j = i + 1
        if j < len(words) and words[j] == "s":
            j += 1
        nxt = words[j:j + 4]
        if nxt and (nxt[0] in _NAME_HOMONYM_NEXT
                    or (raw[j][:1].isupper() and nxt[0] in _OTHER_PLACE_NEXT and nxt[0] not in own)):
            return True
        # 'Is Kathmandu a good clothing brand?' — a homonym noun a few words on, after an article
        if len(nxt) >= 2 and nxt[0] in {"a", "an", "the"} and set(nxt[1:]) & _NAME_HOMONYM_NEXT:
            return True
        if i and words[i - 1] in _NAME_HOMONYM_BEFORE:
            return True
    return hit and bool(_HOMONYM_THEME_RE.search(" ".join(_TOKEN_RE.findall(fold(text)))))


def _normalise_question(question: Optional[str]) -> str:
    text = deaccent(" ".join((question or "").split()))
    text = re.sub(r"\bViet\s+Nam\b", "Vietnam", text)
    text = re.sub(r"\bLao\s+PDR\b", "Laos", text)
    return _NEUTRAL_PHRASES_RE.sub(" ", text)


def _traveller_frame(words: list[str], i: int) -> bool:
    """The nationality at `i` describes the traveller: 'American tourists', 'for Americans'."""
    nxt = words[i + 1] if i + 1 < len(words) else ""
    prev = words[i - 1] if i else ""
    return nxt in _TRAVELLER_NOUNS or (prev in {"for", "to"} and words[i].endswith("s"))


_CODE_NAME_RE = re.compile(r"\b[A-Z][A-Za-z]*\d")


def names_other_place(question: str, allowed: Iterable[str] = (), countries: Iterable[str] = ()) -> bool:
    """Does `question` name a place (or nationality) outside `allowed` (folded words: the moment's own
    names) and the scope's countries? Every capitalised word counts, the first one too unless it is a
    question opener ('Russia or Mongolia: …', 'Inner Mongolia: …'), and so do all-caps words ('the UK',
    'the USA') and other countries written lowercase ('as safe as japan'). Not counted: months, events
    ('Naadam'), brands ('Visa cards'), kinds of place ('Lake'), English after speak / spoken, and a
    nationality in a traveller frame ('… for American tourists'). A capitalised name with digits ('K2')
    always counts."""
    names, demonyms = _scope_countries(countries)
    ok = {fold(a) for a in allowed if a} | names | demonyms | _NEUTRAL_CAPS | _KIND_CAPS
    if _CODE_NAME_RE.search(_normalise_question(question)):
        return True  # 'K2', 'A380' (letters-only tokens would read 'K' and skip it)
    raw = [m.group(0) for m in _NAME_TOKEN_RE.finditer(_normalise_question(question))]
    words = [fold(w) for w in raw]
    for i, (surface, w) in enumerate(zip(raw, words)):
        if len(w) < 2 or w in ok or (i == 0 and w in _OPENERS):
            continue
        if w == "english" and set(words) & {"speak", "speaks", "speaking", "spoken", "speakers", "language"}:
            continue
        if w in _NATIONALITY_WORDS or w in {"us", "uk", "usa"}:
            if w == "us" and surface == "us":
                continue  # the pronoun
            if _traveller_frame(words, i) or (w == "us" and i + 1 < len(words) and words[i + 1].startswith("dollar")):
                continue
            return True
        if surface[0].isupper() or w in ALL_COUNTRY_WORDS:
            return True
    return False


def is_comparison(question: str) -> bool:
    """'Is Lake Khuvsgul bigger than Lake Baikal?', 'Is Mongolia as safe as the UK?' — a comparison
    with somewhere else."""
    return bool(_COMPARISON_RE.search(" ".join(_TOKEN_RE.findall(fold(_normalise_question(question))))))


def is_world_question(question: str) -> bool:
    """'Which restaurant is the most famous in the world?' — about the world, not a scope."""
    q = " ".join(_TOKEN_RE.findall(fold(question)))
    return bool(re.search(r"\b(?:in\s+the\s+world|world\s+s|worlds)\b", q))


def variant_place_question(question: str, names: Iterable[str], countries: Iterable[str] = (),
                           allowed: Iterable[str] = (), exempt: Optional[Iterable[str]] = None) -> bool:
    """A question that names one of the moment's places (`names`: folded words and joined names) only
    through a spelling variant is in scope when it is no world question, no comparison ('Is Lake
    Khuvsgul bigger than Lake Baikal?'), no war / politics / diet question (`harm_question`: 'When did
    Saigon fall?'; `exempt`: own-place words that lift it, default `names`) and names no other place
    ('Where is Terrell, Texas?'; `allowed`: the seed's other name words). A listed exonym alone
    (review round 2: Karakorum is also the Karakoram range, Rangoon a crab dish) also needs a travel
    word (`_TRAVEL_CONTEXT`, `_PRACTICAL_RE`: 'Is Karakorum worth visiting?') and none of its homonym
    words (`_ALIAS_HOMONYMS`: 'How high is the Karakorum range?')."""
    names = {n for n in names if n}
    allowed = {a for a in allowed if a}
    kinds = variant_kinds(question, names)
    if not kinds or is_world_question(question) or is_comparison(question):
        return False
    if harm_question(question, names if exempt is None else exempt):
        return False
    if all(k == "alias" for k in kinds.values()):
        if not _travel_context(question):
            return False
        q_words = set(_TOKEN_RE.findall(fold(question)))
        if any(q_words & _ALIAS_HOMONYMS.get(joined_name(v), frozenset()) for v in kinds):
            return False
    ok = names | allowed | {w for v in kinds for w in _TOKEN_RE.findall(v)}
    return not names_other_place(question, ok, countries)


def _country_mention(words: list[str], raw: list[str], names: set[str], demonyms: set[str]) -> Optional[str]:
    """The question names a scope country as a destination: the country written capitalised
    ('Mongolia', "Mongolia's", 'Viet Nam'), a capitalised plural demonym ('Mongolians'), or a
    capitalised demonym with a travel noun right after it ('Mongolian gers', 'Lao national dish') and
    no homonym noun ('Mongolian beef', 'Burmese python'). Returns 'name' (the country is named),
    'demonym' (only a demonym) or None."""
    found: Optional[str] = None
    for i, (surface, w) in enumerate(zip(raw, words)):
        if not surface[:1].isupper():
            continue
        if w in names:
            return "name"
        if w not in demonyms:
            continue
        if w.endswith("s") and w[:-1] in demonyms:
            found = "demonym"
            continue
        window = []
        for nxt_surface, nxt in zip(raw[i + 1:i + 4], words[i + 1:i + 4]):
            if nxt_surface[:1].isupper():
                break
            window.append(nxt)
        if set(window) & _DEMONYM_HOMONYMS:
            continue
        if set(window) & _DEMONYM_TRAVEL_NOUNS:
            found = "demonym"
    return found


def _country_alt(terms: set[str]) -> str:
    return "|".join(sorted((re.escape(t) for t in terms if t), key=len, reverse=True)) or "(?!)"


def _cost_question(q: str, terms: set[str]) -> bool:
    """A price word about the trip (folded question `q`, `terms`: the scope's country names and
    demonyms): the country itself ('Is Mongolia expensive?', 'How cheap is Laos?'), a travel subject
    ('Is food cheap in Laos?', 'Is Vietnam cheap to visit?') or the traveller's own money ('How much
    money do I need for Mongolia?')."""
    if not _COST_RE.search(q):
        return False
    if _COST_SUBJECT_RE.search(q) or _MONEY_FRAME_RE.search(q):
        return True
    c = _country_alt(terms)
    price = r"(?:expensive|cheap|pricey|affordable)"
    return bool(re.search(rf"\b(?:is|isn\s+t)\s+(?:{c})\s+(?:\w+\s+)?{price}\b", q)
                or re.search(rf"\bhow\s+{price}\s+is\s+(?:{c})\b", q))


def _safety_question(q: str, terms: set[str]) -> bool:
    """'Safe' / 'friendly' about the country, its people, the locals or the trip, for a traveller:
    'Is Mongolia safe for tourists?', 'Are Vietnamese people friendly to foreigners?' — never 'Are
    girls in Vietnam friendly to foreigners?'."""
    if not (_SAFETY_RE.search(q) and _SAFETY_FRAME_RE.search(q)):
        return False
    return bool(re.search(_SAFETY_SUBJECT_TMPL.format(c=_country_alt(terms)), q))


# A cuisine named only by its demonym ('Chinese food', no 'China') is the takeaway down the road when the
# question asks its price or menu.
_DEMONYM_ONLY_DENY_RE = re.compile(r"\b(?:cost|costs|price|prices|pricey|how\s+much|cheap|expensive|order|menu)\b")


def country_travel_question(question: str, countries: Iterable[str], seed_words: Iterable[str] = (),
                            allowed_names: Iterable[str] = ()) -> bool:
    """AA-779 (S245, 20/10; review rounds 1–2) — a question about the scope's country as a travel
    destination, for a moment that it names no place of. All of:
    - it names the country as a destination (`_country_mention`: capitalised 'Mongolia' / "Mongolia's",
      or 'Mongolian gers', never 'Mongolian beef' or lowercase 'china');
    - no people / politics / economy / demographics / history / past-tense question, nor living,
      retiring, names, sport or music (`_COUNTRY_DENY_RE`: 'Who is the president of Mongolia?', 'What
      did soldiers wear in Vietnam?', 'Is Mongolia expensive to live in?'), nor war, diet or health
      (`harm_question`: 'Is Chinese food bad for weight loss?'); a cuisine named only by its demonym
      never with a price or menu ('How much does Chinese food cost?');
    - no comparison or world superlative (`_COMPARISON_RE`: 'as safe as', 'spicier than', 'most like');
    - no other place or nationality (`names_other_place`; allowed: the moment's own names
      `allowed_names`, and a traveller nationality: 'for American tourists');
    - a general practical travel topic (`_PRACTICAL_RE`: best time, weather, visa, currency, food and
      dishes, what to pack, worth visiting, known for; review round 3: a price word only for the country
      itself, a travel subject or the traveller's money, `_cost_question`, never the cost of living;
      'safe' / 'friendly' only for tourists or travel and only about the country, its people, the locals
      or 'it', `_safety_question`), or a traveller frame ('you', visit, travel, famous, best …) AND a word of
      `seed_words` (paa_for_scope passes the kinds of the moment's own places, `place_kind_words`):
      'What is the most famous lake in Mongolia?' stays for a lake moment, not for a dunes moment, and
      'the most famous horse in Mongolia' for none."""
    names, demonyms = _scope_countries(countries)
    if not names:
        return False
    norm = _normalise_question(question)
    raw = _NAME_TOKEN_RE.findall(norm)
    words = [fold(w) for w in raw]
    mention = _country_mention(words, raw, names, demonyms)
    if not mention:
        return False
    q = " ".join(_TOKEN_RE.findall(fold(norm)))
    if (_COUNTRY_DENY_RE.search(q) or _COMPARISON_RE.search(q) or is_world_question(norm)
            or harm_question(question)):
        return False
    if _COST_RE.search(q) and _LIVING_COST_RE.search(q):
        return False  # 'Is electricity expensive in Vietnam?' — the cost of living there
    if mention == "demonym" and _DEMONYM_ONLY_DENY_RE.search(q):
        return False  # 'How much does Chinese food cost?' is a takeaway price, not the country
    if names_other_place(question, allowed_names, countries):
        return False
    if _PRACTICAL_RE.search(q) or _cost_question(q, names | demonyms) or _safety_question(q, names | demonyms):
        return True
    if _EAT_RE.search(q) and _EAT_FRAME_RE.search(q):
        return True  # 'What should you eat in Vietnam?' (dog, cat, snake …: harm_question)
    if not _TRAVEL_FRAME_RE.search(q):
        return False
    shared = (set(content_words(norm)) & set(seed_words)) - names - demonyms - _COUNTRY_GENERIC
    return bool(shared - _NATIONALITY_WORDS)


# ---------------------------------------------------------------- the chain

@dataclass(frozen=True)
class RealQuestion:
    question: str
    source: str

    def as_dict(self) -> dict:
        return {"q": self.question, "source": self.source}


# Tiers whose questions can come from a DE / FR / NL market (`search_demand` of any market, and the
# atom_matches questions landed from it): only those are language-checked. The tour tier is the
# US / English SERP of the tour title, so 'Best time to visit Bhutan?' stays there.
ENGLISH_ONLY_SOURCES = frozenset({SOURCE_SEGMENT, SOURCE_PLACE, SOURCE_SERP_TOPUP, SOURCE_AUTOCOMPLETE})


def merge(tiers: Sequence[tuple[str, Iterable]], per_source: int = PER_SOURCE_MAX,
          limit: int = CHAIN_MAX, names: Iterable[str] = (),
          english_only: frozenset = ENGLISH_ONLY_SOURCES, lowercase_names: Iterable[str] = (),
          countries: Iterable[str] = ()) -> list[RealQuestion]:
    """The chain in tier order: cleaned (whitespace, 10–160 chars), English only for the
    `english_only` tiers, off-meaning questions out (word-sense cues, and a name of `names` — the
    piece's folded place words — used as a common noun: 'What is a moron?' for Mörön; the
    lowercase test with `lowercase_names` / `countries`, never on the all-lowercase autocomplete
    tier), a question already given by an earlier tier skipped, at most `per_source` from one tier
    and `limit` in all."""
    names = tuple(names)
    lowercase_names = tuple(lowercase_names)
    countries = tuple(countries)
    out: list[RealQuestion] = []
    seen: set[str] = set()
    for source, questions in tiers:
        taken = 0
        for raw in questions or []:
            if len(out) >= limit or taken >= per_source:
                break
            if not isinstance(raw, str):
                continue
            q = " ".join(raw.split())
            lower = () if source == SOURCE_AUTOCOMPLETE else lowercase_names
            if not 10 <= len(q) <= 160 or is_off_meaning(q, names, lower, countries):
                continue
            if source in english_only and not looks_english(q):
                continue
            key = question_key(q)
            if key in seen:
                continue
            seen.add(key)
            out.append(RealQuestion(q, source))
            taken += 1
    return out


@dataclass(frozen=True)
class QuestionScope:
    """What a piece is about, resolved from its atoms / Route Segments."""
    segment_ids: tuple[str, ...]
    places: tuple[str, ...]
    countries: tuple[str, ...] = ()


class QuestionSource(Protocol):
    name: str

    async def fetch(self, conn, scope: QuestionScope) -> list[str]: ...


_SEGMENT_MATCH_SQL = f"""
    WITH seg AS (
        SELECT segment_id, questions_count, questions_computed_at
        FROM acp_contract.atom_segment WHERE segment_id = ANY($1::text[])
    ), hits AS (
        SELECT seg.questions_count, seg.questions_computed_at, am.query, am.distance, am.matched_by,
               am.matched_at,
               row_number() OVER (PARTITION BY seg.segment_id ORDER BY am.matched_at DESC, am.id DESC) AS rn
        FROM seg
        JOIN acp_contract.atom_segment_member m ON m.segment_id = seg.segment_id
        JOIN acp_contract.atom_matches am ON am.atom_id = m.atom_id AND am.kind = 'question'
    )
    SELECT query FROM hits
    WHERE CASE WHEN questions_count IS NOT NULL THEN rn <= questions_count
               ELSE questions_computed_at IS NULL
                    OR matched_at >= questions_computed_at - interval '{STALE_MATCH_SLACK}' END
    ORDER BY (matched_by = 'vector') DESC, distance ASC, query
"""


class SegmentMatchSource:
    """`atom_matches` rows of the Segments' atoms. The table is upsert-only (a question a later
    landing pass dropped keeps its old row), so per Segment only the `questions_count` newest rows
    count — exactly the questions its last landing pass claimed."""
    name = SOURCE_SEGMENT

    async def fetch(self, conn, scope: QuestionScope) -> list[str]:
        if not scope.segment_ids:
            return []
        rows = await conn.fetch(_SEGMENT_MATCH_SQL, list(scope.segment_ids))
        return [r["query"] for r in rows]


# AA-779 (B) — the `search_demand.market` the admin SERP top-up stores its rows under: US / English
# SERP with the PAA box expanded 2 levels. A market of its own keeps the top-up apart from Segment
# research's (keyword, market) cache: research reads only its real markets (US, UK, …) and never
# sees a no-volume top-up row as 'measured, no volume'; a top-up never overwrites a research row's
# PAA. S1 prefetch and atom ranking read rows with a volume only; the admin SEO views list it.
TOPUP_MARKET = "US-PAA2"

_PLACE_PAA_SQL = """
    SELECT keyword, market, search_volume, people_also_ask
    FROM acp_contract.search_demand
    WHERE keyword ILIKE ANY($1::text[]) AND market <> $2
      AND jsonb_typeof(people_also_ask) = 'array' AND jsonb_array_length(people_also_ask) > 0
    LIMIT 300
"""
_MARKET_ORDER = {"US": 1, "UK": 2, "AU": 3, "DE": 4, "FR": 5, "NL": 6}
_PLACE_GENERIC = frozenset(PLACE_KINDS) | COMMON_CAPS


def place_words(places: Iterable[str], countries: Iterable[str] = ()) -> set[str]:
    """The distinctive, folded words of the piece's place names a bought keyword must share:
    ≥ 4 letters, not a kind of place ('lake', 'province'), not a country."""
    skip = _PLACE_GENERIC | {w for c in countries for w in _TOKEN_RE.findall(fold(c))}
    return {w for p in places for w in _TOKEN_RE.findall(fold(p)) if len(w) >= 4 and w not in skip}


def rank_place_rows(rows, words: set[str]) -> list[str]:
    """Questions of the keywords that share a place word (whole words, accent-folded), the keyword
    sharing more place words first, then higher volume, then market (US, UK, AU, …)."""
    scored = []
    for r in rows:
        shared = keyword_words(fold(r["keyword"])) & words
        if not shared:
            continue
        paa = r["people_also_ask"]
        if isinstance(paa, str):
            paa = json.loads(paa) if paa else []
        scored.append(((-len(shared), -(r["search_volume"] or 0), _MARKET_ORDER.get(r["market"], 99),
                        r["keyword"]), [q for q in (paa or []) if isinstance(q, str)]))
    scored.sort(key=lambda s: s[0])
    return [q for _, qs in scored for q in qs]


def chain_filter_names(scope: "QuestionScope") -> dict:
    """The off-meaning filter's inputs for one scope (merge kwargs): every distinctive place word,
    the single-word place names the lowercase test may read, and the countries."""
    return {"names": sorted(place_words(scope.places, scope.countries)),
            "lowercase_names": sorted(single_word_places(scope.places, scope.countries)),
            "countries": list(scope.countries)}


def topup_keyword(place: str, country: str) -> str:
    """AA-779 (B) — the `search_demand` keyword the admin SERP top-up stores a place's questions
    under: the folded words of '<place> <country>' ('moron mongolia' for Mörön, Mongolia). The
    country makes Google read the town, not the word. Stored under market `TOPUP_MARKET`."""
    words = _TOKEN_RE.findall(fold(f"{place} {country}"))
    return " ".join(dict.fromkeys(words))


def topup_keywords(scope: QuestionScope) -> set[str]:
    return {topup_keyword(p, c) for p in scope.places for c in scope.countries if p and c}


class PlacePaaSource:
    """`search_demand` PAA bought (segment research, admin) for a keyword naming one of the
    piece's places — e.g. 'lake khovsgol' for a Lake Khövsgöl moment. Rows the SERP top-up wrote
    (`TOPUP_MARKET`) are left to `SerpTopupSource` (their own label)."""
    name = SOURCE_PLACE

    async def fetch(self, conn, scope: QuestionScope) -> list[str]:
        words = place_words(scope.places, scope.countries)
        if not words:
            return []
        rows = await conn.fetch(_PLACE_PAA_SQL, sorted(f"%{w}%" for w in words), TOPUP_MARKET)
        return rank_place_rows([r for r in rows if r["market"] != TOPUP_MARKET], words)


_TOPUP_SQL = """
    SELECT keyword, market, search_volume, people_also_ask
    FROM acp_contract.search_demand
    WHERE keyword = ANY($1::text[]) AND market = $2
      AND jsonb_typeof(people_also_ask) = 'array' AND jsonb_array_length(people_also_ask) > 0
"""


class SerpTopupSource:
    """AA-779 (B) — PAA the admin-only SERP top-up (services/acp_shared/question_topup.py, click
    depth 2) bought for '<place> <country>' of the piece's places. Read only: this module never
    buys (AA-646)."""
    name = SOURCE_SERP_TOPUP

    async def fetch(self, conn, scope: QuestionScope) -> list[str]:
        keys = topup_keywords(scope)
        if not keys:
            return []
        rows = await conn.fetch(_TOPUP_SQL, sorted(keys), TOPUP_MARKET)
        words = place_words(scope.places, scope.countries)
        rows = [r for r in rows if r["keyword"] in keys and r["market"] == TOPUP_MARKET]
        return rank_place_rows(rows, words) if words else []


# AA-779 (C) autocomplete appends its reader here, after serp_topup.
DB_SOURCES: tuple[QuestionSource, ...] = (SegmentMatchSource(), PlacePaaSource(), SerpTopupSource())

_SCOPE_SEGMENTS_SQL = """
    SELECT DISTINCT s.segment_id, s.canonical_place
    FROM acp_contract.atom_segment s
    WHERE s.segment_id IN (
        SELECT asm.segment_id FROM acp_contract.atom_segment_member asm WHERE asm.atom_id = ANY($1::text[])
        UNION
        SELECT COALESCE(al.segment_id_canonical, x.id)
        FROM unnest($2::text[]) AS x(id)
        LEFT JOIN acp_contract.atom_segment_alias al ON al.segment_id_old = x.id
    )
"""
_SCOPE_ATOMS_SQL = """
    SELECT DISTINCT ta.place, rt.country
    FROM acp_contract.tour_atoms ta
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    WHERE ta.atom_id = ANY($1::text[])
"""


async def resolve_scope(conn, atom_ids: Sequence[str], segment_ids: Sequence[str] = ()) -> QuestionScope:
    """The Segments of the atoms (plus given Route Segments, through a merged id's alias), their
    places, the atoms' own places and their tours' countries."""
    atom_ids = [str(a) for a in atom_ids if a]
    segment_ids = [str(s) for s in segment_ids if s]
    seg_rows = await conn.fetch(_SCOPE_SEGMENTS_SQL, atom_ids, segment_ids) if (atom_ids or segment_ids) else []
    atom_rows = await conn.fetch(_SCOPE_ATOMS_SQL, atom_ids) if atom_ids else []
    places = [r["canonical_place"] for r in seg_rows] + [r["place"] for r in atom_rows if r["place"]]
    return QuestionScope(
        segment_ids=tuple(sorted({r["segment_id"] for r in seg_rows})),
        places=tuple(dict.fromkeys(p for p in places if p)),
        countries=tuple(sorted({r["country"] for r in atom_rows if r["country"]})),
    )


async def collect_real_questions(pool, *, atom_ids: Sequence[str], segment_ids: Sequence[str] = (),
                                 tour_paa: Iterable = (),
                                 sources: Sequence[QuestionSource] = DB_SOURCES) -> list[RealQuestion]:
    """The whole chain for one piece. Soft-fails per source (a failed read is logged and skipped;
    the tour tier always remains), so writing never stops on it. $0: DB reads only."""
    tiers: list[tuple[str, list]] = []
    scope: Optional[QuestionScope] = None
    try:
        async with pool.acquire() as conn:
            scope = await resolve_scope(conn, atom_ids, segment_ids)
            for source in sources:
                try:
                    tiers.append((source.name, await source.fetch(conn, scope)))
                except Exception as e:  # noqa: BLE001 — a missing source never blocks the write
                    logger.warning("real_questions_source_failed", source=source.name, error=str(e)[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("real_questions_scope_failed", error=str(e)[:200])
    tiers.append((SOURCE_TOUR, list(tour_paa or [])))
    # The place words also drop a question using a place name as a common noun: the place_paa tier
    # buys exactly those ('moron' for Mörön), and T8 shows the merged list to the tenant.
    return merge(tiers, **(chain_filter_names(scope) if scope else {}))


# ---------------------------------------------------------------- admin record

def label_questions(kept: Sequence[str], candidates: Sequence[dict]) -> list[dict]:
    """`kept` (paa_for_scope output, normalised text) with the source of the candidate each came
    from. A job payload from before the chain has no candidates: those questions came from the tour
    snapshot."""
    by_key = {question_key(c.get("q", "")): c.get("source") for c in candidates or [] if isinstance(c, dict)}
    return [{"question": q, "source": by_key.get(question_key(q), SOURCE_TOUR)} for q in kept or []]


def source_counts(candidates: Sequence[dict], kept: Sequence[dict]) -> dict:
    """{"candidates": {source: n}, "kept": {source: n}} for the admin piece view."""
    def _count(items, field):
        out: dict[str, int] = {}
        for it in items or []:
            src = it.get(field) if isinstance(it, dict) else None
            if src:
                out[src] = out.get(src, 0) + 1
        return out
    return {"candidates": _count(candidates, "source"), "kept": _count(kept, "source")}


FAQ_MATCH_OVERLAP = 0.8
# Words a light rewording adds or drops without changing what is asked ('Does Mongolia have any
# volcanoes?' → 'Does Mongolia have volcanoes?', r7): left out of the overlap.
_MATCH_FILLER = frozenset("any really actually ever exactly also still just there".split())


def _match_words(text: str) -> set[str]:
    return set(content_words(text or "")) - _MATCH_FILLER


def _match_source(question: str, pool_: Sequence[dict]) -> Optional[str]:
    key = question_key(question)
    words = _match_words(question)
    for item in pool_:
        cand = item.get("question") or item.get("q") or ""
        if question_key(cand) == key:
            return item.get("source")
    for item in pool_:
        cand_words = _match_words(item.get("question") or item.get("q") or "")
        if words and cand_words and len(words & cand_words) / max(len(words), len(cand_words)) >= FAQ_MATCH_OVERLAP:
            return item.get("source")
    return None


def faq_sources(faq_questions: Sequence[str], kept: Sequence[dict],
                candidates: Sequence[dict] = ()) -> list[dict]:
    """Each final FAQ question with the real source it matches (same text after folding, or
    ≥ 80% shared content words, filler words like 'any' left out) — the questions given to the
    writer first, then every candidate; `writer` when it matches none."""
    pool_ = list(kept or []) + list(candidates or [])
    return [{"question": q, "source": _match_source(q, pool_) or SOURCE_WRITER} for q in faq_questions or []]


# ---------------------------------------------------------------- real questions first in the FAQ (S245 r7)

def missing_real_questions(faq_questions: Sequence[str], given: Sequence[dict]) -> list[str]:
    """The real questions the writer was given (`given`: the in-scope, labelled
    `search_questions`, in chain order) that no FAQ question asks yet — what the FAQ
    regeneration must take first. A writer-made label never counts as real."""
    out: list[str] = []
    for item in given or []:
        if not isinstance(item, dict) or item.get("source") not in REAL_SOURCES:
            continue
        q = item.get("question") or item.get("q") or ""
        if q and not any(_match_source(f, [item]) for f in faq_questions or []):
            out.append(q)
    return out


def real_first_pairs(pairs: Sequence[tuple[str, str]], pool_: Sequence[dict], *,
                     max_pairs: int) -> tuple[list[tuple[str, str]], int]:
    """The FAQ with the pairs that ask a real question (matched as in `faq_sources` against
    `pool_`) first, in their order, then the writer's own in theirs. Only a reorder: a writer
    pair goes only when the FAQ is over `max_pairs` (S245 r7 review — trimming writer pairs to
    lift the real share would cut good answers and overstate the gain). Returns (pairs, real pair
    count)."""
    pairs = list(pairs or [])
    flags = [bool(_match_source(q, pool_)) for q, _ in pairs] if pool_ else [False] * len(pairs)
    real = [p for p, r in zip(pairs, flags) if r]
    if not real:
        return pairs, 0
    writer = [p for p, r in zip(pairs, flags) if not r]
    out = (real + writer)[:max_pairs]
    return out, min(len(real), max_pairs)


# ---------------------------------------------------------------- measuring (scripts/aa779_question_coverage.py)

def count_real(labelled: Sequence[dict], names: Iterable[str] = (), lowercase_names: Iterable[str] = (),
               countries: Iterable[str] = ()) -> int:
    """How many of `labelled` ({question, source}) are real: a real source and not off-meaning for
    the piece's place `names` (a junk question that passed an older filter never counts)."""
    names, lower, countries = tuple(names), tuple(lowercase_names), tuple(countries)
    return sum(1 for item in labelled or [] if isinstance(item, dict) and item.get("source") in REAL_SOURCES
               and not is_off_meaning(item.get("question") or item.get("q") or "", names, lower, countries))


def piece_question_stats(writer_input: dict, names: Iterable[str] = (), lowercase_names: Iterable[str] = (),
                         countries: Iterable[str] = ()) -> dict:
    """Done-when line 2 for one stored blog (`content_piece.writer_input`): the real questions it was
    given and the FAQ questions that ask one. A FAQ question counts as real only when it is not
    off-meaning itself and its recorded source is real; when it matches a given question, that one
    must not be off-meaning either."""
    names, lower, countries = tuple(names), tuple(lowercase_names), tuple(countries)

    def off(q: str) -> bool:
        return is_off_meaning(q, names, lower, countries)
    given = [g for g in (writer_input or {}).get("search_questions") or [] if isinstance(g, dict)]
    clean_given = [g for g in given if g.get("source") in REAL_SOURCES
                   and not off(g.get("question") or "")]
    junk_given = [g for g in given if g not in clean_given]
    faq = [f for f in (writer_input or {}).get("faq_questions") or [] if isinstance(f, dict)]
    faq_real = 0
    for f in faq:
        q = f.get("question") or ""
        if f.get("source") not in REAL_SOURCES or off(q):
            continue
        if junk_given and _match_source(q, junk_given) and not _match_source(q, clean_given):
            continue
        faq_real += 1
    out = {"given_real": len(clean_given), "given_off_meaning": len(junk_given), "faq_total": len(faq),
           "faq_real": faq_real}
    # S245 r7 review — pair counts before / after the FAQ step, so a higher real share can be told
    # apart from writer pairs removed (absent on pieces written before).
    for key in ("faq_pairs_before", "faq_pairs_after"):
        if isinstance((writer_input or {}).get(key), dict):
            out[key] = writer_input[key]
    return out


__all__ = [
    "SOURCE_SEGMENT", "SOURCE_PLACE", "SOURCE_TOUR", "SOURCE_SERP_TOPUP", "SOURCE_AUTOCOMPLETE",
    "SOURCE_WRITER", "REAL_SOURCES", "RealQuestion", "QuestionScope", "QuestionSource", "DB_SOURCES",
    "SegmentMatchSource", "PlacePaaSource", "SerpTopupSource", "TOPUP_MARKET", "topup_keyword", "topup_keywords",
    "ENGLISH_ONLY_SOURCES", "merge", "looks_english", "is_off_meaning", "single_word_places",
    "chain_filter_names", "deaccent", "joined_name", "name_skeleton", "place_variant_match", "name_variants_in",
    "names_own_place", "own_place_forms", "own_place_named", "place_kind_words", "harm_question", "variant_kinds",
    "person_question", "name_homonym", "travel_context",
    "place_variant_kind", "scope_name_key", "variant_place_question", "names_other_place",
    "is_comparison", "ALL_COUNTRY_WORDS", "DEMONYMS", "country_terms", "country_travel_question", "is_world_question",
    "question_key",
    "place_words", "rank_place_rows", "resolve_scope", "collect_real_questions", "label_questions", "source_counts",
    "faq_sources", "missing_real_questions", "real_first_pairs", "count_real", "piece_question_stats",
]
