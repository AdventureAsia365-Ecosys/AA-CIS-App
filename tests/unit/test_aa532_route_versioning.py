"""AA-532 — services/acp_contract/route_detection.py's DB-facing functions
(run_route_detection/create_route_pick), switched from DELETE+INSERT-whole to versioning
(supersede, never delete) to stop a real FK violation against acp_shared.subject.route_id
(migration 133) on any tenant with an active Subject.

AA-545 — `run_route_detection()` is now `(pool)`, no `tenant_id`, platform-wide; `route_id` is
`{tour_id}:{first}-{last}` (no tenant prefix); `route`/`Moment` carry no `score` anymore (see
docs/implementation-notes/AA-545.md and route_detection.py's own docstrings) — row shapes and
assertions below updated accordingly, mechanics (versioning itself) unchanged.

Mocks the asyncpg pool — no live DB. `run_route_detection()` acquires the pool 3 separate times
(read moments/old_hubs/current_routes -> hub create/reuse loop -> final supersede+insert
transaction); `pool.acquire()`/`ctx.__aenter__()` return the SAME mock connection every call
(same convention test_aa450_cta_slot_lookup.py already uses), so one `conn.fetch.side_effect`
list drains across all 3 acquisitions in call order."""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.acp_contract import route_detection

TOUR = str(uuid.uuid4())


def _make_pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _txn_conn():
    """A conn whose `.transaction()` is a real (mocked) async context manager, as
    `async with conn.transaction():` in run_route_detection's final write step needs."""
    conn = AsyncMock()
    txn_ctx = AsyncMock()
    txn_ctx.__aenter__ = AsyncMock(return_value=None)
    txn_ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_ctx)
    return conn


def _moment_row(segment_id, day, place="Kyoto", action="walk"):
    return {
        "segment_id": segment_id, "tour_id": TOUR,
        "canonical_place": place, "canonical_action": action, "day": day,
    }


