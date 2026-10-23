"""S1-first step 1 (M2a, S248) — the per-tour fact table, built from the raw source by code. Pure, $0, no LLM.

The S246 content-quality advisory (root `docs/audits/2026-10-21-S246-content-quality-advisory/`, §5 step 1 and
q4-q8 §5) asks for one table per tour that the new S1 writer reads instead of the raw source, where every value
points back at the source text it came from:

| section | kind | what it holds |
|---|---|---|
| duration | derived | hours / one_day / multi_day / unknown, day + night counts, from `day_parse.parse_duration` |
| days | verbatim | the source "Day N" lines (ranges expanded), from `day_parse.source_day_markers` |
| schedule | verbatim | clock-time lines ("09:00 – Arrive …") and numbered stop lines ("01" + title) of a no-day tour |
| places | verbatim | capitalised names in the source (`hygiene.proper_nouns`) with the sentence they sit in |
| numbers | verbatim | each figure with the unit / noun right after it ("20km", "30 species of mammals") |
| included / excluded | verbatim | inclusion / exclusion lines, plus "not included" sentences anywhere in the source |
| optional | verbatim | sentences that make an activity conditional ("optional", "if time permits", "we will try to") |
| conflicts | derived | the source contradicting itself (duration vs day markers, an item both included and excluded) |
| missing | derived | agency fields the source never states (group size, minimum age, difficulty, start time, …): the |
|   |   | "ask the supplier" list (advisory Q5); the writer must not invent them |
| do_not_use | verbatim | what the page must not repeat: URLs, e-mails, phone numbers, the operator's own name |

Two kinds of value (q4-q8 §5): a **verbatim** entry is a substring of its source field, and its `quote` (the
sentence or line it came from) is too — `verify_pointers` proves that for every entry; a **derived** entry is
computed from verbatim spans and lists them in `quotes`. The step-1 success bar "100% of values match the source
verbatim" is `verify_pointers(...) == []`.

Nothing here is wired into the pipeline (no gate, no shadow code): S1-first freezes new gates for four weeks
(advisory Q2). The writer path reads the table from step 2 on, behind a flag.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from services.acp_content_writing.hygiene import fold, phrase_tokens, proper_nouns
from services.content_generation.day_parse import parse_duration, source_day_markers

SCHEMA = "s1-fact-table/1"
# The raw source fields S1 reads (content_generation/grounding.SOURCE_FIELDS), in reading order.
SOURCE_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries",
                 "country", "duration", "price", "inclusions", "exclusions")
TEXT_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries", "inclusions", "exclusions")

# ------------------------------------------------------------------ text helpers


def field_text(value: Any) -> str:
    """A source field as one text (a highlights list joined by newlines), the form every pointer indexes."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(field_text(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {field_text(v)}" for k, v in value.items())
    return str(value)


def source_fields(tour: dict) -> dict[str, str]:
    return {f: field_text(tour.get(f)) for f in SOURCE_FIELDS}


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])[^\S\n]+(?=[^a-z\s])|\n")


def sentences(text: str) -> list[str]:
    """Sentences and lines of `text`, each a verbatim substring (stripped). A line break ends a sentence, so a
    bullet line is one unit; a period inside a figure ("2.5 hours") or before a lower-case word does not."""
    return [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s and s.strip()]


_BULLET_RE = re.compile(r"^[\s•●▪◦·\-–—*×✓✔✗✘]+")
_HEADER_RE = re.compile(r"^(?:what(?:'s|’s| is)?\s+(?:not\s+)?included|(?:the\s+)?(?:tour\s+|cost\s+|price\s+)?"
                        r"(?:inclusions?|exclusions?|includes?|excludes?|not included|included)|"
                        r"(?:cost|price)\s+(?:includes?|excludes?))\s*[:\-–—]?\s*$", re.I)


def item_lines(text: str) -> list[str]:
    """The item lines of an inclusions / exclusions field (bullets stripped, header and marker-only lines
    dropped), each a verbatim substring of `text`."""
    out = []
    for raw in re.split(r"\n|\t|•|●|▪", text or ""):
        line = _BULLET_RE.sub("", raw).strip()
        if len(line) < 3 or _HEADER_RE.match(line):
            continue
        out.append(line)
    return out


# ------------------------------------------------------------------ items (included / excluded / optional)

