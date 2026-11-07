"""AA-900 W0 (root ADR 0020, decisions D1/D3/D4/D15) — the interim F9 policy and the F9 failure classes.

Why: the F9 judge (`quality_gates.gate_brand_voice`) files every product-truth reason under one blocking
`F9_cta_fact` entry: an unsupported claim of any kind, a missing CTA, a broken sentence (TEXT_INTEGRITY), and, as a
non-blocking entry, a judge outage. The S271 audit (critic F15) found that counting that entry inflates the fidelity
baseline with infrastructure and text failures, and that most holds are claims the policy allows (place background,
reflection, hypotheticals). Until the policy registry and a calibrated claim instrument exist (W1/W2), F9 may hold
only on the class every decision agrees on.

Interim rule (declared diff, mode `interim`, the default):

* `F9_cta_fact` stays BLOCKING only for a quoted body claim with a `why_not_in_source` that names a concrete trip
  activity, service, time or view (`trip_promise_kinds`). Its violations are rebuilt with those claims only, in the
  stored format, so the sentence repair targets them alone.
* every other F9 class warns: the entry is stored `blocking: false` (it goes to the flags, the admin still sees
  it) — place background and other unsupported claims, a missing or unclear CTA (the configured CTA is put in word
  for word by `hygiene.ensure_cta`), TEXT_INTEGRITY, a title / meta claim, a fact flag with no named claim.
* the entry records what was decided in `f9_policy` (`version`, `classes`, `warned`, `blocking_before`).

`T10_F9_POLICY_MODE=legacy` keeps the pre-W0 verdict (the rollback switch; a replay sets it to show the diff).

`violation_classes` / `entry_classes` classify a stored entry (any era: the pre-AA-925 "… treated as fail" outage
text too), so counts can keep `judge_unavailable` and `text_integrity` apart from the fidelity classes.

Pure: no DB, no LLM, no I/O. Imports nothing from quality_gates / sentence_repair (both import this module).
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

from services.acp_content_writing.claim_rules import mask_names
from services.quality import registry as qr

POLICY_VERSION = "f9-interim-v1"
F9_POLICY_MODE_ENV = "T10_F9_POLICY_MODE"
INTERIM_MODE = "interim"
LEGACY_MODE = "legacy"

# The stored form of one unsupported body claim inside an F9_cta_fact violation (sentence_repair and the admin
# highlight parse it through this one pattern).
F9_CLAIM_RE = re.compile(r'"(.+?)" \(claims: (.+?); source: (.+?)\)(?:; |$)', re.DOTALL)
CLAIMS_MARK = "unsupported claim(s):"
TEXT_INTEGRITY_PREFIX = "TEXT_INTEGRITY: "
_SEO_FIELD_PREFIXES = ("title ", "meta ")
# quality_gates.F9_NO_REASON (a test keeps the two equal; this module cannot import quality_gates)
NO_REASON_MARK = "judge gave no checkable reason"
_STATUS_RE = re.compile(r"^audit (\w+):")

# ---------------------------------------------------------------------------------------------- classes

TRIP_PROMISE = "trip_promise"                  # holds (interim)
UNSUPPORTED_OTHER = "unsupported_other"        # place background, generalisation … (warn until W1)
FACT_CHECK_UNQUOTED = "fact_check_unquoted"    # FACT_CHECK with phrases, no named claim (no-source prompt)
SEO_FIELD_CLAIM = "seo_field_claim"            # a claim in the search title / meta
CTA_NOT_REFLECTED = "cta_not_reflected"
TEXT_INTEGRITY = "text_integrity"
JUDGE_UNAVAILABLE = "judge_unavailable"        # infrastructure: no verdict
REASONLESS = "reasonless"                      # a verdict with no checkable reason
OTHER = "other"

FIDELITY_CLASSES = frozenset({TRIP_PROMISE, UNSUPPORTED_OTHER, FACT_CHECK_UNQUOTED, SEO_FIELD_CLAIM})
INFRASTRUCTURE_CLASSES = frozenset({JUDGE_UNAVAILABLE})
HOLD_CLASSES = frozenset({TRIP_PROMISE})

# ---------------------------------------------------------------------------------------------- trip promise

_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|several|a few|a couple of|half an?)"
# Who the sentence makes the trip happen to: the reader, the operator, the group, the trip itself, a day of it.
# AA-1047 W1 fix pass 5 (review finding 3): "visitors" and a bare "everyone" are the trip too ("a monk performs a
# blessing for visitors", "a speedboat whisks everyone to the island").
TRIP_SUBJECT_RE = re.compile(
    r"\b(?:you|your|yourself|we|our|us|travell?ers?|guests?|visitors?|participants|everyone(?: on (?:the|this) "
    r"(?:trip|tour))?"
    r"|(?:the|your|our|each|every) group|(?:this|the) (?:trip|tour|journey|itinerary|route|holiday|walk|day)"
    r"|(?:on|by) day " + _NUM + r"|day \d+)\b", re.I)
# A service or inclusion the trip provides. AA-1047 W1 fix pass 4 (review finding 2): a service noun is a trip
# promise on its own, with no reader / group subject ("Lunch is served on a floating restaurant", "A sunrise balloon
# flight over the plain opens the day", "A tuk-tuk takes the group to the night market"); the vehicles, crafts and
# people a trip lays on are services too.
SERVICE_RE = re.compile(
    r"\b(?:guides?|guided|guiding|transfers?|drivers?|porters?|hotels?|lodges?|camps?|tents?|homestays?|"
    r"guesthouses?|breakfasts?|lunch(?:es)?|dinners?|meals?|picnics?|tastings?|tickets?|permits?|visas?|"
    r"(?:entry|entrance) fees?|boat (?:ride|trip)s?|jeeps?|safaris?|(?:cooking|craft) (?:class|lesson)(?:es|s)?|"
    r"workshops?|briefings?|cruises?|(?:hot[- ]air )?balloons?(?: (?:flight|ride)s?)?|tuk[- ]?tuks?|junks?|"
    r"sampans?|ferr(?:y|ies)|kayaks?|canoes?|bicycles?|cyclos?|rickshaws?|minibus(?:es)?|naturalists?|rangers?|"
    r"mahouts?|instructors?|chefs?|feasts?|banquets?|barbecues?|bbqs?|drinks|cocktails?|massages?|spas?|sails?|"
    r"demos?)\b"
    # a compound boat (speedboat, longtail boat, houseboat), with no leading word boundary (review finding 3)
    r"|\w*boats?\b", re.I)
# A passive (or "get") promise with no subject: the trip is the agent ("The caves are explored by torchlight",
# "Sunrise is watched from the hill", "Bicycles are provided"). Present / future / perfect: a simple past passive
# ("was built", "was visited by a king") is the place's history. AA-1047 W1 fix pass 5 (review finding 3): any one
# word may sit between the auxiliary and the participle ("are then explored", "is then reached"), and a perfect
# passive is a promise too ("a private viewing has been arranged"; fail closed: "had been" as well).
PASSIVE_PROMISE_RE = re.compile(
    r"\b(?:is|are|will\s+be|can\s+be|(?:has|have|had)\s+been|gets?|getting)\s+(?:\w+\s+)?"
    r"(?:served|provided|reached|explored|watched|"
    r"enjoyed|cruised|visited|toured|hiked|trekked|climbed|kayaked|paddled|sailed|crossed|spotted|viewed|tasted|"
    r"sampled|offered|hosted|arranged|organi[sz]ed|included|laid\s+on|led|guided|taught|followed|admired|spent|"
    r"taken|picked\s+up|dropped\s+off|transferred|driven|flown|met|greeted|welcomed|shown)\b", re.I)
# The reader / the group as the OBJECT of a verb that does something to them ("locals teach you to weave",
# "a tuk-tuk takes the group to the market", "Day 2 brings you to the village").
OBJECT_TRIP_RE = re.compile(
    r"\b(?:teach\w*|taught|show(?:s|ing|ed)?|tak(?:e|es|ing)|took|bring\w*|brought|lead(?:s|ing)?|led|guid\w*|"
    r"invit\w*|welcom\w*|treat\w*|transfer\w*|driv(?:e|es|ing)|drove|pick(?:s|ed|ing)?\s+up|meet(?:s|ing)?|"
    r"greet\w*|accompan\w*|escort\w*|host(?:s|ed|ing)?|serv(?:e|es|ed|ing)|feed(?:s|ing)?|introduc\w*)\s+"
    r"(?:you|yourself|the group|your group|our group|guests|travell?ers|everyone)\b", re.I)
# A highlight / inclusion list line or a bullet is about the trip ("Highlights: Angkor at sunrise, ...").
LIST_LINE_RE = re.compile(
    r"^\s*(?:[-*•·]\s+|(?:tour\s+|trip\s+|key\s+)?(?:highlights?|inclusions?|included|includes|"
    r"what'?s\s+included|itinerary|programme|program|activities|experiences?)\s*:)", re.I)
INCLUSION_RE = re.compile(
    # "built in" is an inclusion ("time to swim is built in"), not a date ("built in 1200" is place history).
    r"\b(?:includ\w*|arranged|provided|organi[sz]ed|laid on|booked|reserved|planned|scheduled|"
    r"built in(?!\s+(?:the\s+)?(?:\d|(?:early|late|mid)\b))|"
    r"on the itinerary|part of (?:the|this|your) (?:trip|tour|day|itinerary))\b", re.I)
TIME_RE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.)(?!\w)|\b\d{1,2}:\d{2}\b|\bo'clock\b"
    r"|\b(?:at|by|before|after|around|until|from) (?:dawn|sunrise|sunset|dusk|daybreak|first light|nightfall|noon|"
    r"midday|midnight|lunchtime|night)\b"
    r"|\b(?:early|late|this|that|next|each|every|in the|by|one|the following) (?:morning|afternoon|evening|night)\b"
    r"|\b" + _NUM + r"[- ](?:hours?|minutes?|nights?|days?)\b|\bovernight\b"
    r"|\b(?:all|half)[- ](?:day|morning|afternoon)\b",
    re.I)
VIEW_RE = re.compile(
    r"\b(?:views?|viewpoints?|vistas?|panoram\w*|overlook\w*|sweeping|look(?:s|ing)? (?:out )?(?:over|across|down|"
    r"onto)|(?:see|sees|seeing|watch|watches|watching|spot|spotting|glimpse|catch sight of|witness)|"
    r"sunrise over|sunset over)\b", re.I)
# A reader / operator / group subject followed (within three words, no clause break) by a trip activity verb.
_ACT_SUBJECT = r"(?:you|yourself|we|travell?ers|guests|the group|your group|our group|everyone)"
_ACT_VERB = (r"(?:board|ride|rode|hike|trek|walk|cycl|bik|kayak|paddl|raft|swim|swam|snorkel|div|climb|sail|cruis|"
             r"visit|explor|stop|stay|spend|spent|sleep|slept|meet|met|join|tast|sampl|cook|learn|driv|drove|fly|flew|"
             r"cross|camp|reach|arriv|continu|head|descend|ascend|tour|picnic|weav|dye|fish|shop|feed|fed|bath|"
             r"plant|harvest|soak|practi[sc]|tr(?:y|ies|ied))\w*")
ACTIVITY_RE = re.compile(
    rf"\b{_ACT_SUBJECT}(?:'ll|'re|'ve)?(?:\s+(?:will|can|could|may|might|then|also|first|finally|get to|go on to|"
    rf"set off to|are able to))?(?:\s+[a-z]+){{0,2}}?\s+{_ACT_VERB}\b", re.I)
_QUOTES = str.maketrans({"’": "'", "‘": "'"})


def trip_promise_kinds(sentence: Optional[str], lexicon=None) -> tuple[str, ...]:
    """The concrete trip promise kinds a sentence names: `service`, `time`, `view`, `activity` (D1/D4/D15), () when
    it names none. A question names none (an FAQ question asks, it does not promise). Place names the scope lexicon
    knows are blanked first (claim_rules.mask_names: 'Sunset Point', 'Camp Road' do not count).

    The trip is the subject when the sentence names the reader / operator / group / trip / a day, makes the reader or
    the group the object of a doing verb (``OBJECT_TRIP_RE``), uses a present passive promise with no subject
    (``PASSIVE_PROMISE_RE``: "the caves are explored by torchlight") or is a highlight / list line
    (``LIST_LINE_RE``). A service noun is a promise on its own (fail closed, AA-1047 W1 fix pass 4); so is an
    activity / event noun or an offer verb with a trip subject or no background marker (fix pass 5)."""
    text = (sentence or "").translate(_QUOTES).strip()
    if not text or text.rstrip(" *\"')").endswith("?"):
        return ()
    masked = mask_names(text, lexicon=lexicon) if lexicon is not None else text
    passive = bool(PASSIVE_PROMISE_RE.search(masked))
    object_trip = bool(OBJECT_TRIP_RE.search(masked))
    subject = bool(TRIP_SUBJECT_RE.search(masked) or passive or object_trip or LIST_LINE_RE.match(masked))
    kinds: list[str] = []
    if SERVICE_RE.search(masked) or (subject and INCLUSION_RE.search(masked)):
        kinds.append("service")
    if subject and TIME_RE.search(masked):
        kinds.append("time")
    if subject and VIEW_RE.search(masked):
        kinds.append("view")
    # AA-1047 W1 fix pass 5 (review finding 3): an activity / event noun or an offer verb with no positive background
    # marker is the trip's too ("The day takes in ... a cookery demo", "A welcome cocktail awaits on arrival", "Outside
    # the cave a local band plays traditional music", "Mr Sok leads a meditation session at dawn").
    offered = bool(_OFFERED_NOUN_RE.search(masked) or _OFFER_RE.search(masked)) and (
        subject or not _background_marker(masked, locative=True))
    if ACTIVITY_RE.search(masked) or passive or object_trip or offered:
        kinds.append("activity")
    return tuple(kinds)


# ---------------------------------------------------------------------------------------------- claim span

# Clause breaks inside a sentence: punctuation, a dash, and a coordinating / relative link that starts a new clause.
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:[,;:()—–]|\s-\s)\s*|\s+(?=(?:and|but|while|whereas|where|which|"
                              r"before|after|then)\b)", re.I)
_WORD_RE = re.compile(r"[a-z0-9']+")
_SPAN_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "to", "in", "on", "at", "for", "with", "is", "are", "its",
                             "it", "this", "that", "by", "from", "as"})
# A verb-like word of the claim (an auxiliary, a past / -ing / 3rd-person form): a claim-word tie between two clauses
# goes to the clause that holds the claim's verb ("Camp Road was paved": "which was paved in 1990", not "on Camp Road").
_VERBISH_RE = re.compile(r"^(?:was|were|is|are|be|been|has|have|had|will|can|could|may|might|\w{3,}(?:ed|ing|en|es))$")


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower().translate(_QUOTES)) if w not in _SPAN_STOPWORDS}


def _clauses(sentence: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT_RE.split(sentence or "") if c and c.strip()]


def _masked(text: str, lexicon=None) -> str:
    return mask_names(text, lexicon=lexicon) if lexicon is not None else text


def _names_trip_term(text: str, lexicon=None) -> bool:
    """The text names a service, a time or a view (with or without a trip subject of its own)."""
    masked = _masked(text, lexicon)
    return bool(SERVICE_RE.search(masked) or TIME_RE.search(masked) or VIEW_RE.search(masked))


def _claim_clause(sentence: str, claim: str) -> tuple[Optional[str], Optional[str]]:
    """(the clause of ``sentence`` that holds the claim's words, the clause before it): most claim words, a tie to
    the clause with more of the claim's verb-like words, then the first. (None, None) when no clause shares a word."""
    cw = _words(claim)
    clauses = _clauses(sentence)
    if not cw or not clauses:
        return None, None
    best, best_key, prev = None, (0, 0), None
    for i, c in enumerate(clauses):
        shared = cw & _words(c)
        key = (len(shared), sum(1 for w in shared if _VERBISH_RE.match(w)))
        if key[0] and key > best_key:
            best, best_key, prev = c, key, (clauses[i - 1] if i else None)
    return best, prev


def claim_span(sentence: Optional[str], claim: Optional[str], lexicon=None) -> str:
    """The part of ``sentence`` the judge's ``claim`` is about: the clause that holds the claim's words (most overlap;
    a tie goes to the clause with the claim's verb, then the first). When that clause has no trip subject of its own
    and is not place background (``_is_place_background``), the sentence's trip subject is put in front when the
    clause continues the subject's verb phrase ("and" / "then") or names a service, a time or a view ("You stay at a
    lodge, where dinner is served on the terrace"; "Day 3 is a free day, with a cooking class"): a promise in a
    relative or attached clause is still the trip's. A claim with no word in common with the sentence (a paraphrase,
    a placeholder) or an empty one falls back to the whole sentence (the pre-W1 behaviour: fail safe, a missed
    promise is worse than a warned background). ``lexicon`` masks the scope's place names ('Camp Road')."""
    sentence = sentence or ""
    claim = (claim or "").strip()
    if not claim:
        return sentence
    best, prev = _claim_clause(sentence, claim)
    if best is None:
        return sentence
    if not TRIP_SUBJECT_RE.search(_masked(best, lexicon)) and not _is_place_background(best, prev, lexicon):
        at = sentence.find(best)
        subjects = [m for m in TRIP_SUBJECT_RE.finditer(sentence) if m.end() <= at]
        if subjects and (_same_clause_chain(sentence, best, subjects[-1].group(0))
                         or _names_trip_term(best, lexicon)):
            return f"{subjects[-1].group(0)} {_CHAIN_LEAD_RE.sub('', best).strip()}"
    return best


_CHAIN_LEAD_RE = re.compile(r"^\s*(?:and|then)\b", re.I)


def _same_clause_chain(sentence: str, clause: str, subject: str) -> bool:
    """The subject governs ``clause`` when the clause continues the subject's verb phrase (it opens with "and" /
    "then": "you visit the market and taste the tea")."""
    return bool(_CHAIN_LEAD_RE.match(clause)) and subject.lower() in sentence[:sentence.find(clause)].lower()


# ---------------------------------------------------------------------------------------------- place background

# AA-1047 W1 fix pass 2 (review: the span alone dropped activity promises in attached clauses to warn). A claim
# clause is demoted to warn only when it is POSITIVELY place background; anything else that leaves the span with no
# trip kind is decided by the whole sentence (fail closed: a false hold costs a click, a false keep ships a promise).
#
# A clause describing a place opens with a relative pronoun ("which / whose / where ...") or a locative preposition
# ("near / beside / opposite ...").
_PLACE_RELATIVE_RE = re.compile(r"^\s*(?:which|whose|where)\b", re.I)
_LOCATIVE_RE = re.compile(r"^\s*(?:near|beside|next to|close to|opposite|across from|behind|below|above|outside|"
                          r"inside|alongside|just off|off|along)\b", re.I)
# What makes it background and not an offer: its history, its standing, its fame, its fixed setting. AA-1047 W1 fix
# pass 4 (review finding 1, blocking): no catch-all passive ("was upgraded / chosen / named for ..." made a lodge's
# view promise read as history); only the history verbs, and "named" only after / by / in a year.
#
# AA-1047 W1 fix pass 5 (review finding 1, blocking): the markers come in three kinds.
# - HISTORY (past / perfect / dated): always a marker ("was built", "has stood since", "for 900 years").
_HISTORY_MARKER_RE = re.compile(
    r"\b(?:built|founded|erected|constructed|carved|restored|rebuilt|destroyed|paved|named\s+(?:after|by|in\s+\d)|"
    r"dates?\s+(?:back|from)|"
    r"dating|(?:has|have|had)\s+(?:stood|guarded|watched|served\s+as)|stood|for\s+(?:centuries|generations|"
    r"decades|ages|\d+\s+years|hundreds\s+of\s+years|over\s+\d+\s+years)|since\s+(?:the\s+)?\d"
    r"|in\s+(?:the\s+)?\d{3,4}s?"
    r"\b|\d{1,2}(?:st|nd|rd|th)[-\s]century|centur(?:y|ies)\s+old|(?:was|were)\s+once|once\s+\w+ed)\b", re.I)
# - STATIVE (the place's fixed setting is the clause's verb): "which sits above the rapids", "is perched on".
_STATIVE_MARKER_RE = re.compile(
    r"\b(?:sits|stands|lies|is\s+(?:located|set|perched|nestled)|perched|nestled)\b", re.I)
# - FAME (famous / renowned / known for / once / legend / said to): a marker ONLY as the predicate of a copula ("which
#   is famous for its noodles", "which is home to gibbons", "is said to be haunted") or, in a locative clause, as an
#   adjective that ends the clause ("near the temple famed for its views"). "where a family famous for its noodles
#   cooks a feast" / "where a famous master demonstrates calligraphy" carry another verb: not proven background.
_FAME_WORDS = r"(?:famed|famous|renowned|celebrated|known|legendary|home\s+to|said\s+to\s+(?:be|have)|once)"
_COPULA = r"(?:is|are|was|were|remains?|remained|becomes?|became|(?:has|have|had)\s+(?:long\s+)?been)"
_FAME_COPULA_RE = re.compile(
    rf"\b{_COPULA}\s+(?:(?:also|still|long|widely|rightly|justly|best|perhaps|now|locally)\s+)*{_FAME_WORDS}\b", re.I)
_FAME_TAIL_RE = re.compile(
    r"\b(?:famed|famous|renowned|celebrated|known|legendary)\s+(?:for|as)\s+(?:[\w'-]+\s+){0,3}[\w'-]+\W*$", re.I)
def _background_marker(masked_clause: str, *, locative: bool = False) -> bool:
    """The clause carries a PROVEN background marker: history, a stative verb, or fame in a copula frame (or, in a
    locative clause, a fame adjective that ends it)."""
    return bool(_HISTORY_MARKER_RE.search(masked_clause) or _STATIVE_MARKER_RE.search(masked_clause)
                or _FAME_COPULA_RE.search(masked_clause)
                or (locative and _FAME_TAIL_RE.search(masked_clause)))
# The head noun of a relative clause's antecedent that is the trip's own stay / vehicle / table: a relative clause
# about it ("You stay at the Bay Lodge, which ...") describes what the trip provides, never place background.
_TRIP_ANTECEDENT_RE = re.compile(
    r"^(?:lodges?|eco-?lodges?|camps?|campsites?|hotels?|resorts?|guesthouses?|homestays?|hostels?|inns?|villas?|"
    r"bungalows?|chalets?|cabins?|tents?|treehouses?|rooms?|suites?|dorms?|boats?|junks?|cruises?|ships?|vessels?|"
    r"sampans?|rafts?|houseboats?|barges?|trains?|carriages?|buses|bus|vans?|coaches?|jeeps?|cars?|restaurants?|"
    r"cafes?|kitchens?|decks?|terraces?|balcon(?:y|ies)|verandas?|stilt-?houses?|farmstays?|ger|gers|yurts?)$", re.I)
_LAST_WORD_RE = re.compile(r"([A-Za-z][A-Za-z'-]*)\W*$")
# AA-1047 W1 fix pass 5 (review finding 2, blocking): a stay / vehicle with a proper name ("You board the Jasmine",
# "You spend two nights at the Mekong Riverside", "You overnight at Ondra Retreat") is the trip's own, whatever its
# head noun: a trip subject, a stay / board verb, then (counts, "the night", one preposition) a capitalised name that
# ends the clause.
_STAY_VERB_RE = re.compile(
    r"\b(?:stay(?:s|ed|ing)?|spend(?:s|ing)?|spent|overnight(?:s|ed|ing)?|sleep(?:s|ing)?|slept|board(?:s|ed|ing)?|"
    r"sail(?:s|ed|ing)?\s+aboard|cruis(?:e|es|ed|ing)\s+aboard|check(?:s|ed|ing)?\s+in(?:to)?|lodg(?:e|es|ed|ing)|"
    r"bed\s+down)\b", re.I)
_NAMED_STAY_TAIL_RE = re.compile(
    r"^(?:\s+(?:the|a|an|\d+|one|two|three|four|five|six|seven|several|few|couple|of|night|nights|evening))*"
    r"(?:\s+(?:at|aboard|on|onboard|in|into|onto))?(?:\s+(?:the|a|an))?(?:\s+[A-Z][\w'&-]*)+\W*$")
# A view / time word in a relative clause after a trip-subject clause is the trip's, unless the marker is history.
_STAY_AMENITY_RE = re.compile(r"\b(?:sundecks?|sun\s+decks?|pools?|infinity|yoga|spa|hammocks?|jacuzzis?|"
                              r"hot\s+tubs?|cabins?|candlelit)\b", re.I)
# A view the guest gets FROM the stay ("every room overlooking the rapids", "views from the private deck") or a
# view / time attached with "with" ("restored in 2010 with a rooftop bar overlooking the river at sunset").
_GUEST_VIEW_RE = re.compile(r"\b(?:rooms?|suites?|decks?|sundecks?|balcon(?:y|ies)|terraces?|verandas?|rooftop|"
                            r"pools?|infinity|yoga|every|each|private|your)\b", re.I)
_WITH_RE = re.compile(r"\bwith\b", re.I)


def _antecedent_is_trip_service(prev: Optional[str]) -> bool:
    """The clause before a relative clause ends on the trip's stay, vehicle or table (its head noun, unmasked: the
    kind of thing matters, not whether its name is known)."""
    m = _LAST_WORD_RE.search((prev or "").translate(_QUOTES))
    if not m:
        return False
    head = m.group(1)
    return bool(_TRIP_ANTECEDENT_RE.match(head) or SERVICE_RE.fullmatch(head))


def _antecedent_is_named_stay(prev: Optional[str]) -> bool:
    """The clause before a relative clause is the trip staying on / boarding a named place or vessel ("You board the
    Jasmine", "You spend two nights at the Mekong Riverside"): its antecedent is the trip's own stay or vehicle."""
    text = (prev or "").translate(_QUOTES)
    if not TRIP_SUBJECT_RE.search(text):
        return False
    verbs = list(_STAY_VERB_RE.finditer(text))
    return bool(verbs and _NAMED_STAY_TAIL_RE.match(text[verbs[-1].end():]))


def _guest_view(masked_clause: str) -> bool:
    """A view word with a room / deck / balcony / terrace / 'every' / 'private', or a view or time word after 'with'."""
    if VIEW_RE.search(masked_clause) and _GUEST_VIEW_RE.search(masked_clause):
        return True
    w = _WITH_RE.search(masked_clause)
    tail = masked_clause[w.end():] if w else ""
    return bool(tail and (VIEW_RE.search(tail) or TIME_RE.search(tail)))
# A thing a visitor does or is given, named as a noun ("a kayak excursion awaits", "weaving lessons are held").
ACTIVITY_NOUN_RE = re.compile(
    r"\b(?:excursions?|outings?|tours?|trips?|hikes?|hiking|treks?|trekking|walks?|walking|rides?|riding|cruises?|"
    r"kayak\w*|canoe\w*|raft\w*|paddl\w*|climbs?|climbing|swims?|swimming|snorkel\w*|div(?:e|es|ing)|cycl\w*|"
    r"bik(?:e|es|ing)|"
    r"lessons?|class(?:es)?|workshops?|demonstrations?|performances?|shows?|ceremon(?:y|ies)|sessions?|visits?|"
    r"flights?|safaris?|picnics?|tastings?|cooking|weaving|naturalists?|experts?|hosts?|instructors?|"
    r"parades?|festivals?|concerts?|fireworks|processions?|dances?|activit(?:y|ies)|demos?|blessings?|music|"
    r"bands?|massages?|meditation|viewings?)\b", re.I)
# The activity / event nouns that never double as a plain verb ("Pilgrims walk the circuit" is not a walk offer): the
# offer guard of ``trip_promise_kinds`` reads these, the background check the wider ``ACTIVITY_NOUN_RE``.
_OFFERED_NOUN_RE = re.compile(
    r"\b(?:excursions?|outings?|lessons?|class(?:es)?|workshops?|demonstrations?|demos?|performances?|"
    r"ceremon(?:y|ies)|sessions?|flights?|picnics?|tastings?|parades?|festivals?|concerts?|fireworks|processions?|"
    r"blessings?|music|bands?|massages?|meditation|viewings?|activit(?:y|ies)|cookery|tours?|trips?|cruises?|"
    r"safaris?)\b", re.I)
# A clause that hands something to the visitor ("awaits", "offers", "is served") is never background.
# AA-1047 W1 fix pass 5 (review finding 3): + the day-shape verbs ("rounds off", "ends with", "takes in", "whisks")
# and a performance ("performs a blessing", "plays traditional music", "demonstrates").
_OFFER_RE = re.compile(r"\b(?:await\w*|offer\w*|provid\w*|serv(?:e|es|ed|ing)|host\w*|welcom\w*|invit\w*|"
                       r"gives?|show(?:s|n)?\s+(?:you|guests)|points?\s+out|teach\w*|lead\w*|led|"
                       r"rounds?\s+off|ends?\s+with|takes?\s+in|whisk\w*|perform\w*|plays?|playing|"
                       r"demonstrat\w*)\b", re.I)
_CLOCK_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.)(?!\w)|\b\d{1,2}:\d{2}\b|\bo'clock\b", re.I)


