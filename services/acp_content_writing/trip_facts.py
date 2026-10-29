"""AA-768 (S234 round 3) — deterministic trip-fact grounding, rarity guard and date authority.

The S234 round 2 panel (§2 classes 3, 4, 5, 7) found the defects that now decide blog verdicts are
not hard errors the hygiene rules see, but trip facts the writer bent:
- an activity the seed lists as OPTIONAL written as part of the plan (#14, #16, #18);
- a time or an ordinal the seed never gives ('first night', 'mid-morning', 'the southern tip' as
  where you stand) (#11, #14, #19);
- a family brand pitching a moment whose seed says the access is physically demanding / a
  multi-hour ride, without saying so (#5, #15, #17, #19);
- the seed's quantifier escalated ('many other reindeer-herding traditions' → 'most') or a
  rarity / uniqueness claim the seed does not make ('no equivalent', 'almost anywhere on earth',
  'categorically different') (6/8 blogs);
- a role the seed dates as ENDED ('served as the capital and seat of government of Bhutan until
  1955') written in the present ('remains Bhutan's … administrative center today') or a span
  computed wrongly ('nearly three centuries' for 1637–1955) (6/6 Punakha posts, 3 metas).

Everything here is pure (no LLM, no DB). Each finding quotes its sentence as
"<problem>: '<sentence>'" — the shape `sentence_repair.plan` parses — so the gates F17_trip_fact
and F18_rarity (quality_gates.py) send them to a sentence repair on the cheap t9_repair stage. A
title / meta finding starts with "meta " / "title " and is left to the SEO fit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from services.acp_content_writing.hygiene import (
    COMMON_CAPS, content_words, fold, phrase_tokens, sentence_spans, split_seed, strip_tags,
)
from services.acp_shared.citation_tags import TAG, trailing_tags
from services.acp_shared.offered import (LOOSE_MARK_RE, OFFERED, activity_text, decide, is_status_line,
                                         status_of_line, strip_status_lines)

# ---------------------------------------------------------------- seed moments

_LABEL_SPLIT_RE = re.compile(r"\s+[—–]\s+")
_DAY_LINE_RE = re.compile(r"^Day\s+(\d+)\s+of the trip(?::\s*(.*))?$", re.I)
_BACKGROUND_HEAD = "Place background"
_TRIP_LINE_PREFIXES = ("Source itinerary:", "Why it matters:", "Day ")
_THIN_PREFIX = "Thin moment:"
# Generic place words never identify a moment on their own ('Sod Camp' is named by 'sod').
_GENERIC = frozenset({"lake", "camp", "ruins", "ruin", "settlement", "settlements", "reindeer", "herder", "herders",
                      "ancient", "monastery", "temple", "falls", "dzong", "nest", "visit", "province", "district"})
# 'Khövsgöl Province' / 'Khövsgöl Aimag' name the province, not the lake moment.
_PROVINCE_AFTER_RE = re.compile(r"\s+(?:province|aimag)\b", re.I)
# The citation tags right after a sentence (AA-912: the shared tag definition, services/acp_shared/citation_tags.py).
_TAG_RUN_RE = re.compile(rf"(?:\s?{TAG})+")


@dataclass
class Moment:
    """One moment block of a seed: its name, the words that identify it in a sentence, its trip
    lines (label, Source itinerary, Why it matters, Day lines) and its place-background lines."""
    name: str
    tokens: frozenset
    trip_lines: list[str] = field(default_factory=list)
    background: list[str] = field(default_factory=list)
    days: list[int] = field(default_factory=list)
    # S234 round 4 — the seed builder's "Thin moment: only N source fact(s)…" note
    # (atom_seed.thin_note, < THIN_FACTS facts): the moment may not carry a heading or the coda
    thin: bool = False
    # AA-773 — the seed's "Status:" line for a trip line, by its index in `trip_lines` (OFFERED /
    # INCLUDED). The Status line itself is never a trip line: its 'optional' / 'not included' are
    # instructions, not itinerary text.
    statuses: dict = field(default_factory=dict)
    # AA-985 round 2 — the block's "Location:" and "Naming:" lines (other names for the place)
    location: str = ""
    naming: str = ""

    @property
    def lines(self) -> list[str]:
        return self.trip_lines + self.background


def _moment_tokens(label_name: str, naming: str) -> frozenset:
    toks = {t for t in phrase_tokens(label_name) if t not in _GENERIC}
    # capitalised words of the label only ('Tsaatan reindeer herders' settlements' → tsaatan)
    caps = {fold(w) for w in re.findall(r"[A-ZÀ-ÖØ-Þ][\w’'-]+", label_name)}
    toks = {t for t in toks if t in {c for w in caps for c in phrase_tokens(w)}} or toks
    for quoted in re.findall(r"'([^']+)'", naming or ""):
        if quoted[:1].isupper() and len(quoted.split()) == 1:
            toks |= {t for t in phrase_tokens(quoted) if t not in _GENERIC}
    return frozenset(toks)


def parse_moments(seed: Optional[str]) -> list[Moment]:
    """The moment blocks of a seed's own half (blank-line separated; each opens on its label line
    '<place> — <action>'). A block without a label (a Facts block, a stray paragraph) is skipped."""
    own = split_seed(seed)[0]
    out: list[Moment] = []
    for block in re.split(r"\n\s*\n", own):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if not lines or not _LABEL_SPLIT_RE.search(lines[0]) or lines[0].startswith(("Location:", "- ")):
            continue
        name = _LABEL_SPLIT_RE.split(lines[0], 1)[0].strip()
        naming = next((ln for ln in lines if ln.startswith("Naming:")), "")
        location = next((ln for ln in lines if ln.startswith("Location:")), "")
        m = Moment(name=name, tokens=_moment_tokens(name, naming), trip_lines=[lines[0]], location=location,
                   naming=naming)
        in_bg = False
        for ln in lines[1:]:
            if ln.startswith(_BACKGROUND_HEAD):
                in_bg = True
                continue
            if in_bg and ln.startswith("- "):
                m.background.append(ln[2:])
                continue
            in_bg = False
            if ln.startswith(_THIN_PREFIX):
                m.thin = True
            if is_status_line(ln):
                status = status_of_line(ln)
                if status:
                    m.statuses[len(m.trip_lines) - 1] = status
                continue
            day = _DAY_LINE_RE.match(ln)
            if day:
                m.days.append(int(day.group(1)))
            if ln.startswith(_TRIP_LINE_PREFIXES):
                m.trip_lines.append(ln)
        out.append(m)
    return out


def names_moment(sentence: str, moment: Moment) -> bool:
    """Does `sentence` name `moment` (one of its identifying words, accent-folded; a province
    named after the place — 'Khövsgöl Province' — does not count)?"""
    if not moment.tokens:
        return False
    # AA-912: the province look-ahead reads the SAME tag-stripped text the word offsets come from
    # (it read the raw sentence: wrong offsets on a tagged sentence, and quadratic on a long tag)
    clean = strip_tags(sentence or "")
    for m in re.finditer(r"[A-Za-zÀ-ÖØ-öø-ÿ’'-]+", clean):
        toks = set(phrase_tokens(m.group(0)))
        if toks & moment.tokens and not _PROVINCE_AFTER_RE.match(clean, m.end()):
            return True
    return False


def moments_named(sentence: str, moments: list[Moment]) -> list[Moment]:
    return [m for m in moments if names_moment(sentence, m)]


# ---------------------------------------------------------------- sentences of a piece

def _body_spans(text: str, cta: Optional[str] = None, kinds=("prose", "bullet", "faq_a")) -> list[dict]:
    from services.acp_content_writing.hygiene import _is_cta
    return [s for s in sentence_spans(text or "") if s["kind"] in kinds and not _is_cta(s["clean"], cta)]


def _quoted(problem: str, sentence: str) -> str:
    return f"{problem}: '{sentence[:300]}'"


def _paragraph_lead(text: str, span: dict, spans: list[dict]) -> Optional[str]:
    """The sentence before `span` in the same paragraph, or — for a bullet — the prose line right
    above its list (the list's lead-in)."""
    prev = [s for s in spans if s["end"] <= span["start"]]
    if not prev:
        return None
    p = prev[-1]
    gap = text[p["end"]:span["start"]]
    if span["kind"] == "bullet":
        k = len(prev) - 1
        while k >= 0 and prev[k]["kind"] == "bullet":
            k -= 1
        return prev[k]["clean"] if k >= 0 else None
    return p["clean"] if "\n\n" not in gap else None


# ---------------------------------------------------------------- (a) optional activities

# AA-773 — the same pattern offered.py uses for "no Status line" (one definition).
_OPTIONAL_MARK_RE = LOOSE_MARK_RE
# A hedge in the same clause (or the sentence / list lead-in before it). 'offers' and 'allows' are
# not hedges: 'Boat rides on the water offer a perspective…' and 'Time at the lake allows for days
# shaped around … nomadic families' (S234 r2 #14, #18) both state the plan.
_HEDGE_RE = re.compile(
    r"\b(?:optional(?:ly)?|options?|can|could|may|might|if\b|choose|choice|chance|opportunit(?:y|ies)|possible"
    r"|available|whether|depending|for those who|those who (?:want|wish)|free to|up to you|on request)\b", re.I)
# only a semicolon opens a new clause; ' — ' is an apposition ('optional activities — boat rides, …')
_CLAUSE_RE = re.compile(r"\s*;\s*")
# words that never identify an activity on their own ('families visit the ruins' is not 'visits to
# local nomadic families')
_GENERIC_ACTIVITY_WORDS = ("visit", "visits", "families", "family", "people", "day", "days", "time", "trip", "journey",
                           "tour", "local")
# a list splits at commas / semicolons (", and" too); a bare "and" stays inside an item
# ('trekking along shorelines and forest paths' is one activity)
_ITEM_SPLIT_RE = re.compile(r"\s*(?:,|;)\s*(?:and\s+|or\s+)?")
_ACTIVITY_STOP = frozenset(content_words("available extra cost fee charge additional arranged supplement surcharge "
                                         "activities activity include includes including optional surrounding local "
                                         "across along water terrain"))
_SINGLE_WORD_ACTIVITIES = frozenset({"horseback", "kayaking", "rafting"})


@dataclass(frozen=True)
class OptionalActivity:
    phrase: str
    words: frozenset  # content words that identify it ('boat', 'ride')


def optional_activities(moments: list[Moment]) -> list[OptionalActivity]:
    """Activities a moment's TRIP lines mark as optional ('Optional activities include boat rides on
    the water, horseback riding…'). Place-background lines never count ('Visitors can swim in the
    lower pools' is what the place allows, not the trip's plan). A source fragment that starts in
    lower case right after an optional line of the same block continues that list (A3 split
    'Optional activities include …, visits to local nomadic families, trekking along shorelines…'
    into one atom per item).

    AA-773 — a trip line the seed gave a Status line is read with the function that wrote it
    (offered.decide): the Status covers ONE clause of the line (the one naming the moment), so an
    OFFERED moment adds that clause's activity and an included one drops only that clause; the rest
    of the line keeps the rule above (review S235 B1: 'Drive to the lake. Optional boat rides. Horse
    riding is available at extra cost.' still lists the boat rides)."""
    out: list[OptionalActivity] = []
    seen: set[str] = set()

    def add(clause: str) -> None:
        for item in _ITEM_SPLIT_RE.split(clause):
            words = [w for w in content_words(item) if w not in _ACTIVITY_STOP]
            key = " ".join(words)
            if not words or key in seen:
                continue
            seen.add(key)
            phrase = re.sub(r"^(?:activities\s+)?(?:include|includes|including)\s+", "", item.strip(" .").strip(),
                            flags=re.I)
            out.append(OptionalActivity(phrase=phrase, words=frozenset(words)))

    def legacy(src: str, prev_optional: bool) -> bool:
        mark = _OPTIONAL_MARK_RE.search(src)
        if mark:
            add(src[mark.end():])
        elif prev_optional and src[:1].islower():
            add(src)
        else:
            return False
        return True

    for m in moments:
        prev_optional = False
        for i, line in enumerate(m.trip_lines):
            src = line.split("Source itinerary:", 1)[1].strip() if "Source itinerary:" in line else ""
            if not src:
                continue
            status = m.statuses.get(i)
            if status is None:
                prev_optional = legacy(src, prev_optional)
                continue
            src = " ".join(src.split())
            span = "" if (status == OFFERED and prev_optional and src[:1].islower()) \
                else decide(src, _line_label(m, line)).span
            if status == OFFERED:
                add(activity_text(span or src) or (span or src))
            rest = src.replace(span, " ", 1) if span else ("" if status == OFFERED else src)
            prev_optional = legacy(rest, False) if rest.strip(" .,;") else False
            prev_optional = prev_optional or status == OFFERED
    return out


def _line_label(m: Moment, line: str) -> str:
    """The moment label a trip line belongs to: 'Day N of the trip: <label> — Source itinerary: …' in a
    merged block, else the block's label line."""
    head = line.split(" — Source itinerary:", 1)[0]
    day = _DAY_LINE_RE.match(head)
    if day and day.group(2) and _LABEL_SPLIT_RE.search(day.group(2)):
        return day.group(2)
    return m.trip_lines[0] if m.trip_lines else ""


def _stem(word: str) -> str:
    """'riding' / 'rides' / 'ride' → 'rid'; 'trekking' / 'trek' → 'trek'; 'horse' stays apart from
    'horseback'."""
    w = word.lower()
    for suf in ("ing", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            break
    if len(w) > 3 and w[-1] == w[-2]:
        w = w[:-1]
    return w[:-1] if w.endswith("e") and len(w) > 3 else w


# AA-789 (S234 r4 #14 'on foot, and by boat, and optionally on horseback', #5 'by boat … with only horse riding
# optional') — travelling BY a vehicle the activity names is that activity: 'by boat' is the optional boat ride.
_MODE_RE = re.compile(r"\b(?:by|on)\s+(?:a\s+|the\s+)?(boats?|horses?|horseback|kayaks?|canoes?|rafts?|camels?"
                      r"|bikes?|bicycles?|reindeer)\b", re.I)


def _mentions_activity(clause: str, act: OptionalActivity) -> bool:
    """Two of the activity's words ('boat' + 'ride'; 'nomadic' + 'families'), at least one of them
    not generic, or one word that names an activity on its own ('horseback'), or travel by the vehicle
    the activity names ('by boat' — AA-789)."""
    have = {_stem(w) for w in content_words(clause)}
    hits = {_stem(w) for w in act.words} & have
    generic = {_stem(w) for w in content_words(" ".join(_GENERIC_ACTIVITY_WORDS))}
    specific = hits - generic
    single = {_stem(w) for w in _SINGLE_WORD_ACTIVITIES}
    if (len(hits) >= min(2, len(act.words)) and bool(specific)) or bool(hits & single):
        return True
    act_stems = {_stem(w) for w in act.words}
    return any(_stem(m.group(1)) in act_stems for m in _MODE_RE.finditer(clause))


# AA-789 — the items of a list inside one clause ('on foot, and by boat, and optionally on horseback'): a hedge
# covers its own item and the items after it ('Optional activities include boat rides, …'), never an earlier one,
# except a collective predicate in the last item ('Boat rides, riding and trekking are all optional').
_LIST_ITEM_RE = re.compile(r"\s*,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+")
_COLLECTIVE_HEDGE_RE = re.compile(
    r"\b(?:are|remain|stay)\s+(?:all\s+|both\s+|each\s+|entirely\s+)?(?:optional|available|possible|on\s+request"
    r"|up\s+to\s+you|extras?)\b", re.I)


def _item_hedged(clause: str, act: OptionalActivity) -> Optional[bool]:
    """True / False when one list item of `clause` mentions `act` alone (see _LIST_ITEM_RE); None when no single
    item does (the words spread over items) — the caller then reads the whole clause, as before AA-789."""
    items = [i for i in _LIST_ITEM_RE.split(clause) if i and i.strip()]
    if len(items) < 2:
        return None
    k = next((n for n, item in enumerate(items) if _mentions_activity(item, act)), None)
    if k is None:
        return None
    # a hedge before the item or in it covers it ('Across the days at the lake, active options include …, boat
    # rides'; '…the boundary and, if you wish, a boat ride'); one after it does not ('…, and optionally on horseback')
    return bool(any(_HEDGE_RE.search(item) for item in items[:k + 1]) or _COLLECTIVE_HEDGE_RE.search(items[-1]))


def optional_as_fixed(text: str, moments: list[Moment], cta: Optional[str] = None) -> list[str]:
    """Sentences that state an optional activity as part of the plan: a clause mentions it, and
    neither that clause nor the sentence before it in the paragraph (or a list's lead-in line)
    hedges it ('Optional activities include boat rides…; the journey also follows shorelines and
    forest paths on foot' — the second clause, S234 r2 #16)."""
    acts = optional_activities(moments)
    if not acts:
        return []
    spans = _body_spans(text, cta)
    out = []
    for s in spans:
        if s["in_faq"]:
            continue
        hit, first_clause = None, True
        for k, clause in enumerate(_CLAUSE_RE.split(s["clean"])):
            hit = next((a for a in acts if _mentions_activity(clause, a)), None)
            if hit is not None:
                item = _item_hedged(clause, hit)
                hedged = bool(_HEDGE_RE.search(clause)) if item is None else item
                if not hedged:
                    first_clause = k == 0
                    break
            hit = None
        if hit is None:
            continue
        # the lead-in sentence hedges the first clause only: '…; the journey also follows shorelines
        # and forest paths on foot' states the plan whatever came before it (S234 r2 #16)
        lead = _paragraph_lead(text, s, spans)
        if first_clause and lead and _HEDGE_RE.search(lead):
            continue
        out.append(_quoted(f"presents '{hit.phrase}' as part of the plan, but the source lists it as optional — "
                           "say it is optional or cut it", s["clean"]))
    return out


# AA-789 (S234 r4 #18: the Tsaatan day, a planned day-5 moment, written as "optional") — the reverse of
# optional_as_fixed: a clause that calls a moment optional when the source plans the whole moment. Only the explicit
# words: 'can' / 'may' describe what a planned day allows ('children can feed the reindeer').
_CALLED_OPTIONAL_RE = re.compile(
    r"\b(?:optional(?:ly)?|add-on|not\s+included|if\s+(?:you|they|families)\s+(?:wish|choose|prefer|like)"
    r"|for\s+those\s+who\s+(?:want|wish|choose))\b", re.I)
INCLUDED_AS_OPTIONAL_PROBLEM = ("calls {name} optional, but the source plans it as part of the trip — say it is part "
                                "of the plan, or cut 'optional'")


def _planned_moment(m: Moment) -> bool:
    """No trip line of the moment marks anything optional (no OFFERED status, no loose optional mark): the source
    plans the whole moment. A mixed moment (Lake Khövsgöl: the boundary visit planned, boat rides optional) is not
    one — a sentence about its optional part is optional_as_fixed's."""
    if any(st == OFFERED for st in m.statuses.values()):
        return False
    return not any(_OPTIONAL_MARK_RE.search(ln.split("Source itinerary:", 1)[1]) for ln in m.trip_lines
                   if "Source itinerary:" in ln)


# 'optional' + a noun names what is optional: 'for optional trekking' calls the trekking optional, not the moment
# the clause is about ('Sod Camp offers … a mountain range nearby for optional trekking'); 'an optional day / visit /
# excursion' is the moment itself.
_MOMENT_NOUNS = frozenset({"day", "days", "visit", "visits", "excursion", "excursions", "extra", "extension", "stop",
                           "trip", "outing", "detour", "side", "afternoon", "morning", "evening", "overnight"})


_FUNCTION_WORDS = frozenset("""on in at for to and or but if as by with from during until after before unless
when while so this that these those the a an extra""".split())


def _targets_moment(clause: str, m: re.Match) -> bool:
    if not m.group(0).lower().startswith("optional") or m.group(0).lower() == "optionally":
        return True
    nxt = re.match(r"\s+([A-Za-zÀ-ÖØ-öø-ÿ’'-]+)", clause[m.end():])
    if nxt is None:
        return True
    word = nxt.group(1)
    # a function word after it is the predicative use ('… is optional on day five'): the moment is what is optional
    return word.lower() in _MOMENT_NOUNS or word.lower() in _FUNCTION_WORDS or word[:1].isupper()


def included_as_optional(text: str, moments: list[Moment], cta: Optional[str] = None) -> list[str]:
    """Sentences (outside the FAQ) with a clause that names a planned moment (`_planned_moment`) and calls it
    optional (`_CALLED_OPTIONAL_RE`), when that clause mentions none of the source's optional activities."""
    planned = [m for m in moments if m.trip_lines and _planned_moment(m)]
    if not planned:
        return []
    acts = optional_activities(moments)
    out = []
    for s in _body_spans(text, cta):
        if s["in_faq"]:
            continue
        for clause in _CLAUSE_RE.split(s["clean"]):
            if not any(_targets_moment(clause, m) for m in _CALLED_OPTIONAL_RE.finditer(clause)) \
                    or any(_mentions_activity(clause, a) for a in acts):
                continue
            named = moments_named(clause, planned)
            if named:
                out.append(_quoted(INCLUDED_AS_OPTIONAL_PROBLEM.format(name=named[0].name), s["clean"]))
                break
    return out


# AA-789 (S234 r4 #4, #5, #14, #15, #16, #18, #19: 'lunch at Mörön' when the source says 'Stop for lunch near
# Moron') — a meal or stop the source puts NEAR a place, written as AT / IN it.
_NEAR_NAME_RE = re.compile(r"\bnear\s+((?:[A-ZÀ-ÖØ-Þ][\w’'-]+)(?:\s+[A-ZÀ-ÖØ-Þ][\w’'-]+)?)")
_STOP_WORDS_RE = r"(?:lunch|dinner|breakfast|picnic|meal|stop|stops|stopover|break)"
NEAR_AS_AT_PROBLEM = "puts the {what} in {name}, but the source says near it — say 'near {name}'"


def near_as_at(text: str, moments: list[Moment], cta: Optional[str] = None) -> list[str]:
    """Body sentences that put a meal / stop AT or IN a place the moment's trip lines put it NEAR. Accent-folded
    ('Mörön' = 'Moron'); a sentence that also says 'near <place>' is fine."""
    names: dict[str, str] = {}  # folded → as the source writes it
    for m in moments:
        for ln in m.trip_lines:
            src = ln.split("Source itinerary:", 1)[1] if "Source itinerary:" in ln else ""
            if re.search(rf"\b{_STOP_WORDS_RE}\b", src, re.I):
                names.update({fold(n.group(1)): n.group(1) for n in _NEAR_NAME_RE.finditer(src)})
    if not names:
        return []
    out = []
    for s in _body_spans(text, cta):
        clean = fold(s["clean"])
        for name, shown in names.items():
            if f"near {name}" in clean:
                continue
            hit = re.search(rf"\b({_STOP_WORDS_RE})\b[^.!?]{{0,40}}?\b(?:at|in)\s+(?:the\s+town\s+of\s+)?"
                            rf"{re.escape(name)}\b", clean)
            if hit:
                out.append(_quoted(NEAR_AS_AT_PROBLEM.format(what=hit.group(1), name=shown), s["clean"]))
                break
    return out


# ---------------------------------------------------------------- (b) times, ordinals, positions

_ORDINAL = r"(?:first|second|third|fourth|fifth|last|final|opening|closing)"
_PERIOD = r"(?:night|nights|evening|evenings|morning|mornings|afternoon|afternoons|day|dawn|sunrise|sunset)"
_TIME_RE = re.compile(
    rf"\b(?:the\s+)?{_ORDINAL}\s+{_PERIOD}\b"
    r"|\b(?:mid|early|late)[- ](?:morning|afternoon|evening)\b"
    # never after 'at': no one says 'at the afternoon' of a time of day — 'you arrive at the afternoon
    # sharp, not already running low' (S234 r1 #3, S233 #6) is energy, not a schedule
    r"|(?<!\b[Aa]t\s)\b(?:a|the|each|every|this|that|one)\s+(?:quiet\s+|slow\s+|early\s+|late\s+)?"
    r"(?:morning|afternoon|evening|dawn|sunrise|sunset|dusk)\b"
    r"|\b(?:in|by|until|before|after)\s+the\s+(?:morning|afternoon|evening)\b"
    r"|\b(?:at|by|before|after)\s+(?:dawn|dusk|sunrise|sunset|noon|midday|nightfall)\b", re.I)
_POSITION_RE = re.compile(
    r"\b(?:north|south|east|west|northern|southern|eastern|western|far|upper|lower)(?:most)?\s+"
    r"(?:tip|end|shore|shores|bank|rim)\b(?!\s+of\s+(?:a|the|this)\s+(?:journey|trip|route|day))", re.I)
_TIME_KEY_RE = re.compile(r"night|evening|morning|afternoon|dawn|sunrise|sunset|dusk|noon|midday|nightfall", re.I)


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", fold(text or "")))


# AA-789 (S234 r4 #11 'worn out by midday', a false F17 time claim) — figures of speech that carry a time word but
# give no schedule. A time phrase inside one is never a timing claim.
_TIME_IDIOM_RE = re.compile(
    r"\b(?:worn\s+out|tired(?:\s+out)?|exhausted|spent|flagging|done\s+in|bored|restless|hungry|flat)\s+"
    r"(?:by|before)\s+(?:noon|midday|lunch(?:time)?|the\s+afternoon|mid-?afternoon|the\s+evening|nightfall)\b"
    r"|\bat\s+the\s+end\s+of\s+the\s+day\b|\bday\s+in,?\s+day\s+out\b|\ball\s+day\s+long\b"
    r"|\bcall(?:s|ed|ing)?\s+it\s+a\s+day\b|\b(?:night\s+and\s+day|day\s+and\s+night)\b|\baround\s+the\s+clock\b"
    r"|\bthe\s+morning\s+after\b|\bmake\s+(?:a|your|their)\s+day\s+of\s+it\b", re.I)


def _in_idiom(sentence: str, m: re.Match) -> bool:
    return any(i.start() <= m.start() and m.end() <= i.end() for i in _TIME_IDIOM_RE.finditer(sentence))


_ORDINAL_NUM = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
# a source time word and the words a piece may use for it ('Overnight in a ger camp' → 'the evening')
_TIME_EQUIV = {"night": {"night", "overnight", "evening"}, "evening": {"evening", "overnight", "night"},
               "morning": {"morning"}, "afternoon": {"afternoon"}}
# meal and day-part anchors in the trip lines that place a stretch of the day: 'Time is left for
# swimming and resting before lunch' (Kuang Si) puts the trek and the swim in the morning, so 'a
# morning of trekking' / 'the morning' is the source's timing (S234 r1 #14, S233 #1, #10). They
# support a plain period only — 'mid-morning' / 'early afternoon' still need the word itself (r2 #11).
_TIME_ANCHORS = (("before lunch", "morning"), ("breakfast", "morning"), ("after lunch", "afternoon"),
                 ("dinner", "evening"), ("overnight", "evening"), ("overnight", "night"))
_QUALIFIED_TIME_RE = re.compile(r"\b(?:mid|early|late)[- ]", re.I)


def _supported_time(phrase: str, sentence: str, moments: list[Moment]) -> bool:
    """A time phrase is supported when the TRIP lines (of the moment the sentence names, else of
    any moment) carry its time word — and, for an ordinal ('first night'), the ordinal phrase. An
    ordinal DAY is supported by the moment's day number ('On the fourth day … Mörön', day 4)."""
    named = moments_named(sentence, moments) or moments
    trip = _norm(" ".join(ln for m in named for ln in m.trip_lines))
    om = re.match(rf"(?:the\s+)?({_ORDINAL})\s+(\w+)", phrase, re.I)
    if om:
        if om.group(2).lower() == "day" and om.group(1).lower() in _ORDINAL_NUM:
            return _ORDINAL_NUM[om.group(1).lower()] in {d for m in named for d in m.days}
        return _norm(re.sub(r"^the\s+", "", phrase, flags=re.I)) in trip
    key = _TIME_KEY_RE.search(phrase)
    if not key:
        return False
    words = set(trip.split())
    if not _QUALIFIED_TIME_RE.search(phrase):
        words |= {period for anchor, period in _TIME_ANCHORS if f" {anchor} " in f" {trip} "}
    return bool(_TIME_EQUIV.get(key.group(0).lower(), {key.group(0).lower()}) & words)


def _supported_position(phrase: str, sentence: str, moments: list[Moment]) -> bool:
    """A position ('the southern tip') is supported when a seed line has it AND the sentence shares
    at least two other content words with that line (so the place background 'Khatgal at the
    lake's southern tip' supports a sentence about Khatgal, not 'standing at the southern tip')."""
    want = _norm(phrase)
    pw = set(content_words(phrase))
    sw = set(content_words(sentence)) - pw
    for m in moments:
        for line in m.lines:
            if want in _norm(line) and len(sw & (set(content_words(line)) - pw - {"lake"})) >= 2:
                return True
    return False


def timing_claims(text: str, moments: list[Moment], cta: Optional[str] = None) -> list[str]:
    """Time-of-day, ordinal-night and position claims the seed does not give (S234 r2: 'the first
    night' #14/#19, 'a swim mid-morning' #11, 'standing at the southern tip' #14)."""
    if not moments:
        return []
    out = []
    for s in _body_spans(text, cta):
        for m in _TIME_RE.finditer(s["clean"]):
            if not _in_idiom(s["clean"], m) and not _supported_time(m.group(0), s["clean"], moments):
                out.append(_quoted(f"gives a time the source does not ('{m.group(0)}') — cut it or use the source's "
                                   "own timing", s["clean"]))
                break
        else:
            for m in _POSITION_RE.finditer(s["clean"]):
                if not _supported_position(m.group(0), s["clean"], moments):
                    out.append(_quoted(f"places the trip somewhere the source does not ('{m.group(0)}') — cut it",
                                       s["clean"]))
                    break
    return out


def unsupported_time_keys(text: str, moments: list[Moment], cta: Optional[str] = None) -> set[str]:
    """AA-777 (r6 fix 1) — every time / ordinal / position phrase of `text` the seed does not give, as a
    comparable key: the time word ('the afternoon', 'in the afternoon' → 'afternoon'), else the folded
    phrase ('first day', 'southern tip'). Unlike `timing_claims` it lists every phrase of a sentence, not
    the first, so a repair's new phrase is seen even when the sentence already had another one."""
    if not moments:
        return set()
    out: set[str] = set()
    for s in _body_spans(text, cta):
        for m in _TIME_RE.finditer(s["clean"]):
            if not _in_idiom(s["clean"], m) and not _supported_time(m.group(0), s["clean"], moments):
                key = _TIME_KEY_RE.search(m.group(0))
                out.add(key.group(0).lower() if key else _norm(re.sub(r"^the\s+", "", m.group(0), flags=re.I)))
        for m in _POSITION_RE.finditer(s["clean"]):
            if not _supported_position(m.group(0), s["clean"], moments):
                out.add(_norm(m.group(0)))
    return out


# ---------------------------------------------------------------- (d) family access caveat

# children, not families: 'private families, family offices' (Atlas & Hearth) is not a family brand
_CHILD_AUDIENCE_RE = re.compile(r"\b(?:child(?:ren)?|kids?|teens?|teenagers?|youngsters?)\b|\baged\s+\d", re.I)
_CAVEAT_RE = re.compile(
    r"\b(?:physically demanding|demanding|strenuous|arduous|difficult|challenging|steep|multi-hour"
    r"|(?:\w+\s+(?:to|-)\s+)?\w+\s+hours?\s+each\s+way|elevation gain|horse(?:back)?\s+or\s+reindeer\s+ride"
    r"|(?:horse|horseback|reindeer)\s+ride|boggy|crossing rivers|rough terrain|fitness)\b", re.I)
# Words that, anywhere in the piece, show the reader was told the access is hard.
_CAVEAT_ECHO_RE = re.compile(
    r"\b(?:physically\s+(?:demanding|challenging|hard|tough|tiring)|demanding|strenuous|arduous|steep|multi-hour"
    r"|hours?\s+each\s+way|\w+\s+hours?\b[^.]{0,40}?\b(?:ride|riding|horse|reindeer|hike|trek|walk)"
    r"|(?:horse|reindeer)\s+(?:or\s+(?:horse|reindeer)\s+)?rides?\b|by horse|elevation gain|boggy|crossing rivers"
    r"|fitness|(?:difficult|challenging|tough|hard|long)\s+(?:terrain|access|journey|ride|hike|trek|climb|walk"
    r"|going)|(?:difficult|hard)\s+to\s+reach)\b", re.I)


def audience_has_children(audience: Optional[str]) -> bool:
    return bool(_CHILD_AUDIENCE_RE.search(audience or ""))


def _caveat_line(moment: Moment) -> Optional[str]:
    lines = [ln for ln in moment.lines if _CAVEAT_RE.search(ln)]
    # the physical line first ('multi-hour horse or reindeer ride', 'physically demanding'); a
    # permit-only line is administrative and is not this rule's business
    return lines[0] if lines else None


def missing_family_caveat(text: str, moments: list[Moment], audience: Optional[str],
                          cta: Optional[str] = None) -> list[dict]:
    """For a brand whose audience includes children: every moment the piece pitches (names in its
    body) whose seed carries a physical access caveat ('access requires a multi-hour horse or
    reindeer ride', 'physically demanding travel', 'significant elevation gain') must have that
    caveat said somewhere in the piece. Returns [{"moment", "caveat", "sentence"}] — `sentence` is
    the body sentence a repair extends with one caveat sentence (the last one naming the moment
    outside the FAQ)."""
    if not audience_has_children(audience):
        return []
    sections = _sections(text)
    out = []
    for m in moments:
        caveat = _caveat_line(m)
        if caveat is None:
            continue
        naming = [sp for sec in sections for sp in sec
                  if sp["kind"] in ("prose", "bullet") and not sp["in_faq"] and names_moment(sp["clean"], m)
                  and not _is_cta_sentence(sp["clean"], cta)]
        if not naming:
            continue
        # the caveat must be said where the moment is: in a section (or FAQ pair) that names it — 'a
        # child who wants to be on horseback' at the lake does not tell a parent the Tsaatan ride is long
        zone = [sp for sec in sections if any(names_moment(x["clean"], m) for x in sec) for sp in sec]
        if any(_caveat_echoed(sp["clean"], caveat) for sp in zone):
            continue
        out.append({"moment": m.name, "caveat": caveat, "sentence": naming[-1]["clean"]})
    return out


def _caveat_terms(caveat: str) -> set[str]:
    """The caveat line's own _CAVEAT_RE terms, normalised ('multi-hour', 'boggy', 'rough terrain')."""
    return {_norm(m.group(0)) for m in _CAVEAT_RE.finditer(caveat or "")}


def _caveat_echoed(sentence: str, caveat: str) -> bool:
    """Is the access caveat said in `sentence`? The generic echo words, or one of the caveat line's
    own terms — so the seed's caveat, said in its own words, always clears the rule ('The trail
    crosses rough terrain.' matches no generic echo, S234 round 3 review)."""
    if _CAVEAT_ECHO_RE.search(sentence):
        return True
    have = f" {_norm(sentence)} "
    return any(f" {t} " in have for t in _caveat_terms(caveat))


_PRONOUN_OPEN_RE = re.compile(r"^(?:it|its|they|their|them|this|these|those|he|she|his|her)\b", re.I)
# where the caveat's own clause starts inside a line that opens on a pronoun: '…, and access requires
# a multi-hour horse or reindeer ride…'
_CAVEAT_CLAUSE_START_RE = re.compile(r"(?:,\s+and\s+|;\s+|,\s+but\s+)")


def caveat_sentence(caveat: str) -> Optional[str]:
    """The sentence a piece gets for a missing access caveat, in the seed's own words: the line
    itself when it names its subject ('The hike to the monastery … takes most visitors roughly two to
    three hours each way, with a significant elevation gain.'); for a line that opens on a pronoun
    ('Their territory lies deep in the taiga, …, and access requires a multi-hour horse or reindeer
    ride, often crossing rivers and boggy forest terrain.') the clause that carries the caveat
    ('Access requires a multi-hour horse or reindeer ride, often crossing rivers and boggy forest
    terrain.'). None when neither reads on its own."""
    line = " ".join((caveat or "").split()).strip()
    if not line:
        return None
    if line[-1] not in ".!?":
        line += "."
    if not _PRONOUN_OPEN_RE.match(line):
        return line
    first = _CAVEAT_RE.search(line)
    if not first:
        return None
    cuts = [m.end() for m in _CAVEAT_CLAUSE_START_RE.finditer(line, 0, first.start())]
    if not cuts:
        return None
    clause = line[cuts[-1]:].strip()
    if _PRONOUN_OPEN_RE.match(clause) or len(clause.split()) < 4:
        return None
    return clause[0].upper() + clause[1:]


def _caveat_anchor(text: str, moment: Moment, cta: Optional[str]) -> Optional[dict]:
    """Where the caveat goes: after the first body sentence that names the moment inside a section
    whose heading names it (the moment's own section), else after the first body sentence that
    names it — not a closing route summary that lists every stop."""
    heading_names = False
    first = own = None
    for sp in sentence_spans(text or ""):
        if sp["kind"] == "heading":
            heading_names = names_moment(sp["clean"], moment)
            continue
        if sp["kind"] not in ("prose", "bullet") or sp["in_faq"] or _is_cta_sentence(sp["clean"], cta):
            continue
        if names_moment(sp["clean"], moment):
            first = first or sp
            if heading_names and own is None:
                own = sp
    return own or first


def add_family_caveats(text: str, ctx: Optional["TripFactContext"], ignore: frozenset = frozenset()
                       ) -> tuple[str, list[str]]:
    """Deterministic fix for `missing_family_caveat` (S234 round 3 review): the seed's caveat
    (`caveat_sentence`) goes in right after the last body sentence that names the moment — no LLM
    call, and no repeat guard that could turn the 'keep this sentence and add one' repair into a cut
    of the anchor. An insertion the integrity check rejects (a new near-duplicate of a neighbour) is
    skipped and the finding is left to the sentence repair. Returns (text, log)."""
    from services.acp_content_writing.hygiene import integrity_problems
    if ctx is None:
        return text, []
    log: list[str] = []
    by_name = {m.name: m for m in ctx.moments}
    for miss in missing_family_caveat(text, ctx.moments, ctx.audience, ctx.cta):
        add = caveat_sentence(miss["caveat"])
        anchor = _caveat_anchor(text, by_name[miss["moment"]], ctx.cta)
        if not add or anchor is None:
            continue
        end = anchor["end"]
        tags = _TAG_RUN_RE.match(text, end)  # AA-912: shared tag body, matched in place (no tail copy)
        if tags:
            end = tags.end()
        trial = text[:end] + " " + add + text[end:]
        if integrity_problems(text, trial, ignore, ctx.cta):
            continue
        text = trial
        log.append(f"added the source's access caveat for {miss['moment']}: '{add[:160]}'")
    return text, log


def _is_cta_sentence(sentence: str, cta: Optional[str]) -> bool:
    from services.acp_content_writing.hygiene import _is_cta
    return _is_cta(sentence, cta)


def _sections(text: str) -> list[list[dict]]:
    """The piece's sentences grouped by H2/H3 section; inside the FAQ each question opens a group
    (a pair is its own section)."""
    groups: list[list[dict]] = [[]]
    for sp in sentence_spans(text or ""):
        if sp["kind"] == "heading" or (sp["kind"] == "faq_q"):
            groups.append([])
            if sp["kind"] == "heading":
                continue
        if sp["kind"] in ("prose", "bullet", "faq_q", "faq_a"):
            groups[-1].append(sp)
    return [g for g in groups if g]


# ---------------------------------------------------------------- rarity / quantifier guard

RARITY_CLAIM_RE = re.compile(
    r"\b(?:no|very few|few|almost no|hardly any)\s+(?:real\s+|true\s+|direct\s+)?(?:equivalents?|parallels?"
    r"|counterparts?)\b"
    r"|\bunlike\s+(?:almost\s+|nearly\s+|virtually\s+|practically\s+)?(?:anything|anywhere|any other)\b"
    r"|\b(?:almost\s+|nearly\s+|practically\s+)?(?:anywhere|nowhere|any\s?place|anything)\s+(?:else\s+)?on\s+"
    r"(?:earth|the planet)\b"
    # AA-789 (S234 r4 #17 'a landscape most of the world has never considered', #19 'a significant fraction' for
    # 0.4%, #4 'facts that are hard to overstate'): overclaims with no rarity word in them
    r"|\bmost\s+of\s+the\s+world\b|\bthe\s+(?:rest\s+of\s+the\s+)?world\s+has\s+(?:never|rarely|barely)\b"
    r"|\b(?:significant|substantial|sizeable|sizable|considerable)\s+(?:fraction|share|proportion|portion"
    r"|percentage)\b"
    r"|\b(?:hard|difficult|impossible)\s+to\s+overstate\b"
    r"|\b(?:almost\s+|nearly\s+|practically\s+)?nowhere\s+else\b"
    r"|\bcategorically\s+different\b"
    r"|\bno\s+other\s+(?:place|lake|culture|people|community|region|destination|landscape)\b"
    r"|\b(?:one of|among)\s+the\s+(?:last|few|only)\b"
    r"|\bthe\s+(?:last|only)\s+(?:remaining\s+|surviving\s+)?\w+(?:[- ]\w+)?\s+(?:of its kind|in the world|on earth)\b"
    r"|\bunparalleled\b|\bunmatched\b|\bunrivall?ed\b|\bincomparable\b"
    r"|\bunique\s+(?:in|on|to)\s+(?:the world|earth|asia|central asia)\b", re.I)
# 'one of the last' ≈ 'among the last'; 'on earth' ≈ 'in the world'
_RARITY_EQUIV = (("among the", "one of the"), ("on earth", "in the world"), ("’s", "'s"))
# 'one of the world's most distinctive living cultures' — supported only when the seed has a
# 'one of / among … most' superlative with the same scope ('one of Mongolia's most distinct ethnic
# groups' does not support 'the world's'; 'one of Bhutan's most revered' supports '… sites in Bhutan')
_SUPERLATIVE_RE = re.compile(
    r"\b(?:one of|among)\s+(?:the\s+)?(?:([A-Za-zÀ-ÿ]+)(?:'s|’s)\s+)?(?:most|least)\s+[\w-]+"
    r"(?:[^.;:—–]{0,60}?\b(?:in|of)\s+(?:all\s+of\s+)?(?:the\s+)?((?:[A-Z][\w-]+\s?){1,2}))?")
_QUANT_RE = re.compile(
    r"(?<!\bthe\s)(?<!\bone of the\s)(?<!['’]s\s)(?<!\bits\s)(?<!\btheir\s)\b(most|all|every)\s+"
    r"(?:other\s+|of\s+(?:the\s+)?)?([a-z][a-z-]+)", re.I)
_SEED_QUANT_RE = re.compile(
    r"\b(many|some|several|numerous|a number of)\s+(?:other\s+|of\s+(?:the\s+)?)?([a-z][a-z-]+)", re.I)
_PEOPLE_NOUNS = frozenset({"travellers", "travelers", "visitors", "people", "families", "guests", "tourists",
                           "parents"})


def _superlative_word(match: str) -> str:
    """'one of Mongolia's most distinct ethnic groups' → 'distinct' (the word after most / least, folded)."""
    m = re.search(r"\b(?:most|least)\s+([\w-]+)", match or "", re.I)
    return fold(m.group(1)) if m else ""


def _norm_rarity(text: str) -> str:
    t = fold(text)
    for a, b in _RARITY_EQUIV:
        t = t.replace(a, b)
    return " ".join(re.findall(r"[a-z0-9']+", t))


def rarity_claims(text: str, source: str, cta: Optional[str] = None,
                  extra_texts: tuple = ()) -> list[str]:
    """Rarity / uniqueness claims the source does not make, and quantifiers stronger than the
    source's ('many other reindeer-herding traditions' → 'most other …'). `source` = the moment's
    own seed lines + Facts. `extra_texts` (title, meta) are checked as single sentences and their
    findings start with "title " / "meta "."""
    src = _norm_rarity(source)
    seed_quants: dict[str, str] = {}
    for m in _SEED_QUANT_RE.finditer(source or ""):
        seed_quants.setdefault(m.group(2).lower()[:6], m.group(1).lower())
    seed_scopes = {fold((m.group(1) or m.group(2) or "").strip()) for m in _SUPERLATIVE_RE.finditer(source or "")}
    seed_supers = {_superlative_word(m.group(0)) for m in _SUPERLATIVE_RE.finditer(source or "")}
    spans = [(s["clean"], "") for s in _body_spans(text, cta)]
    spans += [(t, f"{where} ") for where, t in extra_texts if t]
    out = []
    for sentence, prefix in spans:
        problem = None
        for m in RARITY_CLAIM_RE.finditer(sentence):
            if _norm_rarity(m.group(0)) not in src:
                problem = f"makes a rarity claim the source does not ('{m.group(0)}') — keep to what the source says"
                break
        if problem is None:
            for m in _SUPERLATIVE_RE.finditer(sentence):
                scope = fold((m.group(1) or m.group(2) or "").strip())
                # AA-789 (S234 r4 #16 'one of the most coherent and varied family itineraries') — a superlative
                # with no scope is backed by the source's superlative only when it is the same one ('one of the
                # most distinct' ← seed 'one of Mongolia's most distinct'), not by any superlative in the seed
                if not scope and seed_scopes and _superlative_word(m.group(0)) in seed_supers:
                    continue
                if scope not in seed_scopes or not scope:
                    problem = (f"makes a rarity claim the source does not ('{m.group(0).strip()[:60]}') — keep to "
                               "what the source says")
                    break
        if problem is None:
            for m in _QUANT_RE.finditer(sentence):
                quant, noun = m.group(1).lower(), m.group(2).lower()
                if quant == "every" and noun not in _PEOPLE_NOUNS:
                    continue
                if re.search(rf"\b{quant}\s+(?:other\s+|of\s+(?:the\s+)?)?{re.escape(noun[:6])}", src):
                    continue
                weaker = seed_quants.get(noun[:6])
                if weaker:
                    problem = (f"says '{m.group(0)}' where the source says '{weaker}' — keep the source's "
                               f"'{weaker}'")
                    break
                if noun in _PEOPLE_NOUNS:
                    problem = f"generalises about '{m.group(0)}', which the source does not — cut it"
                    break
        if problem:
            out.append(prefix + _quoted(problem, sentence))
    return out


# ---------------------------------------------------------------- date authority

_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
_UNTIL_RE = re.compile(r"\buntil\s+(?:the\s+year\s+)?(1[0-9]{3}|20[0-9]{2})\b", re.I)
_ROLE_START_RE = re.compile(
    r"\b(?:from|since|became|made|declared|designated|named)\b[^.;]*?\b(1[0-9]{3})\b(?!\s*[–-])", re.I)
_START_RE = re.compile(r"\b(?:built|founded|established|constructed|completed)\b[^.]*?\b(1[0-9]{3})\b", re.I)
_GOV_RE = re.compile(r"\b(?:capital|seat of government|seat of power|government(?:al)?|administrative|administration"
                     r"|political)\b", re.I)
_PRESENT_RE = re.compile(
    r"\b(?:remains?|still|today|nowadays|continues?|serves?|functions?|merges?|combines?|unites?|houses?|active)\b"
    r"|\bis\s+(?:now\s+|still\s+)?(?:the|its|[A-Z]\w+(?:'s|’s))\b", re.I)
# past tense, or a purpose of the founding ('placed it there to function as the administrative …')
_PAST_RE = re.compile(r"\b(?:until|was|were|served|had|former(?:ly)?|once|used to|historic(?:al(?:ly)?)?|became"
                      r"|would|established|founded|built|chosen|placed|intended|designed|meant"
                      r"|to (?:function|serve|act|be)|for centuries|for over|for nearly|for almost"
                      r"|for more than)\b", re.I)
# AA-778 — ': ' too: hygiene.fix_dashes turns most single dashes into a colon before the gates, so
# '... until 1955: today it remains ...' must still split there. The trailing space keeps '10:30' whole.
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|:\s|—|–|\s-\s|,\s*(?:and|but|while|yet)\s+|\bbut\b"
                              r"|,\s+(?=founded|built|still))\s*", re.I)
_SPAN_RE = re.compile(
    r"\b(nearly|almost|just under|close to|over|more than|just over|about|around|roughly|some)?\s*"
    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(centuries|hundred years)\b", re.I)
_SMALL = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10}


# where the role went: '… until 1955, when the capital was moved to Thimphu'; review round — also 'when Thonburi
# became the capital', 'replaced by Hanoi', 'the capital is now Naypyidaw'
_SUCC_NAME = r"([A-ZÀ-ÖØ-Þ][\w’'-]+(?:\s+[A-ZÀ-ÖØ-Þ][\w’'-]+)?)"
_SUCCESSOR_RE = re.compile(
    rf"\b(?:moved|transferred|relocated|shifted)\s+to\s+{_SUCC_NAME}"
    rf"|\breplaced\s+by\s+{_SUCC_NAME}"
    rf"|\b{_SUCC_NAME}\s+(?:became|was\s+made|was\s+declared)\s+the\s+(?:new\s+)?(?:national\s+)?capital"
    rf"|\bthe\s+(?:new\s+)?capital\s+(?:is\s+now|has\s+since\s+been|became)\s+{_SUCC_NAME}")


def _successor(text: str) -> Optional[str]:
    for m in _SUCCESSOR_RE.finditer(text):
        name = next(g for g in m.groups() if g)
        if fold(name.split()[0]) not in COMMON_CAPS:
            return name
    return None


@dataclass(frozen=True)
class EndedRole:
    role: str       # 'the capital and seat of government of Bhutan'
    start: Optional[int]
    end: int
    successor: Optional[str] = None  # 'Thimphu' of '… until 1955, when the capital was moved to Thimphu'
    # AA-985 round 2 — WHO held the role: the proper-noun subject of the seed line ('Punakha Dzong served as …'),
    # else the moment's own name; `aliases` are the names a sentence may use for it ('Punakha Dzong', 'Punakha')
    entity: Optional[str] = None
    aliases: tuple = ()


def _role_start(lines: list[str]) -> Optional[int]:
    """The year the governmental role began, only when the seed SAYS so in one sentence: 'from <year>'
    or 'became … in <year>' next to the role word, or a founding year in a sentence that founds the
    place FOR the role ('built in 1637–1638 … who founded it to serve as the administrative and
    religious centre'). A building's founding year alone is not the role's start (S234 round 3
    review: 'Built in 1647 … became the seat of government in 1907 … until 1955' is 48 years, not 308)."""
    for ln in lines:
        for sent in re.split(r"(?<=[.;])\s+", ln):
            if not _GOV_RE.search(sent):
                continue
            m = _ROLE_START_RE.search(sent) or _START_RE.search(sent)
            if m:
                return int(m.group(1))
    return None


# AA-985 round 2 — singular place nouns a definite NP may use for the entity ('the dzong', 'this fortress'), and
# the plural / class forms that make a sentence a statement about the CLASS ('dzongs', 'a dzong', 'like all dzongs')
_PLACE_KINDS = frozenset({"dzong", "fortress", "fortress-monastery", "monastery", "lhakhang", "goemba", "gompa",
                          "temple", "palace", "castle", "fort", "citadel", "building", "complex", "structure",
                          "town", "city", "site"})
_PLACE_KIND_PLURALS = frozenset({"dzongs", "fortresses", "fortress-monasteries", "monasteries", "lhakhangs",
                                 "goembas", "gompas", "temples", "palaces", "castles", "forts", "citadels", "buildings",
                                 "complexes", "structures", "towns", "cities", "sites"})
# the head word of a named place another sentence may mention ('Mongar Dzong', 'Kuensel Phodrang Nature Park')
_PLACE_HEADS = ("Dzong|Fortress|Monastery|Lhakhang|Goemba|Gompa|Temple|Palace|Castle|Fort|Citadel|Park|Valley"
                "|Village|Town|City|District|Province|Museum|Bridge|Pass|Chorten|Stupa|Lake|Square|Market|Hall"
                "|Institute|Corporation|Centre|Center|Nunnery|Gate|Garden|Gardens")
_NAME_WORD = r"[A-ZÀ-ÖØ-Þ][\w’'-]*"
_ROLE_SUBJECT_RE = re.compile(rf"((?:{_NAME_WORD}\s+){{0,4}}{_NAME_WORD})\s+(?:served|was|acted)\s+as\b")


def _name_aliases(name: str) -> list[str]:
    """'Punakha Dzong (Pungtang Dewachen Phodrang)' → ['Punakha Dzong', 'Pungtang Dewachen Phodrang', 'Punakha']:
    the name, a parenthesised other name, and the name without its place-kind head word."""
    out = []
    for part in [re.sub(r"\(.*?\)", "", name).strip()] + re.findall(r"\(([^)]+)\)", name):
        part = part.strip(" ,")
        if not part or not part[:1].isupper():
            continue
        out.append(part)
        words = part.split()
        if len(words) >= 2 and words[-1].lower() in _PLACE_KINDS and words[-2][:1].isupper():
            short = " ".join(words[:-1])
            # 'Imperial City' → not 'Imperial': a lone generic adjective names nothing (review round)
            if not (len(words) == 2 and (fold(short) in COMMON_CAPS or fold(short) in _GENERIC_NAME_WORDS)):
                out.append(short)
    return out


_GENERIC_NAME_WORDS = frozenset({"imperial", "royal", "grand", "great", "old", "new", "holy", "sacred", "forbidden",
                                 "golden", "ancient", "national", "central", "upper", "lower", "main", "inner",
                                 "outer", "summer", "winter", "historic", "historical"})


def _location_parts(m: "Moment") -> list[str]:
    return [p.strip() for p in re.sub(r"\(.*?\)", "", m.location[len("Location:"):]).split(",") if p.strip()]


def _moment_names(m: "Moment") -> list[str]:
    """The moment's own names: its label name, the first part of its Location line, its quoted Naming aliases."""
    names = _name_aliases(m.name)
    parts = _location_parts(m)
    if parts:
        names += _name_aliases(parts[0])
    names += [q for q in re.findall(r"'([^']+)'", m.naming) if q[:1].isupper()]
    return names


def _role_entity(m: "Moment", before_until: str) -> tuple[Optional[str], tuple]:
    """Who held the ended role: the proper-noun subject of the line that dates it ('Punakha Dzong served as …
    until 1955'); when that line opens on a pronoun ('It served as …'), the moment itself (its label name, the
    first part of its Location line, its quoted Naming aliases). Review round: when the proper subject IS the moment
    or the place the moment sits in ('Hue served as …' for the Imperial City, Location '…, Hue, …'), the moment's
    own names are names for the entity too."""
    own = _moment_names(m)
    for sm in _ROLE_SUBJECT_RE.finditer(before_until):
        words = sm.group(1).split()
        while words and fold(words[0]) in COMMON_CAPS:
            words = words[1:]
        if words:
            name = " ".join(words)
            aliases = _name_aliases(name)
            if fold(name) in {fold(n) for n in own} | {fold(p) for p in _location_parts(m)}:
                aliases += own
            return name, tuple(dict.fromkeys(aliases))
    return (m.name if own else None), tuple(dict.fromkeys(own))


def ended_roles(seed: Optional[str]) -> list[EndedRole]:
    """Roles the seed dates as ended: a governmental role ('capital', 'seat of government',
    'administrative') in a sentence with 'until <year>'. The start is `_role_start` of the same
    moment block (None when the seed does not date the role's beginning)."""
    out = []
    for m in parse_moments(seed):
        lines = m.lines
        start = _role_start(lines)
        for ln in lines:
            um = _UNTIL_RE.search(ln)
            if not um or not _GOV_RE.search(ln[:um.start()]):
                continue
            rm = re.search(r"\b(?:served|was|acted)\s+as\s+(.+?)\s+until\b", ln, re.I)
            role = rm.group(1).strip() if rm else "the seat of government"
            successor = _successor(ln[um.end():])
            entity, aliases = _role_entity(m, ln[:um.start()])
            out.append(EndedRole(role=role, start=start, end=int(um.group(1)),
                                 successor=successor, entity=entity, aliases=aliases))
    return out


def _span_contradicts(qual: Optional[str], n: int, years: int) -> bool:
    target = n * 100
    q = (qual or "").lower()
    if q in ("nearly", "almost", "just under", "close to"):
        return not (target - 60 <= years < target)
    if q in ("over", "more than", "just over"):
        return not (target < years <= target + 99)
    return abs(years - target) > 50


# 'capital' / 'seat of government' / 'governmental' always name the ended role; 'administrative' only
# with a national scope — 'administrative and spiritual life has grown around' (S233 #12) is not a
# claim that the dzong is still Bhutan's administrative centre (it is still a district seat).
_GOV_STRONG_RE = re.compile(r"\b(?:capital|seat of government|seat of power|government(?:al)?)\b", re.I)
_GOV_WEAK_RE = re.compile(r"\b(?:administrative|administration|political)\b", re.I)


def _seed_country(seed: Optional[str]) -> Optional[str]:
    for ln in split_seed(seed)[0].splitlines():
        if ln.startswith("Location:"):
            parts = [p.strip() for p in re.sub(r"\(.*?\)", "", ln[len("Location:"):]).split(",") if p.strip()]
            return parts[-1] if parts else None
    return None


# a district / local government is not the national role the seed dates as ended: 'The dzong still
# houses the district government offices' is true of Punakha today (S234 round 3 review)
_LOCAL_UNIT = r"(?:district|local|dzongkhag|provincial|regional|municipal|county|village|town)"
_LOCAL_GOV_RE = re.compile(
    rf"\b{_LOCAL_UNIT}\s+(?:[\w-]+\s+)?"
    r"(?:government(?:al)?|administration|administrative|offices?|seat)\b", re.I)
# AA-985 — the same local role told the other way round: 'government offices for district administration',
# 'the seat of government for the district', 'the administrative centre of Punakha District'
_LOCAL_OF_RE = re.compile(
    r"\b(?:government(?:al)?|administration|administrative|offices?|seat|capital|headquarters|cent(?:re|er))\b"
    r"(?:\s+[\w’'-]+){0,3}?\s+(?:of|for)\s+(?:the\s+|its\s+|this\s+|(?-i:[A-Z])[\w’'-]+\s+){0,2}"
    r"(?:district|dzongkhag|province|region|municipality|county)\b", re.I)
# AA-985 — a building that HOSTS government offices / officials does not claim to BE the seat of government:
# 'it houses both monk bodies and government offices' names a function, not the ended role. Ministries and
# agencies are not on the list: hosting the ministries is being the seat of government (review round).
_GOV_HOSTED_RE = re.compile(
    r"\bgovernment(?:al)?\s+(?:offices?|officials?|departments?|staff|employees|workers|functions|bodies"
    r"|buildings?|business)\b", re.I)
# AA-985 — a 'capital' of something other than government is a different role ('the spiritual capital')
_OTHER_CAPITAL_RE = re.compile(
    r"\b(?:spiritual|cultural|religious|monastic|buddhist|culinary|food|tourism|tourist|adventure|trekking|rice"
    r"|arts?|craft|commercial|economic|financial|festival|textile)\s+capital\b", re.I)
# AA-985 round 2 — 'the capital' as a place on the map ('on a forested hillside above the capital', 'a drive from the
# capital') is where something is, not a role it holds; 'government-run' / '-owned' names who runs something else
_CAPITAL_PLACE_RE = re.compile(
    r"\b(?:from|to|towards?|above|below|beyond|near|outside|around|past|into|overlooking|across|in|within|at"
    r"|(?:north|south|east|west)\s+of)\s+(?:the\s+)?(?:national\s+)?capital(?:\s+city)?\b", re.I)
_GOV_RUN_RE = re.compile(
    r"\bgovernment-(?:run|owned|funded|built|backed|sponsored|operated|managed|supported|subsidi[sz]ed)\b", re.I)
_NATIONAL_SCOPE = r"national|the country(?:'s|’s)?|the kingdom(?:'s|’s)?|of the (?:country|kingdom)"


def _scope_alt(country: Optional[str]) -> str:
    scope = _NATIONAL_SCOPE
    if country:
        scope += rf"|{re.escape(country)}(?:'s|’s)?"
    return scope


def _national_scope(clause: str, country: Optional[str]) -> bool:
    return bool(re.search(rf"\b(?:{_scope_alt(country)})", clause, re.I))


def _local_role(role: Optional[EndedRole]) -> bool:
    """The ended role is itself a local one ('served as the district administrative centre until 1990')."""
    return bool(role and (_LOCAL_GOV_RE.search(role.role) or _LOCAL_OF_RE.search(role.role)))


def _strip_other_roles(clause: str, country: Optional[str]) -> tuple[str, bool, bool, bool]:
    """The clause with every phrase that names a DIFFERENT role removed: local governance phrases in
    either word order, a non-governmental 'capital', hosted government offices. Returns (rest, local_scoped,
    hosted, national). A local phrase coordinated with the national scope stays ('the government of the
    district and the kingdom' is also the kingdom's government)."""
    national = _national_scope(clause, country)
    coordinated = re.compile(rf"\s*(?:and|&)\s+(?:of\s+|for\s+)?(?:{_scope_alt(country)})", re.I)

    def _drop(m: re.Match) -> str:
        return m.group(0) if coordinated.match(m.string, m.end()) else " "

    local_scoped = bool(_LOCAL_GOV_RE.search(clause) or _LOCAL_OF_RE.search(clause))
    rest = _LOCAL_OF_RE.sub(_drop, _LOCAL_GOV_RE.sub(_drop, clause))
    rest = _OTHER_CAPITAL_RE.sub(" ", rest)
    rest = _CAPITAL_PLACE_RE.sub(" ", rest)
    rest = _GOV_RUN_RE.sub(" ", rest)
    hosted = bool(_GOV_HOSTED_RE.search(rest))
    rest = _GOV_HOSTED_RE.sub(" ", rest)
    return rest, local_scoped, hosted, national


def role_claim(clause: str, country: Optional[str], role: Optional[EndedRole] = None) -> bool:
    """AA-985 — does this clause name the SAME governmental role the seed dates as ended (role identity,
    not place identity)? A present-tense clause about the place holding a different, still-current role
    is not a claim of the ended one: the district administration it still houses, the government offices
    it hosts, its standing as a 'spiritual capital'. Those phrases are removed first (`_strip_other_roles`);
    what is left names the national role by 'capital' / 'seat of government' / bare 'government(al)', or by
    'administrative' / 'political' / hosted government offices with a national scope ('Bhutan's',
    'national', 'the kingdom's'). A local phrase does not hide a strong term beside it: 'It remains the
    capital and the district seat' still claims the capital. When the ended role is itself local, a clause
    naming a local governance role is that role."""
    if _local_role(role):
        return bool(_LOCAL_GOV_RE.search(clause) or _LOCAL_OF_RE.search(clause))
    rest, _local, hosted, national = _strip_other_roles(clause, country)
    if _GOV_STRONG_RE.search(rest):
        return True
    if not (_GOV_WEAK_RE.search(rest) or hosted):
        return False
    return national


# ---------------------------------------------------------------- AA-985 round 2: entity binding
#
# Role identity (above) asks WHICH role a clause claims; entity binding asks WHOSE. The S248 census found the
# rule still firing on statements about the class ('A dzong is … the administrative headquarters of a
# Bhutanese district', 'In Bhutan, dzongs house government offices'), about other places ('Kuenselphodrang, on
# a forested hillside above the capital', 'Khaling, where the National Handloom Development Corporation operates
# a government-run weaving centre') and about other subjects ('The weavers at the government looms'). A clause
# now claims the ended role only when its subject is the entity the seed dates it for: its name or an alias, or
# a singular pronoun / definite NP ('it', 'the dzong', 'its role') whose antecedent is that entity.

ENTITY, OTHER, GENERIC = "entity", "other", "generic"

_PRON_SINGULAR = frozenset({"it", "its"})
_PRON_PLURAL = frozenset({"they", "their", "them", "these", "those", "theirs"})
_PRON_PERSON = frozenset({"he", "she", "his", "her", "we", "our", "you", "your", "i", "my", "us"})
_DET_GENERIC = frozenset({"a", "an", "every", "each", "any", "all", "most", "many", "some", "no", "such", "both",
                          "several", "few", "other", "another", "certain"})
_DET_DEFINITE = frozenset({"the", "this", "that"})
_CLASS_NOUNS = frozenset({"type", "types", "kind", "kinds", "class", "form", "forms", "style", "category"})
_RELATIONAL_NOUNS = frozenset({"purpose", "role", "roles", "function", "status", "mission", "legacy", "position",
                               "standing", "duty", "duties", "authority", "importance"})
_GOV_HEADS = frozenset({"government", "governments", "parliament", "assembly", "king", "monarch", "monarchy",
                        "cabinet", "ministries", "ministry", "seat", "capital", "administration", "court", "throne"})
_NP_STOPS = frozenset("""of in at on for with from alongside above below near beside inside within to into by across
around along beneath under over behind through throughout and or is are was were be been being has have had remains
remain remained still today now also continues continue continued serves served houses housed stands stand sits sit
holds hold hosts host keeps keep retains retain became becomes operates operate produces produce reflects reflect
lies rises occupies occupy meets meet governs govern shares share offers offer functions embodies can could will
would may might must does do did""".split())
# a leading phrase that is not the subject: a preposition / participle phrase ('In Bhutan,', 'From Dewathang,',
# 'Built in 1637,', 'For nearly three centuries'), or a linking / time word ('and how', 'today', 'still')
_LEAD_WORD_RE = re.compile(
    r"^(?:and|but|yet|so|or|then|how|why|whereas|because|although|though|today|now|still|nowadays|also|indeed"
    r"|thus|here|there|even\s+today|even\s+now|to\s+this\s+day)\b,?\s*", re.I)
_LEAD_PREP = frozenset("""like unlike in on at from for during after before since across throughout within beyond
above below near among amid despite under over through by with without inside outside as beside along around
built founded set perched located situated standing rising nestled completed constructed seen viewed approached
once""".split())
_CLASS_LEAD = frozenset({"like", "as"})  # 'Like all dzongs, …' / 'As with every dzong, …' — a class statement
_KIND_PLURAL_ALT = "|".join(sorted(_PLACE_KIND_PLURALS, key=len, reverse=True))
# a class comparison anywhere in the clause: 'exactly as the older western dzongs do', 'in active dzongs today'
_CLASS_RE = re.compile(
    rf"\b(?:like|as(?:\s+(?:with|in))?)\s+(?:(?:all|every|most|many|other|the)\s+)(?:(?!of\b)[\w-]+\s+){{0,3}}?"
    rf"(?:{_KIND_PLURAL_ALT})\b"
    rf"|\b(?:in|across|throughout)\s+(?:all|every|active|other|most|many)\s+(?:[\w-]+\s+){{0,2}}?"
    rf"(?:{_KIND_PLURAL_ALT})\b", re.I)
_SPAN_ABOUT_RE = re.compile(rf"{_GOV_RE.pattern}|\bserved\b", re.I)
_RELATIVE_RE = re.compile(r",\s+(?=(?:which|where|who|whose)\b)", re.I)
_WORD_RE = re.compile(r"[\w’'-]+|[,;:]")
_EMBEDDED_IT_RE = re.compile(
    r"\bit\s+(?:is|remains|still|continues|serves|houses|functions|stays|retains|keeps|holds|hosts)\b", re.I)
_APPOSITIVE_NAME_RE = re.compile(rf"^[^,]{{1,60}},\s+((?:{_NAME_WORD}\s+){{0,4}}{_NAME_WORD})\s*,")
# review round — an alias run on into another capitalised word is a longer, different name ('Punakha Tshechu')
_CAP_NEXT_RE = re.compile(r"[ \t]+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]")
_UPPER_RE = re.compile(r"[A-ZÀ-ÖØ-Þ]")


def _possessive_base(word: str) -> str:
    return re.sub(r"(?:'s|’s|'|’)$", "", word, flags=re.I)


@lru_cache(maxsize=2048)
def _fold_map(text: str) -> tuple[str, tuple]:
    """`fold(text)` and, for each folded character, the index of the original character it came from."""
    out, idx = [], []
    for i, c in enumerate(text):
        f = fold(c)
        out.append(f)
        idx.extend([i] * len(f))
    return "".join(out), tuple(idx)


@dataclass(frozen=True)
class _Binder:
    """What a sentence may call the entity (`alias_re`, over fold()ed text), the other places it may mention instead
    (`other_re`), the entity's own kind word ('dzong'), and the country (a subject 'Bhutan's government' is the
    government)."""
    alias_re: Optional[re.Pattern]
    other_re: Optional[re.Pattern]
    kind: Optional[str]
    country: Optional[str]
    other_names: frozenset = frozenset()

    def alias_spans(self, text: str) -> list[tuple[int, int]]:
        """(start, end) of every entity alias in `text`, matched blind to case and accents ('Huế', 'Kyōto',
        'PUNAKHA DZONG'). A lower-case match of a one-word alias counts only in all-lower-case text ('the hue of
        the river' is not Hue); an alias run on into another capitalised word is another name ('Punakha
        Tshechu', 'Punakha Valley')."""
        if not self.alias_re or not text:
            return []
        folded, idx = _fold_map(text)
        lower_text = not _UPPER_RE.search(text)
        out = []
        for m in self.alias_re.finditer(folded):
            a, z = idx[m.start()], idx[m.end() - 1] + 1
            span = text[a:z]
            if not (span[:1].isupper() or " " in span or lower_text):
                continue
            if not span.isupper() and _CAP_NEXT_RE.match(text, z):
                continue
            out.append((a, z))
        return out

    def starts_with_alias(self, text: str) -> bool:
        return any(a == 0 for a, _z in self.alias_spans(text.lstrip()))

    def is_alias(self, words: list[str]) -> bool:
        """The name run `words` is the entity: it opens on an alias, or ends on one ('Hue Imperial City')."""
        text = " ".join(_possessive_base(w) for w in words)
        return any(a == 0 or z == len(text) for a, z in self.alias_spans(text))

    def mentions(self, text: str) -> list[tuple[int, int, str, str]]:
        """(start, end, kind, matched text) of every entity / other-place mention, in order; an other-place match
        overlapping an entity match is the entity ('Punakha Dzong' is both)."""
        ent = self.alias_spans(text)
        found = [(a, z, ENTITY, text[a:z]) for a, z in ent]
        for m in self.other_re.finditer(text) if self.other_re else ():
            start, words = m.start(), m.group(0).split()
            while len(words) > 1 and fold(words[0]) in COMMON_CAPS:  # 'The Dzong', 'From Mongar Dzong'
                start = text.index(words[1], start + len(words[0]))
                words = words[1:]
            if len(words) < 2 and words[0] not in self.other_names:
                continue
            if not any(a < m.end() and start < z for a, z in ent):
                found.append((start, m.end(), OTHER, text[start:m.end()]))
        return sorted(found)


def _binder(seed: Optional[str], role: EndedRole, country: Optional[str]) -> _Binder:
    aliases = [a for a in role.aliases if a]
    folded = {fold(a) for a in aliases}
    alias_re = (re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(a) for a in sorted(folded, key=len, reverse=True))
                           + r")(?![\w-])") if aliases else None)
    others = [role.successor] if role.successor else []
    for m in parse_moments(seed):
        others += [n for n in _name_aliases(m.name) if fold(n) not in folded]
    others = [o for o in dict.fromkeys(others) if o and fold(o) not in folded and fold(o) != fold(country or "")]
    alt = [re.escape(o) for o in sorted(others, key=len, reverse=True)]
    alt.append(rf"(?:{_NAME_WORD}\s+){{1,4}}(?:{_PLACE_HEADS})")
    other_re = re.compile(r"\b(?:" + "|".join(alt) + r")(?![\w-])")
    head = (role.entity or "").split()
    kind = head[-1].lower() if head and head[-1].lower() in _PLACE_KINDS else None
    return _Binder(alias_re=alias_re, other_re=other_re, kind=kind, country=country, other_names=frozenset(others))


