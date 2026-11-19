"""The Place cards list filter (AA-772 page, AA-1039 R6: AA Catalogue › Place cards) and its My work tile (AA-1156).

One filter text for GET /admin/content-quality/place-cards (api/routers/admin_content_quality.py list_place_cards) and
the My work tile, so the tile counts exactly the cards the page lists for the same ``status`` (the page reads
``?status=`` from its URL). acp_shared.place_card.status: auto_checked (drafted and checked by the models, not yet
reviewed by a person), verified, rejected.
"""
from __future__ import annotations

from typing import Any, Optional

TO_REVIEW = "auto_checked"


def list_filter(status: Optional[str] = None, q: Optional[str] = None) -> tuple[str, list]:
    """The WHERE text (alias ``pc``) and its parameters ($1.. in order) for an optional status and search text."""
    where, params = ["true"], []
    if status:
        params.append(status)
        where.append(f"pc.status = ${len(params)}")
    if q:
        params.append(f"%{q}%")
        where.append(f"(pc.display_name ILIKE ${len(params)} OR pc.locality ILIKE ${len(params)} "
                     f"OR pc.country ILIKE ${len(params)})")
    return " AND ".join(where), params


async def summary(conn: Any, *, status: Optional[str] = TO_REVIEW) -> dict:
    """How many cards the list shows for ``status`` and when the oldest of them was created."""
    where, params = list_filter(status)
    row = await conn.fetchrow(
        f"SELECT count(*) AS n, min(pc.created_at) AS oldest FROM acp_shared.place_card pc WHERE {where}", *params)
    return {"count": int(row["n"] or 0), "oldest_at": row["oldest"]}
