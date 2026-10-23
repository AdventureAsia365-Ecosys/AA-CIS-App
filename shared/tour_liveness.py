"""AA-986 L1: the "tour is served" rule of CIS, in one place.

A CIS tour is served when its Master Content row is live (``master_status = 'active'`` and not deleted) AND its
source version is live (``source_status = 'active'`` and not deleted). ``catalog._live_tour`` (CIS 258) has used both
halves since AA-847; ``acp_contract.v_active_tour_atoms`` used only the Master half until CIS 271, so the atoms of a
tour trashed or superseded at S0/A0 stayed live for A3, the Slate and the tenant routes while the catalog hid it
(audit S248, docs/audits/2026-10-23-S248-tour-deletion-propagation/memo.md section 2.2).

Readers that cannot use the view (it runs with its owner's rights and the tenant pool is not granted it: CIS 241)
inline the rule from here instead of copying the predicates by hand. CIS 271 writes the same predicates into the
view; tests/unit/test_aa986_tour_liveness.py checks that the migration and these fragments agree.

The fragments are plain SQL with fixed aliases chosen by the caller (never user input).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Error code of the 409 an admin action returns when the source of the tour is not live (AA-986 L1).
SOURCE_NOT_LIVE = "source_not_live"


def live_master_sql(pt: str = "pt") -> str:
    """Master Content half of the rule, on published_tours aliased ``pt``."""
    return f"{pt}.master_status = 'active' AND {pt}.deleted_at IS NULL"


def live_source_sql(rt: str = "rt") -> str:
    """Source half of the rule, on raw_tours aliased ``rt`` (trashed, superseded and deleted versions fail it)."""
    return f"{rt}.source_status = 'active' AND {rt}.deleted_at IS NULL"


def served_tour_joins_sql(tour_col: str, *, pt: str = "pt", rt: str = "rt") -> str:
    """The two inner joins that keep only served tours, for a row whose tour id is ``tour_col``
    (for example ``ta.tour_id``). Same rows as acp_contract.v_active_tour_atoms (CIS 271) applies to atoms."""
    return (
        f"JOIN gold_aa_internal.published_tours {pt}\n"
        f"      ON {pt}.tour_id = {tour_col} AND {live_master_sql(pt)}\n"
        f"    JOIN silver_aa_internal.raw_tours {rt}\n"
        f"      ON {rt}.tour_id = {pt}.tour_id AND {live_source_sql(rt)}"
    )


def live_source_exists_sql(tour_col: str, *, rt: str = "rt_live") -> str:
    """``EXISTS`` test: the tour ``tour_col`` has a live source version row."""
    return (f"EXISTS (SELECT 1 FROM silver_aa_internal.raw_tours {rt} "
            f"WHERE {rt}.tour_id = {tour_col} AND {live_source_sql(rt)})")


SOURCE_STATE_SQL = """
    SELECT source_status::text AS source_status, deleted_at
      FROM silver_aa_internal.raw_tours
     WHERE tour_id = $1::uuid
"""


@dataclass(frozen=True)
class SourceState:
    """The source version row of a tour, as far as liveness goes."""
    tour_id: str
    exists: bool
    source_status: Optional[str] = None
    deleted_at: Optional[datetime] = None

    @property
    def live(self) -> bool:
        return self.exists and self.source_status == "active" and self.deleted_at is None

    @property
    def reason(self) -> str:
        """Short machine reason: missing | trashed | superseded | deleted | live."""
        if not self.exists:
            return "missing"
        if self.source_status and self.source_status != "active":
            return self.source_status
        if self.deleted_at is not None:
            return "deleted"
        return "live"


async def source_state(conn, tour_id) -> SourceState:
    """Read the source row of ``tour_id`` (one indexed read on the raw_tours primary key)."""
    row = await conn.fetchrow(SOURCE_STATE_SQL, str(tour_id))
    if row is None:
        return SourceState(tour_id=str(tour_id), exists=False)
    return SourceState(tour_id=str(tour_id), exists=True, source_status=row["source_status"],
                       deleted_at=row["deleted_at"])


_REASON_TEXT = {
    "missing": "has no source row in S0/A0",
    "trashed": "was deleted (trashed) in S0/A0",
    "superseded": "was replaced by a newer upload in S0/A0 (this version is superseded)",
    "deleted": "is marked deleted in S0/A0",
}


def source_not_live_message(state: SourceState, action: str) -> str:
    """Staff-facing sentence for the 409. Starts with the code so API clients can match it; the admin UI shows
    the whole ``detail`` string (frontend/app/_kit/api.ts)."""
    why = _REASON_TEXT.get(state.reason, "is not live in S0/A0")
    if state.reason == "superseded":
        todo = "Work on the newer version of this tour instead, or dismiss this item."
    else:
        todo = "Restore the source tour first (Upload page, Trashed Source Tours, Restore), or dismiss this item."
    return f"{SOURCE_NOT_LIVE}: cannot {action}: the source tour {why}. {todo}"


class SourceNotLiveError(ValueError):
    """Raised by the publish path (services/export/handler.process_export) when the source of the version is
    not live. A ValueError so callers that already treat 'not publishable' as ValueError keep working."""

    code = SOURCE_NOT_LIVE

    def __init__(self, state: SourceState, action: str = "publish to Master Content"):
        super().__init__(state, action)
        self.state = state
        self.action = action

    def __str__(self) -> str:
        return source_not_live_message(self.state, self.action)