def _strip_lead(clause: str) -> tuple[str, bool]:
    """The clause without its leading non-subject phrases (always a suffix of `clause.strip()`), and whether one of
    them was a class comparison ('Like all dzongs in Bhutan, it houses …'). 'Unlike other dzongs, Punakha Dzong …'
    is a contrast, not a class statement."""
    s = clause.strip()
    generic = False
    for _ in range(4):
        m = _LEAD_WORD_RE.match(s)
        if m and m.end():
            s = s[m.end():]
            continue
        words = s.split()
        first = words[0].lower().strip(",") if words else ""
        if first not in _LEAD_PREP:
            break
        comma = s.find(",")
        if 0 < comma <= 100 and ":" not in s[:comma]:
            lead = s[:comma]
            if first in _CLASS_LEAD and re.search(rf"\b(?:{_KIND_PLURAL_ALT})\b|\b(?:all|every)\b", lead, re.I):
                generic = True
            s = s[comma + 1:].lstrip()
            continue
        # no comma: the phrase's object runs until a pronoun, a determiner or a new name after it
        # ('For nearly three centuries it …', 'In 1955 the capital …', 'Inside Punakha Dzong the government …')
        toks = list(re.finditer(r"\S+", s))
        k = 1
        while k < len(toks) and k <= 8:
            w = toks[k].group(0)
            lw = w.lower().strip(",")
            if k >= 2 and (lw in _PRON_SINGULAR | _PRON_PLURAL | _PRON_PERSON | _DET_DEFINITE | {"a", "an"}
                           or (w[:1].isupper() and not toks[k - 1].group(0)[:1].isupper())):
                break
            k += 1
        if k >= len(toks) or k > 8:
            break
        s = s[toks[k].start():]
    return s, generic


