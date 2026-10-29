"""AA-789 tier 1 — `SeedGraph`: the typed facts of a seed that the seed-alignment checks compare copy against.

It reads the seed as text, line by line and sentence by sentence, so it works on the T9 seed blocks
(`atom_seed.compose_seed`: label line, 'Source itinerary:', 'Status:', 'Naming:', 'Place background' bullets) and on
the flat '<Place> (day N): …' lines of older fixtures alike. Each relation is extracted only from an explicit
marker; anything the parser is unsure of is left out, and a missing relation makes the check abstain (it never
fires on a guess).

Relations:
- optional items: what an 'Optional activities include …' / 'optional <noun phrase>' / 'Status: OFFERED' sentence
  offers, each with the one word that names it in copy (its anchor: 'boat', 'horseback', 'kayak', 'alms').
- planned moments: the moments the seed plans (label, source sentences without an offer marker), each with its
  names (capitalised words) and its activity heads ('excursion', 'trek', 'hike', 'visit').
- near relations: '<lunch | stop | break …> near <Place>', and the places the same seed sentence also puts an event
  AT (then the check abstains for that place).
- world figures: '<number> percent of the world's <resource>' facts.
- superlatives: the superlative words the seed itself uses (largest, most distinct, last …).
- brief: writer-instruction lines (thin-moment note, SCOPE, brand pack, angle brief, system prompt) and the
  caller's brief texts: audience ranges, quoted phrases, voice words.

Imports nothing from quality_gates (invariant I1: this layer cannot build a gate result).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from services.acp_content_writing.hygiene import fold, split_seed

# Lines (or line parts) that are writer instructions, not source facts.
_BRIEF_LINE_RE = re.compile(r"^\s*(?:Thin moment\b|SCOPE\b|Brand pack\b|Angle brief\b|System prompt\b|AUDIENCE\b"
                            r"|Status:)|writer instruction", re.I)
_STATUS_RE = re.compile(r"^\s*Status:", re.I)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9'\"“(])|\s+—\s+(?=Source itinerary:)")
# An offer marker inside a seed sentence.
_OFFER_RE = re.compile(r"\boptional\b|\bat (?:an )?extra cost\b|\bon request\b|\bOFFERED\b", re.I)
# 'Optional activities include A, B and C' / 'Optional activities: A, B' / 'Optional extras are A and B'
_OFFER_LIST_RE = re.compile(r"\boptional (?:activities|activity|extras?|excursions?|options?)\b\s*"
                            r"(?:include|includes|including|are|is|:)?\s*:?\s*(?P<list>[^;.]+)", re.I)
# 'optional <noun phrase>' up to the next clause break: 'optional alms-giving ceremony'
_OFFER_NP_RE = re.compile(r"\boptional (?!activities|activity|extras?\b|options?\b)(?P<np>[a-z][\w' -]{2,60}?)"
                          r"(?=[;,.)]|\s+(?:and|or|with|then|before|after)\b|$)", re.I)
_LIST_SPLIT_RE = re.compile(r",\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+")
# Words of an offered item that never name it on their own.
GENERIC_ITEM_WORDS = frozenset({
    "ride", "rides", "riding", "visit", "visits", "visiting", "activity", "activities", "take", "taking", "local",
    "water", "terrain", "surrounding", "across", "through", "along", "the", "a", "an", "of", "to", "on", "in", "at",
    "with", "and", "or", "family", "families", "class", "workshop", "ceremony", "trip", "tour", "short", "traditional",
    "extra", "available", "board", "boarding", "small", "group", "guided", "excursion", "excursions", "optional"})
_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ’'-]+")
_CAP_RE = re.compile(r"\b[A-ZÀ-ÖØ-Þ][\w’'-]+")
# A capitalised word that is grammar or a calendar word, not a name.
_NOT_A_NAME = frozenset({"day", "days", "the", "a", "an", "source", "itinerary", "why", "it", "matters", "place",
                         "background", "optional", "activities", "status", "offered", "included", "not", "naming",
                         "location", "next", "morning", "evening", "afternoon", "lunch", "stop", "visit", "hike",
                         "trek", "board", "cross", "arrive", "fly", "drive", "journey", "half", "overnight", "time",
                         "this", "piece", "covers", "of", "trip", "other", "trips", "season", "facts", "fact"})
# Events a 'near' relation is about.
NEAR_EVENTS = frozenset({"lunch", "stop", "break", "picnic", "dinner", "breakfast", "overnight", "camp", "rest",
                         "pause", "halt", "stay"})
_NEAR_RE = re.compile(r"\b(?P<ev>" + "|".join(sorted(NEAR_EVENTS)) + r")\b[^.;]{0,40}?\bnear (?:the )?"
                      r"(?P<place>[A-ZÀ-ÖØ-Þ][\w’'-]+(?:[ -][A-ZÀ-ÖØ-Þ][\w’'-]+)*)", re.I)
_AT_EVENT_RE = re.compile(r"\b(?:" + "|".join(sorted(NEAR_EVENTS | {"show", "lunch", "dinner"})) + r")\b[^.;]{0,40}?"
                          r"\b(?:at|in) (?:the )?(?P<place>[\w’' -]{2,40})", re.I)
_NUM_WORDS = {"zero": 0, "half": 50, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
              "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
              "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_WORLD_FIGURE_RE = re.compile(r"(?P<num>\d+(?:\.\d+)?|[a-z]+(?:[- ][a-z]+)?) (?:per ?cent|%)\s+of (?:the )?"
                              r"(?:world|planet|earth|globe)(?:'s|’s)?\b", re.I)
_SUPERLATIVE_RE = re.compile(r"\b(?:most|least) (?P<adj>[a-z]+)\b|\b(?P<jjs>[a-z]{3,}est)\b|\bthe (?P<only>only)\b"
                             r"|\b(?P<last>last) of its kind\b|\bone of the (?P<last2>last)\b", re.I)
# '-est' words that are not superlatives.
_NOT_JJS = frozenset({"west", "rest", "forest", "interest", "harvest", "honest", "modest", "guest", "nest", "test",
                      "best", "request", "contest", "earnest", "chest", "crest", "quest", "protest", "suggest",
                      "invest", "arrest", "digest", "manifest", "southwest", "northwest", "pest", "vest", "zest"})
_RANGE_RE = re.compile(r"\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\b")
_QUOTED_RE = re.compile(r"(?<![\w’'])['‘\"“](?P<q>[^'’\"”]{3,60})['’\"”](?![\w’'])")
_VOICE_LINE_RE = re.compile(r"(?:voice|tone|system prompt)[^:]*:\s*(?P<words>[^.;\n]+)", re.I)
_LABEL_DASH_RE = re.compile(r"\s+[—–]\s+")
_DAY_PREFIX_RE = re.compile(r"^\s*(?:Day \d+(?: of the trip)?:\s*)", re.I)
_BARE_DAY_RE = re.compile(r"^\s*Day \d+ of the trip\s*$", re.I)
_PAREN_DAY_RE = re.compile(r"(?P<name>[A-ZÀ-ÖØ-Þ][^:()\n]{1,60}?)\s*\((?:day|Day) \d+\):")


def norm(word: str) -> str:
    """The comparison form of a word (or a spaCy lemma): accent-folded lower case, a final '-ing' / plural 's' cut
    from long words ('kayaking' → 'kayak', 'rides' → 'ride')."""
    w = re.sub(r"['’]s$", "", fold(word)).strip("'’-")
    if w.endswith("ing") and len(w) > 6:
        w = w[:-3]
    elif w.endswith("ies") and len(w) > 5:
        w = w[:-3] + "y"
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 4:
        w = w[:-1]
    return w


_GENERIC = frozenset(norm(w) for w in GENERIC_ITEM_WORDS)
# Two names of one thing an anchor must also match.
_ANCHOR_ALIASES = {"horseback": {"horse", "horseback"}, "horse": {"horse", "horseback"}, "kayak": {"kayak", "canoe"}}


def number_value(text: str) -> Optional[float]:
    """'0.4', '70', 'seventy', 'twenty-five', 'zero point four' → a number, else None."""
    t = fold(text).replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        pass
    if " point " in f" {t} ":
        whole, _, frac = t.partition("point")
        w = number_value(whole.strip()) if whole.strip() else 0.0
        digits = [number_value(x) for x in frac.split()]
        if w is None or not digits or any(d is None or d > 9 for d in digits):
            return None
        return float(f"{int(w)}." + "".join(str(int(d)) for d in digits))
    total = 0
    for part in re.split(r"[- ]", t):
        if part not in _NUM_WORDS:
            return None
        total += _NUM_WORDS[part]
    return float(total)


@dataclass
class OptionalItem:
    text: str
    anchor: str  # norm() of the word that names the item in copy
    anchors: frozenset  # anchor plus its aliases
    words: frozenset  # norm() of every content word


@dataclass
class PlannedMoment:
    name: str
    names: frozenset  # norm() of the capitalised words that name the moment
    heads: frozenset  # norm() of the activity words its source sentences plan ('excursion', 'hike')
    text: str


@dataclass
class NearRelation:
    events: frozenset
    place: str
    place_key: str  # fold() of the place, for alias-tolerant matching ('Moron' == 'Mörön')


@dataclass
class SeedGraph:
    source_text: str = ""
    brief_text: str = ""
    optional_items: list[OptionalItem] = field(default_factory=list)
    planned_text: str = ""
    planned_words: frozenset = frozenset()
    moments: list[PlannedMoment] = field(default_factory=list)
    near: list[NearRelation] = field(default_factory=list)
    at_places: frozenset = frozenset()  # fold() of places the seed puts an event AT
    world_figures: list[float] = field(default_factory=list)
    superlatives: frozenset = frozenset()  # norm() of the seed's own superlative words
    source_words: frozenset = frozenset()  # norm() of every word of the source
    naming_words: frozenset = frozenset()  # norm() of the words of the seed's Naming lines
    audience_ranges: list[tuple[int, int]] = field(default_factory=list)
    brief_quotes: list[str] = field(default_factory=list)
    voice_words: frozenset = frozenset()
    place_names: frozenset = frozenset()  # fold() of the capitalised words of the seed (names of its places)

    @property
    def empty(self) -> bool:
        return not self.source_text.strip()


def _words(text: str) -> list[str]:
    """norm() of every word, and of each part of a hyphenated one ('non-technical' → also 'technical')."""
    out = []
    for w in _WORD_RE.findall(text or ""):
        out.append(norm(w))
        if "-" in w:
            out += [norm(p) for p in w.split("-") if len(p) > 2]
    return out


def _sentences(line: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT_RE.split(line) if s.strip()]


def _strip_label(line: str) -> tuple[str, str]:
    """(label, source) of a line: a '<place> — <action> — … — Source itinerary: …' line keeps the part after
    'Source itinerary:' as source and the rest as its label; a bare '<place> — <action>' line is all label; any other
    line is all source."""
    line = _DAY_PREFIX_RE.sub("", line)
    if "Source itinerary:" in line:
        label, _, src = line.partition("Source itinerary:")
        return label.strip(" —–-"), src.strip()
    if _LABEL_DASH_RE.search(line) and not re.search(r"[.!?]\s", _LABEL_DASH_RE.split(line, 1)[0]):
        return line.strip(), ""
    return "", line.strip()


def _label_parts(label: str) -> tuple[str, str]:
    """(place name, action) of a label: 'Uran Togoo — trek into the caldera — Day 3' → ('Uran Togoo', 'trek into the
    caldera'). Day parts are dropped."""
    parts = [p.strip() for p in _LABEL_DASH_RE.split(label) if p.strip() and not re.match(r"Day \d+", p.strip())]
    return (parts[0] if parts else ""), " ".join(parts[1:])


def _item_from(text: str, planned_words: frozenset) -> Optional[OptionalItem]:
    text = re.sub(r"^(?:the|a|an)\s+", "", text.strip(" .,;:'\""), flags=re.I)
    words = [p for w in _WORD_RE.findall(text) for p in w.split("-") if p]  # 'alms-giving' → 'alms', 'giving'
    content = [norm(w) for w in words if norm(w) not in _GENERIC and len(w) > 2]
    for w in content:
        if w in planned_words:
            continue
        aliases = _ANCHOR_ALIASES.get(w, {w})
        return OptionalItem(text=text, anchor=w, anchors=frozenset(aliases),
                            words=frozenset(norm(x) for x in words))
    return None


def _offered_parts(sentence: str) -> list[str]:
    parts: list[str] = []
    m = _OFFER_LIST_RE.search(sentence)
    if m:
        parts += [p for p in _LIST_SPLIT_RE.split(m.group("list")) if p.strip()]
    for m2 in _OFFER_NP_RE.finditer(sentence):
        if not m or not (m.start() <= m2.start() < m.end()):
            parts.append(m2.group("np"))
    status = re.search(r"the itinerary says:\s*[\"“](?P<q>[^\"”]+)", sentence)
    if status:
        parts += _offered_parts(status.group("q"))
    return parts


def build(seed: Optional[str], brief_texts: Optional[Iterable[str]] = None) -> SeedGraph:
    own, _siblings = split_seed(seed)
    source_lines: list[str] = []
    brief_lines: list[str] = []
    naming_lines: list[str] = []
    names_only: list[str] = []  # Location / Day lines: names of the place, never a fact
    labels: list[str] = []
    for raw in own.splitlines():
        line = raw.strip().lstrip("- ").strip()
        if not line:
            continue
        if _BRIEF_LINE_RE.search(line) and not _STATUS_RE.match(line):
            brief_lines.append(line)
            continue
        if line.startswith("Naming"):
            naming_lines.append(line)
            continue
        if line.startswith(("Location:", "Place background (checked", "Other trips that include")) or \
                _BARE_DAY_RE.match(line):
            names_only.append(line)
            continue
        line = re.sub(r"^(?:Why it matters|Season):\s*", "", line)
        label, src = _strip_label(line)
        if label:
            labels.append(label)
        if src:
            source_lines.append(src)
    brief_lines += [t for t in (brief_texts or []) if t]
    g = SeedGraph(source_text="\n".join(source_lines), brief_text="\n".join(brief_lines))

    sentences = [s for ln in source_lines for s in _sentences(ln)]
    offered = [s for s in sentences if _OFFER_RE.search(s)]
    planned = [s for s in sentences if not _OFFER_RE.search(s) and not _STATUS_RE.match(s)]
    # a sentence that offers items and then plans one ('…; trekking along shorelines') keeps its planned tail
    planned += [s.split(";", 1)[1] for s in offered if ";" in s and not _OFFER_RE.search(s.split(";", 1)[1])]
    g.planned_text = "\n".join(planned)
    g.planned_words = frozenset(_words(g.planned_text))
    seen: set[str] = set()
    for s in offered:
        for part in _offered_parts(s):
            item = _item_from(part, g.planned_words)
            if item and item.anchor not in seen:
                seen.add(item.anchor)
                g.optional_items.append(item)

    g.moments = _moments(own, labels, planned)
    for s in sentences:
        for m in _NEAR_RE.finditer(s):
            place = m.group("place").strip()
            g.near.append(NearRelation(events=frozenset({norm(m.group("ev"))} | (
                {"lunch"} if "lunch" in fold(s[:m.end()]) else set())), place=place, place_key=fold(place)))
    at: set[str] = set()
    for rel in g.near:
        for s in sentences:
            if rel.place_key in fold(s) and _AT_EVENT_RE.search(s.replace(f"near {rel.place}", "")):
                at.add(rel.place_key)
    g.at_places = frozenset(at)

    all_source = g.source_text + "\n" + "\n".join(labels)
    for m in _WORLD_FIGURE_RE.finditer(all_source):
        v = number_value(m.group("num"))
        if v is not None:
            g.world_figures.append(v)
    sup: set[str] = set()
    for m in _SUPERLATIVE_RE.finditer(all_source + "\n" + "\n".join(s for s in (seed or "").splitlines()
                                                                   if s.startswith("(r1 seed"))):
        word = m.group("adj") or m.group("jjs") or m.group("only") or m.group("last") or m.group("last2")
        if word and fold(word) not in _NOT_JJS:
            sup.add(norm(word))
    # '(r1 seed also: …)' style notes inside a background line are source too
    for m in _SUPERLATIVE_RE.finditer(seed or ""):
        word = m.group("adj") or m.group("jjs") or m.group("only") or m.group("last") or m.group("last2")
        if word and fold(word) not in _NOT_JJS and "writer instruction" not in fold(seed or "")[
                max(0, m.start() - 200):m.start()]:
            sup.add(norm(word))
    g.superlatives = frozenset(sup)
    g.source_words = frozenset(_words(all_source))
    g.naming_words = frozenset(_words(" ".join(naming_lines)))
    g.place_names = frozenset(fold(w).strip("'’") for w in _CAP_RE.findall(
        all_source + " " + " ".join(naming_lines + names_only + labels))
                              if fold(w) not in _NOT_A_NAME and len(w) > 2)
    for m in _RANGE_RE.finditer(g.brief_text):
        a, b = int(m.group(1)), int(m.group(2))
        if a < b and re.search(r"aged?|years?|children|kids|audience", g.brief_text[max(0, m.start() - 40):m.end()],
                               re.I):
            g.audience_ranges.append((a, b))
    g.brief_quotes = [m.group("q") for m in _QUOTED_RE.finditer(g.brief_text) if len(m.group("q").split()) >= 3]
    voice: set[str] = set()
    for m in _VOICE_LINE_RE.finditer(g.brief_text):
        voice |= {norm(w) for w in re.findall(r"[A-Za-z]+", m.group("words")) if len(w) > 3}
    g.voice_words = frozenset(voice - {"never", "adopt", "persona", "voice", "tone"})
    return g


def _block_lines(lines: list[str]) -> list[str]:
    """The planned lines of one block: no instruction, Naming, Location, Why it matters, Season or bare Day line, and
    no Place background bullet (background says what the place is, never what the trip does)."""
    out: list[str] = []
    in_bg = False
    for ln in lines:
        if ln.startswith("Place background"):
            in_bg = True
            continue
        if in_bg and ln.startswith("- "):
            continue
        in_bg = False
        # 'Why it matters' / 'Season' say why or when, not what the trip does
        if _BRIEF_LINE_RE.search(ln) or ln.startswith(("Naming", "Location", "Why it matters", "Season")) or \
                _BARE_DAY_RE.match(ln):
            continue
        out.append(ln)
    return out


def _moments(own: str, labels: list[str], planned: list[str]) -> list[PlannedMoment]:
    """Planned moments, each with its own planned sentences: one per blank-line block of a T9 seed (its first line is
    the '<place> — <action>' label), else one per '<Place> (day N):' name of a flat line, else one for the seed. A
    moment's names are the capitalised words of its place name only (body text capitalises sentence starts)."""
    blocks: list[tuple[str, str, str]] = []  # (place name, action, body)
    paragraphs = [p for p in re.split(r"\n\s*\n", own) if p.strip()]
    if len(paragraphs) > 1:
        for p in paragraphs:
            lines = [ln.strip() for ln in p.strip().splitlines() if ln.strip()]
            label, src = _strip_label(lines[0])
            name, action = _label_parts(label)
            rest = [_strip_label(ln) for ln in _block_lines(lines[1:])]
            # a label whose own source line is an offer ('take boat rides' over 'Optional activities include …')
            # plans nothing
            offered_src = " ".join(sr for _, sr in [("", src)] + rest if sr and _OFFER_RE.search(sr))
            if offered_src and set(_words(action)) & set(_words(offered_src)) - _GENERIC:
                action = ""  # the label names what the offered source line offers
            action = " ".join(([action] if not _OFFER_RE.search(src) else [])
                              + [_label_parts(lb)[1] for lb, sr in rest if lb and not _OFFER_RE.search(sr)])
            blocks.append((name, action, "\n".join([src] + [sr for _, sr in rest if sr])))
    else:
        for m in _PAREN_DAY_RE.finditer(own):
            nxt = _PAREN_DAY_RE.search(own, m.end())
            blocks.append((m.group("name"), "", own[m.end():nxt.start() if nxt else len(own)]))
        if not blocks:
            parts = [_label_parts(lb) for lb in labels]
            blocks.append((" ".join(n for n, _ in parts), " ".join(a for _, a in parts), "\n".join(planned)))
    out: list[PlannedMoment] = []
    for name, action, body in blocks:
        body_planned = [s for s in _sentences(body) if not _OFFER_RE.search(s) and not _STATUS_RE.match(s)]
        body_planned += [s.split(";", 1)[1] for s in _sentences(body)
                         if _OFFER_RE.search(s) and ";" in s and not _OFFER_RE.search(s.split(";", 1)[1])]
        text = "\n".join(body_planned)
        names = {norm(w) for w in _CAP_RE.findall(name) if fold(w) not in _NOT_A_NAME}
        if not _OFFER_RE.search(action):
            text = (action + "\n" + text).strip()
        out.append(PlannedMoment(name=name, names=frozenset(n for n in names if len(n) > 2),
                                 heads=frozenset(_words(text)), text=text))
    return out


__all__ = ["SeedGraph", "OptionalItem", "PlannedMoment", "NearRelation", "build", "norm", "number_value",
           "NEAR_EVENTS", "GENERIC_ITEM_WORDS"]