# Item groups an inclusion claim is checked by. "meals" is the generic group: a source that excludes any meal
# contradicts a page saying "meals included".
ITEM_GROUPS: dict[str, str] = {
    "breakfast": r"breakfasts?",
    "lunch": r"lunch(?:es)?|picnic",
    "dinner": r"dinners?|suppers?",
    "meals": r"meals?|food|full[\s-]board|half[\s-]board",
    "drinks": r"drinks?|beverages?|alcohol(?:ic)?",
    "guide": r"guides?|guiding|guide services",
    "transport": r"transport(?:ation)?|transfers?|vehicles?|pick[\s-]?ups?|drop[\s-]?offs?",
    "flights": r"flights?|air\s?fares?|airfares?|airline tickets?",
    "train": r"trains?|rail(?:way)? tickets?|train services",
    "accommodation": r"accommodation|hotels?|lodges?|guest\s?houses?|homestays?|resorts?|tents?|camping",
    "entrance_fees": r"entrance(?: fees?)?|entry fees?|admission(?: fees?)?|park fees?|permits?|tickets?",
    "insurance": r"insurances?",
    "tips": r"tips|tipping|gratuit(?:y|ies)",
    "equipment": r"equipment|gear|bikes?|bicycles?|helmets?|kayaks?|rafts?",
    "visa": r"visas?",
}
_ITEM_RES = {k: re.compile(rf"\b(?:{v})\b", re.I) for k, v in ITEM_GROUPS.items()}
MEAL_GROUPS = ("breakfast", "lunch", "dinner", "meals")
CONFLICT_GROUPS = frozenset({"breakfast", "lunch", "dinner", "drinks", "tips", "visa"})


def item_groups(text: str) -> list[str]:
    return [k for k, rx in _ITEM_RES.items() if rx.search(text or "")]


_NOT_INCLUDED_RE = re.compile(r"\bnot\s+(?:be\s+)?(?:included|include|provided|covered)\b|\bexclud(?:ed|es|ing)\b|"
                              r"\bat\s+(?:your\s+)?own\s+(?:cost|expense)\b|\bown\s+expense\b|"
                              r"\bpaid\s+(?:separately|locally|directly)\b|\bextra\s+(?:cost|charge)\b", re.I)
_OPTIONAL_RE = re.compile(r"\boptional(?:ly)?\b|\bif\s+time\s+permits\b|\b(?:upon|on)\s+request\b|"
                          r"\bat\s+(?:an?\s+)?(?:extra|additional)\s+(?:cost|charge)\b|\badditional\s+costs?\s+"
                          r"(?:may\s+)?apply\b|\bwe\s+(?:will\s+)?try\s+to\b|\bweather\s+permitting\b|"
                          r"\bsubject\s+to\b|\bif\s+(?:guests\s+are\s+)?(?:capable|possible|available)\b|"
                          r"\bat\s+(?:an?\s+)?extra\b|\bfor\s+an?\s+(?:extra|additional)\s+(?:fee|cost)\b", re.I)


# An exclusion that depends on the itinerary ("meals not mentioned in the itinerary", "drinks unless specified"):
# listed, but it cannot contradict a page that names one the itinerary does include.
_CONDITIONAL_RE = re.compile(r"\b(?:unless|except|other\s+than|not\s+(?:mentioned|specified|described|listed|stated)|"
                             r"apart\s+from|as\s+per|other\s+than)\b", re.I)


def _entry(value: str, field: str, quote: str, **extra) -> dict:
    return {"kind": "verbatim", "value": value, "field": field, "quote": quote, **extra}


# ------------------------------------------------------------------ numbers bound to nouns

UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "km": ("km", "kms", "kilometre", "kilometres", "kilometer", "kilometers"),
    "m": ("m", "mtr", "mtrs", "metre", "metres", "meter", "meters", "masl"),
    "ft": ("ft", "feet", "foot"),
    "mi": ("mi", "mile", "miles"),
    "h": ("h", "hr", "hrs", "hour", "hours"),
    "min": ("min", "mins", "minute", "minutes"),
    "day": ("day", "days"),
    "night": ("night", "nights"),
    "week": ("week", "weeks"),
    "pct": ("%", "percent", "per cent"),
    "people": ("people", "pax", "person", "persons", "guests", "participants", "travellers", "travelers"),
    "year": ("year", "years", "yrs"),
    "money": ("$", "usd", "us$", "yen", "¥", "jpy", "twd", "nt$", "inr", "rs", "€", "eur", "thb", "baht",
              "rupees", "dollars", "dollar", "vnd", "kip", "won", "krw", "rmb", "cny", "lkr", "npr", "php", "pkr"),
    "storey": ("storey", "storeys", "story", "stories", "floor", "floors"),
    "step": ("step", "steps", "stairs"),
    "ha": ("ha", "hectare", "hectares", "acre", "acres"),
    "km2": ("sq km", "sq. km", "sqkm", "km2", "km²", "square kilometre", "square kilometres", "square kilometer",
            "square kilometers", "sq mi", "square miles"),
    "m2": ("sq m", "m2", "m²", "square metre", "square metres", "square meter", "square meters", "sq ft",
           "square feet", "squared meter", "squared meters", "squared metre", "squared metres"),
    "star": ("star",),
}
_UNIT_OF = {alias: unit for unit, aliases in UNIT_ALIASES.items() for alias in aliases}
_UNIT_ALT = "|".join(sorted((re.escape(a).replace(r"\ ", r"[^\S\n]+") for a in _UNIT_OF
                             if a not in ("%", "$", "¥", "€", "us$", "nt$")), key=len, reverse=True))
# A figure: optional currency sign, digits with thousands separators / decimals, an optional range end, then an
# optional unit glued or spaced ("20km", "3-4 hrs", "4,250 metres", "¥150", "15,250-foot").
NUMBER_RE = re.compile(
    r"(?P<cur>(?:us\$|nt\$|\$|¥|€)[^\S\n]?)?(?<![\w.,])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:[^\S\n]*(?:-|–|—|to)[^\S\n]*(?P<num2>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?))?"
    r"(?P<pct>[^\S\n]?%)?"
    r"(?:[^\S\n]?\+?[^\S\n]?-?(?P<unit>(?:" + _UNIT_ALT + r"))(?![a-z]))?",
    re.I)
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’-]*")
_STOP = frozenset("""a an the of and or to in on at by for from with into onto over under about around approx
approximately roughly some each per this that these those its their our your is are was were be been up down
nearly almost over just under more than less only total plus""".split())
YEAR_RE = re.compile(r"^(1[0-9]{3}|20[0-9]{2})$")


def norm_number(token: str) -> str:
    t = (token or "").replace(",", "")
    if "." in t:
        t = t.rstrip("0").rstrip(".") or "0"
    return t


def unit_of(match: re.Match) -> Optional[str]:
    if match.group("cur"):
        return "money"
    if match.group("pct"):
        return "pct"
    u = match.group("unit")
    return _UNIT_OF.get(re.sub(r"\s+", " ", u.lower())) if u else None


def next_words(text: str, start: int, n: int = 3) -> list[str]:
    """Up to `n` folded, singular content words after `start` (stop words skipped), the noun a bare figure counts.
    Stops at a line break or a closing bracket / clause mark right after the figure ("Ban Som (12) and begin…"
    counts nothing), so a figure never borrows the next line's or clause's words."""
    out = []
    pos = start
    for m in _WORD_RE.finditer(text, start):
        if re.search(r"[\n)\];,:]", text[pos:m.start()]):
            break
        pos = m.end()
        w = fold(m.group(0)).strip("'’-")
        if not w or w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
        if len(out) >= n or text[m.end():m.end() + 1] in ".;:!?\n":
            break
    return out


_ORDINAL_AFTER_RE = re.compile(r"(?:st|nd|rd|th)\b", re.I)
_CLOCK_AFTER_RE = re.compile(r"^(?:[:.]\d{2}\b|[^\S\n]?(?:am|pm|a\.m\.|p\.m\.)(?![a-z]))", re.I)
_CLOCK_BEFORE_RE = re.compile(r"\d[:.]$")
_LABEL_BEFORE_RE = re.compile(r"(?:\b[A-Z][A-Za-z]*\.?|#|No\.?)[^\S\n]*$")


def number_role(text: str, m: re.Match) -> str:
    """figure | ordinal ("12th-century") | clock ("9:00", "4 pm") | label ("Route 199", "Day 3", "Watchtower 8")."""
    after = text[m.end("num2") if m.group("num2") else m.end("num"):]
    before = text[:m.start()]
    if not m.group("unit") and _ORDINAL_AFTER_RE.match(after):
        return "ordinal"
    if _CLOCK_AFTER_RE.match(after) or _CLOCK_BEFORE_RE.search(before):
        return "clock"
    if not m.group("cur") and _LABEL_BEFORE_RE.search(before[-24:]) and not m.group("unit"):
        return "label"
    return "figure"