def _of_alias(toks: list[str], b: _Binder) -> bool:
    """'… in Kyoto' / '… of Luang Prabang' right after a named head: the head is part of the entity."""
    return len(toks) >= 2 and toks[0].lower() in ("in", "of") and b.starts_with_alias(" ".join(toks[1:7]))


def _subject(s: str, b: _Binder) -> tuple[str, Optional[str]]:
    """(category, kind word) of the subject that opens `s` (leading phrases already removed):
    ENTITY / OTHER / GENERIC, 'topic' (a pronoun or definite NP to resolve against the antecedent), 'gov'
    (the government / the seat of government itself is the subject: bound only by a place reference)."""
    toks = _WORD_RE.findall(s)
    if not toks or toks[0] in ",;:":
        return "topic", None
    if b.starts_with_alias(s):  # any case, any accents: 'Huế is …', 'punakha dzong is …'
        return ENTITY, None
    first, lw = toks[0], toks[0].lower()
    # a descriptive subject named by an appositive: 'The capital's dzong, Tashichho Dzong, still houses …',
    # 'Bhutan's oldest dzong, Punakha Dzong, is …' — the name decides
    app = _APPOSITIVE_NAME_RE.match(s)
    if app and (lw in _DET_DEFINITE or (b.country and fold(_possessive_base(first)) == fold(b.country))):
        return (ENTITY if b.is_alias(app.group(1).split()) else OTHER), None
    if lw in _PRON_SINGULAR:
        return "topic", None
    if lw in _PRON_PLURAL or lw in _DET_GENERIC:
        return GENERIC, None
    if lw in _PRON_PERSON:
        return OTHER, None
    if lw in _DET_DEFINITE:
        np = []
        for t in toks[1:6]:
            if t in ",;:" or t.lower() in _NP_STOPS:
                break
            np.append(t)
        if not np:
            return "topic", None  # 'This is …', 'That remains …'
        # the NP ends at a (non-possessive) government head: 'the national government works from …'
        gov_at = next((n for n, t in enumerate(np) if t.lower() in _GOV_HEADS), None)
        if gov_at is not None:
            np = np[:gov_at + 1]
        low = [_possessive_base(t).lower() for t in np]
        if any(w in _PLACE_KIND_PLURALS or w in _CLASS_NOUNS for w in low):
            return GENERIC, None
        names = [t for t in np if t[:1].isupper() and _possessive_base(t).lower() not in _PLACE_KINDS]
        if names:
            # 'The Imperial Palace in Kyoto', 'The Royal Palace of Luang Prabang' (review round)
            if b.is_alias(np[np.index(names[0]):]) or _of_alias(toks[1 + len(np):], b):
                return ENTITY, None
            return OTHER, None
        if low[-1] in _GOV_HEADS:
            return "gov", None
        kinds = [w for w in low if w in _PLACE_KINDS]
        if kinds:
            return "topic", kinds[-1]
        if any(w in _RELATIONAL_NOUNS for w in low):
            return "topic", None
        return OTHER, None
    if first[:1].isupper():
        run = []
        for t in toks:
            if t in ",;:" or not t[:1].isupper():
                break
            run.append(t)
            if t != _possessive_base(t):
                break
        base = _possessive_base(run[0]).lower()
        if base in _PLACE_KIND_PLURALS:
            return GENERIC, None
        if b.is_alias(run):
            return ENTITY, None
        rest = [t.lower() for t in toks[len(run):len(run) + 3]]
        if b.country and fold(" ".join(_possessive_base(t) for t in run)) == fold(b.country):
            return ("gov", None) if any(w in _GOV_HEADS for w in rest) else (OTHER, None)
        if base in _GOV_HEADS:
            return "gov", None
        if base in _PLACE_KINDS:
            return "topic", base
        if _of_alias(toks[len(run):], b):  # 'Imperial Palace in Kyoto is …'
            return ENTITY, None
        return OTHER, None
    if lw in _PLACE_KIND_PLURALS:
        return GENERIC, None
    if lw in _GOV_HEADS:
        return "gov", None
    if lw in _PLACE_KINDS:
        return "topic", lw
    return OTHER, None


