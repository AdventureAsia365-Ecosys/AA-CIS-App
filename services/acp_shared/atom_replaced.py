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

TENANT_SENTENCE = (
    "Adventure Asia has updated the part of the tour this post was planned from, so it can't be written from "
    "the angle you picked. Pick the angle again to write this post."
)
ADMIN_SENTENCE = (
    "This request's atom is no longer live (the tour was atomized again since it was made), so new angles "
    "can't be generated for it. The tenant has to pick the angle again."
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

    def __init__(self, internal: str = ""):
        super().__init__(internal)
        # The internal message (ids) is for logs; the tenant gets ``detail``.
        self.internal = internal

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