def _is_place_background(clause: Optional[str], prev: Optional[str] = None, lexicon=None) -> bool:
    """The claim's clause is positively place background (warn): no trip subject, no service / inclusion, no
    activity verb or noun, no offer, no clock time; and it describes a place: a relative clause with a background
    marker ("which has guarded the valley for 900 years", "which was built by the salt traders") or right after a
    locative clause (its antecedent is that place: "..., near the Tarvo fort, which looks out over the valley"), or
    a locative clause ("near the temple famed for its views"). A view word is allowed only under a background
    marker or a locative antecedent, a time word only under a background marker ("where monks have chanted at dawn
    for centuries"); an event noun (parade, festival, show) is never background. Anything less is not proven
    background.

    Never background (AA-1047 W1 fix pass 4): a present passive promise or the reader as an object; a relative
    clause whose antecedent is the trip's stay / vehicle / table (``_antecedent_is_trip_service``: "You stay at the
    Bay Lodge, which was chosen for its sunset views"); a view from a room / deck / every room, or a view or time
    after "with" (``_guest_view``)."""
    if not clause or not clause.strip():
        return False
    m = _masked(clause, lexicon)
    if (TRIP_SUBJECT_RE.search(m) or SERVICE_RE.search(m) or INCLUSION_RE.search(m) or ACTIVITY_RE.search(m)
            or ACTIVITY_NOUN_RE.search(m) or _OFFER_RE.search(m) or _CLOCK_RE.search(m)
            or PASSIVE_PROMISE_RE.search(m) or OBJECT_TRIP_RE.search(m) or _guest_view(m)):
        return False
    if _PLACE_RELATIVE_RE.match(m):
        if _antecedent_is_trip_service(prev) or _antecedent_is_named_stay(prev):
            return False
        prev_m = _masked(prev or "", lexicon)
        # AA-1047 W1 fix pass 5 (review finding 2): after a trip-subject clause, a view / time / stay-amenity word is
        # the trip's ("You board the Jasmine, which is renowned for its sundeck views at sunset") unless the marker is
        # history ("You take a sunset cruise past the fort, which has guarded the river for 900 years").
        if (TRIP_SUBJECT_RE.search(prev_m) and (VIEW_RE.search(m) or TIME_RE.search(m) or _STAY_AMENITY_RE.search(m))
                and not _HISTORY_MARKER_RE.search(m)):
            return False
        # After a locative antecedent a view word is the place's ("which looks out over the valley"), but a time
        # word with no background marker reads as an event the visitor attends ("which comes alive with lanterns
        # each evening"): not proven background.
        return _background_marker(m) or bool(prev and _LOCATIVE_RE.match(prev_m) and not TIME_RE.search(m))
    if _LOCATIVE_RE.match(m):
        if _background_marker(m, locative=True):
            return True
        # AA-1047 W1 fix pass 5: with no marker, only a bare place phrase ("near the old Tarvo fort") is background;
        # a longer locative clause may carry its own verb ("behind the temple monks bless every visitor").
        return not (TIME_RE.search(m) or VIEW_RE.search(m)) and len(_WORD_RE.findall(m.lower())) <= 6
    return False