# review round — a contrast or adjunct lead names a place the main clause is NOT about: 'Unlike Mongar Dzong, it
# remains …', 'After visiting Mongar Dzong, you will see it is …'; its names are never an antecedent
_CONTRAST_LEAD_RE = re.compile(
    r"^\s*(?:unlike|like|compared\s+(?:with|to)|in\s+contrast\s+(?:to|with)|contrary\s+to|apart\s+from|besides"
    r"|(?:after|before|when|while)\s+(?:visiting|seeing|leaving|exploring|touring))\s+[^,]{1,80},\s*", re.I)
_CHUNK_RE = re.compile(r"[^.!?\n]+[.!?]*")


def _subject_name(chunk: str, b: _Binder) -> Optional[str]:
    """The proper noun that opens a sentence / clause as its subject ('Hanoi lies …', 'Vientiane sits …'), when it
    is not the entity, the country, a common capitalised word, a government word or a place-kind word."""
    s, _generic = _strip_lead(chunk)
    run = []
    for t in _WORD_RE.findall(s)[:6]:
        if t in ",;:" or not t[:1].isupper():
            break
        if not run and fold(t) in COMMON_CAPS:  # 'The', 'Visitors'
            continue
        run.append(t)
        if _possessive_base(t) != t:
            break
    if not run:
        return None
    first = fold(_possessive_base(run[0]))
    if first in COMMON_CAPS or first in _GOV_HEADS or first in _PLACE_KINDS or first in _PLACE_KIND_PLURALS:
        return None
    name = " ".join(_possessive_base(t) for t in run)
    if b.is_alias(run) or (b.country and fold(name) == fold(b.country)):
        return None
    # the clause still talks about 'it' ('Zhabdrung founded it in 1637'): the subject is an agent, the topic
    # carries on from before
    if re.search(r"\b(?:it|its)\b", s[len(run[0]):], re.I):
        return None
    return name


