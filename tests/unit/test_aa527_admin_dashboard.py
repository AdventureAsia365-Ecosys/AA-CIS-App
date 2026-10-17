"""AA-527 (bổ sung, Phương án C dashboard) — api/routers/admin_dashboard.py.

Reworked AA-551 (07/09/2026): `tour_id` is now optional on segments/score/routes ("All tours"
platform-wide mode — see that file's own module docstring for the full reasoning), each gained a
`market` filter + pagination (`limit`/`offset` + a `full_count` window-function column consumed by
`_safe(..., exclude=("full_count",))`), and a new `GET /admin/dashboard/summary` endpoint feeds the
rebuilt `/admin/atom-curation` page's header stat bar. `slate` was changed again by AA-564 (per
AA-563's investigation): now requires `tenant_id` instead of `tour_id` (Slate genuinely is
per-tenant, ADR-0003 — the old `tour_id` scoping was itself the bug, not a design choice), gained
an optional `channel` filter, and each row now carries its real topic name (`canonical_place`/
`canonical_action`/`hub_name`/`tour_name`/`segment_count`) via the same LEFT JOINs the Tenant
Portal's `fetch_slate()` already runs — see `TestListSlate` below.

Mocks the asyncpg pool — no live DB. Same x-admin-secret convention/helpers as
test_aa300_admin_atoms.py (monkeypatch api.routers.admin.ADMIN_SECRET, a real fake pool/request).
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routers import admin_a4, admin_dashboard

_TEST_SECRET = "test-admin-secret"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _TEST_SECRET)


def _make_pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


class TestListSegments:
    @pytest.mark.asyncio
    async def test_returns_segment_rows_scoped_to_tour(self):
        """AA-545 — no more tenant_id/tenant_name (atom_segment is platform-wide); `market` is
        a real column since a Segment can carry one row per finite market. AA-551 — `tour_id`/
        `tour_name`/`full_count` are new SELECT columns (full_count excluded from the JSON by
        `_safe`)."""
        tour_id = str(uuid.uuid4())
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"tour_id": tour_id, "tour_name": "Sri Lanka Discovery",
             "segment_id": "seg1", "canonical_place": "Sigiriya", "canonical_action": "climb",
             "member_count": 3, "market": "US",
             "total_rank": 2, "recurrence": 5, "excluded_reason": None,
             "route_id": "r1", "route_hub_name": "Cultural Triangle", "full_count": 1},
        ]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.list_segments(request, tour_id=tour_id, x_admin_secret=_TEST_SECRET)

        assert result["total"] == 1
        assert result["data"][0]["canonical_place"] == "Sigiriya"
        assert "full_count" not in result["data"][0]
        query, *params = conn.fetch.call_args[0]
        assert "ta.tour_id = $1::uuid" in query
        assert tour_id in params

    @pytest.mark.asyncio
    async def test_empty_when_no_segments(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.list_segments(request, tour_id=str(uuid.uuid4()), x_admin_secret=_TEST_SECRET)
        assert result["total"] == 0
        assert result["data"] == []

    @pytest.mark.asyncio
    async def test_all_tours_mode_omits_tour_filter(self):
        """AA-551 — the actual gap AA-550 found: `tour_id` is now optional. No tour_id means the
        WHERE clause must not filter on ta.tour_id at all.

        Every other Query(...)-declared param is passed explicitly here (real values, not left at
        its Python-level default) — calling a FastAPI route function directly, bypassing FastAPI's
        own dependency resolution, means an omitted param keeps the raw `Query(...)` sentinel
        object as its value (always truthy) rather than the `None`/int FastAPI would actually
        inject; a real request never hits this, but a direct unit-test call must sidestep it."""
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_dashboard.list_segments(
            request, tour_id=None, market=None, place_search=None, min_recurrence=None,
            limit=50, offset=0, x_admin_secret=_TEST_SECRET,
        )

        query, *params = conn.fetch.call_args[0]
        assert "ta.tour_id = " not in query
        assert params == [50, 0]  # just limit/offset, no tour_id/market/place_search/min_recurrence

    @pytest.mark.asyncio
    async def test_market_and_place_search_filters_applied(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_dashboard.list_segments(
            request, tour_id=None, market="US", place_search="sigiriya",
            min_recurrence=2, limit=50, offset=0, x_admin_secret=_TEST_SECRET,
        )

        query, *params = conn.fetch.call_args[0]
        assert "ar.market = $1" in query
        assert "canonical_place ILIKE $2" in query
        assert "ar.recurrence >= $3" in query
        assert params[0] == "US"
        assert params[1] == "%sigiriya%"
        assert params[2] == 2

    @pytest.mark.asyncio
    async def test_wrong_admin_secret_rejected(self):
        from fastapi import HTTPException
        conn = AsyncMock()
        pool = _make_pool(conn)
        request = _make_request(pool)
        with pytest.raises(HTTPException) as exc:
            await admin_dashboard.list_segments(request, tour_id=str(uuid.uuid4()), x_admin_secret="wrong")
        assert exc.value.status_code == 403


class TestListScore:
    @pytest.mark.asyncio
    async def test_returns_ranking_rows(self):
        """AA-545 — no more tenant_id/tenant_name (atom_ranking is platform-wide, PK
        (market, tour_id, segment_id)); `market` is the row's own PK column now. AA-551 —
        `tour_id`/`tour_name`/`full_count` added."""
        tour_id = str(uuid.uuid4())
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"tour_id": tour_id, "tour_name": "Sri Lanka Discovery",
             "market": "US", "segment_id": "seg1",
             "canonical_place": "Sigiriya", "canonical_action": "climb",
             "demand_rank": 1, "recurrence_rank": 2, "questions_rank": 3, "said_rank": 4,
             "total_rank": 10, "demand_market": "US", "demand_volume": 1300,
             "recurrence": 5, "questions": 12, "said": 3, "excluded_reason": None,
             "computed_at": "2026-09-01T00:00:00", "full_count": 1},
        ]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.list_score(request, tour_id=tour_id, x_admin_secret=_TEST_SECRET)
        assert result["total"] == 1
        assert result["data"][0]["total_rank"] == 10

    @pytest.mark.asyncio
    async def test_all_tours_mode_with_rank_range(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        # market explicit None — see the comment on the equivalent Segment test above for why.
        await admin_dashboard.list_score(
            request, tour_id=None, market=None, min_total_rank=5, max_total_rank=20,
            limit=50, offset=0, x_admin_secret=_TEST_SECRET,
        )

        query, *params = conn.fetch.call_args[0]
        assert "ar.tour_id = " not in query
        assert "ar.total_rank >= $1" in query
        assert "ar.total_rank <= $2" in query
        assert params[:2] == [5, 20]


class TestListRoutes:
    @pytest.mark.asyncio
    async def test_returns_routes(self):
        """AA-545 — no more tenant_id/tenant_name/stored score (route is platform-wide,
        composition-only); `market`/`score` are computed columns now (AVG(total_rank) per
        market, joined in — one row per (Route version, market)). AA-551 — `tour_name`/
        `full_count` added. AA-554 mục G — `hub_grouping_backlog` (a static `True` flag
        documenting "no real Hub view exists yet") is gone: a real Hub view now exists
        (`GET /admin/dashboard/hubs`, see TestListHubs below), so the flag would be stale."""
        tour_id = str(uuid.uuid4())
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"route_id": "r1", "tour_id": tour_id, "tour_name": "Sri Lanka Discovery",
             "hub_id": None, "hub_name": "Cultural Triangle",
             "ordered_segment_ids": '["seg1", "seg2"]',
             "first_day": 1, "last_day": 3, "created_at": "2026-09-01T00:00:00",
             "version": 1, "superseded_at": None, "market": "US", "score": 5.0, "full_count": 1},
        ]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.list_routes(request, tour_id=tour_id, x_admin_secret=_TEST_SECRET)
        assert result["total"] == 1
        assert "hub_grouping_backlog" not in result
        # ordered_segment_ids comes back as a raw JSON string (no jsonb codec, same gap
        # admin_atoms.py's media handling already found) — _safe() must parse it.
        assert result["data"][0]["ordered_segment_ids"] == ["seg1", "seg2"]

    @pytest.mark.asyncio
    async def test_does_not_filter_superseded_rows_when_one_tour_selected(self):
        """AA-532 — every OTHER reader of acp_contract.route filters `superseded_at IS NULL`;
        this admin audit panel deliberately does not WHEN SCOPED TO ONE TOUR, since showing
        version history is the point. AA-551 — this is now conditional on `tour_id` being given;
        the "All tours" branch is covered by the test below instead."""
        tour_id = str(uuid.uuid4())
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"route_id": "r1", "tour_id": tour_id, "tour_name": "T", "hub_id": None,
             "hub_name": "Cultural Triangle", "ordered_segment_ids": '["seg1"]',
             "first_day": 1, "last_day": 3, "created_at": "2026-09-01T00:00:00",
             "version": 1, "superseded_at": "2026-09-02T00:00:00", "market": "US", "score": 5.0,
             "full_count": 2},
            {"route_id": "r1:v2", "tour_id": tour_id, "tour_name": "T", "hub_id": None,
             "hub_name": "Cultural Triangle", "ordered_segment_ids": '["seg1", "seg2"]',
             "first_day": 1, "last_day": 3, "created_at": "2026-09-02T00:00:00",
             "version": 2, "superseded_at": None, "market": "US", "score": 4.0, "full_count": 2},
        ]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.list_routes(request, tour_id=tour_id, x_admin_secret=_TEST_SECRET)
        assert result["total"] == 2
        query, *_params = conn.fetch.call_args[0]
        where_clause = query.split("WHERE")[1].split("GROUP BY")[0]
        assert "superseded_at" not in where_clause

    @pytest.mark.asyncio
    async def test_all_tours_mode_filters_current_only(self):
        """AA-551 — "All tours" (no tour_id) defaults to current-only, unlike the per-tour audit
        view above — a platform-wide table listing every historical version at once would be
        noisy with no UI ask for it."""
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        # All other Query(...)-declared params explicit — see the Segment test above for why a
        # direct function call (bypassing FastAPI's own resolution) needs this.
        await admin_dashboard.list_routes(
            request, tour_id=None, market=None, min_days=None, max_days=None, hub_name_search=None,
            limit=50, offset=0, x_admin_secret=_TEST_SECRET,
        )

        query, *_params = conn.fetch.call_args[0]
        where_clause = query.split("WHERE")[1].split("GROUP BY")[0]
        assert "r.superseded_at IS NULL" in where_clause
        assert "r.tour_id = " not in where_clause

    @pytest.mark.asyncio
    async def test_day_span_and_hub_name_filters(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_dashboard.list_routes(
            request, tour_id=None, market=None, min_days=2, max_days=5, hub_name_search="triangle",
            limit=50, offset=0, x_admin_secret=_TEST_SECRET,
        )

        query, *params = conn.fetch.call_args[0]
        assert "last_day - r.first_day + 1) >= $1" in query
        assert "last_day - r.first_day + 1) <= $2" in query
        assert "r.hub_name ILIKE $3" in query
        assert params[:3] == [2, 5, "%triangle%"]


class TestListHubs:
    """AA-554 mục G — GET /admin/dashboard/hubs, the new Hub half of the Route/Hub table split."""

    @pytest.mark.asyncio
    async def test_returns_hub_rows(self):
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"hub_id": str(uuid.uuid4()), "hub_name": "Nakasendo Way",
             "created_at": "2026-09-01T00:00:00", "updated_at": "2026-09-05T00:00:00",
             "tour_names": ["Sri Lanka Discovery", "Ceylon Highlands"], "route_count": 2,
             "full_count": 1},
        ]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.list_hubs(request, x_admin_secret=_TEST_SECRET)
        assert result["total"] == 1
        assert result["data"][0]["hub_name"] == "Nakasendo Way"
        assert result["data"][0]["route_count"] == 2
        assert "full_count" not in result["data"][0]

    @pytest.mark.asyncio
    async def test_empty_result_when_no_shared_hub_exists(self):
        """Real prod data today: acp_contract.hub has 0 rows (no 2 tours currently share enough of
        a route to form a family) — confirms the endpoint returns a clean empty result rather than
        erroring, matching the frontend's "No Hub yet" empty-state."""
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.list_hubs(request, x_admin_secret=_TEST_SECRET)
        assert result["total"] == 0
        assert result["data"] == []

    @pytest.mark.asyncio
    async def test_only_current_routes_count_toward_a_hub(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_dashboard.list_hubs(
            request, tour_id=None, market=None, hub_name_search=None,
            limit=50, offset=0, x_admin_secret=_TEST_SECRET,
        )

        query, *_params = conn.fetch.call_args[0]
        # Note: a plain `.split("WHERE")` would wrongly match the `FILTER (WHERE ...)` clause in
        # the SELECT list first — checked directly against the query instead.
        assert "\n            WHERE r.superseded_at IS NULL" in query

    @pytest.mark.asyncio
    async def test_hub_name_search_and_tour_filter(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)
        tour_id = str(uuid.uuid4())

        await admin_dashboard.list_hubs(
            request, tour_id=tour_id, market=None, hub_name_search="nakasendo",
            limit=50, offset=0, x_admin_secret=_TEST_SECRET,
        )

        query, *params = conn.fetch.call_args[0]
        assert "r.tour_id = $1" in query
        assert "h.hub_name ILIKE $2" in query
        assert params[:2] == [tour_id, "%nakasendo%"]


class TestListSlate:
    """AA-564 2.1/2.2 — tenant_id-scoped (not tour_id), real topic name joined in."""

    @pytest.fixture(autouse=True)
    def _no_aliases(self, monkeypatch):
        # AA-857 — the alias read is its own query; these tests are about the Subject query.
        self.live_labels = AsyncMock(return_value={})
        monkeypatch.setattr(admin_dashboard, "live_segment_labels", self.live_labels)
        # AA-863 — the seed reads are their own query set (tests/unit/test_aa863_seed_audit.py).
        self.subject_seeds = AsyncMock(return_value={})
        monkeypatch.setattr(admin_dashboard.seed_audit, "load_subject_seeds", self.subject_seeds)

    @pytest.mark.asyncio
    async def test_subject_on_an_alias_old_id_shows_the_live_segment(self):
        """AA-857 — same live id + label as the tenant's Slate (fetch_slate)."""
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"subject_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "tenant_name": "WanderLux",
             "channel": "facebook", "state": "picked", "score": 1.0,
             "segment_id": "old1", "route_id": None, "cleared_bar_reason": '{}',
             "created_at": "2026-10-16T00:00:00",
             "canonical_place": "stale label", "canonical_action": "visit",
             "hub_name": None, "route_tour_name": None, "route_segment_count": None,
             "segment_tour_names": None},
        ]
        self.live_labels.return_value = {"old1": ("live1", "Delhi", "explore")}
        result = await admin_dashboard.list_slate(
            _make_request(_make_pool(conn)), tenant_id=str(uuid.uuid4()), channel=None,
            x_admin_secret=_TEST_SECRET,
        )
        row = result["data"][0]
        assert (row["segment_id"], row["canonical_place"], row["canonical_action"]) == (
            "live1", "Delhi", "explore")
        assert self.live_labels.await_args.args[1] == ["old1"]

    @pytest.mark.asyncio
    async def test_returns_subjects_and_state_breakdown(self):
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"subject_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "tenant_name": "WanderLux",
             "channel": "instagram", "state": "picked", "score": 12.5,
             "segment_id": "seg1", "route_id": None, "cleared_bar_reason": '{"needs_demand": true}',
             "created_at": "2026-09-01T00:00:00",
             "canonical_place": "Bolaven Plateau", "canonical_action": "visit coffee and tea plantation",
             "hub_name": None, "route_tour_name": None, "route_segment_count": None,
             "segment_tour_names": ["Southern Laos Explorer"]},
            {"subject_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "tenant_name": "WanderLux",
             "channel": "blog", "state": "proposed", "score": None,
             "segment_id": None, "route_id": "r1", "cleared_bar_reason": '{}',
             "created_at": "2026-09-01T00:00:00",
             "canonical_place": None, "canonical_action": None,
             "hub_name": "Vat Phou Loop", "route_tour_name": "4000 Islands Adventure",
             "route_segment_count": 3, "segment_tour_names": None},
        ]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.list_slate(
            request, tenant_id=str(uuid.uuid4()), channel=None, x_admin_secret=_TEST_SECRET,
        )
        assert result["total"] == 2
        assert result["by_state"]["picked"] == 1
        assert result["by_state"]["proposed"] == 1
        assert result["by_state"]["cut"] == 0

        segment_row, route_row = result["data"]
        # Segment-based row: topic comes from canonical_place/action, tour_name from the
        # (possibly multi-tour) aggregate, no segment_count.
        assert segment_row["canonical_place"] == "Bolaven Plateau"
        assert segment_row["tour_name"] == "Southern Laos Explorer"
        assert segment_row["segment_count"] is None
        # Route-based row: topic comes from hub_name, tour_name/segment_count from the Route join.
        assert route_row["hub_name"] == "Vat Phou Loop"
        assert route_row["tour_name"] == "4000 Islands Adventure"
        assert route_row["segment_count"] == 3
        # Internal join-only columns must not leak into the response.
        for row in result["data"]:
            assert "route_tour_name" not in row
            assert "route_segment_count" not in row
            assert "segment_tour_names" not in row

    @pytest.mark.asyncio
    async def test_cut_state_rows_are_not_filtered_out(self):
        """AA-556 regression guard — found live while verifying that issue's own "cut count
        rises on the admin dashboard" acceptance criteria: the SQL used to hard-filter
        `s.state != 'cut'`, so `by_state["cut"]` could never be anything but 0 no matter how many
        Subjects were actually cut. Confirmed directly against RDS (2 real cut linkedin Subjects,
        endpoint reporting cut: 0) before fixing. This asserts a cut-state row survives into both
        `data` and `by_state`, and that the query itself no longer excludes it."""
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"subject_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "tenant_name": "WanderLux",
             "channel": "linkedin", "state": "cut", "score": None,
             "segment_id": "seg1", "route_id": None, "cleared_bar_reason": '{}',
             "created_at": "2026-09-10T00:00:00",
             "canonical_place": "Luang Prabang", "canonical_action": "sunrise alms",
             "hub_name": None, "route_tour_name": None, "route_segment_count": None,
             "segment_tour_names": ["Laos Classic"]},
        ]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.list_slate(
            request, tenant_id=str(uuid.uuid4()), channel=None, x_admin_secret=_TEST_SECRET,
        )
        assert result["total"] == 1
        assert result["by_state"]["cut"] == 1
        assert result["data"][0]["state"] == "cut"

        query, *_ = conn.fetch.call_args[0]
        assert "!= 'cut'" not in query

    @pytest.mark.asyncio
    async def test_scoped_by_tenant_id_not_tour_id(self):
        tenant_id = str(uuid.uuid4())
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.list_slate(
            request, tenant_id=tenant_id, channel=None, x_admin_secret=_TEST_SECRET,
        )
        assert result["tenant_id"] == tenant_id
        query, *params = conn.fetch.call_args[0]
        assert "s.tenant_id = $1" in query
        assert params[0] == tenant_id

    @pytest.mark.asyncio
    async def test_channel_filter_passed_through(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_dashboard.list_slate(
            request, tenant_id=str(uuid.uuid4()), channel="blog", x_admin_secret=_TEST_SECRET,
        )
        query, *params = conn.fetch.call_args[0]
        assert "s.channel = $2" in query
        assert params[1] == "blog"


class TestDashboardSummary:
    """AA-551 — new GET /admin/dashboard/summary, the rebuilt page's header stat bar."""

    @pytest.mark.asyncio
    async def test_returns_all_six_counts(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {
            "tour_count": 42, "atom_count": 377, "segment_count": 138,
            "score_count": 138, "route_count": 13, "hub_count": 6,
        }
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.dashboard_summary(
            request, tour_id=None, market=None, x_admin_secret=_TEST_SECRET,
        )

        assert result["tour_count"] == 42
        assert result["atom_count"] == 377
        assert result["hub_count"] == 6
        conn.fetchrow.assert_awaited_once()
        query, *params = conn.fetchrow.call_args[0]
        assert params == [None, None]

    @pytest.mark.asyncio
    async def test_passes_tour_id_and_market_through(self):
        tour_id = str(uuid.uuid4())
        conn = AsyncMock()
        conn.fetchrow.return_value = {
            "tour_count": 1, "atom_count": 10, "segment_count": 4,
            "score_count": 4, "route_count": 1, "hub_count": 1,
        }
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_dashboard.dashboard_summary(
            request, tour_id=tour_id, market="US", x_admin_secret=_TEST_SECRET,
        )

        assert result["tour_id"] == tour_id
        assert result["market"] == "US"
        _query, *params = conn.fetchrow.call_args[0]
        assert params == [tour_id, "US"]

    @pytest.mark.asyncio
    async def test_wrong_admin_secret_rejected(self):
        from fastapi import HTTPException
        conn = AsyncMock()
        pool = _make_pool(conn)
        request = _make_request(pool)
        with pytest.raises(HTTPException) as exc:
            await admin_dashboard.dashboard_summary(request, tour_id=None, market=None, x_admin_secret="wrong")
        assert exc.value.status_code == 403


class TestContentLogPublishLogTourFilter:
    """AA-527 (bổ sung) — the tour_id filter added to admin_a4.py's pre-existing endpoints.
    Unchanged by AA-551 (these 3 sections moved to a new page, /admin/tenant-activity, but the
    backend they call was not touched)."""

    @pytest.mark.asyncio
    async def test_content_log_tour_id_filters_on_trip_id(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)
        tour_id = str(uuid.uuid4())

        await admin_a4.get_content_log(request, tenant_id=None, tour_id=tour_id, limit=200, x_admin_secret=_TEST_SECRET)

        query, *params = conn.fetch.call_args[0]
        assert "agr.trip_id = $1::uuid" in query
        assert tour_id in params

    @pytest.mark.asyncio
    async def test_publish_log_tour_id_joins_through_content_piece(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)
        tour_id = str(uuid.uuid4())

        await admin_a4.get_publish_log(request, tenant_id=None, tour_id=tour_id, limit=200, x_admin_secret=_TEST_SECRET)

        query, *params = conn.fetch.call_args[0]
        assert "JOIN acp_shared.content_piece cp ON cp.piece_id = pl.piece_id" in query
        assert "agr.trip_id = $1::uuid" in query
        assert tour_id in params

    @pytest.mark.asyncio
    async def test_publish_log_without_tour_id_has_no_join(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_a4.get_publish_log(request, tenant_id=None, tour_id=None, limit=200, x_admin_secret=_TEST_SECRET)

        query, *_params = conn.fetch.call_args[0]
        assert "JOIN acp_shared.content_piece" not in query