def claim_kinds(sentence: Optional[str], claim: Optional[str], lexicon=None) -> tuple[str, ...]:
    """The trip promise kinds of one judged claim. Its clause positively place background (``_is_place_background``):
    () (warn). Otherwise the kinds of its span (``claim_span``), and when the span names none, those of the whole
    sentence (fail safe: "You stay in the village, where a kayak excursion awaits", "You visit the market, the temple
    and the floating village", "..., while a naturalist points out the birds" are the trip's)."""
    sentence = sentence or ""
    claim_text = (claim or "").strip()
    if claim_text:
        clause, prev = _claim_clause(sentence, claim_text)
        if clause is not None and _is_place_background(clause, prev, lexicon):
            return ()
    span = claim_span(sentence, claim, lexicon)
    kinds = trip_promise_kinds(span, lexicon)
    if not kinds and span != sentence:
        kinds = trip_promise_kinds(sentence, lexicon)
    return kinds


# ---------------------------------------------------------------------------------------------- classification

def parse_claims(violation: str) -> list[tuple[str, str, str]]:
    """(sentence, claim, why_not_in_source) of every unsupported body claim in one stored violation."""
    if CLAIMS_MARK not in (violation or ""):
        return []
    return [(s.strip(), c.strip(), w.strip()) for s, c, w in
            F9_CLAIM_RE.findall(violation.split(CLAIMS_MARK, 1)[1])]