def _antecedents(text: str, b: _Binder) -> list[tuple[int, str, str, int, bool]]:
    """(position, kind, text, chunk, is_subject) of every place `text` names, in order: the binder's names, plus
    (review round) the proper-noun subject that opens each sentence / clause, so a paragraph naming the new capital
    in one plain word ('Hanoi lies 700 km to the north. It is …') is not read as being about the entity. Names
    inside a contrast lead are left out. `is_subject`: a name the binder knows (the entity, the successor, a seed
    moment, a named place) standing as the chunk's subject."""
    out, chunk = [], 0
    for cm in _CHUNK_RE.finditer(text or ""):
        for at, clause in _clauses(cm.group(0)):
            lead = _CONTRAST_LEAD_RE.match(clause)
            skip = lead.end() if lead else 0
            body, off = clause[skip:], cm.start() + at + skip
            s, _generic = _strip_lead(body)
            subj = len(body) - len(body.lstrip()) + len(body.strip()) - len(s)
            out += [(off + f[0], f[2], f[3], chunk, f[0] - subj <= 4 and f[0] >= subj) for f in b.mentions(body)]
            name = _subject_name(body, b)
            if name:
                out.append((off, OTHER, name, chunk, False))
            chunk += 1
    return sorted(out)


def _resolve(preceding: str, kind: Optional[str], heading: Optional[str], b: _Binder) -> str:
    """The antecedent of a pronoun / definite NP: in the same paragraph, the last sentence / clause that names a
    place — its subject when that is a known place ('Tokyo replaced Kyoto in 1869. It is …' is Tokyo), else the
    last place it names ('Zhabdrung founded Punakha Dzong in 1637. It …' is the dzong); a definite 'the dzong'
    prefers names ending in 'Dzong'. Then the section heading; then, with nothing named at all, the entity (the
    piece is about the seed's moment — the rule fails closed)."""
    found = _antecedents(preceding, b)
    if kind:
        same = [f for f in found if f[2].lower().endswith(kind)]
        found = same or found
    if found:
        last = [f for f in found if f[3] == found[-1][3]]
        subject = next((f for f in last if f[4]), None)
        return (subject or last[-1])[1]
    if heading:
        kinds = {f[2] for f in b.mentions(heading)}
        if kinds == {OTHER}:
            return OTHER
    return ENTITY


# the government as subject (review round): the words around the verb that place the government somewhere
_GOV_OF_SKIP = frozenset({"the", "its", "this", "that", "national", "royal", "central", "federal", "whole", "kingdom",
                          "country", "nation", "state", "realm", "and"}) | _GOV_HEADS
_ADVERBS = frozenset({"still", "now", "today", "also", "always", "even", "again", "currently", "nowadays", "actually",
                      "continues", "continue", "to", "presently", "officially", "formally", "largely", "mostly",
                      "only", "partly"})
_LOC_PREPS = frozenset({"in", "at", "inside", "within", "from"})
_OCCUPY_VERBS = frozenset({"occupies", "occupy", "fills", "fill"})
_COPULA = frozenset({"is", "are", "remains", "remain", "stays", "stay"})
_PART_NOUNS = frozenset({"hall", "halls", "wall", "walls", "courtyard", "courtyards", "chamber", "chambers", "room",
                         "rooms", "tower", "towers", "precinct", "precincts", "grounds", "compound", "utse"})


def _ref(toks: list[str], j: int, preceding: str, heading: Optional[str], b: _Binder) -> Optional[str]:
    """ENTITY / OTHER when `toks[j:]` opens on a reference to a place ('Punakha Dzong', 'it', 'the dzong', 'these
    halls', 'here', 'Thimphu'); None when it is not a place ('restoring Punakha Dzong', 'the road')."""
    if j >= len(toks):
        return None
    if b.starts_with_alias(" ".join(toks[j:j + 7])):
        return ENTITY
    t = toks[j].lower()
    if t in ("it", "its", "here", "there"):
        return _resolve(preceding, None, heading, b)
    if t in ("the", "this", "that", "these", "those"):
        if b.starts_with_alias(" ".join(toks[j + 1:j + 8])):
            return ENTITY
        for w in toks[j + 1:j + 4]:
            lw = _possessive_base(w).lower()
            if lw in _PLACE_KINDS or lw in _PART_NOUNS:
                return _resolve(preceding, lw if lw in _PLACE_KINDS else None, heading, b)
            if w in ",;:" or lw in _NP_STOPS:
                break
        return None
    if toks[j][:1].isupper() and fold(toks[j]) not in COMMON_CAPS:
        return OTHER
    return None


