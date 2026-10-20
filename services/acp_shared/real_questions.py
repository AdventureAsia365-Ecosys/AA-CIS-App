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
  The FAQ's scope filter (quality_gates.paa_for_scope) reads `own_place_tokens` (exact own names),
  `variant_place_question` (romanisation variants and exonyms, with the other-place / comparison
  checks) and `country_travel_question` (practical travel questions about the scope's country) from here.
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


def place_variant_match(word: str, name: str, *, capitalised: bool = True) -> bool:
    """Is `word` (a question word, or a run of words) a spelling of the place name `name`? Equal after
    folding and joining ('Hoian' / 'Hoi An'); a listed exonym (`_PLACE_ALIAS_GROUPS`: Ulan Bator,
    Karakorum, Muren) of a `capitalised` word; or, both ≥ VARIANT_MIN_LEN letters, the same skeleton
    (`name_skeleton`: Khuvsgul, Hovsgol, Khovsgol, Khövsgöl are one) — and, only for a `capitalised`
    word, a skeleton with one inserted vowel ('Khuvsugul'). Never a consonant change: Karakoram is not
    Karakorum, Darkhan is not Darkhad, Mandala is not Mandalay."""
    w, n = joined_name(word), joined_name(name)
    if not w or not n:
        return False
    if w == n:
        return True
    aliases = _PLACE_ALIASES.get(n, frozenset())
    if capitalised and w in aliases:
        return True
    if len(w) < VARIANT_MIN_LEN or len(n) < VARIANT_MIN_LEN:
        return False
    sw = name_skeleton(w)
    keys = {name_skeleton(n)} | ({name_skeleton(a) for a in aliases if len(a) >= VARIANT_MIN_LEN}
                                 if capitalised else set())
    if sw in keys:
        return True
    return capitalised and any(_inserted_vowel(sw, k) or _inserted_vowel(k, sw) for k in keys)


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


def name_variants_in(question: str, names: Iterable[str]) -> set[str]:
    """Folded words (or word runs: 'ha long') of `question` that are a spelling variant — not the
    exact single word — of one of the place names `names` (folded words or joined names:
    'khuvsgul' for 'khovsgol', 'halong' for 'ha long', 'ulan bator' for 'ulaanbaatar'). A country or
    nationality word is never a place variant ('Cambodian' is not Cambodia's own place)."""
    targets = {joined_name(n) for n in names if n}
    targets = {t for t in targets if t and t not in ALL_COUNTRY_WORDS}
    if not targets:
        return set()
    out: set[str] = set()
    for surface, capitalised in _question_word_runs(question):
        key = joined_name(surface)
        if not key or key in ALL_COUNTRY_WORDS or (" " not in surface and key in targets):
            continue
        if any(place_variant_match(key, t, capitalised=capitalised) for t in targets):
            out.add(surface)
    return out


def names_own_place(question: str, names: Iterable[str]) -> bool:
    """Does `question` name one of the folded place words `names`, exactly or as a spelling
    variant (`name_variants_in`)?"""
    names = {n for n in names if n}
    return bool(set(_TOKEN_RE.findall(fold(question))) & names) or bool(name_variants_in(question, names))


def own_place_tokens(question: str, names: Iterable[str], real_names: Iterable[str] = ()) -> set[str]:
    """The exact own-place words `question` names: a folded word of `names` (the seed's name tokens,
    line openers included) that is one of `real_names` (names written mid-line, in the Location line
    or the label) and not a common English word or place qualifier ('long' of Ha Long, 'great' of
    Great Wall, 'ancient' of Ancient Town), or that
    the question writes capitalised after its first word ('Is the Great Wall …'). 'How long …' and
    'Can you trek …' never name Ha Long or a 'Trek …' line opener."""
    names = {n for n in names if n}
    real = {n for n in real_names if n}
    caps: set[str] = set()
    for i, m in enumerate(_NAME_TOKEN_RE.finditer(deaccent(question))):
        if i and m.group(0)[0].isupper():
            caps.add(fold(m.group(0)))
    out: set[str] = set()
    for tok in set(_TOKEN_RE.findall(fold(question))) & names:
        if tok in caps or (tok in real and tok not in _COMMON_ENGLISH and tok not in PLACE_KINDS):
            out.add(tok)
    return out


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
countryside village villages family families tea
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
    r"|visas?|passports?|currency|money|cash|atms?|credit\s+cards?|cards|tipping|tip"
    r"|food|foods|dish|dishes|cuisine|restaurants?|tap\s+water|drinking\s+water"
    r"|pack|packing|wear|clothes|clothing|worth|expensive|cheap|budget|afford\w*|(?:known|famous)\s+for"
    r"|how\s+many\s+days|how\s+long\s+(?:should|do|does|to|is\s+enough)"
    r"|speak\s+english|english\s+(?:is\s+)?(?:\w+\s+)?spoken|english\s+speaking"
    r"|toilets?|bathrooms?|showers?|wi\s+fi|wifi|internet|sim|electricity|plugs?|sockets?"
    r"|get(?:ting)?\s+around|transport\w*)\b")
_SAFETY_RE = re.compile(r"\b(?:safe|safety|dangerous|friendly|welcoming)\b")
_SAFETY_FRAME_RE = re.compile(
    r"\b(?:touris\w*|travel\w*|visit\w*|you|i|we|foreigners?|solo|backpack\w*|citizens?|americans|brits)\b")
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
    r"|person|famous\s+people|girlfriends?|wife|wives|husbands?|marry|married|marriage|dating|history|historical)\b")
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
ancient
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