def number_mentions(text: str) -> list[dict]:
    """Every figure in `text`: {span, numbers, unit, words, role, start}. `span` is verbatim; `numbers` are
    normalised ("4,250" → "4250"; a range gives both ends); `words` are the content words after the figure;
    `role` tells a figure from an ordinal, a clock time or a label number (number_role)."""
    out = []
    for m in NUMBER_RE.finditer(text or ""):
        nums = [norm_number(m.group("num"))]
        if m.group("num2"):
            nums.append(norm_number(m.group("num2")))
        out.append({"span": m.group(0).strip(), "numbers": nums, "unit": unit_of(m), "start": m.start(),
                    "role": number_role(text, m), "words": next_words(text, m.end())})
    return out


# ------------------------------------------------------------------ missing agency fields

MISSING_PROBES: dict[str, str] = {
    "group_size": r"\bgroup\s+size\b|\b(?:max(?:imum)?|min(?:imum)?|up\s+to)\s+(?:of\s+)?\d+\s+"
                  r"(?:people|pax|persons|guests|participants|travell?ers)\b|\bgroups?\s+of\s+(?:up\s+to\s+)?\d|"
                  r"\bprivate\s+(?:tour|group)\b|\b\d+\s*(?:-|to)\s*\d+\s+(?:guests|pax|people|persons)\b|"
                  r"\bbased\s+on\s+(?:\d+|one|two|three|four)\s+adults?\b",
    "minimum_age": r"\bmin(?:imum)?\s+age\b|\bage[sd]?\s+\d|\byears\s+of\s+age\b|"
                   r"\b\d+\s*(?:\+\s*)?years?\s+(?:old|and\s+(?:above|over|older))\b|"
                   r"\bchildren\s*\(?\s*(?:under|over|above|from|\d)|\(\d+\s+and\s+(?:under|over|older)\)",
    "difficulty": r"\bdifficult(?:y|ies)?\b|\bgrade[ds]?\b|\b(?:easy|moderate|challenging|strenuous|demanding)\b|"
                  r"\bfitness\b|\bbeginners?\b|\ball\s+abilities\b",
    "start_time": r"\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s?(?:am|pm|a\.m\.|p\.m\.)\b|\bpick[\s-]?up\s+time\b",
    "meals": r"\b(?:breakfasts?|lunch(?:es)?|dinners?|meals?)\b",
    "accommodation": r"\b(?:accommodation|hotels?|lodges?|guest\s?houses?|homestays?|resorts?|tents?|camp(?:ing|s)?|"
                     r"ryokans?|overnight|houseboats?)\b",
    "transport": r"\b(?:transport(?:ation)?|transfers?|vehicles?|cars?|vans?|bus(?:es)?|jeeps?|trains?|flights?|"
                 r"ferry|ferries|boats?|drive|driven)\b",
    "exclusions": r"",   # answered by the exclusions field / a "not included" sentence
    "price": r"",        # answered by the price field
}
_MISSING_RES = {k: re.compile(v, re.I) for k, v in MISSING_PROBES.items() if v}

_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.I)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_PHONE_RE = re.compile(r"(?<!\w)\+\d[\d \t().-]{8,}\d(?!\w)|(?<!\w)\(?\d{2,4}\)?[ \t.-]\d{3,4}[ \t.-]\d{3,4}(?!\w)")
_CLOCK_LINE_RE = re.compile(r"^[\s–—\-•*]*(\d{1,2}[:.]\d{2})(?:\s*(?:-|–|to)\s*\d{1,2}[:.]\d{2})?\s*[–—\-:]?\s*(.+)$")
_STOP_NUM_LINE_RE = re.compile(r"^\s*(\d{1,2})\s*$")


# ------------------------------------------------------------------ names

