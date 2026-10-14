"""AA-712 — day-by-day skeleton of a published tour (port of AA-TripPlanner-Web
backend/extraction/tour_days.py, AA-675, @ 6523816 — rules and prompt unchanged).

For each itinerary day: title, where the day starts and ends, where the traveller sleeps
(`overnight_place`, the anchor the tour graph links days and tours on) and the other named places.
Source: `published_tours.aa_itineraries` ("Day N — title" blocks).

  1. parse_days()       split the text into day blocks (pure);
  2. rule_day()         "A to B" titles and "overnight/stay in X" sentences (pure);
  3. the a3_place_days model (was tp_extract) reads the whole tour once, only when a non-last day
     has no overnight from the rules (needs_model);
  4. ground_llm_day()   every place the model returns must appear in THAT day's own text;
  5. merge_day()        model value when grounded, else the rule value.

TripPlanner measured on Dev (29/09/2026, 121 tours, 1,489 days): rules alone find a start/end or an
overnight for 55% of days, an explicit "overnight in X" for 21%.

AA-712 additions: `days_hash` (gates the paid call, design §3), `is_transit_day` (outlier rule,
design §4) and `stay_atom_overnights` (trial lever: how many rule-missed nights the day's `stay`
atom would fill — measured and reported, not used to skip the model yet).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import structlog

logger = structlog.get_logger()

# Bump when parse/rule/prompt/anchor logic changes: every tour's days_hash changes and the next
# place_extract recomputes (and may pay for) its skeleton.
RULE_VERSION = 1

# "Day 3 — Stok to Sakti", "Day 4-5: Trek to Base Camp", "Day 12 - Departure"
_DAY_HEAD = re.compile(
    r"^\s*Day\s+(\d{1,2})(?:\s*[-–—&]\s*(\d{1,2}))?\s*[—–:\-]\s*(.+?)\s*$", re.M | re.I)
# "Stok to Sakti via Thiksay—48 Kilometers" -> ("Stok", "Sakti")
_A_TO_B = re.compile(
    r"^(?:(?:drive|trek|fly|ride|cycle|transfer|travel|walk|hike)\s+(?:from\s+)?)?"
    r"(?P<a>[A-Z][^,:;—–]*?)\s+to\s+(?P<b>[A-Z][^,:;—–(]*?)"
    r"(?:\s+via\s+[^—–]*)?(?:\s*[—–-]\s*.*)?$", re.I)
# "Overnight stay in Sakti.", "overnight at Hotel X", "Stay in Leh", "Night in the camp"
# Keywords are case-insensitive; the place itself must start with a capital letter.
_OVERNIGHT = re.compile(
    r"\b(?i:overnight(?:\s+stay)?|stay|night|sleep)\s+(?i:is\s+)?(?i:in|at)\s+(?i:a\s+|an\s+|the\s+)?"
    r"(?P<p>[A-Z][\w'’.\- ]*?)(?=\s*(?:[.,;:()]|$|\s+(?:with|near|before|after|where|and|for|on)\b))",
    re.M)
_TRAILING_NOISE = re.compile(r"\s*(?:\d+\s*(?:km|kilometers|kilometres|miles|hours?|hrs?)\b.*)$", re.I)


@dataclass
class DayBlock:
    day_from: int
    day_to: int
    title: str
    body: str

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}"


@dataclass
class DayRecord:
    day_index: int
    title: str
    start_place: Optional[str] = None
    end_place: Optional[str] = None
    overnight_place: Optional[str] = None
    places: list[str] = field(default_factory=list)
    extracted_by: str = "rule"
    is_transit_day: bool = False


def days_hash(itinerary_text: Optional[str]) -> str:
    """sha256(RULE_VERSION ‖ aa_itineraries): equal hash → the paid day call is skipped."""
    return hashlib.sha256(f"{RULE_VERSION}\n{itinerary_text or ''}".encode("utf-8")).hexdigest()


def parse_days(text: str) -> list[DayBlock]:
    """Split an itinerary into "Day N — title" blocks. A range ("Day 4-5") covers both days."""
    heads = list(_DAY_HEAD.finditer(text or ""))
    blocks: list[DayBlock] = []
    for i, m in enumerate(heads):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if end < start:
            end = start
        body_end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        blocks.append(DayBlock(start, end, m.group(3).strip(), text[m.end():body_end].strip()))
    return blocks


def _clean(place: Optional[str]) -> Optional[str]:
    if not place:
        return None
    p = _TRAILING_NOISE.sub("", place).strip(" .,-–—'\"")
    return p or None


def rule_day(block: DayBlock) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(start, end, overnight) found by the rules alone; None where they find nothing."""
    start = end = None
    m = _A_TO_B.match(block.title)
    if m:
        start, end = _clean(m.group("a")), _clean(m.group("b"))
    o = _OVERNIGHT.search(block.body) or _OVERNIGHT.search(block.title)
    overnight = _clean(o.group("p")) if o else None
    return start, end, overnight


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def grounded(place: Optional[str], text: str) -> bool:
    """True when `place` (case/punctuation-insensitive) appears in `text`."""
    if not place:
        return False
    p = _norm(place)
    return bool(p) and f" {p} " in f" {_norm(text)} "