def names_other_place(question: str, allowed: Iterable[str] = (), countries: Iterable[str] = ()) -> bool:
    """Does `question` name a place (or nationality) outside `allowed` (folded words: the moment's own
    names) and the scope's countries? Every capitalised word counts, the first one too unless it is a
    question opener ('Russia or Mongolia: …', 'Inner Mongolia: …'), and so do all-caps words ('the UK',
    'the USA') and other countries written lowercase ('as safe as japan'). Not counted: months, events
    ('Naadam'), brands ('Visa cards'), kinds of place ('Lake'), English after speak / spoken, and a
    nationality in a traveller frame ('… for American tourists')."""
    names, demonyms = _scope_countries(countries)
    ok = {fold(a) for a in allowed if a} | names | demonyms | _NEUTRAL_CAPS | _KIND_CAPS
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


def variant_place_question(question: str, names: Iterable[str], countries: Iterable[str] = ()) -> bool:
    """A question that names one of the moment's places (`names`: folded words and joined names) only
    through a spelling variant is in scope when it is no world question, no comparison ('Is Lake
    Khuvsgul bigger than Lake Baikal?') and names no other place ('Where is Terrell, Texas?')."""
    names = {n for n in names if n}
    variants = name_variants_in(question, names)
    if not variants or is_world_question(question) or is_comparison(question):
        return False
    allowed = names | {w for v in variants for w in _TOKEN_RE.findall(v)}
    return not names_other_place(question, allowed, countries)


def _country_mention(words: list[str], raw: list[str], names: set[str], demonyms: set[str]) -> bool:
    """The question names a scope country as a destination: the country written capitalised
    ('Mongolia', "Mongolia's", 'Viet Nam'), a capitalised plural demonym ('Mongolians'), or a
    capitalised demonym with a travel noun right after it ('Mongolian gers', 'Lao national dish') and
    no homonym noun ('Mongolian beef', 'Burmese python')."""
    for i, (surface, w) in enumerate(zip(raw, words)):
        if not surface[:1].isupper():
            continue
        if w in names:
            return True
        if w not in demonyms:
            continue
        if w.endswith("s") and w[:-1] in demonyms:
            return True
        window = []
        for nxt_surface, nxt in zip(raw[i + 1:i + 4], words[i + 1:i + 4]):
            if nxt_surface[:1].isupper():
                break
            window.append(nxt)
        if set(window) & _DEMONYM_HOMONYMS:
            continue
        if set(window) & _DEMONYM_TRAVEL_NOUNS:
            return True
    return False


def country_travel_question(question: str, countries: Iterable[str], seed_words: Iterable[str] = (),
                            allowed_names: Iterable[str] = ()) -> bool:
    """AA-779 (S245, 20/10; review round 1) — a question about the scope's country as a travel
    destination, for a moment that it names no place of. All of:
    - it names the country as a destination (`_country_mention`: capitalised 'Mongolia' / "Mongolia's",
      or 'Mongolian gers', never 'Mongolian beef' or lowercase 'china');
    - no people / politics / economy / demographics / history question (`_COUNTRY_DENY_RE`: 'Who is the
      president of Mongolia?', 'What percent of Mongolians …', 'Did Mongolians …');
    - no comparison or world superlative (`_COMPARISON_RE`: 'as safe as', 'spicier than', 'most like');
    - no other place or nationality (`names_other_place`; allowed: the moment's own names
      `allowed_names`, and a traveller nationality: 'for American tourists');
    - a general practical travel topic (`_PRACTICAL_RE`: best time, weather, visa, currency, food and
      dishes, what to pack, worth visiting, expensive / cheap, known for; 'safe' / 'friendly' only for
      tourists or travel), or a traveller frame ('you', visit, travel, famous, best …) AND a non-generic
      word shared with the moment's source (`seed_words`, content_words stems): 'What is the most famous
      lake in Mongolia?' stays for a lake moment, not for a dunes moment."""
    names, demonyms = _scope_countries(countries)
    if not names:
        return False
    norm = _normalise_question(question)
    raw = _NAME_TOKEN_RE.findall(norm)
    words = [fold(w) for w in raw]
    if not _country_mention(words, raw, names, demonyms):
        return False
    q = " ".join(_TOKEN_RE.findall(fold(norm)))
    if _COUNTRY_DENY_RE.search(q) or _COMPARISON_RE.search(q) or is_world_question(norm):
        return False
    if names_other_place(question, allowed_names, countries):
        return False
    if (_PRACTICAL_RE.search(q) or (_SAFETY_RE.search(q) and _SAFETY_FRAME_RE.search(q))
            or (_EAT_RE.search(q) and _EAT_FRAME_RE.search(q))):
        return True
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
    "names_own_place", "own_place_tokens", "scope_name_key", "variant_place_question", "names_other_place",
    "is_comparison", "ALL_COUNTRY_WORDS", "DEMONYMS", "country_terms", "country_travel_question", "is_world_question",
    "question_key",
    "place_words", "rank_place_rows", "resolve_scope", "collect_real_questions", "label_questions", "source_counts",
    "faq_sources", "missing_real_questions", "real_first_pairs", "count_real", "piece_question_stats",
]