def violation_items(violation: str, lexicon=None) -> list[dict]:
    """One {class, …} item per reason in one stored F9_cta_fact violation (any era). A claim item carries its
    `sentence`, `claim`, `why` and, for a trip promise, its `kinds`."""
    v = str(violation or "")
    if v.startswith(_SEO_FIELD_PREFIXES):
        return [{"class": SEO_FIELD_CLAIM}]
    if v.startswith(TEXT_INTEGRITY_PREFIX):
        return [{"class": TEXT_INTEGRITY}]
    if "judge unavailable" in v or "treated as fail" in v:
        return [{"class": JUDGE_UNAVAILABLE}]
    if v.startswith(NO_REASON_MARK):
        return [{"class": REASONLESS}]
    head = v.split(CLAIMS_MARK, 1)[0]
    items: list[dict] = []
    if "CTA_NOT_REFLECTED" in head:
        items.append({"class": CTA_NOT_REFLECTED})
    claims = parse_claims(v)
    for sentence, claim, why in claims:
        # AA-1047 W1 (W0 review #1356): classify the CLAIM's span, not the whole sentence — a place-background claim
        # in a sentence that also names a supported dinner is not a "service" promise.
        kinds = claim_kinds(sentence, claim, lexicon) if why else ()
        items.append({"class": TRIP_PROMISE if kinds else UNSUPPORTED_OTHER, "sentence": sentence, "claim": claim,
                      "why": why, **({"kinds": list(kinds)} if kinds else {})})
    if not claims and "FACT_CHECK_MANUAL_CHECK" in head:
        items.append({"class": FACT_CHECK_UNQUOTED})
    return items or [{"class": OTHER}]


