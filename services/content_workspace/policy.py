"""AA-1148 (S290) — the policy values of the content workspace (hand-made tours and the supplier links), read from the
platform settings registry (CIS 478, Platform › Settings; tech lead S279: a threshold or limit is a setting, never a
code literal or an env var alone).

    cis.content.manual_tour.max_days         the most day cards a hand-made tour may have
    cis.content.manual_tour.min_inclusions   the "What is included" items a hand-made tour needs before it can publish
    cis.content.manual_tour.min_exclusions   the "What is not included" items it needs before it can publish
    cis.content.supplier_link.bulk_max       the most tours one "link to a supplier" action may change

:func:`limits` reads the four (the reader's 30 s cache; the compiled defaults when the database cannot be read). Same
file in the migration PR (CIS 478, which seeds the keys) and the code PR that wires it.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_DAYS_KEY = "cis.content.manual_tour.max_days"
MIN_INCLUSIONS_KEY = "cis.content.manual_tour.min_inclusions"
MIN_EXCLUSIONS_KEY = "cis.content.manual_tour.min_exclusions"
BULK_MAX_KEY = "cis.content.supplier_link.bulk_max"
KEYS: tuple[str, ...] = (MAX_DAYS_KEY, MIN_INCLUSIONS_KEY, MIN_EXCLUSIONS_KEY, BULK_MAX_KEY)


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


__all__ = ["BULK_MAX_KEY", "KEYS", "Limits", "MAX_DAYS_KEY", "MIN_EXCLUSIONS_KEY", "MIN_INCLUSIONS_KEY", "limits"]
