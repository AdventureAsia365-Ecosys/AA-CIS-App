"""AA-1148 (S290) — the policy values of the content workspace (hand-made tours and the supplier links), read from the
platform settings registry (CIS 478, Platform › Settings; tech lead S279: a threshold or limit is a setting, never a
code literal or an env var alone).

    cis.content.manual_tour.max_days         the most day cards a hand-made tour may have
    cis.content.manual_tour.min_inclusions   the "What is included" items a hand-made tour needs before it can publish
    cis.content.manual_tour.min_exclusions   the "What is not included" items it needs before it can publish
    cis.content.supplier_link.bulk_max       the most tours one "link to a supplier" action may change

and the business option lists of the hand-made tour form (data an administrator edits, never code lists):

    cis.content.manual_tour.tour_types       the tour types [{code, label, label_vi}], in the order shown
    cis.content.manual_tour.quick_inclusions the one-click "What is included" items
    cis.content.manual_tour.quick_exclusions the one-click "What is not included" items

:func:`limits` reads the four limits and :func:`form_options` the three lists (the reader's cache; the compiled defaults
when the database cannot be read). A malformed entry of a list is skipped, never an error: an administrator's typo
hides one option, it does not break the form. Same file in the migration PR (CIS 478, which seeds the keys) and the
code PR that wires it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

MAX_DAYS_KEY = "cis.content.manual_tour.max_days"
MIN_INCLUSIONS_KEY = "cis.content.manual_tour.min_inclusions"
MIN_EXCLUSIONS_KEY = "cis.content.manual_tour.min_exclusions"
BULK_MAX_KEY = "cis.content.supplier_link.bulk_max"
TOUR_TYPES_KEY = "cis.content.manual_tour.tour_types"
QUICK_INCLUSIONS_KEY = "cis.content.manual_tour.quick_inclusions"
QUICK_EXCLUSIONS_KEY = "cis.content.manual_tour.quick_exclusions"
KEYS: tuple[str, ...] = (MAX_DAYS_KEY, MIN_INCLUSIONS_KEY, MIN_EXCLUSIONS_KEY, BULK_MAX_KEY, TOUR_TYPES_KEY,
                         QUICK_INCLUSIONS_KEY, QUICK_EXCLUSIONS_KEY)
# The shape of an option entry: a code is a stored identifier, a label fits one form line.
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_LABEL_MAX = 200  # technical-constant: request size bound of one label, like facts.TEXT_SHORT


@dataclass(frozen=True)
class Limits:
    max_days: int
    min_inclusions: int
    min_exclusions: int
    bulk_max: int

    def as_dict(self) -> dict:
        return {"max_days": self.max_days, "min_inclusions": self.min_inclusions,
                "min_exclusions": self.min_exclusions, "bulk_max": self.bulk_max}


async def limits() -> Limits:
    """The values in force (platform scope)."""
    from shared import settings

    return Limits(
        max_days=int(await settings.aget(MAX_DAYS_KEY)),
        min_inclusions=int(await settings.aget(MIN_INCLUSIONS_KEY)),
        min_exclusions=int(await settings.aget(MIN_EXCLUSIONS_KEY)),
        bulk_max=int(await settings.aget(BULK_MAX_KEY)),
    )


@dataclass(frozen=True)
class FormOptions:
    tour_types: tuple[dict, ...]          # {code, label, label_vi}
    quick_inclusions: tuple[str, ...]
    quick_exclusions: tuple[str, ...]

    def tour_type(self, code: Optional[str]) -> Optional[dict]:
        return next((t for t in self.tour_types if t["code"] == code), None)

    def as_dict(self) -> dict:
        return {"tour_types": [dict(t) for t in self.tour_types], "quick_inclusions": list(self.quick_inclusions),
                "quick_exclusions": list(self.quick_exclusions)}


def _text(value: Any) -> str:
    return " ".join(value.split())[:_LABEL_MAX] if isinstance(value, str) else ""


def clean_tour_types(value: Any) -> tuple[dict, ...]:
    """The well-formed entries of the tour types setting, first one of each code kept."""
    out: list[dict] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        code, label = item.get("code"), _text(item.get("label"))
        if not (isinstance(code, str) and _CODE.match(code) and label) or any(t["code"] == code for t in out):
            continue
        out.append({"code": code, "label": label, "label_vi": _text(item.get("label_vi")) or label})
    return tuple(out)


def clean_items(value: Any) -> tuple[str, ...]:
    """The non-empty text items of a quick-pick list, duplicates (case-blind) dropped."""
    out: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = _text(item)
        if text and text.lower() not in {x.lower() for x in out}:
            out.append(text)
    return tuple(out)


async def form_options() -> FormOptions:
    """The option lists in force (platform scope)."""
    from shared import settings

    return FormOptions(
        tour_types=clean_tour_types(await settings.aget(TOUR_TYPES_KEY)),
        quick_inclusions=clean_items(await settings.aget(QUICK_INCLUSIONS_KEY)),
        quick_exclusions=clean_items(await settings.aget(QUICK_EXCLUSIONS_KEY)),
    )


__all__ = ["BULK_MAX_KEY", "FormOptions", "KEYS", "Limits", "MAX_DAYS_KEY", "MIN_EXCLUSIONS_KEY", "MIN_INCLUSIONS_KEY",
           "QUICK_EXCLUSIONS_KEY", "QUICK_INCLUSIONS_KEY", "TOUR_TYPES_KEY", "clean_items", "clean_tour_types",
           "form_options", "limits"]