def violation_classes(violation: str, lexicon=None) -> list[str]:
    return [it["class"] for it in violation_items(violation, lexicon)]


def entry_classes(g: dict, lexicon=None) -> dict[str, int]:
    """{class: count} of one failed F9_cta_fact entry ({} for any other entry or a pass). An entry the interim policy
    already decided keeps the classes it recorded (its warned violations are no longer in `violations`)."""
    if not isinstance(g, dict) or g.get("gate") != qr.F9_CTA_FACT or g.get("passed"):
        return {}
    recorded = (g.get("f9_policy") or {}).get("classes") if isinstance(g.get("f9_policy"), dict) else None
    if isinstance(recorded, dict):
        return {str(k): int(n) for k, n in recorded.items()}
    out: dict[str, int] = {}
    for v in g.get("violations") or []:
        for cls in violation_classes(v, lexicon):
            out[cls] = out.get(cls, 0) + 1
    return out


def _claims_violation(status: str, claims: Iterable[dict]) -> str:
    return (f"audit {status}: FACT_CHECK_MANUAL_CHECK — {CLAIMS_MARK} "
            + "; ".join(f'"{c["sentence"]}" (claims: {c["claim"]}; source: {c["why"]})' for c in claims))


def policy_mode() -> str:
    raw = (os.environ.get(F9_POLICY_MODE_ENV) or "").strip().lower()
    return raw if raw in (INTERIM_MODE, LEGACY_MODE) else INTERIM_MODE