class TestRunRouteDetectionVersioning:
    @pytest.mark.asyncio
    async def test_brand_new_identity_inserts_version_1_unsuffixed_id(self):
        conn = _txn_conn()
        conn.fetch.side_effect = [
            [_moment_row("s1", 1), _moment_row("s2", 2, place="Magome")],  # moments
            [],  # old_hubs
            [],  # current_routes (none exist yet)
            [],  # max_version_rows (identity never seen before)
        ]
        pool = _make_pool(conn)

        result = await route_detection.run_route_detection(pool)

        assert result["routes_written"] == 1
        assert result["routes_superseded"] == 0
        assert result["routes_unchanged"] == 0
        # never a DELETE anywhere in this flow
        for call in conn.execute.call_args_list:
            assert "DELETE" not in call.args[0]
        insert_call = conn.executemany.call_args
        route_id = insert_call.args[1][0][0]
        assert route_id == f"{TOUR}:1-2"  # no tenant prefix, no :vN suffix for a first version
        version = insert_call.args[1][0][-1]
        assert version == 1

    @pytest.mark.asyncio
    async def test_unchanged_route_is_left_alone_no_write(self):
        """The build prompt's own ask: 'chỉ tạo version mới nếu nội dung Route thực sự đổi'."""
        conn = _txn_conn()
        conn.fetch.side_effect = [
            [_moment_row("s1", 1), _moment_row("s2", 2, place="Magome")],
            [],
            [{
                "route_id": f"{TOUR}:1-2", "tour_id": TOUR, "hub_id": None,
                "hub_name": "Kyoto → Magome", "ordered_segment_ids": json.dumps(["s1", "s2"]),
                "first_day": 1, "last_day": 2, "version": 1,
            }],
            [{"tour_id": TOUR, "first_day": 1, "last_day": 2, "max_version": 1}],  # max_version_rows
        ]
        pool = _make_pool(conn)

        result = await route_detection.run_route_detection(pool)

        assert result["routes_unchanged"] == 1
        assert result["routes_written"] == 0
        assert result["routes_superseded"] == 0
        conn.execute.assert_not_called()
        conn.executemany.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_route_supersedes_old_row_never_deletes_it(self):
        """The real bug this issue fixes: the old row must survive (a Subject's FK depends on
        it), only a NEW version is written alongside it."""
        conn = _txn_conn()
        old_route_id = f"{TOUR}:1-2"
        conn.fetch.side_effect = [
            [_moment_row("s1", 1), _moment_row("s2", 2, place="Magome")],  # new derivation
            [],
            [{  # old current row has a DIFFERENT segment set -> content changed
                "route_id": old_route_id, "tour_id": TOUR, "hub_id": None,
                "hub_name": "Old Name", "ordered_segment_ids": json.dumps(["s_old"]),
                "first_day": 1, "last_day": 2, "version": 1,
            }],
            [{"tour_id": TOUR, "first_day": 1, "last_day": 2, "max_version": 1}],  # max_version_rows
        ]
        pool = _make_pool(conn)

        result = await route_detection.run_route_detection(pool)

        assert result["routes_superseded"] == 1
        assert result["routes_written"] == 1
        assert result["routes_unchanged"] == 0

        # the old row is marked superseded, never deleted
        supersede_call = conn.execute.call_args_list[0]
        assert "UPDATE acp_contract.route SET superseded_at = now()" in supersede_call.args[0]
        assert old_route_id in supersede_call.args[1]
        for call in conn.execute.call_args_list:
            assert "DELETE" not in call.args[0]

        # the new row is version 2, route_id carries the :v2 suffix (base id stays taken by the
        # still-present superseded row)
        insert_row = conn.executemany.call_args.args[1][0]
        assert insert_row[0] == f"{old_route_id}:v2"
        assert insert_row[-1] == 2

    @pytest.mark.asyncio
    async def test_identity_that_disappears_is_superseded_with_no_replacement(self):
        conn = _txn_conn()
        old_route_id = f"{TOUR}:1-2"
        conn.fetch.side_effect = [
            [],  # this run derives NOTHING (e.g. Segments dropped below LEAST_DAYS/LEAST_PLACES)
            [],
            [{
                "route_id": old_route_id, "tour_id": TOUR, "hub_id": None,
                "hub_name": "Old Name", "ordered_segment_ids": json.dumps(["s1", "s2"]),
                "first_day": 1, "last_day": 2, "version": 1,
            }],
            [{"tour_id": TOUR, "first_day": 1, "last_day": 2, "max_version": 1}],  # max_version_rows
        ]
        pool = _make_pool(conn)

        result = await route_detection.run_route_detection(pool)

        assert result["routes_superseded"] == 1
        assert result["routes_written"] == 0
        conn.executemany.assert_not_called()
        supersede_call = conn.execute.call_args_list[0]
        assert old_route_id in supersede_call.args[1]

    @pytest.mark.asyncio
    async def test_old_hubs_query_filters_current_routes_only(self):
        """AA-532 — hub-family matching must only look at CURRENT routes; a superseded one lying
        around (never deleted) must not keep matching a family it no longer represents."""
        conn = _txn_conn()
        conn.fetch.side_effect = [[], [], [], []]
        pool = _make_pool(conn)

        await route_detection.run_route_detection(pool)

        old_hubs_query = conn.fetch.call_args_list[1].args[0]
        assert "r.superseded_at IS NULL" in old_hubs_query

    @pytest.mark.asyncio
    async def test_reactivated_identity_reuses_superseded_id_as_next_version(self):
        """AA-723 — a tour deactivated then reactivated (AA-713) superseded its routes. On the
        reactivation recompute the identity is 'new' in current_routes, but the raw route_id still
        exists as a superseded row. The insert must pick the next version, not collide on
        route_pkey (the live UniqueViolationError found in S213)."""
        conn = _txn_conn()
        base_id = f"{TOUR}:2-3"
        conn.fetch.side_effect = [
            [_moment_row("s1", 2), _moment_row("s2", 3, place="Magome")],  # derivation reappears
            [],  # old_hubs
            [],  # current_routes — empty, the tour was deactivated so its routes are superseded
            [{"tour_id": TOUR, "first_day": 2, "last_day": 3, "max_version": 1}],  # superseded v1 exists
        ]
        pool = _make_pool(conn)

        result = await route_detection.run_route_detection(pool)

        assert result["routes_written"] == 1
        assert result["routes_superseded"] == 0  # nothing current to supersede
        insert_row = conn.executemany.call_args.args[1][0]
        assert insert_row[0] == f"{base_id}:v2"  # NOT the raw base_id that still exists superseded
        assert insert_row[-1] == 2


class TestCreateRoutePick:
    @pytest.mark.asyncio
    async def test_superseded_route_returns_none_not_a_stale_pick(self):
        """AA-532 — a route_id that still exists as a row (never deleted) but has been
        superseded must not be pick-able as if it were still current."""
        conn = AsyncMock()
        conn.fetchrow.return_value = None  # WHERE ... AND superseded_at IS NULL matches 0 rows
        pool = _make_pool(conn)

        result = await route_detection.create_route_pick(str(uuid.uuid4()), "some-route-id", pool)

        assert result is None
        query = conn.fetchrow.call_args.args[0]
        assert "superseded_at IS NULL" in query