# Title-case words a writer uses in day titles and lists, and a source in its headings, that are not names
# ("Day 3 — Arrival and Orientation").
GENERIC_TITLE_WORDS = frozenset("""
arrival arrive arriving departure depart return returning transfer transfers approach overview orientation descent
ascent ascend crossing exploration explore activities activity study encounter encounters relaxation relax settlement
conclusion commencement deeper session views view kingdom base mid centre center country sites site religious modern
mount mt
monuments monument highlands foothills final first second third last rest optional international airport air
trek trekking hike hiking cycling ride riding drive driving tour touring journey experience welcome farewell leisure
free morning afternoon evening night day days stay overnight excursion excursions visit visits highlights
ocean sea gulf rock volcano church peninsula route road loop circuit trail heritage
discovery adventure safari safaris camp lodge village villages town city capital temples market island islands
coast coastal beach lakes lake river valley valleys pass peak mountain mountains plateau forest forests national
park reserve sanctuary glacier gorge canyon falls waterfall waterfalls cave caves bay shrine museum palace fort
fortress castle garden gardens tower wall gate bridge station terminal port harbour harbor square street district
province region state north south east west northern southern eastern western central upper lower old new great
grand little royal imperial sacred holy ancient historic historical traditional local cultural culture classic
wildlife bird birds birding boat cruise ferry flight train rail railway bus jeep car bike bicycle kayak kayaking
rafting surf surfing climb climbing walk walking swim swimming snorkel snorkeling diving yoga cooking class tea
coffee wine food street rice
""".split())

# ------------------------------------------------------------------ the table


def _duration_section(fields: dict[str, str]) -> dict:
    raw = fields["duration"]
    dur = parse_duration(raw)
    if dur.sub_day:
        kind = "hours"
    elif dur.day_count == 1 and not dur.ambiguous:
        kind = "one_day"
    elif dur.day_count and dur.day_count > 1:
        kind = "multi_day"
    else:
        kind = "unknown"
    return {"kind": "derived", "value": kind, "days": dur.days, "nights": dur.nights, "hours": dur.hours,
            "range": list(dur.range) if dur.range else None, "day_count": dur.day_count,
            "quotes": [{"field": "duration", "quote": raw.strip()}] if raw.strip() else []}


def _days_section(fields: dict[str, str]) -> list[dict]:
    """One entry per source "Day N" line, with `places`: the names in that day's block (its line up to the next
    marker) — the stops of the day, each a verbatim substring of the itinerary."""
    text = fields["itineraries"]
    lines = text.splitlines()
    markers = source_day_markers(text)
    out = []
    for i, (first, last, idx) in enumerate(markers):
        end = markers[i + 1][2] if i + 1 < len(markers) else len(lines)
        places: list[str] = []
        for line in lines[idx:end]:
            for sent in sentences(line):
                for name in sorted(proper_nouns(sent, keep_initial=True)):
                    if name not in places:
                        places.append(name)
        out.append(_entry(lines[idx].strip(), "itineraries", lines[idx].strip(), day_from=first, day_to=last,
                          places=places))
    return out


def _schedule_section(fields: dict[str, str]) -> list[dict]:
    """Clock-time lines and numbered stop lines ("01" then its title line) of the itinerary, verbatim — the shape a
    no-day (hours / one-day) tour usually has. Empty for a tour with day markers."""
    lines = fields["itineraries"].splitlines()
    out = []
    for i, line in enumerate(lines):
        s = line.strip()
        m = _CLOCK_LINE_RE.match(s)
        if m and len(m.group(2)) >= 3:
            out.append(_entry(s, "itineraries", s, time=m.group(1)))
            continue
        m = _STOP_NUM_LINE_RE.match(s)
        if m and i + 1 < len(lines) and lines[i + 1].strip():
            title = lines[i + 1].strip()
            out.append(_entry(title, "itineraries", title, stop=int(m.group(1))))
    return out


def _sentence_of(text: str, start: int) -> str:
    for s in sentences(text):
        idx = text.find(s)
        while idx != -1:
            if idx <= start < idx + len(s):
                return s
            idx = text.find(s, idx + 1)
    return text.strip()


_OPERATOR_CTX_RE = re.compile(r"\b(?:[Cc]ontact|beyond\s+the\s+(?:reach|control)\s+of|operated\s+by|organi[sz]ed\s+by|"
                              r"book\s+with)\s+((?:[A-Z][\w’'&.-]*[^\S\n]?){1,4})")


def _lower_vocab(fields: dict[str, str]) -> set[str]:
    """Folded words the source also writes in lower case: an ordinary word, not a name."""
    text = "\n".join(fields.values())
    return {fold(w) for w in re.findall(r"(?<![A-Za-z])[a-z][a-z’'-]+", text)}


