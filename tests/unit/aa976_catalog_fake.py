"""AA-976 test helpers: an in-memory stand-in for the catalog views the catalog.http.v1 reader queries, a fake tenant
pool, and a small FastAPI app carrying only the catalog router. No DB, no network."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

from fastapi import FastAPI

from api.routers.catalog_http import CatalogProblem, catalog_problem_handler, router
from services.catalog_http import reader

SID_A = "1f0e8c1e-0000-4000-8000-00000000000a"
SID_B = "1f0e8c1e-0000-4000-8000-00000000000b"
SID_C = "1f0e8c1e-0000-4000-8000-00000000000c"
SID_RETIRED = "1f0e8c1e-0000-4000-8000-0000000000d1"
SID_PAUSED = "1f0e8c1e-0000-4000-8000-0000000000d2"
DEST_HANOI = "2a2a2a2a-0000-4000-8000-000000000001"
DEST_HALONG = "2a2a2a2a-0000-4000-8000-000000000002"
DEST_BALI = "2a2a2a2a-0000-4000-8000-000000000003"
T0 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


def tour_id_of(source_id: str) -> str:
    """The (current) raw_tours.tour_id behind a source key; stable per key in the fake."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tour:{source_id}"))


def tour_row(source_id: str, *, title: str = "Hanoi and Ha Long", country: str = "Vietnam",
             updated_at: datetime = T0, duration_days: Optional[int] = 3) -> dict:
    return {
        "source_id": source_id, "tour_id": tour_id_of(source_id), "title": title,
        "summary": "Old Quarter and the bay.", "country": country,
        "duration_days": duration_days, "activities": ["Kayaking", "Food tour"],
        "inclusions": "- Guide\n- Boat cruise; Breakfast daily",
        "exclusions": "International flights\nEntrance fees at the pagodas\nTips for guide and driver\nVisa",
        "updated_at": updated_at,
    }


