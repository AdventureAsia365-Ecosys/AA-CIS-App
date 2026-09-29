"""AA-690 — A0 ingest Jev gates (docs/architecture/at-series-v2-design.md §4.0 A0-1, A0-3, A0-4).

One decide() call per uploaded row asks, about the same state (name, country, duration, summary,
itinerary):
  - a0_row_kind          (choice) — is the row a tour at all, or a POI / hotel / service row?
  - a0_itinerary_usable  (noul)   — does the itinerary describe real day-by-day content?
  - a0_country           (choice) — only when the alias resolver found no country.

Used by BOTH the Upload preview (admin_pipeline.ingest dry_run) and the Commit path
(services/ingestion/handler.process_file), so the two report the same outcome for the same file.

Only an enforce-mode question with a confident verdict changes anything (drop the row, fill the
country). In shadow the verdicts are logged (calibration data, AA-661) and shown as notes in the
preview. Jev errors/timeouts fail open: the row is handled exactly as before.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from shared.llm_client.decide import Decision, decide

STAGE = "a0_ingest"
ROW_KIND_Q = "a0_row_kind"
ITINERARY_Q = "a0_itinerary_usable"
COUNTRY_Q = "a0_country"
TOUR_KINDS = {"multi_day_tour", "day_tour"}
CONCURRENCY = 8
# Shadow notes are shown in the preview only when Jev leans this way clearly enough to be worth a look.
_NOTE_CONFIDENCE = 0.6
_NOTE_ITINERARY_P = 0.3

DROP_MESSAGES = {
    "not_a_tour": "Jev is confident this row is not a tour ({kind}) — it will be skipped on Commit.",
    "thin_itinerary": "Jev is confident the itinerary has no real day-by-day content — it will be skipped on Commit.",
}


@dataclass
class RowVerdict:
    drop_reason: Optional[str] = None      # not_a_tour | thin_itinerary — enforced verdicts only
    kind: Optional[str] = None             # Jev's pick for a0_row_kind (any zone), for messages
    country: Optional[str] = None          # enforced, confident pick when the resolver had none
    notes: list[str] = field(default_factory=list)  # shadow/grey observations for the preview


def _state(row: dict) -> dict:
    return {
        "name": row.get("src_name") or "",
        "country": row.get("country") or "",
        "duration": row.get("duration") or "",
        "summary": str(row.get("src_summary") or row.get("src_description") or "")[:600],
        "itinerary": str(row.get("src_itineraries") or "")[:2500],
    }


def verdict_from(decision: Decision, ask_country: bool) -> RowVerdict:
    """Pure: turn one row's Decision into what ingest does with it (unit-tested)."""
    out = RowVerdict()
    kind_v = decision.verdicts.get(ROW_KIND_Q)
    if kind_v is not None:
        out.kind = kind_v.choice
    picked_kind = decision.choice(ROW_KIND_Q)              # only when enforced + confident
    if picked_kind is not None and picked_kind not in TOUR_KINDS:
        out.drop_reason = "not_a_tour"
    elif decision.rejected(ITINERARY_Q):
        out.drop_reason = "thin_itinerary"
    if ask_country:
        out.country = decision.choice(COUNTRY_Q)
        if out.country == "other":
            out.country = None

    # Notes for the preview when nothing was enforced but Jev leans clearly.
    if out.drop_reason is None and kind_v is not None and kind_v.zone != "error" \
            and kind_v.choice and kind_v.choice not in TOUR_KINDS \
            and (kind_v.probability or 0) >= _NOTE_CONFIDENCE:
        out.notes.append(f"Jev: looks like {kind_v.choice.replace('_', ' ')} ({kind_v.probability:.2f})")
    itin_v = decision.verdicts.get(ITINERARY_Q)
    if out.drop_reason is None and itin_v is not None and itin_v.probability is not None \
            and itin_v.zone != "error" and itin_v.probability <= _NOTE_ITINERARY_P:
        out.notes.append(f"Jev: itinerary looks thin ({itin_v.probability:.2f})")
    country_v = decision.verdicts.get(COUNTRY_Q)
    if ask_country and out.country is None and country_v is not None and country_v.choice \
            and country_v.choice != "other" and (country_v.probability or 0) >= _NOTE_CONFIDENCE:
        out.notes.append(f"Jev: country is probably {country_v.choice} ({country_v.probability:.2f})")
    return out


async def assess_rows(rows: list[dict], pool, source: str) -> list[RowVerdict]:
    """One verdict per row, in order. Never raises (decide() fails open)."""
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _one(i: int, row: dict) -> RowVerdict:
        ask_country = not row.get("country")
        keys = [ROW_KIND_Q, ITINERARY_Q] + ([COUNTRY_Q] if ask_country else [])
        async with sem:
            decision = await decide(STAGE, f"raw:{source}:{i}:{(row.get('src_name') or '')[:80]}",
                                    _state(row), keys, pool=pool)
        return verdict_from(decision, ask_country)

    return list(await asyncio.gather(*[_one(i, r) for i, r in enumerate(rows)]))


async def assess_rows_standalone(rows: list[dict], source: str) -> list[RowVerdict]:
    """For the Commit path (process_file), which has no app pool: a small private pool for the
    duration of the assessment. Never raises — any failure yields empty verdicts (fail-open)."""
    import asyncpg

    from shared.secrets import get_database_url
    try:
        pool = await asyncpg.create_pool(get_database_url(), min_size=1, max_size=4)
    except Exception:
        return [RowVerdict() for _ in rows]
    try:
        return await assess_rows(rows, pool, source)
    finally:
        await pool.close()