def clean_name(name: str, lower: set[str]) -> str:
    """A capitalised run minus the ordinary words a sentence start or a heading adds ("Visit Mingora" → "Mingora",
    "Early" → ""): leading words the source also writes in lower case or that are generic title words. The result
    is still a verbatim substring of the run (only whole leading words are cut)."""
    rest = name.strip(" ’'\"“”‘")
    while rest:
        head, _sep, tail = rest.partition(" ")
        f = fold(head.strip("’'\"“”‘"))
        if f in lower or f in GENERIC_TITLE_WORDS or f.rstrip("s") in GENERIC_TITLE_WORDS:
            rest = tail.strip()
            continue
        break
    return rest.strip(" ’'\"“”‘")


def _places_section(fields: dict[str, str]) -> list[dict]:
    """Names in the source. A one-word name is kept only when the source also capitalises it mid-sentence (a
    sentence-initial "Prior" / "Enjoy" is grammar, not a place); operator names (do_not_use) are left out."""
    lower = _lower_vocab(fields)
    mid = {t for f in TEXT_FIELDS for sent in sentences(fields[f]) for n in proper_nouns(sent)
           for t in phrase_tokens(n)}
    operators = {fold(e["value"]) for e in _do_not_use(fields) if e.get("reason") == "operator"}
    seen: set[str] = set()
    out = []
    for f in TEXT_FIELDS:
        for sent in sentences(fields[f]):   # per sentence: a capitalised run never crosses a line break
            for raw in sorted(proper_nouns(sent, keep_initial=True)):
                name = clean_name(raw, lower)
                key = " ".join(phrase_tokens(name))
                if (not key or key in seen or name not in sent or re.search(r"[’']t\b", name)
                        or any(op in fold(name) for op in operators)
                        or (len(name.split()) == 1 and key not in mid)):
                    continue
                seen.add(key)
                out.append(_entry(name, f, sent))
    return out


def _numbers_section(fields: dict[str, str]) -> list[dict]:
    out = []
    for f in SOURCE_FIELDS:
        if f in ("country",):
            continue
        text = fields[f]
        for mention in number_mentions(text):
            out.append(_entry(mention["span"], f, _sentence_of(text, mention["start"]),
                              numbers=mention["numbers"], unit=mention["unit"], words=mention["words"],
                              role=mention["role"]))
    return out


def _items_sections(fields: dict[str, str]) -> tuple[list[dict], list[dict], list[dict]]:
    included = [_entry(line, "inclusions", line, groups=item_groups(line)) for line in item_lines(fields["inclusions"])]
    excluded = [_entry(line, "exclusions", line, groups=item_groups(line),
                       conditional=bool(_CONDITIONAL_RE.search(line))) for line in item_lines(fields["exclusions"])]
    optional = []
    for f in ("name", "summary", "description", "highlights", "itineraries", "inclusions"):
        for s in sentences(fields[f]):
            if _NOT_INCLUDED_RE.search(s) and (f != "inclusions" or not _OPTIONAL_RE.search(s)):
                excluded.append(_entry(s, f, s, groups=item_groups(s), conditional=bool(_CONDITIONAL_RE.search(s))))
            if _OPTIONAL_RE.search(s):
                optional.append(_entry(s, f, s, groups=item_groups(s)))
    return included, excluded, optional


def _conflicts(fields: dict[str, str], duration: dict, days: list[dict], included: list[dict],
               excluded: list[dict]) -> list[dict]:
    out = []
    exact = None if duration["value"] in ("hours", "unknown") or duration["range"] else duration["day_count"]
    if days and exact:
        nums: set[int] = set()
        for d in days:
            nums.update(range(d["day_from"], d["day_to"] + 1))
        if len(nums) >= 2 and len(nums) != exact and max(nums) != exact:
            out.append({"kind": "derived", "value": "duration_vs_day_markers",
                        "detail": f"duration says {exact} days, the itinerary has {len(nums)} day(s) "
                                  f"(last day {max(nums)})",
                        "quotes": duration["quotes"] + [{"field": "itineraries", "quote": days[-1]["value"]}]})
    name_dur = parse_duration(fields["name"])
    if exact and name_dur.exact_days and name_dur.exact_days != exact:
        out.append({"kind": "derived", "value": "name_vs_duration",
                    "detail": f"the name says {name_dur.exact_days} days, the duration says {exact}",
                    "quotes": duration["quotes"] + [{"field": "name", "quote": fields["name"].strip()}]})
    # Only a specific item the source both includes and excludes outright ("lunch" in both lists). Generic groups
    # ("meals", "flights", "insurance") are usually qualified on one side ("meals in Kathmandu", "international
    # flights", "personal insurance") and are left to the reader; a conditional exclusion never conflicts.
    inc_groups = {g: e for e in included for g in e["groups"]}
    for e in excluded:
        if e.get("conditional"):
            continue
        for g in e["groups"]:
            if g in inc_groups and g in CONFLICT_GROUPS:
                out.append({"kind": "derived", "value": "item_included_and_excluded", "detail": g,
                            "quotes": [{"field": inc_groups[g]["field"], "quote": inc_groups[g]["quote"]},
                                       {"field": e["field"], "quote": e["quote"]}]})
    return out


