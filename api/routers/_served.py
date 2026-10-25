"""AA-986 L3 (root ADR 0015 decision 5): the tenant /v1 side of a tour's served state, shared by the routers.

* ``annotate(conn, items, tour_key="tour_id")`` adds served_status / served_reason / discontinued_at / served_notice
  to each item from one read of gold_aa_internal.v_tour_liveness (works on the main pool and the tenant pool).
* ``refuse_http(exc)`` turns a shared.tour_liveness.TourNotServedError (new work on a retired / paused tour) into the
  409 the tenant sees: ``detail`` = "<code>: <plain sentence>", header ``X-Error-Code`` = tour_discontinued |
  tour_paused. api/main.py also registers it as an exception handler, for services that raise it.
"""
from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from shared import tour_liveness as _tl
from shared.tour_liveness import TourNotServedError, with_served


async def annotate(conn, items: list, tour_key: str = "tour_id") -> list:
    """Each dict of ``items`` gains the served fields of the tour id under ``tour_key`` (in place, returned)."""
    states = await _tl.served_states(conn, [str(i.get(tour_key)) for i in items if i.get(tour_key)])
    for item in items:
        tid = item.get(tour_key)
        with_served(item, states.get(str(tid)) if tid else None)
    return items


def refuse_http(exc: TourNotServedError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers)


async def tour_not_served_handler(_request, exc: TourNotServedError) -> JSONResponse:
    """App handler for a typed 409 refusal. Also registered for ``services.acp_shared.atom_replaced.AtomReplaced``
    (AA-990): it only reads ``status_code`` / ``detail`` / ``headers``, which both classes carry."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


async def annotate_by_request(conn, tenant_id, items: list, request_key: str = "request_id") -> list:
    """Each dict of ``items`` gains the served fields of the tour of its angle-gate request (``request_key``), from
    one read of the requests' tours and one of their states, on ``conn`` (tenant pool or main pool)."""
    tours = await _tl.request_tours(conn, tenant_id, [i.get(request_key) for i in items])
    states = await _tl.served_states(conn, [t for t in tours.values() if t])
    for item in items:
        tour = tours.get(str(item.get(request_key)))
        with_served(item, states.get(tour) if tour else None)
    return items
