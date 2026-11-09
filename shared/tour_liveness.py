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


# ── AA-986 L3: the served state as tenants see it (root ADR 0015 decision 5) ─────────────────────────────────────────
# The state is per catalog key (raw_tours.source_key = COALESCE(source_group_id, tour_id)), read from
# gold_aa_internal.v_tour_liveness (CIS 272: SELECT to cis_app and aa_app_user, so the main pool and the tenant pool
# both read it). A tenant row points at one version's tour id (its T2 copy at the Master of the version it adopted):
# the key of that version gives the state, so a superseded version whose key is served again reads `served` (Q2: a
# supersede is invisible to the tenant).
SERVED_STATES_SQL = """
    SELECT rt.tour_id::text AS tour_id, tl.state, tl.reason, tl.changed_at
      FROM silver_aa_internal.raw_tours rt
      JOIN gold_aa_internal.v_tour_liveness tl
        ON tl.source_id = (COALESCE(rt.source_group_id, rt.tour_id))::text
     WHERE rt.tour_id = ANY($1::uuid[])
"""

# ── AA-1057 GAP 1: the Subject rule for a Segment Subject's request ────────────────────────────────────────────────
# A Segment (place) Subject is picked on ONE representative member (the seed atom, slate._resolve_representative_atom):
# the request stores that atom and its tour as `trip_id`. The Subject itself lives while its Segment still has at least
# one served member (the rule of the Slate's `segment_dead`, the AA-subject gate's `segment_live` and the derivative
# rule of services/tour_lifecycle/derivatives.py). Every liveness gate on a request (the write / goal / publish 409, the
# served badge, the retirement fan-out's flags, notices, mails and job cancels, the WordPress move to draft) applies
# that rule, not the representative tour alone: retiring the representative while another member is served changes
# nothing for the tenant. The fragments below are plain SQL on the base tables (the tenant pool has no grant on
# acp_contract.v_active_tour_atoms, CIS 241); the member rule is the view's (CIS 271) through served_tour_joins_sql.
# Aliases are fixed (`*_sm`, `*_mt`) and the caller passes only its own fixed aliases / SQL (never user input).
# A Subject stored on a Segment id that merged into another (atom_segment_alias) is read on the surviving id.

def _segment_member_body_sql(r: str, *, sfx: str, exclude_tours: Optional[str] = None) -> str:
    """FROM ... WHERE of the served members of the Segment of request ``r``'s Subject (no SELECT list)."""
    s, m, a, ta = f"s_{sfx}", f"m_{sfx}", f"a_{sfx}", f"ta_{sfx}"
    excl = f"\n           AND {ta}.tour_id NOT IN ({exclude_tours})" if exclude_tours else ""
    return (
        f"FROM acp_shared.subject {s}\n"
        f"          JOIN acp_contract.atom_segment_member {m}\n"
        f"            ON {m}.segment_id IN ({s}.segment_id, (SELECT {a}.segment_id_canonical\n"
        f"                 FROM acp_contract.atom_segment_alias {a} WHERE {a}.segment_id_old = {s}.segment_id))\n"
        f"          JOIN acp_contract.tour_atoms {ta} ON {ta}.atom_id = {m}.atom_id AND NOT {ta}.deleted\n"
        f"               AND NOT {ta}.is_empty_marker\n"
        f"          {served_tour_joins_sql(f'{ta}.tour_id', pt=f'pt_{sfx}', rt=f'rt_{sfx}')}\n"
        f"         WHERE {s}.subject_id = {r}.subject_id AND {s}.tenant_id = {r}.tenant_id\n"
        f"           AND {s}.segment_id IS NOT NULL{excl}"
    )


def segment_served_member_sql(r: str = "r", *, exclude_tours: Optional[str] = None) -> str:
    """``EXISTS`` test: request ``r`` (an angle_gate_request alias) has a Segment Subject whose Segment has a served
    member. ``exclude_tours``: a SQL list of tour ids whose members do not count (the tours a lifecycle event is
    about: "would the Segment still have a served member without them?")."""
    return f"EXISTS (SELECT 1 {_segment_member_body_sql(r, sfx='sm', exclude_tours=exclude_tours)})"


def segment_served_member_tour_sql(r: str = "r", *, exclude_tours: Optional[str] = None) -> str:
    """Scalar subquery: one served member tour of request ``r``'s Segment Subject (lowest tour id), else NULL."""
    body = _segment_member_body_sql(r, sfx="mt", exclude_tours=exclude_tours)
    return f"(SELECT ta_mt.tour_id {body}\n         ORDER BY ta_mt.tour_id LIMIT 1)"