def _bare_locative(toks: list[str], k: int) -> bool:
    """'here' / 'there', or 'inside' / 'within' with nothing after it ('Inside, the government …', '… sits
    inside.')."""
    t = toks[k].lower()
    return t in ("here", "there") or (t in ("inside", "within") and (k + 1 == len(toks) or toks[k + 1] in ",;:"))


def _gov_binding(p: str, s: str, preceding: str, heading: Optional[str], b: _Binder) -> str:
    """The government / the capital is the subject (review round). It is bound to the entity only by a LOCATIVE
    relation — never by which verb it has:
    - a locative lead: 'Here the national government …', 'Inside, the government …', 'Inside Punakha Dzong …';
    - a place right after the verb: 'meets inside Punakha Dzong', 'is housed in the dzong', 'still sits here';
    - the place as the object of 'occupies' / 'fills' or of a copula: 'occupies the dzong', 'is still Punakha
      Dzong';
    - 'from' the place anywhere: 'governs Bhutan from the dzong / from these halls'.
    With no finite verb at all it is an appositive or a bullet ('Still the seat of Bhutan's government'): the
    antecedent decides. What the government does TO or AT the place ('maintains the dzong', 'sponsors festivals at
    Punakha Dzong', 'restricts photography inside') does not say the government sits there."""
    ps = p.strip()
    lead = ps[:len(ps) - len(s)] if s and ps.endswith(s) else ""
    ltoks = _WORD_RE.findall(lead)
    for j, t in enumerate(ltoks):
        if _bare_locative(ltoks, j):
            return _resolve(preceding, None, heading, b)
        if t.lower() in _LOC_PREPS:
            r = _ref(ltoks, j + 1, preceding, heading, b)
            if r is not None:
                return r
    toks = _WORD_RE.findall(s)
    i = 0
    while i < len(toks) and i < 8 and _possessive_base(toks[i]).lower() not in _GOV_HEADS:
        i += 1
    if i >= len(toks) or i >= 8:
        return OTHER
    i += 1
    if i < len(toks) and toks[i].lower() == "of":  # 'the seat of Bhutan's government', 'the government of the kingdom'
        i += 1
        while i < len(toks) and toks[i] not in ",;:" and (toks[i].lower() in _GOV_OF_SKIP or toks[i][:1].isupper()
                                                          or _possessive_base(toks[i]) != toks[i]):
            i += 1
    while i < len(toks) and toks[i].lower() in _ADVERBS:
        i += 1
    if i >= len(toks) or toks[i] in ",;:":
        return _resolve(preceding, None, heading, b)  # no finite verb: an appositive / a bullet
    verb = toks[i].lower()
    k = i + 1
    while k < len(toks) and toks[k].lower() in _ADVERBS:
        k += 1
    if verb in _OCCUPY_VERBS or verb in _COPULA:
        r = _ref(toks, k, preceding, heading, b)
        if r is not None:
            return r
    if k < len(toks) and re.fullmatch(r"[a-z]+(?:ed|en)", toks[k]):  # 'is housed in', 'is based at'
        k += 1
        while k < len(toks) and toks[k].lower() in _ADVERBS:
            k += 1
    if k < len(toks):
        if _bare_locative(toks, k):
            return _resolve(preceding, None, heading, b)
        if toks[k].lower() in _LOC_PREPS:
            r = _ref(toks, k + 1, preceding, heading, b)
            if r is not None:
                return r
    if verb not in _COPULA:  # 'The capital is still a drive from the dzong' places the capital, not the government
        for n in range(i + 1, len(toks)):
            if toks[n].lower() == "from":
                r = _ref(toks, n + 1, preceding, heading, b)
                if r is not None:
                    return r
    return OTHER


def _piece_binding(piece: str, preceding: str, heading: Optional[str], b: _Binder,
                   relative_np: Optional[str] = None) -> str:
    """ENTITY / OTHER / GENERIC for one clause piece."""
    if _CLASS_RE.search(piece):
        return GENERIC
    rel = re.match(r"(which|who|whose|where)\b\s*", piece, re.I)
    if rel and relative_np is not None and rel.group(1).lower() != "where":
        return _np_binding(relative_np, preceding, heading, b)
    p = piece[rel.end():] if rel else piece
    s, generic = _strip_lead(p)
    if generic:
        return GENERIC
    cat, kind = _subject(s, b)
    if cat in (OTHER, GENERIC):
        em = _EMBEDDED_IT_RE.search(s, 1)
        if em:  # the claim sits in a complement clause: 'Visitors rarely realise it is still the seat of government'
            return _resolve(f"{preceding} {s[:em.start()]}", None, heading, b)
        return cat
    if cat == ENTITY:
        return ENTITY
    if cat == "topic":
        # review round: the stripped lead is not the antecedent ('Unlike Mongar Dzong, it remains …')
        return _resolve(preceding, kind, heading, b)
    if rel and relative_np is not None:  # 'Punakha Dzong, where Bhutan's government still sits'
        return _np_binding(relative_np, preceding, heading, b)
    return _gov_binding(p, s, preceding, heading, b)


def _np_binding(np_text: str, preceding: str, heading: Optional[str], b: _Binder) -> str:
    """The binding of the noun phrase a relative pronoun or an appositive refers to (the end of the text before
    ', which' / ', still the …')."""
    tail = " ".join(np_text.split()[-5:]).rstrip(" .")
    last = tail.split()[-3:]
    if any(w.lower() in _PLACE_KIND_PLURALS or w.lower() in _CLASS_NOUNS for w in last):
        return GENERIC
    found = [f for f in b.mentions(tail) if f[1] == len(tail)]  # the name right before the comma
    if found:
        return found[-1][2]
    if re.search(rf"(?:^|\s)(?:it|(?:the|this)\s+(?:[\w-]+\s+)?(?:{'|'.join(_PLACE_KINDS)}))$", tail, re.I):
        return _resolve(preceding + " " + np_text[:len(np_text) - len(tail)], None, heading, b)
    return OTHER  # 'climbs to the capital, which …' is about the capital, not the entity


def binds_entity(clause: str, preceding: str, heading: Optional[str], b: _Binder,
                 about=_GOV_RE, appositive_np: Optional[str] = None) -> bool:
    """AA-985 round 2 — is this clause ABOUT the entity whose role ended? Judged on each piece that names the role
    (`about`): a relative piece (', which …', ', where …') has its own subject. `appositive_np` (review round):
    the clause was split off at ', still the …' and describes that noun phrase ('Punakha Dzong, still the capital
    of Bhutan, sits …')."""
    starts = [0] + [m.end() for m in _RELATIVE_RE.finditer(clause)]
    bounds = list(zip(starts, starts[1:] + [len(clause)]))
    for k, (a, z) in enumerate(bounds):
        piece = clause[a:z].strip().rstrip(",")
        if not about.search(piece):
            continue
        if k == 0 and appositive_np:
            binding = _np_binding(appositive_np, preceding, heading, b)
            if binding == OTHER and not appositive_np.split()[-1][:1].isupper():
                # after a full clause ('Punakha Dzong sits where two rivers meet, still the seat of …') the
                # appositive describes that clause's subject
                binding = _piece_binding(appositive_np, preceding, heading, b)
        else:
            rel_np = clause[bounds[k - 1][0]:a].rstrip(", ") if k else None
            binding = _piece_binding(piece, preceding + " " + clause[:a], heading, b, rel_np)
        if binding == ENTITY:
            return True
    return False


# a clause split off at ', still the …' / ', still Bhutan's …' is an appositive to the noun phrase before it
_APPOSITIVE_HEAD_RE = re.compile(r"(?i:still|today|now)\s+(?:(?i:the|an?|its|one)\b|[A-ZÀ-ÖØ-Þ])")


def _clause_units(sentence: str) -> list[tuple[int, str, Optional[str]]]:
    """`_clauses` with, for an appositive clause ('Punakha Dzong, still the capital of Bhutan, sits …'), the clause
    before it as the noun phrase it describes."""
    out, prev = [], None
    for at, clause in _clauses(sentence):
        appos = None
        if prev is not None and re.search(r",\s*$", sentence[:at]) and _APPOSITIVE_HEAD_RE.match(clause.lstrip()):
            appos = prev.rstrip(", ")
        out.append((at, clause, appos))
        prev = clause
    return out


def _main_clause(clause: str) -> str:
    """The clause without a contrast lead: 'Unlike Thimphu, it is still …' is not about Thimphu."""
    m = _CONTRAST_LEAD_RE.match(clause)
    return clause[m.end():] if m else clause


def _clauses(sentence: str) -> list[tuple[int, str]]:
    """`_CLAUSE_SPLIT_RE.split(sentence)` with each clause's start offset."""
    out, pos = [], 0
    for m in _CLAUSE_SPLIT_RE.finditer(sentence):
        out.append((pos, sentence[pos:m.start()]))
        pos = m.end()
    out.append((pos, sentence[pos:]))
    return out


def _contexts(text: str, cta: Optional[str]) -> list[tuple[str, str, Optional[str]]]:
    """(sentence, text of the paragraph before it, nearest heading above) for every body sentence."""
    body = {s["start"] for s in _body_spans(text, cta)}
    out, para, heading, prev_end = [], [], None, None
    for s in sentence_spans(text or ""):
        if s["kind"] == "heading":
            heading, para, prev_end = s["clean"], [], s["end"]
            continue
        if prev_end is not None and "\n\n" in text[prev_end:s["start"]]:
            para = []
        if s["start"] in body:
            out.append((s["clean"], " ".join(para), heading))
        para.append(s["clean"])
        prev_end = s["end"]
    return out


def date_authority_claims(text: str, seed: Optional[str], cta: Optional[str] = None,
                          extra_texts: tuple = ()) -> list[str]:
    """Present-tense claims of a role the seed dates as ended ('remains Bhutan's … administrative
    center today', 'merges Bhutan's spiritual and governmental authority') and computed spans that
    contradict the seed's dates ('for nearly three centuries' for 1637–1955), in the body and in
    the title / meta (`extra_texts`, prefixed)."""
    roles = ended_roles(seed)
    if not roles:
        return []
    role = roles[0]
    country = _seed_country(seed)
    binder = _binder(seed, role, country)
    units = [(s, "", para, heading) for s, para, heading in _contexts(text, cta)]
    units += [(t, f"{w} ", "", None) for w, t in extra_texts if t]
    out = []
    for sentence, prefix, para, heading in units:
        problem = None
        for at, clause, appos in _clause_units(sentence):
            # a clause about where the role went is about the successor, not this place: 'In 1955 the
            # capital moved to Thimphu, where it remains today' / 'Today Thimphu is Bhutan's capital' — but not
            # a contrast lead naming it ('Unlike Thimphu, it is still the capital', review round)
            if role.successor and re.search(rf"\b{re.escape(role.successor)}\b", _main_clause(clause)):
                continue
            if (role_claim(clause, country, role) and _PRESENT_RE.search(clause) and not _PAST_RE.search(clause)
                    and binds_entity(clause, f"{para} {sentence[:at]}", heading, binder, appositive_np=appos)):
                problem = (f"says it still holds a role the source dates as ended ({role.role}, until {role.end}) — "
                           "put it in the past")
                break
        # AA-985 — a span is checked against the role's dates only when the sentence is about that role:
        # 'for nearly four centuries it has housed the district administration' counts the building's
        # local role to today, not the national role's 1637–1955 — and (round 2) about that entity
        if (problem is None and role.start and _span_about_role(sentence, country, role)
                and any(binds_entity(c, f"{para} {sentence[:at]}", heading, binder, about=_SPAN_ABOUT_RE,
                                     appositive_np=ap)
                        for at, c, ap in _clause_units(sentence))):
            years = role.end - role.start
            for m in _SPAN_RE.finditer(sentence):
                if re.match(r"\s+ago\b", sentence[m.end():]):
                    continue  # 'almost four centuries ago' counts from today, not the role's span
                n = int(m.group(2)) if m.group(2).isdigit() else _SMALL[m.group(2).lower()]
                if _span_contradicts(m.group(1), n, years):
                    problem = (f"gives the span as '{m.group(0).strip()}', but the source's dates "
                               f"({role.start}–{role.end}) make it {years} years — use the dates")
                    break
        if problem:
            out.append(prefix + _quoted(problem, sentence))
    return out


def _span_about_role(sentence: str, country: Optional[str], role: EndedRole) -> bool:
    """Is this span sentence about the ended role? Decided on what is left once the phrases naming a
    different role are removed (review round): 'For nearly three centuries it served as the capital and
    district seat' keeps 'capital' and is checked; 'For nearly four centuries it has housed the district
    administration' has nothing left and is not. A bare 'served' sentence with no governance phrase at all
    is still checked, as before."""
    served = bool(re.search(r"\bserved\b", sentence, re.I))
    if not (_GOV_RE.search(sentence) or served):
        return False
    if _local_role(role):
        return True
    rest, local_scoped, hosted, national = _strip_other_roles(sentence, country)
    if _GOV_RE.search(rest):
        return True
    if hosted:
        return national
    return served and not local_scoped and not _OTHER_CAPITAL_RE.search(sentence)


def _centuries_phrase(years: int) -> str:
    n, r = divmod(years, 100)
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}
    if n == 0:
        return f"{years} years"
    if r <= 30:
        return f"just over {words.get(n, n)} centur{'y' if n == 1 else 'ies'}"
    if r >= 70:
        return f"nearly {words.get(n + 1, n + 1)} centuries"
    return f"about {words.get(n, n)} and a half centuries"


def date_span_lines(seed: Optional[str]) -> list[str]:
    """Stated-fact lines the writer gets for each ended role: with a start year the seed gives for
    the role, 'It served as the capital and seat of government of Bhutan for 318 years (1637–1955),
    just over three centuries; it has not held that role since 1955.'; without one, only 'It has not
    served as … since 1955.' (no span is computed from a building's founding year)."""
    out = []
    for r in ended_roles(seed):
        if r.start and r.end > r.start:
            years = r.end - r.start
            approx = f", {_centuries_phrase(years)}" if years >= 100 else ""
            out.append(f"It served as {r.role} for {years} years ({r.start}–{r.end}){approx}; "
                       f"it has not held that role since {r.end}.")
        else:
            out.append(f"It has not served as {r.role} since {r.end}.")
    return out


def with_date_spans(seed: Optional[str]) -> Optional[str]:
    """The seed with each `date_span_lines` line added as a place-background bullet right after the
    line that dates the role (S234 r2 #12 'nearly three centuries': give the writer the span as a
    stated fact). Unchanged when there is nothing to add or it is already there."""
    if not seed:
        return seed
    lines = date_span_lines(seed)
    if not lines:
        return seed
    out = []
    added = False
    for ln in seed.split("\n"):
        out.append(ln)
        if not added and _UNTIL_RE.search(ln) and _GOV_RE.search(ln):
            for extra in lines:
                bullet = f"- {extra}"
                if bullet not in seed:
                    out.append(bullet)
            added = True
    return "\n".join(out)


# ---------------------------------------------------------------- section placement (repair integrity)

def misplaced_sentences(text: str, moments: list[Moment], cta: Optional[str] = None) -> list[str]:
    """Body sentences that name ONE moment M and sit under a heading that names another moment N
    (and not M), when M was already told in an earlier section — S234 r2 #4: a repair put 'Sod Camp,
    a standard ger camp, offers trekking access…' inside the Lake Khövsgöl section, after Sod Camp's
    own paragraph under Uran Togoo."""
    if len(moments) < 2:
        return []
    out = []
    heading: Optional[str] = None
    told: dict[str, int] = {}
    section = 0
    for sp in sentence_spans(text or ""):
        if sp["kind"] == "heading":
            heading, section = sp["clean"], section + 1
            continue
        if sp["kind"] not in ("prose", "bullet") or sp["in_faq"] or _is_cta_sentence(sp["clean"], cta):
            continue
        named = moments_named(sp["clean"], moments)
        # the sentence is ABOUT the moment: its name opens the sentence ('Sod Camp, a standard ger
        # camp, offers…'); 'Arriving at Lake Khövsgöl…' under Mörön is a transition, not a misplacement
        if len(named) == 1 and heading and names_moment(" ".join(sp["clean"].split()[:3]), named[0]):
            m = named[0]
            owner = moments_named(heading, moments)
            if owner and m not in owner and told.get(m.name, section) < section:
                out.append(sp["clean"])
        for m in named:
            told.setdefault(m.name, section)
    return out


_MISPLACED_CONTAINED = 0.5