SYSTEM_PROMPT = (
    "You read an Adventure Asia tour itinerary and return, for every day, where the day starts, "
    "where it ends, where the traveller sleeps, and the other named places visited. You copy "
    "place names exactly as they are written in that day's text. You never add a place that is "
    "not written in that day's text, and you never guess."
)
MAX_BODY_CHARS = 900
MAX_TOKENS = 4000


def build_user_prompt(tour_name: str, blocks: list[DayBlock], max_body_chars: int = MAX_BODY_CHARS) -> str:
    days = "\n\n".join(
        f"[Day {b.day_from}{f'-{b.day_to}' if b.day_to != b.day_from else ''}] {b.title}\n"
        f"{b.body[:max_body_chars]}"
        for b in blocks)
    return f"""Tour: {tour_name}

{days}

Return ONLY a JSON object:
{{"days": [{{"day": <first day number of the block>, "start": <place or null>, "end": <place or null>,
            "overnight": <place or null>, "places": [<other named places visited>]}}]}}

Rules:
- One entry per [Day ...] block above, in order.
- "overnight" is the town/camp/lodge where the traveller sleeps that night. Use null on the last
  day when the tour ends (departure) or when the text does not say.
- If the day stays in one place, start and end are that place.
- Every name must be copied from that day's own text. Do not use a place from another day."""


def parse_llm_days(raw: str) -> dict[int, dict]:
    """{day_from: {...}} from the model's JSON (code fences tolerated). Raises ValueError."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
    data = json.loads(s)
    days = data.get("days") if isinstance(data, dict) else data
    if not isinstance(days, list):
        raise ValueError("model output has no 'days' list")
    out: dict[int, dict] = {}
    for d in days:
        if isinstance(d, dict) and isinstance(d.get("day"), int):
            out[d["day"]] = d
    return out


def ground_llm_day(d: dict, block: DayBlock) -> dict:
    """Keeps only the places that appear in the block's own text."""
    text = block.text

    def one(v):
        return v.strip() if isinstance(v, str) and grounded(v, text) else None

    places = d.get("places") if isinstance(d.get("places"), list) else []
    return {
        "start": one(d.get("start")), "end": one(d.get("end")), "overnight": one(d.get("overnight")),
        "places": [p.strip() for p in places if isinstance(p, str) and grounded(p, text)],
    }


def merge_day(block: DayBlock, rule: tuple, llm: Optional[dict]) -> list[DayRecord]:
    """One record per day the block covers. Grounded model values win; rules fill the gaps."""
    r_start, r_end, r_over = rule
    g = llm or {}
    start = g.get("start") or r_start
    end = g.get("end") or r_end
    overnight = g.get("overnight") or r_over
    used_llm = any(g.get(k) for k in ("start", "end", "overnight")) or bool(g.get("places"))
    used_rule = any((r_start, r_end, r_over))
    by = "rule+llm" if used_llm and used_rule else ("llm" if used_llm else "rule")
    places = [p for p in dict.fromkeys(g.get("places") or []) if p not in (start, end, overnight)]
    transit = is_transit_day(block.text)
    return [DayRecord(day, block.title, start, end, overnight, places, by, transit)
            for day in range(block.day_from, block.day_to + 1)]


def needs_model(blocks: list[DayBlock], rules: list[tuple]) -> bool:
    """The model is only called when some day has no overnight place from the rules."""
    return any(r[2] is None for b, r in zip(blocks, rules) if b is not blocks[-1] or len(blocks) == 1)


GenerateFn = Callable[[str, str], Awaitable[str]]          # (system, user) -> raw text


@dataclass
class DaysResult:
    records: list[DayRecord]
    model_called: bool = False
    model_failed: bool = False
    rule_missed_nights: int = 0