def segment_member_on_tours_sql(r: str, tours: str) -> str:
    """``EXISTS`` test: request ``r``'s Segment Subject has a member atom (live or not) on one of ``tours``."""
    return (f"EXISTS (SELECT 1 FROM acp_shared.subject s_mo\n"
            f"          JOIN acp_contract.atom_segment_member m_mo\n"
            f"            ON m_mo.segment_id IN (s_mo.segment_id, (SELECT a_mo.segment_id_canonical\n"
            f"                 FROM acp_contract.atom_segment_alias a_mo\n"
            f"                WHERE a_mo.segment_id_old = s_mo.segment_id))\n"
            f"          JOIN acp_contract.tour_atoms ta_mo ON ta_mo.atom_id = m_mo.atom_id\n"
            f"         WHERE s_mo.subject_id = {r}.subject_id AND s_mo.tenant_id = {r}.tenant_id\n"
            f"           AND s_mo.segment_id IS NOT NULL AND ta_mo.tour_id IN ({tours}))")


def tour_served_sql(tour_col: str) -> str:
    """``EXISTS`` test: ``tour_col`` is a served tour (live Master + live source; the rule of the joins above)."""
    return (f"EXISTS (SELECT 1 FROM gold_aa_internal.published_tours pt_ts\n"
            f"          JOIN silver_aa_internal.raw_tours rt_ts ON rt_ts.tour_id = pt_ts.tour_id AND "
            f"{live_source_sql('rt_ts')}\n"
            f"         WHERE pt_ts.tour_id = {tour_col} AND {live_master_sql('pt_ts')})")


def request_tour_sql(r: str = "r", ta: str = "ta") -> str:
    """The representative tour of request ``r``: its trip, else its atom's tour (``ta`` = its tour_atoms row)."""
    return f"COALESCE({r}.trip_id, {ta}.tour_id)"


def requests_about_tours_sql(tours: str, *, r: str = "r", ta: str = "ta") -> str:
    """WHERE condition: request ``r`` is about the tours ``tours`` (a SQL list of tour ids, e.g. the versions of a key a
    lifecycle event changed) under the Subject rule. Either
      1. its representative tour is one of them and its Segment Subject (if any) has no served member on another tour,
         or
      2. its representative tour is another one that is not served (retired / paused earlier, while the Segment was
         still served elsewhere), its Segment has a member on these tours, and no served member outside them: the last
         member leaving ends the Subject, so the event is about the request after all.
    A request without a Segment Subject (a Route Subject, an atom request) follows its representative tour (1)."""
    rep = request_tour_sql(r, ta)
    alive = segment_served_member_sql(r, exclude_tours=tours)
    return (f"(({rep} IN ({tours}) AND NOT {alive})\n"
            f"        OR ({r}.subject_id IS NOT NULL AND {rep} NOT IN ({tours}) AND NOT {tour_served_sql(rep)}\n"
            f"            AND {segment_member_on_tours_sql(r, tours)} AND NOT {alive}))")


# The tour an angle-gate request is about: its trip, else the tour of its atom (the S248 probe / L2 derivative rule).
# AA-1057 GAP 1: when that representative tour is paused or retired but the request's Segment Subject still has a
# served member, the request is about that member's tour (served): the Subject rule. Same rows on both pools.
REQUEST_TOURS_SQL = f"""
    SELECT r.request_id::text AS request_id,
           (CASE WHEN tl_rq.state IN ('paused', 'retired')
                 THEN COALESCE({segment_served_member_tour_sql('r')}, {request_tour_sql()})
                 ELSE {request_tour_sql()} END)::text AS tour_id
      FROM acp_shared.angle_gate_request r
      LEFT JOIN acp_contract.tour_atoms ta ON ta.atom_id = r.atom_id
      LEFT JOIN silver_aa_internal.raw_tours rt_rq ON rt_rq.tour_id = {request_tour_sql()}
      LEFT JOIN gold_aa_internal.v_tour_liveness tl_rq
        ON tl_rq.source_id = (COALESCE(rt_rq.source_group_id, rt_rq.tour_id))::text
     WHERE r.request_id = ANY($1::uuid[]) AND r.tenant_id = $2::uuid
"""

# The served state of the tour a LIVE post (publish_log status 'published') is about, for staff routes on the main
# pool (A4): the post's piece -> its angle-gate request (same tenant) -> REQUEST_TOURS_SQL's tour rule -> the key's
# state in v_tour_liveness. NULL when the post is not live, has no tour, or the key has no state (never served).
# AA-1057 GAP 1: 'served' while the request's Segment Subject still has a served member (the Subject rule).
LIVE_POST_TOUR_STATE_SQL = f"""
    SELECT CASE WHEN tl.state IN ('paused', 'retired') AND {segment_served_member_sql('r')}
                THEN 'served' ELSE tl.state END AS state
      FROM acp_shared.publish_log pl
      JOIN acp_shared.content_piece cp ON cp.piece_id = pl.piece_id
      JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id AND r.tenant_id = pl.tenant_id
      LEFT JOIN acp_contract.tour_atoms ta ON ta.atom_id = r.atom_id
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = COALESCE(r.trip_id, ta.tour_id)
      JOIN gold_aa_internal.v_tour_liveness tl
        ON tl.source_id = (COALESCE(rt.source_group_id, rt.tour_id))::text
     WHERE pl.publish_id = $1::uuid AND pl.status = 'published'
"""

