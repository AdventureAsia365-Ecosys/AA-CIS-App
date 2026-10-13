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
from typing import Optional

from services.acp_content_writing.hygiene import (
    COMMON_CAPS, content_words, fold, phrase_tokens, sentence_spans, split_seed, strip_tags,
)

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
        m = Moment(name=name, tokens=_moment_tokens(name, naming), trip_lines=[lines[0]])
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
    for m in re.finditer(r"[A-Za-zÀ-ÖØ-öø-ÿ’'-]+", strip_tags(sentence or "")):
        toks = set(phrase_tokens(m.group(0)))
        if toks & moment.tokens and not _PROVINCE_AFTER_RE.match(sentence, m.end()):
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

_OPTIONAL_MARK_RE = re.compile(r"\b(?:optional(?:ly)?|if you wish|if desired|on request|available|can|may)\b", re.I)
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
_ACTIVITY_STOP = frozenset(content_words("activities activity include includes including optional surrounding local "
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
    into one atom per item)."""
    out: list[OptionalActivity] = []
    seen: set[str] = set()
    for m in moments:
        prev_optional = False
        for line in m.trip_lines:
            src = line.split("Source itinerary:", 1)[1].strip() if "Source itinerary:" in line else ""
            if not src:
                continue
            mark = _OPTIONAL_MARK_RE.search(src)
            if mark:
                clause = src[mark.end():]
            elif prev_optional and src[:1].islower():
                clause = src
            else:
                prev_optional = False
                continue
            prev_optional = True
            for item in _ITEM_SPLIT_RE.split(clause):
                words = [w for w in content_words(item) if w not in _ACTIVITY_STOP]
                key = " ".join(words)
                if not words or key in seen:
                    continue
                seen.add(key)
                phrase = re.sub(r"^(?:activities\s+)?(?:include|includes|including)\s+", "", item.strip(" .").strip(),
                                flags=re.I)
                out.append(OptionalActivity(phrase=phrase, words=frozenset(words)))
    return out


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


def _mentions_activity(clause: str, act: OptionalActivity) -> bool:
    """Two of the activity's words ('boat' + 'ride'; 'nomadic' + 'families'), at least one of them
    not generic, or one word that names an activity on its own ('horseback')."""
    have = {_stem(w) for w in content_words(clause)}
    hits = {_stem(w) for w in act.words} & have
    generic = {_stem(w) for w in content_words(" ".join(_GENERIC_ACTIVITY_WORDS))}
    specific = hits - generic
    single = {_stem(w) for w in _SINGLE_WORD_ACTIVITIES}
    return (len(hits) >= min(2, len(act.words)) and bool(specific)) or bool(hits & single)


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
            if hit is not None and not _HEDGE_RE.search(clause):
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
            if not _supported_time(m.group(0), s["clean"], moments):
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
        tags = re.match(r"(?:\s?\[(?:R|F):[^\]]+\])+", text[end:])
        if tags:
            end += tags.end()
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
    r"|\bunlike\s+(?:anything|anywhere|any other)\b"
    r"|\b(?:almost\s+|nearly\s+|practically\s+)?(?:anywhere|nowhere|any\s?place)\s+(?:else\s+)?on\s+(?:earth"
    r"|the planet)\b"
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
                if scope not in seed_scopes and not (not scope and seed_scopes):
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
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|—|–|\s-\s|,\s*(?:and|but|while|yet)\s+|\bbut\b|,\s+(?=founded|built|still))\s*",
                              re.I)
_SPAN_RE = re.compile(
    r"\b(nearly|almost|just under|close to|over|more than|just over|about|around|roughly|some)?\s*"
    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(centuries|hundred years)\b", re.I)
_SMALL = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10}


# where the role went: '… until 1955, when the capital was moved to Thimphu'
_SUCCESSOR_RE = re.compile(r"\b(?:moved|transferred|relocated|shifted)\s+to\s+([A-Z][\w’'-]+(?:\s+[A-Z][\w’'-]+)?)")


@dataclass(frozen=True)
class EndedRole:
    role: str       # 'the capital and seat of government of Bhutan'
    start: Optional[int]
    end: int
    successor: Optional[str] = None  # 'Thimphu' of '… until 1955, when the capital was moved to Thimphu'


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
            sm = _SUCCESSOR_RE.search(ln[um.end():])
            out.append(EndedRole(role=role, start=start, end=int(um.group(1)),
                                 successor=sm.group(1) if sm else None))
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
_LOCAL_GOV_RE = re.compile(
    r"\b(?:district|local|dzongkhag|provincial|regional|municipal|county|village|town)\s+(?:[\w-]+\s+)?"
    r"(?:government(?:al)?|administration|administrative|offices?|seat)\b", re.I)


def _gov_claim(clause: str, country: Optional[str]) -> bool:
    clause = _LOCAL_GOV_RE.sub(" ", clause)
    if _GOV_STRONG_RE.search(clause):
        return True
    if not _GOV_WEAK_RE.search(clause):
        return False
    scope = r"national|the country(?:'s|’s)?|the kingdom(?:'s|’s)?|of the (?:country|kingdom)"
    if country:
        scope += rf"|{re.escape(country)}(?:'s|’s)?"
    return bool(re.search(rf"\b(?:{scope})", clause, re.I))


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
    units = [(s["clean"], "") for s in _body_spans(text, cta)] + [(t, f"{w} ") for w, t in extra_texts if t]
    out = []
    for sentence, prefix in units:
        problem = None
        for clause in _CLAUSE_SPLIT_RE.split(sentence):
            # a clause about where the role went is about the successor, not this place: 'In 1955 the
            # capital moved to Thimphu, where it remains today' / 'Today Thimphu is Bhutan's capital'
            if role.successor and re.search(rf"\b{re.escape(role.successor)}\b", clause):
                continue
            if _gov_claim(clause, country) and _PRESENT_RE.search(clause) and not _PAST_RE.search(clause):
                problem = (f"says it still holds a role the source dates as ended ({role.role}, until {role.end}) — "
                           "put it in the past")
                break
        if problem is None and role.start and (_GOV_RE.search(sentence) or re.search(r"\bserved\b", sentence, re.I)):
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
    if len(_thin_extra(sp["clean"], m)) <= THIN_EXTRA_MAX:
        return []
    return [_quoted("closes on a thin moment (one source fact) with an interpretation the source does not give "
                    "— say it in one plain sentence, or cut the added clause", sp["clean"])]


def _trim_thin(sentence: str, m: Moment, moments: list[Moment]) -> Optional[str]:
    """The sentence cut before its appended interpretation, when the head still names the moment, has
    ≥ 6 words and ≤ THIN_EXTRA_MAX words the seed does not give ('… purges of 1937 — a single, quiet
    encounter with…' → '… purges of 1937.'). The longest such head; None when there is none."""
    best = None
    for cut in _APPOSITIVE_RE.finditer(sentence):
        head = sentence[:cut.start()].rstrip(" ,;:—–")
        if len(head.split()) < 6 or not names_thin(head, m, moments):
            continue
        if len(_thin_extra(head, m)) <= THIN_EXTRA_MAX:
            best = head
    return (best + ".") if best else None


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
            tags = re.search(r"(?:\s?\[(?:R|F):[^\]]+\])+\s*$", raw)
            trimmed = _trim_thin(sp["clean"], m, moments)
            if trimmed:
                text = text[:sp["start"]] + trimmed + (tags.group(0).rstrip() if tags else "") + text[sp["end"]:]
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
    own = split_seed(seed)[0]
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
    moments: list = field(init=False)

    def __post_init__(self):
        self.moments = parse_moments(self.seed)
        if not self.source:
            self.source = split_seed(self.seed)[0]


def find_trip_fact_issues(text: str, ctx: Optional[TripFactContext], *, seo_title: Optional[str] = None,
                          meta_description: Optional[str] = None) -> dict[str, list[str]]:
    """{"trip_fact": [...], "rarity": [...]} — every finding quotes its sentence; title/meta
    findings start with "title " / "meta "."""
    if ctx is None:
        return {"trip_fact": [], "rarity": []}
    extra = (("title", seo_title), ("meta", meta_description))
    trip = optional_as_fixed(text, ctx.moments, ctx.cta) + timing_claims(text, ctx.moments, ctx.cta)
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
    rarity = rarity_claims(text, ctx.source, ctx.cta, extra)
    return {"trip_fact": list(dict.fromkeys(trip)), "rarity": list(dict.fromkeys(rarity))}


__all__ = [
    "EndedRole", "Moment", "OptionalActivity", "RARITY_CLAIM_RE", "TripFactContext", "audience_has_children",
    "add_family_caveats", "caveat_sentence", "date_authority_claims", "date_span_lines", "ended_roles",
    "find_trip_fact_issues", "missing_family_caveat", "fix_trip_facts", "fix_thin_moments", "thin_moment_issues",
    "minimised_hazards", "background_bases", "names_thin", "MINIMISE_RE", "THIN_EXTRA_MAX",
    "moments_named", "names_moment", "optional_activities", "optional_as_fixed", "parse_moments", "rarity_claims",
    "timing_claims", "with_date_spans",
]
