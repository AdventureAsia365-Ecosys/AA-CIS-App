"""AA-990 — a T8/T9 request whose atom is no longer live: a typed 409 ``atom_replaced``, not a 500.

An angle-gate request stores the atom (or the Route Segments) it was planned from. A later atomize run can
replace that atom (new atom ids for the same tour), so the stored id no longer resolves in
``acp_contract.v_active_tour_atoms``. Writing (T9) or regenerating angles (admin) for such a request cannot work;
before this module the tenant saw HTTP 500 "atom_id='…' not found (deleted, or not a live platform/tenant atom)".

Carrying old requests over to the new atoms is AA-990 proper; this module only names the condition:

* ``AtomReplaced`` — mixin carried by the services' own error classes (content writing subclasses it, so the
  router and ``api/main.py``'s exception handler answer 409 with ``detail`` = "atom_replaced: <sentence>" and
  header ``X-Error-Code: atom_replaced``, the same shape as ``shared.tour_liveness.TourNotServedError``).
* ``atom_is_live(conn, atom_id, tenant_id)`` — the same liveness test T8/T9 apply when they read the atom.
"""
from __future__ import annotations

ATOM_REPLACED = "atom_replaced"

# Re-picking the angle on the same request (``/reopen`` then ``/choose``) keeps the same atom_id and would hit this
# 409 again, so both sentences send the tenant to Social Content (the Slate) to start a new post from that part of
# the tour, which makes a new request on the current atom.
TENANT_SENTENCE = (
    "Adventure Asia has updated the part of the tour this post was planned from, so this post can't be written "
    "from it any more. Pick this part of the tour again in Social Content to start a new post."
)
ADMIN_SENTENCE = (
    "This request's atom is no longer live (the tour was atomized again since it was made), so new angles "
    "can't be generated for it. The tenant has to start a new post by picking this part of the tour again in "
    "Social Content."
)

# Same filter as services/acp_angle_gate/service.py::_ATOM_QUERY and services/acp_content_writing/service.py::
# _ATOM_TEXT_QUERY (a platform atom or the tenant's own, live, not an empty marker).
_ATOM_LIVE_SQL = """
    SELECT 1 FROM acp_contract.v_active_tour_atoms
    WHERE atom_id = $1 AND owner_scope IN ('platform', $2) AND NOT deleted AND NOT is_empty_marker
"""


class AtomReplaced(Exception):
    """Mixin for a service error raised because a request's atom is no longer live (409 ``atom_replaced``)."""

    status_code = 409
    code = ATOM_REPLACED
    sentence = TENANT_SENTENCE

    # The ids are for logs (the caller logs them); the tenant gets ``detail``. Set through ``about()`` so the
    # constructor keeps the plain Exception signature (pickle / copy safe).
    atom_id: str | None = None
    segment_ids: list | None = None

    def __init__(self, internal: str = ""):
        super().__init__(internal)
        self.internal = internal

    def about(self, *, atom_id: str | None = None, segment_ids: list | None = None):
        """Attach the replaced atom id / Route Segment ids for the log line; returns ``self`` for ``raise``."""
        self.atom_id = atom_id
        self.segment_ids = list(segment_ids) if segment_ids else None
        return self

    @property
    def detail(self) -> str:
        return f"{self.code}: {self.sentence}"

    @property
    def headers(self) -> dict:
        return {"X-Error-Code": self.code}


def admin_detail() -> str:
    return f"{ATOM_REPLACED}: {ADMIN_SENTENCE}"


async def atom_is_live(conn, atom_id: str | None, tenant_id) -> bool:
    """True when ``atom_id`` is a live platform atom or a live atom of ``tenant_id``."""
    if not atom_id:
        return False
    return bool(await conn.fetchval(_ATOM_LIVE_SQL, atom_id, str(tenant_id)))


# ── AA-990 D2 (S252): the computed `atom_replaced` state on the tenant's request reads ────────────────────────────
# No status, no migration: GET /v1/angle-gate/requests/{id} and GET /v1/content-writing/reviews (+ /requests/{id}/
# review) add `atom_replaced` (true when a single-atom request's atom is no longer live) and `atom_replacement`
# ({atom_id, place, itinerary_day} of the unique live atom of the same place — atom_repoint's rule — else null), so
# the Partner portal shows the state and disables Write before the tenant meets the 409. A Route request is never
# flagged here (its own 409 covers a Route whose Segments were all rebuilt away).
#
# AA-859 MAIN exception (like angle_gate service.generation_status): acp_contract.v_active_tour_atoms has no
# tenant-role grant, so this reads on the main pool (cis_app). The caller proved the requests are the tenant's on its
# own tenant connection first, and every statement here still filters on the tenant.
_STATE_SQL = """
    SELECT r.request_id::text AS request_id, r.tenant_id::text AS tenant_id, r.atom_id, r.status,
           (r.route_segment_ids IS NOT NULL) AS is_route,
           EXISTS (SELECT 1 FROM acp_contract.v_active_tour_atoms a
                    WHERE a.atom_id = r.atom_id AND a.owner_scope IN ('platform', r.tenant_id::text)
                      AND NOT a.deleted AND NOT a.is_empty_marker) AS live
      FROM acp_shared.angle_gate_request r
     WHERE r.tenant_id = $1::uuid AND r.request_id = ANY($2::uuid[])
"""


def _state(replaced: bool, cand: dict | None = None) -> dict:
    hint = None
    if cand is not None:
        hint = {"atom_id": cand["atom_id"], "place": cand.get("place"), "itinerary_day": cand.get("itinerary_day")}
    return {"atom_replaced": replaced, "atom_replacement": hint}


async def atom_states(pool, tenant_id, request_ids: list[str]) -> dict[str, dict]:
    """{request_id: {"atom_replaced": bool, "atom_replacement": {...} | None}} for the tenant's requests."""
    from services.acp_shared import atom_repoint as ar

    ids = sorted({str(i) for i in request_ids if i})
    if not ids:
        return {}
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_STATE_SQL, str(tenant_id), ids)]
        stale = [r for r in rows if not r["is_route"] and not r["live"] and r.get("atom_id")]
        old, cands = {}, []
        if stale:
            old = {r["atom_id"]: dict(r) for r in await conn.fetch(ar.OLD_ATOMS_SQL,
                                                                    sorted({s["atom_id"] for s in stale}))}
            sources = sorted({str(o["source_id"]) for o in old.values()})
            cands = [dict(r) for r in await conn.fetch(ar.CANDIDATES_SQL, sources)] if sources else []
    out = {r["request_id"]: _state(False) for r in rows}
    by_atom = {c["atom_id"]: c for c in cands}
    for d in ar.plan_repoints(stale, old, cands):
        if d["outcome"] == "already_live":
            continue
        out[d["request_id"]] = _state(True, by_atom.get(d["new_atom_id"]) if d["outcome"] == "repoint" else None)
    return out


async def with_atom_state(items: list[dict], tenant_id, pool) -> list[dict]:
    """Each item (a dict with `request_id`) gains `atom_replaced` / `atom_replacement` (defaults: false / null).
    Informational: a failing read logs a warning and leaves the defaults (the write still answers the typed 409)."""
    try:
        states = await atom_states(pool, tenant_id, [i.get("request_id") for i in items])
    except Exception as e:  # noqa: BLE001 — never fail the tenant's read over the hint
        import structlog
        structlog.get_logger().warning("aa990_atom_state_failed", tenant_id=str(tenant_id), error=str(e)[:200])
        states = {}
    for i in items:
        i.update(states.get(str(i.get("request_id")), _state(False)))
    return items
