"""Path ids of the tenant routers: a 404 for an id that is not a UUID, before any database work (AA-1144, S285 live
check: GET /v1/tours/mine reached the ``/{tour_id}`` catch-all and the ``$1::uuid`` cast answered 500)."""
import uuid

from fastapi import HTTPException


def require_uuid(value: str, detail: str) -> str:
    """``value`` unchanged when it parses as a UUID, else a 404 with ``detail``."""
    try:
        uuid.UUID(str(value))
    except ValueError:
        raise HTTPException(status_code=404, detail=detail) from None
    return value