async def extract_tour(tour: dict, generate: Optional[GenerateFn]) -> DaysResult:
    """Day records for one tour dict {tour_id, aa_name, aa_itineraries}. Pure except `generate`.
    A model failure falls back to the rules alone (never raises for a bad model answer); a
    BudgetExceeded propagates so the job ends stopped_budget, not as a rules-only `partial` build."""
    blocks = parse_days(tour.get("aa_itineraries") or "")
    if not blocks:
        return DaysResult([])
    rules = [rule_day(b) for b in blocks]
    missed = sum(1 for b, r in zip(blocks, rules) if r[2] is None and (b is not blocks[-1] or len(blocks) == 1))
    out = DaysResult([], rule_missed_nights=missed)
    llm_by_day: dict[int, dict] = {}
    if generate is not None and needs_model(blocks, rules):
        out.model_called = True
        try:
            raw = await generate(SYSTEM_PROMPT, build_user_prompt(tour.get("aa_name") or "", blocks))
            parsed = parse_llm_days(raw)
            llm_by_day = {b.day_from: ground_llm_day(parsed[b.day_from], b)
                          for b in blocks if b.day_from in parsed}
        except Exception as e:  # noqa: BLE001 — rules alone are still a usable answer
            from shared.cost_guard import BudgetExceeded
            if isinstance(e, BudgetExceeded):  # a budget stop is not a model failure: the job stops
                raise
            out.model_failed = True
            logger.warning("place_days_model_failed", tour_id=tour.get("tour_id"), error=str(e)[:200])
    for b, r in zip(blocks, rules):
        out.records.extend(merge_day(b, r, llm_by_day.get(b.day_from)))
    return out


# A night on a train, bus, ferry or flight has no place to put on the map (29/09/2026: "overnight
# bullet train" was geocoded to a point in the sea). The text stays in overnight_place.
_TRANSIT = re.compile(r"\b(train|sleeper|flight|plane|bus|ferry|in transit)\b", re.I)
# A DAY whose text says the traveller flies / takes a train, sleeper, overnight bus or ferry: a long
# jump between two stops is then expected, so it is not an outlier observation (design §4).
_TRANSIT_DAY = re.compile(
    r"\b(flights?|fly|flies|flying|flew|train|sleeper|overnight bus|night bus|ferry|ferries)\b", re.I)


def is_transit(place: Optional[str]) -> bool:
    return bool(place and _TRANSIT.search(place))


def is_transit_day(text: Optional[str]) -> bool:
    return bool(text and _TRANSIT_DAY.search(text))


# A hotel, lodge or camp is not on the map by name (the geocoder and the model both know towns, not
# guesthouses): anchor the night on the day's end place instead. "Base camp" is a place.
_LODGING = re.compile(
    r"\b(hotel|lodge|(?<!base )camp|guest ?house|home ?stay|resort|hostel|farm ?stay|farmstead|inn|manor|"
    r"tea ?house|ger)\b", re.I)


def is_lodging(place: Optional[str]) -> bool:
    return bool(place and _LODGING.search(place))


def anchor_place(overnight: Optional[str], end: Optional[str]) -> Optional[str]:
    """Where a night sits on the map: the overnight place, else (lodging) the day's end place.
    None for a night in transit or with nothing usable."""
    for p in (overnight, end):
        if p and p.strip() and not is_transit(p) and not _LODGING.search(p):
            return p.strip()
    return None


def stay_atom_overnights(atoms: list[dict]) -> dict[int, str]:
    """Trial lever (design §3): the town of each day's `stay` atom — the atom's place when it is
    not lodging, else its place card's locality. {day: place}. Measured only."""
    out: dict[int, str] = {}
    for a in atoms:
        if (a.get("activity_type") or "").lower() != "stay" or a.get("itinerary_day") is None:
            continue
        place = (a.get("place") or "").strip()
        town = place if place and not is_lodging(place) and not is_transit(place) else (a.get("card_locality") or "")
        if town.strip():
            out.setdefault(int(a["itinerary_day"]), town.strip())
    return out


def rule_overnights(itinerary_text: Optional[str]) -> dict[int, Optional[str]]:
    """{day: overnight found by the rules alone (None = missed)} — for the stay-atom measurement."""
    out: dict[int, Optional[str]] = {}
    for b in parse_days(itinerary_text or ""):
        over = rule_day(b)[2]
        for d in range(b.day_from, b.day_to + 1):
            out[d] = over
    return out


def stay_lever_fills(records: list[DayRecord], rule_only_overnight: dict[int, Optional[str]],
                     stay: dict[int, str]) -> int:
    """How many nights the rules missed (non-last days) that the stay-atom rule would fill."""
    last = max((r.day_index for r in records), default=0)
    return sum(1 for d, o in rule_only_overnight.items() if o is None and d != last and stay.get(d))