def apply_policy(g: dict, lexicon=None, *, mode: Optional[str] = None) -> dict:
    """The F9_cta_fact entry under the F9 policy mode (`policy_mode()` unless given). Any other entry, a pass and
    the legacy mode on an undecided entry are returned unchanged; the legacy mode on an entry the interim policy
    decided puts its stored violations and blocking back. Idempotent."""
    if not isinstance(g, dict) or g.get("gate") != qr.F9_CTA_FACT or g.get("passed"):
        return g
    mode = mode or policy_mode()
    decided = g.get("f9_policy") if isinstance(g.get("f9_policy"), dict) else None
    if mode == LEGACY_MODE:
        if decided is None:
            return g
        out = {k: v for k, v in g.items() if k != "f9_policy"}
        out["violations"] = list(g.get("violations") or []) + list(decided.get("warned") or [])
        out["blocking"] = bool(decided.get("blocking_before", True))
        return out
    if decided is not None:
        return g
    blocking_before = g.get("blocking", True)
    classes: dict[str, int] = {}
    hold_claims: list[dict] = []
    warned: list[str] = []
    status = "flagged"
    for v in g.get("violations") or []:
        items = violation_items(v, lexicon)
        for it in items:
            classes[it["class"]] = classes.get(it["class"], 0) + 1
        m = _STATUS_RE.match(str(v))
        status = m.group(1) if m else status
        held = [it for it in items if it["class"] in HOLD_CLASSES]
        hold_claims += held
        rest = [it for it in items if it["class"] not in HOLD_CLASSES]
        if not held:
            warned.append(v)
        elif rest:
            # the claims that warn are kept as their own violation (the CTA part of a combined one is dropped from
            # the repair: the configured CTA is put in word for word in code)
            other = [it for it in rest if "sentence" in it]
            warned.append(_claims_violation(status, other) if other else str(v).split(" — ", 1)[0])
    hold = bool(hold_claims) and blocking_before
    record = {"version": POLICY_VERSION, "classes": classes, "warned": warned if hold else [],
              "blocking_before": blocking_before}
    if hold:
        return {**g, "violations": [_claims_violation(status, hold_claims)], "blocking": True, "f9_policy": record}
    return {**g, "blocking": False, "f9_policy": record}


__all__ = [
    "POLICY_VERSION", "F9_POLICY_MODE_ENV", "INTERIM_MODE", "LEGACY_MODE", "F9_CLAIM_RE", "CLAIMS_MARK",
    "NO_REASON_MARK", "TRIP_PROMISE", "UNSUPPORTED_OTHER", "FACT_CHECK_UNQUOTED", "SEO_FIELD_CLAIM",
    "CTA_NOT_REFLECTED", "TEXT_INTEGRITY", "JUDGE_UNAVAILABLE", "REASONLESS", "OTHER", "FIDELITY_CLASSES",
    "INFRASTRUCTURE_CLASSES", "HOLD_CLASSES", "ACTIVITY_NOUN_RE", "trip_promise_kinds", "claim_span", "claim_kinds",
    "parse_claims",
    "violation_items", "violation_classes", "entry_classes", "policy_mode", "apply_policy",
]
