"""AA-690 A0-2 — near-duplicate tours at ingest (design §4.0 A0-2, Nghiệp S206: skip, no manual review).

The exact check (`lower(trim(src_name))` + provider) misses re-uploads that differ in case, spacing
or a stray character. Measured S206 on 761 live raw_tours, same provider + same country:
  - name word-set ≥ 0.8 AND itinerary word-set ≥ 0.9 → 29 pairs, all the same product uploaded twice;
  - itinerary alike but name different (Golden Triangle 4 vs 5 days) or name alike but itinerary
    different (Classic Laos 6 vs 10 days) → variants, which are real, separate products.
So a deterministic rule is enough (no Jev): a **duplicate** is skipped on Commit with reason
`duplicate_of_existing`; a **variant** only adds a note to the Upload preview. Different providers
are never compared (two operators' treks are two products).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

NAME_MIN = 0.8
ITINERARY_MIN = 0.9
VARIANT_NAME_MIN = 0.6
_NAME_STOP = {"tour", "tours", "trip", "days", "day", "the", "and", "of", "to", "in", "a", "with", "for"}

DUPLICATE_MESSAGE = ("Same provider, name and itinerary as the existing tour '{name}' ({duration}) — "
                     "this row will be skipped on Commit.")
VARIANT_NOTE = "Similar to the existing tour '{name}' ({duration}) from the same provider — a variant?"


def norm_provider(p: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (p or "").lower())


def _name_tokens(s: Optional[str]) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower())) - _NAME_STOP


def _text_tokens(s: Optional[str]) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", (s or "").lower()))


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def duration_days(d: Optional[str]) -> Optional[int]:
    m = re.search(r"\d+", str(d or ""))
    return int(m.group()) if m else None


@dataclass
class Match:
    tour_id: Optional[str]
    src_name: str
    duration: Optional[str]
    name_sim: float
    itinerary_sim: float


@dataclass
class NearDup:
    duplicate: Optional[Match] = None
    variant: Optional[Match] = None

    def message(self) -> Optional[str]:
        return DUPLICATE_MESSAGE.format(name=self.duplicate.src_name, duration=self.duplicate.duration or "?") \
            if self.duplicate else None

    def note(self) -> Optional[str]:
        return VARIANT_NOTE.format(name=self.variant.src_name, duration=self.variant.duration or "?") \
            if self.variant else None


def classify(row: dict, candidates: list[dict]) -> NearDup:
    """Pure. `candidates` = existing rows of the same provider (src_name, country, duration,
    src_itineraries, tour_id). Only same-country candidates count."""
    out = NearDup()
    country = (row.get("country") or "").strip().lower()
    if not norm_provider(row.get("provider")) or not country:
        return out
    name, itin, days = _name_tokens(row.get("src_name")), _text_tokens(row.get("src_itineraries")), \
        duration_days(row.get("duration"))
    best_variant = None
    for c in candidates:
        if (c.get("country") or "").strip().lower() != country:
            continue
        ns = _jaccard(name, _name_tokens(c.get("src_name")))
        its = _jaccard(itin, _text_tokens(c.get("src_itineraries")))
        cdays = duration_days(c.get("duration"))
        m = Match(str(c["tour_id"]) if c.get("tour_id") else None, c.get("src_name") or "",
                  c.get("duration"), round(ns, 2), round(its, 2))
        if ns >= NAME_MIN and its >= ITINERARY_MIN and (days is None or cdays is None or days == cdays):
            if out.duplicate is None or (ns, its) > (out.duplicate.name_sim, out.duplicate.itinerary_sim):
                out.duplicate = m
        elif ns >= VARIANT_NAME_MIN or its >= ITINERARY_MIN:
            if best_variant is None or (ns + its) > (best_variant.name_sim + best_variant.itinerary_sim):
                best_variant = m
    if out.duplicate is None:
        out.variant = best_variant
    return out


_CANDIDATES_SQL = """
    SELECT tour_id::text AS tour_id, src_name, country, duration, src_itineraries
    FROM silver_aa_internal.raw_tours
    WHERE tenant_id = $1::uuid
      AND regexp_replace(lower(coalesce(provider, '')), '[^a-z0-9]', '', 'g') = $2
      AND coalesce(source_status, 'active') <> 'trashed'
"""


class Checker:
    """Per-file helper: loads each provider's existing rows once, and remembers rows accepted
    earlier in the same file so an in-file near-duplicate is caught too."""

    def __init__(self, conn, tenant_id: str):
        self.conn, self.tenant_id = conn, str(tenant_id)
        self._by_provider: dict[str, list[dict]] = {}

    async def check(self, row: dict) -> NearDup:
        p = norm_provider(row.get("provider"))
        if not p:
            return NearDup()
        if p not in self._by_provider:
            rows = await self.conn.fetch(_CANDIDATES_SQL, self.tenant_id, p)
            self._by_provider[p] = [dict(r) for r in rows]
        return classify(row, self._by_provider[p])

    def accept(self, row: dict) -> None:
        p = norm_provider(row.get("provider"))
        if p:
            self._by_provider.setdefault(p, []).append(
                {k: row.get(k) for k in ("tour_id", "src_name", "country", "duration", "src_itineraries")})


__all__ = ["Checker", "NearDup", "classify", "duration_days", "norm_provider"]
