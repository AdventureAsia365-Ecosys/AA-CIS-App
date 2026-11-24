"""AA-1148 (S290) — the structured facts of a hand-made tour: the request shape, the text columns rendered from it and
the required list (what a tour still needs before it can be published).

A hand-made tour is a normal source tour (silver_aa_internal.raw_tours, entry_origin 'manual', CIS 478) whose facts a
person typed in the content workspace instead of an Excel row. The structured form is kept in raw_tours.manual_facts;
the text columns every reader uses (src_itineraries, inclusions, exclusions, duration, group_size, best_time_to_go,
src_summary, src_description) are rendered from it on every save, in the shapes the Excel rows have, so S1, the
re-validation, atomize, Booking and TripPlanner read a hand-made tour like any other. The day blocks use the canonical
"Day N — Title" heading (services/content_generation/day_parse.py).

No DB, no I/O. The limits (most days, fewest inclusions / exclusions) and the tour types come from the settings
registry (services/content_workspace/policy.py); the field length bounds below are request size bounds, not policy. A
tour type is stored as its code plus the label it was saved with (``tour_type_label``), so a type an administrator
later removes or renames never breaks a tour that has it.
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from services.content_workspace.policy import Limits

# technical-constant: request size bounds (one form, typed by a person), not policy values
TEXT_SHORT, TEXT_LONG, TEXT_DAY = 200, 2000, 4000
LIST_ITEMS, DAY_PLACES = 40, 12
MEALS = ("breakfast", "lunch", "dinner")


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", text).strip() if isinstance(text, str) else ""


def _clean_list(items: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        c = _clean(item)
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


class DayIn(BaseModel):
    title: str = Field("", max_length=TEXT_SHORT)
    places: list[str] = Field(default_factory=list, max_length=DAY_PLACES)
    meals: list[Literal["breakfast", "lunch", "dinner"]] = Field(default_factory=list, max_length=3)
    overnight: str = Field("", max_length=TEXT_SHORT)
    description: str = Field("", max_length=TEXT_DAY)

    @field_validator("places")
    @classmethod
    def _places(cls, v: list[str]) -> list[str]:
        if any(len(p) > TEXT_SHORT for p in v):
            raise ValueError(f"a place name is longer than {TEXT_SHORT} characters")
        return v


class FactsIn(BaseModel):
    """The facts form of a hand-made tour. Everything may be empty while it is a draft: the required list says what is
    missing; only the publish needs it complete."""
    name: str = Field("", max_length=TEXT_SHORT)
    country: Optional[str] = Field(None, max_length=60)
    supplier_id: Optional[str] = Field(None, max_length=36)
    summary: str = Field("", max_length=TEXT_LONG)
    nights: Optional[int] = Field(None, ge=0, le=365)
    days: list[DayIn] = Field(default_factory=list, max_length=365)
    inclusions: list[str] = Field(default_factory=list, max_length=LIST_ITEMS)
    exclusions: list[str] = Field(default_factory=list, max_length=LIST_ITEMS)
    group_min: Optional[int] = Field(None, ge=1, le=999)
    group_max: Optional[int] = Field(None, ge=1, le=999)
    # A code of the tour types setting (checked against the options in force by the store, not here).
    tour_type: Optional[str] = Field(None, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    guide_languages: list[str] = Field(default_factory=list, max_length=12)
    best_time_to_go: str = Field("", max_length=500)

    @field_validator("inclusions", "exclusions", "guide_languages")
    @classmethod
    def _items(cls, v: list[str]) -> list[str]:
        if any(len(x) > TEXT_SHORT for x in v):
            raise ValueError(f"an item is longer than {TEXT_SHORT} characters")
        return v


def normalize(form: FactsIn, *, tour_type_label: Optional[str] = None) -> dict:
    """The stored form (raw_tours.manual_facts, version 1): trimmed text, duplicate list items dropped (case-blind),
    meals in breakfast-lunch-dinner order; ``tour_type_label`` is the label of the chosen type (the caller resolves it
    from the options in force)."""
    days = [{"title": _clean(d.title), "places": _clean_list(d.places),
             "meals": [m for m in MEALS if m in set(d.meals)], "overnight": _clean(d.overnight),
             "description": (d.description or "").strip()} for d in form.days]
    return {
        "v": 1, "name": _clean(form.name), "summary": (form.summary or "").strip(), "nights": form.nights,
        "days": days, "inclusions": _clean_list(form.inclusions), "exclusions": _clean_list(form.exclusions),
        "group_min": form.group_min, "group_max": form.group_max, "tour_type": form.tour_type or None,
        "tour_type_label": (tour_type_label or None) if form.tour_type else None,
        "guide_languages": _clean_list(form.guide_languages), "best_time_to_go": _clean(form.best_time_to_go),
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def duration_text(days: int, nights: Optional[int]) -> Optional[str]:
    """'3 days 2 nights' (the Excel duration shape); None without days."""
    if days <= 0:
        return None
    return _plural(days, "day") + (f" {_plural(nights, 'night')}" if nights is not None else "")


def group_text(lo: Optional[int], hi: Optional[int]) -> Optional[str]:
    if lo and hi:
        return f"{lo}-{hi} travellers" if lo != hi else _plural(lo, "traveller")
    if lo:
        return f"from {_plural(lo, 'traveller')}"
    if hi:
        return f"up to {_plural(hi, 'traveller')}"
    return None


def day_block(n: int, day: dict, *, source: bool) -> str:
    """One canonical day block. ``source`` (raw_tours.src_itineraries) adds the places, meals and overnight lines; the
    wording draft gets the title and the description only."""
    title = day.get("title") or f"Day {n}"
    lines = [f"Day {n} — {title}"]
    if source:
        if day.get("places"):
            lines.append("Places: " + ", ".join(day["places"]))
        if day.get("meals"):
            lines.append("Meals: " + ", ".join(m.capitalize() for m in day["meals"]))
        if day.get("overnight"):
            lines.append("Overnight: " + day["overnight"])
    if day.get("description"):
        lines.append(day["description"])
    return "\n".join(lines)


def itinerary_text(days: list[dict], *, source: bool) -> Optional[str]:
    blocks = [day_block(i + 1, d, source=source) for i, d in enumerate(days)]
    return "\n\n".join(blocks) if blocks else None


def render(facts: dict) -> dict:
    """The raw_tours text columns of the stored form (country, supplier and provider are set by the caller)."""
    days = facts.get("days") or []
    extra = []
    if facts.get("tour_type"):
        label = facts.get("tour_type_label") or facts["tour_type"].replace("_", " ").capitalize()
        extra.append("Tour type: " + label)
    if facts.get("guide_languages"):
        extra.append("Guide languages: " + ", ".join(facts["guide_languages"]))
    return {
        "src_name": facts.get("name") or "Untitled tour",
        "src_summary": facts.get("summary") or None,
        "src_description": "\n".join(extra) or None,
        "src_itineraries": itinerary_text(days, source=True),
        "duration": duration_text(len(days), facts.get("nights")),
        "group_size": group_text(facts.get("group_min"), facts.get("group_max")),
        "inclusions": "\n".join(facts.get("inclusions") or []) or None,
        "exclusions": "\n".join(facts.get("exclusions") or []) or None,
        "best_time_to_go": facts.get("best_time_to_go") or None,
    }


def first_wording(facts: dict) -> dict:
    """The fields of the first wording draft of a new hand-made tour: the name, the summary and one canonical block per
    day from the facts; the person rewrites them in the editor (or runs Rewrite, S1)."""
    return {"aa_name": facts.get("name") or None, "aa_summary": facts.get("summary") or None,
            "aa_itineraries": itinerary_text(facts.get("days") or [], source=False), "aa_highlights": []}


_HEAD = re.compile(r"^Days?\s+(\d+)(?:[ \t]*(?:[-–]|to)[ \t]*(\d{1,3}))?\s+—", re.M)


def wording_days(text: Optional[str]) -> int:
    """How many days the canonical "Day N —" headings of a written itinerary cover ("Day 2-3 —" counts two)."""
    covered: set[int] = set()
    for m in _HEAD.finditer(text or ""):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        covered.update(range(a, max(a, b) + 1))
    return len(covered)


def _item(key: str, ok: bool, message: str, *, required: bool = True, **params: Any) -> dict:
    return {"key": key, "ok": bool(ok), "required": required, "message": message, "params": params}


def checklist(facts: dict, *, limits: Limits, country_ok: bool, supplier: Optional[dict],
              wording: Optional[dict]) -> list[dict]:
    """The required list of a hand-made tour, in form order. ``supplier`` is the linked supplier as the Booking view
    lists it (None: not linked; status "unknown" when the view cannot be read, which does not block, "missing" when
    the view no longer lists the id, which does), ``wording`` the fields of the open
    draft (or the live version). Items with ``required`` False are hints that never block a publish."""
    days = facts.get("days") or []
    n_days, nights = len(days), facts.get("nights")
    inc, exc = facts.get("inclusions") or [], facts.get("exclusions") or []
    overlap = sorted({x.lower() for x in inc} & {x.lower() for x in exc})
    sup_status = (supplier or {}).get("status")
    w = wording or {}
    items = [
        _item("name", bool(facts.get("name")), "Give the tour a name"),
        _item("country", country_ok, "Choose the country"),
        _item("supplier", supplier is not None and sup_status not in ("inactive", "missing"),
              "Choose the AA supplier from Booking's list" if supplier is None
              else "The supplier is no longer active in Booking: choose an active one", status=sup_status),
        _item("days", 1 <= n_days <= limits.max_days,
              f"Add the day cards (1 to {limits.max_days} days)", max_days=limits.max_days, count=n_days),
        _item("nights", nights is not None, "Say how many nights the tour has"),
        _item("nights_match", nights is None or n_days == 0 or nights in (n_days - 1, n_days),
              f"{_plural(n_days, 'day')} usually means {_plural(max(n_days - 1, 0), 'night')}: check the nights",
              required=False, days=n_days, nights=nights),
        _item("day_titles", n_days > 0 and all(d.get("title") for d in days), "Every day needs a title",
              missing=[i + 1 for i, d in enumerate(days) if not d.get("title")]),
        _item("day_places", n_days > 0 and all(d.get("places") for d in days), "Every day needs at least one place",
              missing=[i + 1 for i, d in enumerate(days) if not d.get("places")]),
        _item("day_descriptions", n_days > 0 and all(d.get("description") for d in days),
              "Every day needs a description of what happens",
              missing=[i + 1 for i, d in enumerate(days) if not d.get("description")]),
        _item("overnight", all(d.get("overnight") for d in days[: max(min(nights or 0, n_days), 0)]),
              "Say where the travellers sleep on each night", required=False,
              missing=[i + 1 for i, d in enumerate(days[: max(min(nights or 0, n_days), 0)])
                       if not d.get("overnight")]),
        _item("inclusions", len(inc) >= limits.min_inclusions,
              f"What is included: add at least {_plural(limits.min_inclusions, 'item')}",
              minimum=limits.min_inclusions, count=len(inc)),
        _item("exclusions", len(exc) >= limits.min_exclusions,
              f"What is not included: add at least {_plural(limits.min_exclusions, 'item')}",
              minimum=limits.min_exclusions, count=len(exc)),
        _item("no_overlap", not overlap, "An item is both included and not included", items=overlap),
        _item("wording", bool(w.get("aa_name")) and bool(w.get("aa_summary")) and bool(w.get("aa_itineraries")),
              "Write the customer wording: name, summary and the day-by-day text"),
        _item("wording_days", n_days > 0 and wording_days(w.get("aa_itineraries")) == n_days,
              f"The day-by-day text needs one \"Day N —\" block for each of the {_plural(n_days, 'day')}",
              days=n_days, written=wording_days(w.get("aa_itineraries"))),
    ]
    return items


def completeness(items: list[dict]) -> int:
    """Percent of the required items that are done (0..100)."""
    req = [i for i in items if i["required"]]
    return round(100 * sum(1 for i in req if i["ok"]) / len(req)) if req else 100


def missing(items: list[dict]) -> list[dict]:
    return [i for i in items if i["required"] and not i["ok"]]


def facts_from_row(row: Any) -> dict:
    """The stored form of a raw_tours row (manual_facts parsed), or a form rebuilt from the text columns for a tour that
    has none (an Excel tour opened in the workspace: read-only)."""
    import json

    raw = row["manual_facts"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict):
        return raw
    return {"v": 1, "name": row["src_name"] or "", "summary": row["src_summary"] or "", "nights": None, "days": [],
            "inclusions": [x for x in (row["inclusions"] or "").splitlines() if x.strip()],
            "exclusions": [x for x in (row["exclusions"] or "").splitlines() if x.strip()],
            "group_min": None, "group_max": None, "tour_type": None, "tour_type_label": None, "guide_languages": [],
            "best_time_to_go": row["best_time_to_go"] or ""}


__all__ = ["DayIn", "FactsIn", "checklist", "completeness", "day_block", "duration_text", "facts_from_row",
           "first_wording", "group_text", "itinerary_text", "missing", "normalize", "render", "wording_days"]