def fix_misplaced(text: str, moments: list[Moment], cta: Optional[str] = None) -> tuple[str, list[str]]:
    """Delete a misplaced sentence (`misplaced_sentences`) when at least half its content words are
    already in the earlier sentences that name the same moment (it is a later duplicate: r2 #4's Sod
    Camp line repeats 'trekking access … mineral spring … reputed healing properties' from the Sod
    Camp paragraph) and no pronoun is orphaned. Returns (text, log)."""
    from services.acp_content_writing.hygiene import _safe_to_delete, tidy_after_cut
    log: list[str] = []
    for sentence in misplaced_sentences(text, moments, cta):
        spans = sentence_spans(text)
        k = next((n for n, sp in enumerate(spans) if sp["clean"] == sentence), None)
        if k is None:
            continue
        m = moments_named(sentence, moments)[0]
        earlier = set()
        for sp in spans[:k]:
            if sp["kind"] in ("prose", "bullet") and names_moment(sp["clean"], m):
                earlier |= set(content_words(sp["clean"]))
                nxt = next((t for t in spans[spans.index(sp) + 1:] if t["kind"] in ("prose", "bullet")), None)
                if nxt is not None:
                    earlier |= set(content_words(nxt["clean"]))
        words = content_words(sentence)
        share = sum(w in earlier for w in words) / len(words) if words else 0.0
        if share >= _MISPLACED_CONTAINED and _safe_to_delete(spans, k):
            sp = spans[k]
            after = sp["end"] + 1 if text[sp["end"]:sp["end"] + 1] == " " else sp["end"]
            text = tidy_after_cut(text[:sp["start"]] + text[after:])
            log.append(f"deleted a {m.name} sentence repeated under another moment's heading: '{sentence[:160]}'")
    return text, log


def new_misplaced(before: str, after: str, moments: list[Moment], cta: Optional[str] = None) -> list[str]:
    """`misplaced_sentences` that a repair made (not in `before`) — a problem for splice_checked."""
    had = set(misplaced_sentences(before, moments, cta))
    return [f"puts a moment under another moment's heading: '{s[:200]}'"
            for s in misplaced_sentences(after, moments, cta) if s not in had]


# ---------------------------------------------------------------- thin moment weight (S234 round 4)

# Words a sentence about a thin moment may use beyond the moment's own seed lines without adding a
# claim: how the route gets there and where it sits in the trip (calibrated on the 80 stored pieces —
# every plain 'On Day 14, families visit the ruins of an ancient monastery destroyed during the
# political purges of 1937.' has ≤ 2 other content words; every interpreted coda ≥ 4).
_THIN_GLUE = frozenset(content_words(
    "journey route trip day days visit visits visiting later late section close closes closing end ends ending "
    "final last brief briefly single quiet quieter stop stops pass passes passing include includes including "
    "encounter encounters fourteen moment moments itinerary families family travellers travelers stand stands "
    "standing reach reaches arrive arrives near"))
THIN_EXTRA_MAX = 2
_NUMBER_WORDS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
_DAY_REF_RE = re.compile(r"\bdays?\s+(\d{1,2}|[a-z]+)\b", re.I)
# where an interpretation is appended to a plain mention: ' — a quiet coda to…', ', a detail that…'
_APPOSITIVE_RE = re.compile(r"\s+[—–]\s+|;\s+|:\s+|,\s+(?=(?:a|an|the|one|which|where)\b)")


def _thin_words(m: Moment) -> set[str]:
    """The thin moment's identifying words: its label's content words ('ruin', 'ancient',
    'monastery') and the years its trip lines give ('1937')."""
    words = set(content_words(m.name)) - _THIN_GLUE
    for ln in m.trip_lines:
        words |= set(re.findall(r"\b1[0-9]{3}\b|\b20[0-9]{2}\b", ln))
    return words


def _day_refs(text: str) -> set[int]:
    out = set()
    for d in _DAY_REF_RE.finditer(text or ""):
        tok = d.group(1).lower()
        n = int(tok) if tok.isdigit() else _NUMBER_WORDS.get(tok)
        if n is not None:
            out.add(n)
    return out


def names_thin(text: str, m: Moment, moments: list[Moment]) -> bool:
    """Does `text` (a sentence or a heading) name the thin moment `m`: two of its identifying words
    ('the ruins of an ancient monastery', 'Ruins from 1937'), or its day ('Day Fourteen') when no
    other moment shares that day — and no other moment?"""
    if any(names_moment(text, o) for o in moments if o is not m):
        return False
    have = set(content_words(text)) | set(re.findall(r"\b1[0-9]{3}\b|\b20[0-9]{2}\b", text or ""))
    if len(have & _thin_words(m)) >= 2:
        return True
    other_days = {d for o in moments if o is not m for d in o.days}
    return bool(_day_refs(text) & (set(m.days) - other_days))


def _thin_extra(sentence: str, m: Moment) -> list[str]:
    """Content words of `sentence` that are neither in the thin moment's own seed lines nor route glue."""
    own = set(content_words(" ".join(m.lines))) | _thin_words(m)
    return [w for w in content_words(sentence) if w not in own and w not in _THIN_GLUE and not w.isdigit()]


def _thin_moments(moments: list[Moment]) -> list[Moment]:
    return [m for m in moments if m.thin] if len(moments) >= 2 else []


def _heading_sections(text: str) -> list[dict]:
    """[{"heading": span | None, "body": [prose/bullet spans]}] for the body before any FAQ."""
    out: list[dict] = [{"heading": None, "body": []}]
    for sp in sentence_spans(text or ""):
        if sp["in_faq"]:
            break
        if sp["kind"] == "heading":
            out.append({"heading": sp, "body": []})
        elif sp["kind"] in ("prose", "bullet"):
            out[-1]["body"].append(sp)
    return out


def thin_headings(text: str, moments: list[Moment], cta: Optional[str] = None) -> list[dict]:
    """Headings that give a thin moment its own section (r3 #0 'Day 14: A Single Moment of Historical
    Weight', #4, #14, #16 'Day Fourteen', #17, #19): the heading names it, or every body sentence of the
    section that names a moment names only it. [{"heading": span, "moment": m, "first": bool}]."""
    thin = _thin_moments(moments)
    if not thin:
        return []
    out = []
    secs = _heading_sections(text)
    for k, sec in enumerate(secs):
        h = sec["heading"]
        if h is None:
            continue
        for m in thin:
            body = [sp for sp in sec["body"] if not _is_cta_sentence(sp["clean"], cta)]
            named = [sp for sp in body if moments_named(sp["clean"], moments) or names_thin(sp["clean"], m, moments)]
            own = names_thin(h["clean"], m, moments)
            # a generic heading ('A Final Marker of Loss') over a section that opens on the thin moment
            # and is mostly about it
            thin_n = sum(names_thin(sp["clean"], m, moments) for sp in body)
            only = (bool(named) and all(names_thin(sp["clean"], m, moments) for sp in named)
                    and not moments_named(h["clean"], moments) and len(body) <= 3
                    and names_thin(body[0]["clean"], m, moments) and 2 * thin_n >= len(body))
            if own or only:
                has_before = any(s["body"] for s in secs[:k]) or k > 1
                out.append({"heading": h, "moment": m, "first": not has_before})
                break
    return out


def _coda(text: str, moments: list[Moment], cta: Optional[str]) -> Optional[tuple[dict, Moment]]:
    """The closing body sentence before the CTA (the last paragraph before the paragraph or section
    that carries the CTA) when it is about a thin moment: (span, moment) or None."""
    from services.acp_content_writing.hygiene import cta_norm
    thin = _thin_moments(moments)
    want = cta_norm(cta)
    if not thin or not want:
        return None
    body = [sp for sec in _heading_sections(text) for sp in sec["body"] if sp["kind"] == "prose"]
    k = next((n for n, sp in enumerate(body) if f" {want} " in f" {cta_norm(sp['clean'])} "
              or _is_cta_sentence(sp["clean"], cta)), None)
    if not k:
        return None
    para_start = (text or "").rfind("\n", 0, body[k]["start"])
    before = [sp for sp in body[:k] if sp["end"] <= para_start]
    if not before:
        return None
    last = before[-1]
    for m in thin:
        if names_thin(last["clean"], m, moments):
            return last, m
    return None


def thin_coda(text: str, moments: list[Moment], cta: Optional[str] = None) -> list[str]:
    """The thin moment as an interpreted coda (r3 #0, #4, #12, #14, #16, #17, #18, #19: '… destroyed during
    the political purges of 1937 — a quiet coda to two weeks spent moving through a landscape where loss
    and endurance…'): more than THIN_EXTRA_MAX content words the moment's seed does not give."""
    hit = _coda(text, moments, cta)
    if hit is None:
        return []
    sp, m = hit
    extra = _thin_extra(sp["clean"], m)
    if len(extra) <= THIN_EXTRA_MAX:
        return []
    # AA-777 (r6 fix 4) — the message names the words the source does not give, so a repair of a coda whose
    # extras sit in a leading frame ('Before leaving this part of northern Mongolia, …') knows which part to cut
    words = ", ".join(dict.fromkeys(extra))
    return [_quoted("closes on a thin moment (one source fact) with an interpretation the source does not give "
                    f"(words the source does not give — {words}) — say it in one plain sentence, or cut the added "
                    "clause", sp["clean"])]


# AA-777 (r6 fix 4) — a leading adverbial frame that sets the thin moment in the route: 'Before leaving this part
# of northern Mongolia, the route passes the ruins…' (r6 a2133050: all 4 extra words were in the frame).
# r7 review — sequence frames only: a conditional frame ('When road access allows, …', 'Once …', 'While …',
# 'As …') may carry a source-stated condition, and dropping it would turn the sentence into a firm promise.
_LEADING_FRAME_RE = re.compile(r"^(?:before|after|on\s+leaving|on\s+the\s+way)\b[^,;:—–]{3,90},\s+", re.I)


def _trim_thin(sentence: str, m: Moment, moments: list[Moment]) -> Optional[str]:
    """The sentence cut before its appended interpretation, when the head still names the moment, has
    ≥ 6 words and ≤ THIN_EXTRA_MAX words the seed does not give ('… purges of 1937 — a single, quiet
    encounter with…' → '… purges of 1937.'). The longest such head; None when there is none.

    AA-777 (r6 fix 4) — when no head of the sentence qualifies, the same is tried after dropping a
    leading frame ('Before leaving this part of northern Mongolia, ') — the rest whole first, then its
    heads; the first word of what is left is capitalised."""
    best = _thin_head(sentence, m, moments)
    if best is None:
        frame = _LEADING_FRAME_RE.match(sentence)
        rest = sentence[frame.end():] if frame else ""
        if rest:
            rest = rest[0].upper() + rest[1:]
            whole = rest.rstrip().rstrip(".!?…").rstrip()
            if (len(whole.split()) >= 6 and names_thin(whole, m, moments)
                    and len(_thin_extra(whole, m)) <= THIN_EXTRA_MAX):
                best = whole
            else:
                best = _thin_head(rest, m, moments)
    return (best + ".") if best else None


def _thin_head(sentence: str, m: Moment, moments: list[Moment]) -> Optional[str]:
    best = None
    for cut in _APPOSITIVE_RE.finditer(sentence):
        head = sentence[:cut.start()].rstrip(" ,;:—–")
        if len(head.split()) < 6 or not names_thin(head, m, moments):
            continue
        if len(_thin_extra(head, m)) <= THIN_EXTRA_MAX:
            best = head
    return best


def fix_thin_moments(text: str, moments: list[Moment], cta: Optional[str] = None) -> tuple[str, list[str]]:
    """Deterministic fixes (S234 round 4, r3 panel §4 fix 2): a thin moment's own heading is deleted —
    its paragraph joins the section before (never the first heading of the piece); an interpreted coda
    sentence is cut before its appended clause. What is left is an F17_trip_fact finding. Returns
    (text, log)."""
    from services.acp_content_writing.hygiene import tidy_after_cut
    log: list[str] = []
    for _ in range(4):
        heads = [hd for hd in thin_headings(text, moments, cta) if not hd["first"]]
        if not heads:
            break
        h = heads[0]["heading"]
        line_start = text.rfind("\n", 0, h["start"]) + 1
        line_end = text.find("\n", h["end"])
        line_end = len(text) if line_end < 0 else line_end + 1
        text = tidy_after_cut(text[:line_start] + text[line_end:])
        log.append(f"merged the thin moment {heads[0]['moment'].name}'s own heading into the section before: "
                   f"'{h['clean'][:120]}'")
    hit = _coda(text, moments, cta)
    if hit is not None:
        sp, m = hit
        if len(_thin_extra(sp["clean"], m)) > THIN_EXTRA_MAX:
            raw = text[sp["start"]:sp["end"]]
            tags = trailing_tags(raw)  # AA-912: one backward pass (the regex search was O(n^2))
            trimmed = _trim_thin(sp["clean"], m, moments)
            if trimmed:
                text = text[:sp["start"]] + trimmed + tags.rstrip() + text[sp["end"]:]
                log.append(f"cut the interpretation appended to the thin moment {m.name}: '{sp['clean'][:160]}'")
    return text, log


def thin_moment_issues(text: str, moments: list[Moment], cta: Optional[str] = None) -> list[str]:
    out = [_quoted(f"gives the thin moment {hd['moment'].name} (one source fact) its own heading — fold it into "
                   "the section before in one sentence", hd["heading"]["clean"])
           for hd in thin_headings(text, moments, cta)]
    return out + thin_coda(text, moments, cta)


# ---------------------------------------------------------------- hazard polarity (S234 round 4)

# A hazard the seed states for the trip or the place ('sudden storms can arise quickly on the lake,
# making navigation in small boats potentially hazardous', 'a multi-hour horse or reindeer ride').
_SEED_HAZARD_RE = re.compile(
    r"\b(?:storms?|hazard\w*|danger\w*|multi-hour|physically demanding|demanding|strenuous|arduous|boggy"
    r"|rough terrain|unpredictable weather|extreme(?:ly)? cold|steep|elevation gain|crossing rivers)\b", re.I)
# The same hazard in the copy (the sentence that plays it down, or the one right before it).
_COPY_HAZARD_RE = re.compile(
    r"\b(?:storms?|hazard\w*|danger\w*|weather|rides?|riding|terrain|demanding|strenuous|cold|steep|climb\w*"
    r"|elevation|crossing|boggy|marsh\w*|navigation|wind)\b", re.I)
# Copy that negates or minimises it ('That is not a limitation', 'not a reason for caution', r3 #0/#14).
MINIMISE_RE = re.compile(
    r"\b(?:(?:is|are|was|were|it's|that's|isn't|aren't)\s+(?:not|never|no)|isn't|aren't|not|never|no)\s+"
    r"(?:a\s+|an\s+|any\s+|much\s+(?:of\s+)?a\s+)?(?:real\s+|serious\s+|significant\s+|major\s+|great\s+)?"
    r"(?:limitation|reason\s+(?:for|to)\s+(?:caution|concern|worry|hesitate|hesitation|be\s+concerned)"
    r"|cause\s+for\s+(?:concern|alarm|worry)|problem|concern|obstacle|drawback|deterrent|barrier|danger|risk"
    r"|hardship|issue)s?\b"
    r"|\bnothing\s+to\s+(?:worry|fear|be\s+concerned)\s+about\b"
    r"|\bno\s+need\s+to\s+(?:worry|be\s+concerned|be\s+anxious)\b"
    r"|\b(?:perfectly|completely|entirely|totally)\s+safe\b|\beasily\s+manageable\b", re.I)
# A sentence that is ONLY the reassurance ('That is not a limitation; it is simply how…'): deleted.
_REASSURANCE_OPEN_RE = re.compile(r"^(?:that|this|it|these|those|which)\s+(?:is|are|was|were|isn't|aren't|'s|’s)\b",
                                  re.I)


def minimised_hazards(text: str, ctx: "TripFactContext") -> list[dict]:
    """For a brand whose audience includes children, when the seed states a hazard: sentences that
    negate or minimise it — MINIMISE_RE in a sentence that names the hazard or right after one that does
    (same paragraph). [{"span", "pure"}] — `pure`: the sentence opens on 'That is / It is…' (only the
    reassurance), safe to delete."""
    if not audience_has_children(ctx.audience) or not _SEED_HAZARD_RE.search(split_seed(ctx.seed)[0]):
        return []
    spans = _body_spans(text, ctx.cta, kinds=("prose", "bullet", "faq_a"))
    out = []
    for k, sp in enumerate(spans):
        mm = MINIMISE_RE.search(sp["clean"])
        if not mm:
            continue
        prev = spans[k - 1] if k else None
        same_para = prev is not None and "\n\n" not in (text or "")[prev["end"]:sp["start"]]
        if _COPY_HAZARD_RE.search(sp["clean"]) or (same_para and _COPY_HAZARD_RE.search(prev["clean"])):
            pure = bool(_REASSURANCE_OPEN_RE.match(sp["clean"])) and mm.start() < 40
            out.append({"span": sp, "pure": pure})
    return out