class CatalogViews:
    """The rows of catalog.v_tour, v_tour_day, v_tour_place, v_tour_photo, v_tour_component, v_tour_retired and
    v_destination, answering exactly the statements of services/catalog_http/reader.py."""

    def __init__(self) -> None:
        self.tours: dict[str, dict] = {}
        self.details: dict[str, dict[str, list[dict]]] = {}
        self.retired: dict[str, dict] = {}
        self.destinations: dict[str, dict] = {}
        self.sql: list[tuple[str, tuple]] = []
        self.executed: list[str] = []
        self.readonly: list[bool] = []
        self.isolation: list[Optional[str]] = []

    def add_tour(self, source_id: str, **kw: Any) -> dict:
        row = tour_row(source_id, **kw)
        self.tours[source_id] = row
        self.details[source_id] = {
            "days": [
                {"source_id": source_id, "day_number": 1, "title": "Arrive in Hanoi", "body": "Welcome dinner."},
                {"source_id": source_id, "day_number": 2, "title": "Ha Long Bay", "body": "Cruise."},
                {"source_id": source_id, "day_number": 3, "title": "Depart", "body": ""},
            ],
            "places": [
                {"source_id": source_id, "day_number": 1, "position": 1, "place_name": "Hanoi",
                 "place_display_name": "Hanoi", "destination_id": DEST_HANOI, "destination_name": "Hanoi",
                 "country": "Vietnam", "lat": 21.03, "lng": 105.85, "precision": "point"},
                {"source_id": source_id, "day_number": 2, "position": 2, "place_name": "Ha Long Bay",
                 "place_display_name": "Ha Long Bay", "destination_id": DEST_HALONG,
                 "destination_name": "Ha Long Bay", "country": "Vietnam", "lat": 20.91, "lng": 107.18,
                 "precision": "area"},
                {"source_id": source_id, "day_number": 2, "position": 1, "place_name": "Hanoi",
                 "place_display_name": "Hanoi", "destination_id": DEST_HANOI, "destination_name": "Hanoi",
                 "country": "Vietnam", "lat": 21.03, "lng": 105.85, "precision": "point"},
                {"source_id": source_id, "day_number": 2, "position": 3, "place_name": "Somewhere unresolved",
                 "place_display_name": None, "destination_id": None, "destination_name": None,
                 "country": None, "lat": None, "lng": None, "precision": None},
            ],
            "photos": [
                {"source_id": source_id, "photo_id": "p2", "path": "/content/photos/p2", "place_label": "Bay",
                 "position": 2},
                {"source_id": source_id, "photo_id": "p1", "path": "/content/photos/p1", "place_label": None,
                 "position": 1},
            ],
            "components": [
                {"source_id": source_id, "activity": "kayaking", "intensity_level": "moderate"},
                {"source_id": source_id, "activity": "cycling", "intensity_level": "active"},
            ],
        }
        return row

    # asyncpg surface -------------------------------------------------------
    @asynccontextmanager
    async def transaction(self, isolation: Optional[str] = None, readonly: bool = False):
        self.readonly.append(readonly)
        self.isolation.append(isolation)
        yield

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(sql)
        return "SELECT 1"

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict]:
        self.sql.append((sql, args))
        if sql == reader.GET_TOUR_SQL:
            return self.tours.get(args[0])
        if sql == reader.RETIRED_SQL:
            return self.retired.get(args[0])
        if sql == reader.DESTINATION_SQL:
            return self.destinations.get(args[0])
        raise AssertionError(sql)

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        self.sql.append((sql, args))
        if sql == reader.LIST_TOURS_SQL:
            after, since, names, limit = args
            rows = sorted(self.tours.values(), key=lambda r: r["source_id"])
            rows = [r for r in rows
                    if (after is None or r["source_id"] > after)
                    and (since is None or r["updated_at"] >= since)
                    and (names is None or r["country"].lower() in names)]
            return rows[:limit]
        for name, template in reader._DETAIL_SQL.items():  # noqa: SLF001
            if sql == template.format(cond=reader.DETAIL_COND):
                by_tour_id = {tour_id_of(sid): sid for sid in self.details}
                return [r for tid in args[0] for r in self.details.get(by_tour_id.get(tid, ""), {}).get(name, [])]
        raise AssertionError(sql)


class FakePool:
    def __init__(self, conn: CatalogViews, fail: Optional[BaseException] = None) -> None:
        self.conn, self.fail = conn, fail
        self.acquired = 0
        self.released = 0

    async def acquire(self, timeout: Optional[float] = None) -> CatalogViews:
        if self.fail is not None:
            raise self.fail
        self.acquired += 1
        return self.conn

    async def release(self, conn: Any) -> None:
        self.released += 1


def catalog_app(pool: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(CatalogProblem, catalog_problem_handler)
    app.state.tenant_pool = pool
    return app


def seeded() -> CatalogViews:
    views = CatalogViews()
    views.add_tour(SID_A)
    views.add_tour(SID_B, title="Bali temples", country="Indonesia",
                   updated_at=datetime(2026, 10, 22, tzinfo=timezone.utc))
    views.add_tour(SID_C, title="Angkor", country="Cambodia")
    views.retired[SID_RETIRED] = {"state": "retired", "reason": "source_deleted", "changed_at": T0}
    views.retired[SID_PAUSED] = {"state": "paused", "reason": "master_inactive", "changed_at": T0}
    views.destinations[DEST_HANOI] = {"destination_id": DEST_HANOI, "name": "Hanoi", "country": "Vietnam",
                                      "lat": 21.03, "lng": 105.85}
    views.destinations[DEST_BALI] = {"destination_id": DEST_BALI, "name": "Ubud", "country": "Indonesia",
                                     "lat": -8.5, "lng": 115.26}
    return views


__all__ = ["tour_id_of", "SID_A", "SID_B", "SID_C", "SID_RETIRED", "SID_PAUSED", "DEST_HANOI", "DEST_HALONG",
           "DEST_BALI", "T0",
           "CatalogViews", "FakePool", "catalog_app", "seeded", "tour_row", "SimpleNamespace"]