TOUR_DISCONTINUED = "tour_discontinued"
TOUR_PAUSED = "tour_paused"
# A4 refuses to mark a live post about a retired tour (ADR 0015 Q3: the tenant's flag + one-click removal stay).
TENANT_REMOVES_POST = "tenant_removes_post"


@dataclass(frozen=True)
class ServedState:
    """The served state of the catalog key behind one tour id. ``state`` None = no state (no Master was ever
    published for the key, or the row is not a CIS tour): nothing to show, nothing to refuse."""
    state: Optional[str] = None
    reason: Optional[str] = None
    changed_at: Optional[datetime] = None

    @property
    def served(self) -> bool:
        return self.state in (None, "served")

    @property
    def notice(self) -> Optional[str]:
        """What the portal shows: 'discontinued' (badge "AA no longer serves this tour"), 'paused' (badge "Paused"),
        or None. A supersede waiting for its new Master (paused / superseded_pending) shows nothing (Q2)."""
        if self.state == "retired":
            return "discontinued"
        if self.state == "paused" and self.reason != "superseded_pending":
            return "paused"
        return None

    def fields(self) -> dict:
        """served_status / served_reason / discontinued_at (+ served_notice) for a tenant /v1 response."""
        return {
            "served_status": self.state,
            "served_reason": self.reason,
            "discontinued_at": self.changed_at.isoformat() if (self.changed_at and not self.served) else None,
            "served_notice": self.notice,
        }


UNKNOWN = ServedState()


async def served_states(conn, tour_ids) -> dict:
    """{tour_id: ServedState} for the given tour ids (one query; ids without a state are absent)."""
    ids = sorted({str(t) for t in tour_ids if t})
    if not ids:
        return {}
    rows = await conn.fetch(SERVED_STATES_SQL, ids)
    return {r["tour_id"]: ServedState(r["state"], r["reason"], r["changed_at"]) for r in rows}


async def request_tours(conn, tenant_id, request_ids) -> dict:
    """{request_id: tour_id or None} for the tenant's angle-gate requests."""
    ids = sorted({str(r) for r in request_ids if r})
    if not ids:
        return {}
    rows = await conn.fetch(REQUEST_TOURS_SQL, ids, str(tenant_id))
    return {r["request_id"]: r["tour_id"] for r in rows}


def with_served(item: dict, state: Optional[ServedState]) -> dict:
    """``item`` plus the served fields (UNKNOWN when there is no state)."""
    item.update((state or UNKNOWN).fields())
    return item


class TourNotServedError(Exception):
    """New tenant work on a tour AA does not serve now (ADR 0015 decision 5): 409 with a plain sentence."""

    status_code = 409

    def __init__(self, state: ServedState, work: str):
        super().__init__(state, work)
        self.state = state
        self.work = work
        self.code = TOUR_DISCONTINUED if state.state == "retired" else TOUR_PAUSED

    def __str__(self) -> str:
        return self.detail

    @property
    def detail(self) -> str:
        if self.code == TOUR_DISCONTINUED:
            return (f"{TOUR_DISCONTINUED}: Adventure Asia no longer offers this tour, so you can't {self.work} for "
                    "it. What you already made for it stays in your account.")
        if self.state.reason == "superseded_pending":
            return (f"{TOUR_PAUSED}: Adventure Asia is publishing a new version of this tour; you can {self.work} "
                    "again once it is out.")
        return (f"{TOUR_PAUSED}: Adventure Asia has paused this tour for now, so you can't {self.work} for it until "
                "it is back. What you already made for it stays in your account.")

    @property
    def headers(self) -> dict:
        return {"X-Error-Code": self.code}


async def ensure_served(conn, tour_id, work: str) -> None:
    """Raise TourNotServedError when the key of ``tour_id`` is retired or paused. No tour id = nothing to check."""
    if not tour_id:
        return
    state = (await served_states(conn, [tour_id])).get(str(tour_id))
    if state is not None and not state.served:
        raise TourNotServedError(state, work)


async def request_served_states(conn, tenant_id, request_ids) -> dict:
    """{request_id: ServedState} for the tenant's angle-gate requests whose tour has a state (one read of their
    tours, one of the states, on ``conn``)."""
    tours = await request_tours(conn, tenant_id, request_ids)
    states = await served_states(conn, [t for t in tours.values() if t])
    return {rid: states[t] for rid, t in tours.items() if t and t in states}