def fix_minimised_hazards(text: str, ctx: "TripFactContext") -> tuple[str, list[str]]:
    from services.acp_content_writing.hygiene import _safe_to_delete, tidy_after_cut
    log: list[str] = []
    for _ in range(3):
        target = None
        for hit in minimised_hazards(text, ctx):
            if not hit["pure"]:
                continue
            spans = sentence_spans(text)
            k = next((n for n, sp in enumerate(spans) if sp["start"] == hit["span"]["start"]), None)
            if k is not None and _safe_to_delete(spans, k):
                target = hit["span"]
                break
        if target is None:
            break
        after = target["end"] + 1 if text[target["end"]:target["end"] + 1] == " " else target["end"]
        text = tidy_after_cut(text[:target["start"]] + text[after:])
        log.append(f"deleted a sentence that plays down a hazard the source states: '{target['clean'][:160]}'")
    return text, log


# ---------------------------------------------------------------- background place as the trip's base

# 'Khatgal, at the lake's southern tip, serves as the primary gateway and base for boat trips' (r3 #4,
# #16, #19 FAQ): the seed's place background names Khatgal as where visitors TYPICALLY arrive; the
# trip itself never goes there. Wording that makes a place a gateway / base for the reader.
_BASE_RE = re.compile(r"\b(?:gateway|base)\b", re.I)


def _background_places(seed: Optional[str], trip_places: tuple = ()) -> dict[str, str]:
    """Folded key → display: proper-noun places named ONLY in place-background bullets (not in a
    moment's label / Location / Naming / Source itinerary / SCOPE lines) and not a place of the trip
    itself (`trip_places`, the whitelist: Ulaanbaatar via the Mörön airport fact)."""
    from services.acp_content_writing.hygiene import _near_allowed, proper_nouns
    own = strip_status_lines(split_seed(seed)[0])
    core: set[str] = set()
    core_text = "\n".join(ln for ln in own.splitlines() if not ln.lstrip().startswith("- "))
    for p in proper_nouns(core_text, keep_initial=True):
        core |= set(phrase_tokens(p))
    trip: set[str] = set()
    for place in trip_places or ():
        for part in re.split(r"[,(;/]", place or ""):
            trip |= set(phrase_tokens(part))
    frozen = frozenset(core)
    out: dict[str, str] = {}
    bg = "\n".join(ln for ln in own.splitlines() if ln.lstrip().startswith("- "))
    for p in proper_nouns(bg):
        toks = [t for t in phrase_tokens(p) if t not in frozen and not _near_allowed(t, frozen)]
        if toks and not any(t in trip for t in toks):
            out[" ".join(toks)] = " ".join(w for w in p.split() if fold(w) not in COMMON_CAPS)
    return out


def background_bases(text: str, ctx: "TripFactContext") -> list[dict]:
    """Body or FAQ sentences that make a background-only place the reader's gateway or base.
    [{"span", "place"}]."""
    from services.acp_content_writing.hygiene import _has_place, _name_tokens
    places = _background_places(ctx.seed, ctx.trip_places)
    if not places:
        return []
    out = []
    for sp in _body_spans(text, ctx.cta, kinds=("prose", "bullet", "faq_a")):
        if not _BASE_RE.search(sp["clean"]):
            continue
        toks = _name_tokens(sp["clean"])
        named = [disp for key, disp in places.items() if _has_place(toks, key, True)]
        if named:
            out.append({"span": sp, "place": named[0]})
    return out


def fix_background_bases(text: str, ctx: "TripFactContext") -> tuple[str, list[str]]:
    """Delete such a sentence when no pronoun is orphaned; when it is the whole answer of a FAQ pair,
    delete the pair while the FAQ keeps at least FAQ_MIN_PAIRS pairs. Returns (text, log)."""
    from services.acp_content_writing.hygiene import _safe_to_delete, tidy_after_cut
    from services.acp_content_writing.quality_gates import FAQ_MIN_PAIRS
    log: list[str] = []
    for _ in range(4):
        hits = background_bases(text, ctx)
        done = False
        for hit in hits:
            sp = hit["span"]
            spans = sentence_spans(text)
            k = next((n for n, s in enumerate(spans) if s["start"] == sp["start"]), None)
            if k is None:
                continue
            answer = [s for s in spans if s["kind"] == "faq_a"
                      and text.rfind("\n", 0, s["start"]) == text.rfind("\n", 0, sp["start"])]
            if sp["kind"] == "faq_a" and len(answer) == 1:
                pairs = sum(1 for s in spans if s["kind"] == "faq_q")
                q = next((s for s in reversed(spans[:k]) if s["kind"] == "faq_q"), None)
                if q is None or pairs <= FAQ_MIN_PAIRS:
                    continue
                start = text.rfind("\n", 0, q["start"]) + 1
                end = text.find("\n", sp["end"])
                end = len(text) if end < 0 else end + 1
                text = tidy_after_cut(text[:start] + text[end:])
                log.append(f"deleted a FAQ pair that makes {hit['place']} the trip's base (background only): "
                           f"'{q['clean'][:120]}'")
                done = True
                break
            if _safe_to_delete(spans, k):
                after = sp["end"] + 1 if text[sp["end"]:sp["end"] + 1] == " " else sp["end"]
                text = tidy_after_cut(text[:sp["start"]] + text[after:])
                log.append(f"deleted a sentence that makes {hit['place']} the trip's base (background only): "
                           f"'{sp['clean'][:160]}'")
                done = True
                break
        if not done:
            break
    return text, log


# ---------------------------------------------------------------- pace / rest promises (AA-775)

# S234 r1-r4: the writer copied the tenant style guide's pace lines ('balanced with time to rest',
# 'full without being relentless') into copy as facts about the trip. A sentence that matches PACE_RE
# and whose pace word the moment's own source does not back is an F17 `pace_promise` finding.
# AA-789 (S234 r4 #14 'not a technical climb', report 07 fix 2) — a difficulty promise is the same kind of claim: a
# 'technical' route word, 'strenuous', 'gruelling', 'effortless'. Seed-backed words pass (_pace_supported).
PACE_RE = re.compile(r"\b(pace[ds]?|pacing|relentless|exhaust\w*|rest(?:ful|ing)?|rush\w*|manageable|balanced?"
                     r"|technical(?=\s+(?:climb|climbing|ascent|trek|trekking|hike|hiking|walk|route|scramble|terrain))"
                     r"|strenuous|gruell?ing|effortless(?:ly)?)\b", re.I)
_REST_OF_RE = re.compile(r"\bthe\s+rest\s+of\b", re.I)
PACE_WINDOW = 6           # words either side of the pace word
PACE_SUPPORT_SHARE = 0.6  # share of the window's content words found in the source → seed-backed
PACE_PROBLEM = ("states a pace or rest promise the source does not make ('{word}') — cut it, or say only "
                "what the source says")
_INVITE_START = r"(?:ask|speak|talk|chat|contact|reach\s+out|get\s+in\s+touch|enquire|inquire|write|email|call)"


def _cta_paraphrase(sentence: str, cta: Optional[str], brand_name: Optional[str]) -> bool:
    """A sentence (or a clause after ',', ';', ':' or a dash) that OPENS with an invitation verb
    addressed to the brand or 'us' within four words ('Talk to an Atlas & Hearth travel designer about
    … your pace', 'If this has your attention, talk to …', 'Ask Terra Family Expeditions to plan …').
    The configured CTA is already excluded by `_body_spans`; this catches the writer's paraphrase of it.
    The brand's own line ('Terra Family Expeditions builds journeys…') opens with no invitation verb."""
    from services.acp_content_writing.hygiene import _cta_brand_word
    names = [w for w in (_cta_brand_word(cta), (brand_name or "").split(" ")[0]) if w]
    target = "|".join([re.escape(n) for n in names] + ["us"])
    return bool(re.search(rf"(?:^|[,;:—–]\s*){_INVITE_START}\b(?:\s+[\w’'&-]+){{0,4}}?\s+(?:{target})\b",
                          strip_tags(sentence or ""), re.I))


def _pace_supported(sentence: str, m: re.Match, source_words: set) -> bool:
    """The pace word itself is in the source (seed 'time to swim and rest before lunch'), or most of
    the content words around it are."""
    stem = content_words(m.group(1))
    if stem and stem[0] in source_words:
        return True
    words = sentence.split()
    at = len(sentence[:m.start()].split())
    window = content_words(" ".join(words[max(0, at - PACE_WINDOW):at + PACE_WINDOW + 1]))
    return bool(window) and sum(w in source_words for w in window) / len(window) >= PACE_SUPPORT_SHARE


def pace_claims(text: str, source: str, cta: Optional[str] = None, brand_name: Optional[str] = None) -> list[str]:
    """F17 `pace_promise` findings: body sentences (prose, bullet, FAQ answer) with a pace / rest /
    balance word the source does not back. Not a finding: the CTA and its paraphrase
    (`_cta_paraphrase`), 'the rest of …' (remainder), and a seed-backed pace word (`_pace_supported`).
    `source` = the moment's own seed lines + Facts (TripFactContext.source)."""
    return [_quoted(PACE_PROBLEM.format(word=words[0]), sentence)
            for sentence, words in _pace_hits(text, source, cta, brand_name)]


def _pace_hits(text: str, source: str, cta: Optional[str] = None,
               brand_name: Optional[str] = None) -> list[tuple[str, list[str]]]:
    """(sentence, its unbacked pace words in order) for every body sentence with at least one."""
    source_words = set(content_words(source or ""))
    out = []
    for sp in _body_spans(text, cta):
        sentence = sp["clean"]
        if not PACE_RE.search(sentence) or _cta_paraphrase(sentence, cta, brand_name):
            continue
        rest_of = [r.span() for r in _REST_OF_RE.finditer(sentence)]
        words = [m.group(1).lower() for m in PACE_RE.finditer(sentence)
                 if not any(a <= m.start() < b for a, b in rest_of) and not _pace_supported(sentence, m, source_words)]
        if words:
            out.append((sentence, words))
    return out


def unsupported_pace_words(text: str, source: str, cta: Optional[str] = None,
                           brand_name: Optional[str] = None) -> dict[str, str]:
    """AA-777 (r6 fix 1) — the pace / rest words of `text` the source does not back, keyed by stem (so
    'rest' / 'resting' are one word): {stem: the word as written}."""
    return {_stem(w): w for _, words in _pace_hits(text, source, cta, brand_name) for w in words}


def new_specific_claims(original: str, replacement: str, ctx: Optional["TripFactContext"]) -> list[str]:
    """AA-777 (r6 fix 1) — the F17 time / position / pace claims a sentence repair ADDED: phrases of
    `replacement` the seed does not give and `original` did not already carry (r6 R1: 'you arrive at the
    afternoon with energy' → 'the afternoon starts with space…'; R2: 'Time to swim and rest is built into
    the plan before lunch.' → 'The afternoon has room for swimming…'; AA-768 H2: '…a slower pace…').
    Pace is checked on blogs only, like the F17 gate."""
    if ctx is None or not (replacement or "").strip():
        return []
    out = [f"adds a time the source does not give ('{k}')"
           for k in sorted(unsupported_time_keys(replacement, ctx.moments, ctx.cta)
                           - unsupported_time_keys(original, ctx.moments, ctx.cta))]
    if ctx.channel == "blog":
        new = unsupported_pace_words(replacement, ctx.source, ctx.cta, ctx.brand_name)
        old = unsupported_pace_words(original, ctx.source, ctx.cta, ctx.brand_name)
        out += [f"adds a pace or rest promise the source does not make ('{new[k]}')"
                for k in sorted(set(new) - set(old))]
    return out


def fix_trip_facts(text: str, ctx: Optional["TripFactContext"]) -> tuple[str, list[str]]:
    """S234 round 4 — the deterministic trip-fact fixes, run in the job after the last LLM step and
    before the gates: thin-moment heading / coda, a hazard played down for a family brand, a
    background-only place made the trip's base. Returns (text, log)."""
    if ctx is None:
        return text, []
    text, log = fix_thin_moments(text, ctx.moments, ctx.cta)
    text, more = fix_minimised_hazards(text, ctx)
    log += more
    text, more = fix_background_bases(text, ctx)
    return text, log + more


# ---------------------------------------------------------------- one entry point

@dataclass
class TripFactContext:
    seed: str
    source: str = ""          # own seed lines + Facts (rarity support)
    audience: Optional[str] = None
    cta: Optional[str] = None
    # S234 round 4 — places of the full trip (service._trip_places_outside): a background place that is
    # one of them is not 'a base the trip never uses' (Ulaanbaatar via the Mörön airport fact)
    trip_places: tuple = ()
    # AA-775 — F17 pace_promise is checked on blogs only (a LinkedIn post never holds on it); the
    # brand name lets a paraphrased CTA ('Talk to an Atlas & Hearth travel designer…') be skipped.
    channel: Optional[str] = None
    brand_name: Optional[str] = None
    moments: list = field(init=False)

    def __post_init__(self):
        self.moments = parse_moments(self.seed)
        if not self.source:
            self.source = split_seed(self.seed)[0]


SERVICE_CLAIM_PROBLEM = ("states a service level the source does not ('{phrase}') — keep the fact, cut the "
                         "claim word")


def service_claims(text: str, ctx: Optional[TripFactContext]) -> list[str]:
    """AA-800 — F17 findings for a positioning word that states a service level ('a luxury lodge', 'a
    tailored itinerary') with no support in the seed or its Facts (signature_vocabulary.service_level_claims).
    The brand may use its signature words; it may not promise a service level the trip does not have."""
    if ctx is None:
        return []
    from services.acp_shared.signature_vocabulary import service_level_claims
    out = []
    for sp in _body_spans(text, ctx.cta):
        for claim in service_level_claims(sp["clean"], ctx.seed, ctx.source):
            out.append(_quoted(SERVICE_CLAIM_PROBLEM.format(phrase=claim["phrase"]), sp["clean"]))
    return out


def find_trip_fact_issues(text: str, ctx: Optional[TripFactContext], *, seo_title: Optional[str] = None,
                          meta_description: Optional[str] = None) -> dict[str, list[str]]:
    """{"trip_fact": [...], "rarity": [...]} — every finding quotes its sentence; title/meta
    findings start with "title " / "meta "."""
    if ctx is None:
        return {"trip_fact": [], "rarity": []}
    extra = (("title", seo_title), ("meta", meta_description))
    trip = optional_as_fixed(text, ctx.moments, ctx.cta) + timing_claims(text, ctx.moments, ctx.cta)
    trip += included_as_optional(text, ctx.moments, ctx.cta)  # AA-789 — the reverse flip (S234 r4 #18)
    trip += near_as_at(text, ctx.moments, ctx.cta)  # AA-789 — 'lunch at X' when the source says near X
    for miss in missing_family_caveat(text, ctx.moments, ctx.audience, ctx.cta):
        trip.append(_quoted(f"pitches {miss['moment']} to families but never says what the source says about "
                            f"getting there ({miss['caveat'][:160].rstrip('.')}) — keep this sentence and add ONE "
                            "short sentence that states that caveat in the source's words", miss["sentence"]))
    trip += date_authority_claims(text, ctx.seed, ctx.cta, extra)
    # S234 round 4 (r3 panel §4 fixes 2, 3, 4)
    trip += thin_moment_issues(text, ctx.moments, ctx.cta)
    trip += [_quoted("plays down a hazard the source states, for a family audience — keep the hazard, cut the "
                     "reassurance", hit["span"]["clean"]) for hit in minimised_hazards(text, ctx)]
    trip += [_quoted(f"makes {hit['place']} the reader's gateway or base, but this trip does not go there (the "
                     "source names it only as background) — cut it", hit["span"]["clean"])
             for hit in background_bases(text, ctx)]
    if ctx.channel == "blog":  # AA-775
        trip += pace_claims(text, ctx.source, ctx.cta, ctx.brand_name)
    trip += service_claims(text, ctx)  # AA-800 evidence rule
    rarity = rarity_claims(text, ctx.source, ctx.cta, extra)
    return {"trip_fact": list(dict.fromkeys(trip)), "rarity": list(dict.fromkeys(rarity))}


__all__ = [
    "EndedRole", "Moment", "OptionalActivity", "PACE_RE", "RARITY_CLAIM_RE", "TripFactContext", "audience_has_children",
    "add_family_caveats", "caveat_sentence", "date_authority_claims", "date_span_lines", "ended_roles",
    "find_trip_fact_issues", "missing_family_caveat", "fix_trip_facts", "fix_thin_moments", "thin_moment_issues",
    "minimised_hazards", "background_bases", "names_thin", "MINIMISE_RE", "THIN_EXTRA_MAX",
    "moments_named", "names_moment", "optional_activities", "optional_as_fixed", "included_as_optional", "near_as_at",
    "pace_claims", "parse_moments", "rarity_claims", "role_claim", "timing_claims", "with_date_spans",
    "service_claims",
]