def _missing(fields: dict[str, str], excluded: list[dict]) -> list[str]:
    body = "\n".join(fields[f] for f in TEXT_FIELDS) + "\n" + fields["duration"] + "\n" + fields["price"]
    out = []
    for key in MISSING_PROBES:
        if key == "exclusions":
            if not excluded:
                out.append(key)
        elif key == "price":
            if not fields["price"].strip():
                out.append(key)
        elif not _MISSING_RES[key].search(body):
            out.append(key)
    return out


def _do_not_use(fields: dict[str, str]) -> list[dict]:
    out = []
    for f in TEXT_FIELDS:
        text = fields[f]
        for rx, why in ((_URL_RE, "url"), (_EMAIL_RE, "email"), (_PHONE_RE, "phone")):
            for m in rx.finditer(text):
                out.append(_entry(m.group(0), f, _sentence_of(text, m.start()), reason=why))
        for m in _OPERATOR_CTX_RE.finditer(text):   # the supplier's own name ("…beyond the reach of Venture Travel")
            name = m.group(1).strip(" .")
            if name and name.lower() not in ("us", "our", "the"):
                out.append(_entry(name, f, _sentence_of(text, m.start(1)), reason="operator"))
    return out


def build_fact_table(tour: dict) -> dict:
    """The fact table of one raw source tour (the S1 writer's input dict: SOURCE_FIELDS)."""
    fields = source_fields(tour)
    duration = _duration_section(fields)
    days = _days_section(fields)
    included, excluded, optional = _items_sections(fields)
    return {
        "schema": SCHEMA,
        "duration": duration,
        "day_source": "day_markers" if len(days) >= 2 else "none",
        "days": days,
        "schedule": _schedule_section(fields),
        "places": _places_section(fields),
        "numbers": _numbers_section(fields),
        "included": included,
        "excluded": excluded,
        "optional": optional,
        "conflicts": _conflicts(fields, duration, days, included, excluded),
        "missing": _missing(fields, excluded),
        "do_not_use": _do_not_use(fields),
    }


VERBATIM_SECTIONS = ("days", "schedule", "places", "numbers", "included", "excluded", "optional", "do_not_use")


def iter_entries(facts: dict) -> Iterable[tuple[str, dict]]:
    for section in VERBATIM_SECTIONS:
        for e in facts.get(section) or []:
            yield section, e
    yield "duration", facts["duration"]
    for e in facts.get("conflicts") or []:
        yield "conflicts", e


def verify_pointers(facts: dict, tour: dict) -> list[dict]:
    """Every entry whose value or quote is NOT a verbatim substring of the field it points at (an empty list is
    the step-1 bar). A derived entry is checked through its quotes."""
    fields = source_fields(tour)
    bad = []
    for section, e in iter_entries(facts):
        if e.get("kind") == "verbatim":
            text = fields.get(e["field"], "")
            if e["value"] not in text or e["quote"] not in text or e["value"] not in e["quote"]:
                bad.append({"section": section, "value": e["value"], "field": e["field"]})
            for p in e.get("places") or []:
                if p not in text:
                    bad.append({"section": section, "value": p, "field": e["field"]})
        else:
            for q in e.get("quotes") or []:
                if q["quote"] not in fields.get(q["field"], ""):
                    bad.append({"section": section, "value": e.get("value"), "field": q["field"]})
    return bad


def summary_counts(facts: dict) -> dict:
    return {
        "duration_kind": facts["duration"]["value"],
        "day_count": facts["duration"]["day_count"],
        "day_source": facts["day_source"],
        **{s: len(facts.get(s) or []) for s in VERBATIM_SECTIONS},
        "conflicts": [c["value"] for c in facts["conflicts"]],
        "missing": list(facts["missing"]),
    }
