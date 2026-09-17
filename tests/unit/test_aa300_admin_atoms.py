"""AA-300 — curation UI backend (api/routers/admin_atoms.py).
AA-431 update: list/summary/patch endpoints no longer take x_admin_secret
directly — auth+scoping moved into _resolve_atom_owner_scope() (Depends),
which either verifies a tenant Bearer JWT (returns that tenant's tenant_id
as owner_scope) or falls back to verify_admin_secret() (returns None, no
filter — unchanged admin/staff behavior). Tests that call these functions
directly (bypassing real FastAPI Depends resolution, the established
pattern in this repo) now pass owner_scope= explicitly instead of
x_admin_secret=.

AA-475: patch_atoms_bulk/preview_slotgrid and their tests were deleted along
with /admin/curation + /admin/curation/preview (their only callers) — this
file now covers only list_atoms/atoms_summary/patch_atom. AA-526 removed
their one tenant-facing caller (/portal/t6-atoms, now deleted along with
tenant atom visibility entirely) — these endpoints' owner_scope-agnostic
x-admin-secret path (never filtered by owner_scope, confirmed AA-525 Phần 5
mục 3) is exactly what AA-527's new admin Atom Curation page is expected to
build on instead.

Mocks the asyncpg pool — no live DB, no LLM.

ADMIN_SECRET is a module-level constant in api/routers/admin.py, captured
from the environment at import time — verify_admin_secret() reads that
module global directly (it's defined in admin.py, so Python resolves the
name against admin.py's globals even when the function is re-exported into
admin_atoms.py via `from api.routers.admin import verify_admin_secret`).
monkeypatch.setenv() after import has no effect on it; every test here uses
monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", ...) instead.
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import HTTPException

from api.routers import admin_atoms
from api.routers.auth import JWT_ALG, JWT_SECRET

TENANT = "00000000-0000-0000-0000-000000000001"
_TEST_SECRET = "test-admin-secret"


def _tenant_credentials(tenant_id: str = TENANT):
    """A real, correctly-signed tenant JWT wrapped exactly like FastAPI's
    HTTPBearer would hand it to _resolve_atom_owner_scope — same JWT_SECRET/
    JWT_ALG api.routers.auth._create_jwt() uses, so verify_jwt() accepts it."""
    from fastapi.security import HTTPAuthorizationCredentials
    token = jwt.encode({"sub": tenant_id, "role": "tenant"}, JWT_SECRET, algorithm=JWT_ALG)
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    """Every test in this file runs with a known, non-empty ADMIN_SECRET —
    call sites pass _TEST_SECRET explicitly to authenticate."""
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


def _atom_row(**over):
    # media is JSONB on tour_atoms — asyncpg has no jsonb codec registered
    # on this app's connections, so it comes back as a raw JSON string, not
    # a parsed dict (found live in AA-300's preview-slotgrid 500 bug; fixed
    # in admin_atoms.py::_safe()). Fixtures must match real asyncpg shape,
    # not a hand-convenient Python dict, or a regression here goes untested.
    base = {
        "atom_id": "atom_abc1234567", "tour_id": uuid.uuid4(), "tour_name": "Sapa Valley Trek",
        "text": "Crossing the bamboo bridge at Ta Van village", "activity_type": "trek",
        "emotional_hook": None, "visual_potential": 2, "distinctiveness": "LOW",
        "media": '{"has_photo": false, "has_video": false, "media_refs": []}',
        "deleted": False,
        "created_at": "2026-07-01T00:00:00", "updated_at": "2026-07-01T00:00:00",
        "unreviewed": True, "tour_atom_count": 4,
    }
    base.update(over)
    return base


class TestAuthGate:
    def test_wrong_secret_rejected(self):
        with pytest.raises(HTTPException) as exc:
            admin_atoms.verify_admin_secret("wrong-secret")
        assert exc.value.status_code == 403

    def test_no_secret_configured_503(self, monkeypatch):
        monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", "")
        with pytest.raises(HTTPException) as exc:
            admin_atoms.verify_admin_secret("anything")
        assert exc.value.status_code == 503

    def test_correct_secret_passes(self):
        admin_atoms.verify_admin_secret(_TEST_SECRET)  # must not raise

    def test_admin_atoms_reuses_admin_verify_admin_secret(self):
        """PHẦN A decision — admin_atoms.py must import, not redefine."""
        from api.routers.admin import verify_admin_secret
        assert admin_atoms.verify_admin_secret is verify_admin_secret


class TestResolveAtomOwnerScope:
    """AA-431 — the new Depends() that replaced x_admin_secret on list/
    summary/patch. Mirrors _resolve_brand_tenant_id's own test shape
    (admin_pipeline.py, AA-424)."""

    def test_valid_tenant_jwt_returns_tenant_id_as_owner_scope(self):
        tenant_id = str(uuid.uuid4())
        result = admin_atoms._resolve_atom_owner_scope(
            credentials=_tenant_credentials(tenant_id), x_admin_secret=None)
        assert result == tenant_id

    def test_invalid_jwt_401(self):
        from fastapi.security import HTTPAuthorizationCredentials
        bad = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-real-jwt")
        with pytest.raises(HTTPException) as exc:
            admin_atoms._resolve_atom_owner_scope(credentials=bad, x_admin_secret=None)
        assert exc.value.status_code == 401

    def test_no_credentials_falls_back_to_admin_secret_returns_none(self):
        result = admin_atoms._resolve_atom_owner_scope(credentials=None, x_admin_secret=_TEST_SECRET)
        assert result is None

    def test_no_credentials_wrong_secret_403(self):
        with pytest.raises(HTTPException) as exc:
            admin_atoms._resolve_atom_owner_scope(credentials=None, x_admin_secret="wrong")
        assert exc.value.status_code == 403


class TestListAtoms:
    @pytest.mark.asyncio
    async def test_default_batch_size_50(self):
        conn = AsyncMock()
        conn.fetch.return_value = [_atom_row(atom_id=f"atom_{i}") for i in range(50)]
        conn.fetchval.return_value = 137
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=None, atom_ids=None, distinctiveness=None, unreviewed_only=False,
            thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None, limit=50, offset=0,
            owner_scope=None,
        )
        assert result["limit"] == 50
        assert len(result["atoms"]) == 50

    @pytest.mark.asyncio
    async def test_total_count_from_separate_count_query(self):
        """Pagination.tsx (reused as-is on the frontend) needs a total
        matching-filter count, not just the current page's row count — a
        second COUNT(*) query using the same WHERE clause."""
        conn = AsyncMock()
        conn.fetch.return_value = [_atom_row(atom_id=f"atom_{i}") for i in range(50)]
        conn.fetchval.return_value = 137
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=None, atom_ids=None, distinctiveness=None, unreviewed_only=False,
            thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None, limit=50, offset=0,
            owner_scope=None,
        )
        assert result["total"] == 137
        count_query = conn.fetchval.call_args[0][0]
        assert count_query.strip().startswith("SELECT count(*)")

    @pytest.mark.asyncio
    async def test_unreviewed_filter_adds_clause(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=None, atom_ids=None, distinctiveness=None, unreviewed_only=True,
            thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None, limit=50, offset=0,
            owner_scope=None,
        )
        query = conn.fetch.call_args[0][0]
        assert "ta.updated_at = ta.created_at" in query

    @pytest.mark.asyncio
    async def test_thin_only_filter_uses_thin_trip_atom_min(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        from services.acp_shared.atom_constants import THIN_TRIP_ATOM_MIN

        await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=None, atom_ids=None, distinctiveness=None, unreviewed_only=False,
            thin_only=True, include_deleted=False, owner_scope_class=None, lifecycle_stage=None, limit=50, offset=0,
            owner_scope=None,
        )
        query, *params = conn.fetch.call_args[0]
        assert "tc.atom_count <" in query
        assert THIN_TRIP_ATOM_MIN in params

    @pytest.mark.asyncio
    async def test_distinctiveness_filter(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=None, atom_ids=None, distinctiveness="HIGH", unreviewed_only=False,
            thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None, limit=50, offset=0,
            owner_scope=None,
        )
        query, *params = conn.fetch.call_args[0]
        assert "ta.distinctiveness =" in query
        assert "HIGH" in params

    @pytest.mark.asyncio
    async def test_deleted_excluded_by_default(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=None, atom_ids=None, distinctiveness=None, unreviewed_only=False,
            thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None, limit=50, offset=0,
            owner_scope=None,
        )
        query = conn.fetch.call_args[0][0]
        assert "NOT ta.deleted" in query

    @pytest.mark.asyncio
    async def test_empty_marker_atoms_always_excluded(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=None, atom_ids=None, distinctiveness=None, unreviewed_only=False,
            thin_only=False, include_deleted=True, owner_scope_class=None, lifecycle_stage=None, limit=50, offset=0,
            owner_scope=None,
        )
        query = conn.fetch.call_args[0][0]
        assert "NOT ta.is_empty_marker" in query

    # ── AA-345 round 2, Việc 4: tour_ids (plural) deep link ─────────────────
    @pytest.mark.asyncio
    async def test_tour_ids_plural_filters_to_exact_set(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]

        await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=",".join(ids), atom_ids=None, distinctiveness=None,
            unreviewed_only=False, thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None,
            limit=50, offset=0, owner_scope=None,
        )
        query, *params = conn.fetch.call_args[0]
        assert "ta.tour_id = ANY(" in query
        assert ids in params

    @pytest.mark.asyncio
    async def test_tour_ids_plural_wins_over_singular_tour_id(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)
        ids = [str(uuid.uuid4())]

        await admin_atoms.list_atoms(
            request, tour_id="some-other-id", tour_ids=ids[0], atom_ids=None, distinctiveness=None,
            unreviewed_only=False, thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None,
            limit=50, offset=0, owner_scope=None,
        )
        query = conn.fetch.call_args[0][0]
        assert "ta.tour_id = ANY(" in query
        assert "ta.tour_id = $" not in query

    # AA-345 round 4 — Nghiep live-verified a real atomize run with EXACTLY
    # ONE tour_id ("Jomolhari Trek") and the deep link's ?tour_ids=<one uuid>
    # did not show that tour in Curation. Investigated end to end (live DB
    # query + a direct curl against the actual running ECS container,
    # bypassing the gateway/frontend entirely, using the real tour_id from
    # the incident) — both the SQL this handler builds and the live HTTP
    # response are correct for a single tour_id: `ta.tour_id =
    # ANY($1::uuid[])` with a 1-element array matches fine in Postgres, and
    # `test_tour_ids_plural_wins_over_singular_tour_id` above already
    # exercises a single-id string through this code path, but never
    # actually asserted the resulting SQL PARAMS were a proper 1-element
    # list (only that the ANY(...) clause was chosen) — that's the exact gap
    # this closes. Root cause of the live report was not found in this
    # handler; see docs/implementation-notes/AA-345-round4.md for the full
    # investigation (live data proved both this endpoint and
    # GET /admin/atoms/summary return the tour correctly).
    @pytest.mark.asyncio
    async def test_tour_ids_single_element_produces_single_element_param(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)
        single_id = str(uuid.uuid4())

        await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=single_id, atom_ids=None, distinctiveness=None,
            unreviewed_only=False, thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None,
            limit=50, offset=0, owner_scope=None,
        )
        query, *params = conn.fetch.call_args[0]
        assert "ta.tour_id = ANY(" in query
        assert [single_id] in params

    @pytest.mark.asyncio
    async def test_singular_tour_id_still_works_when_tour_ids_absent(self):
        """Backward compat — the older single-tour deep link (still used
        elsewhere) must keep working unchanged."""
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_atoms.list_atoms(
            request, tour_id="abc-123", tour_ids=None, atom_ids=None, distinctiveness=None,
            unreviewed_only=False, thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None,
            limit=50, offset=0, owner_scope=None,
        )
        query, *params = conn.fetch.call_args[0]
        assert "ta.tour_id = $1::uuid" in query
        assert "abc-123" in params

    # ── AA-431 — owner_scope filtering ──────────────────────────────────────
    @pytest.mark.asyncio
    async def test_tenant_owner_scope_adds_filter_clause(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)
        tenant_id = str(uuid.uuid4())

        await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=None, atom_ids=None, distinctiveness=None,
            unreviewed_only=False, thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None,
            limit=50, offset=0, owner_scope=tenant_id,
        )
        query, *params = conn.fetch.call_args[0]
        assert "ta.owner_scope = $" in query
        assert tenant_id in params

    @pytest.mark.asyncio
    async def test_admin_owner_scope_none_adds_no_filter_clause(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await admin_atoms.list_atoms(
            request, tour_id=None, tour_ids=None, atom_ids=None, distinctiveness=None,
            unreviewed_only=False, thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None,
            limit=50, offset=0, owner_scope=None,
        )
        query = conn.fetch.call_args[0][0]
        # AA-527 — the SELECT list itself always includes ta.owner_scope now (so the new admin
        # curation page can show a Platform/legacy-tenant badge per atom); what must NOT appear
        # for owner_scope=None is a FILTER clause on it.
        assert "ta.owner_scope = $" not in query


class TestPatchAtom:
    @pytest.mark.asyncio
    async def test_soft_delete_atom(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _atom_row(deleted=True)
        pool = _make_pool(conn)
        request = _make_request(pool)

        # AA-564 3.1 — deleted=True now also fires a fire-and-forget recompute task; mocked here
        # so this test stays about the PATCH response, not the recompute (see
        # TestDeleteTriggersRecompute below for that).
        with patch("services.export.handler.recompute_segment_score_route", AsyncMock()):
            body = admin_atoms.AtomPatchRequest(deleted=True)
            result = await admin_atoms.patch_atom(
                "atom_abc1234567", body, request, owner_scope=None)
            assert result["deleted"] is True
            await asyncio.sleep(0)  # let the fire-and-forget task run to completion

    @pytest.mark.asyncio
    async def test_edit_text(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _atom_row(text="Corrected text")
        pool = _make_pool(conn)
        request = _make_request(pool)

        body = admin_atoms.AtomPatchRequest(text="Corrected text")
        result = await admin_atoms.patch_atom(
            "atom_abc1234567", body, request, owner_scope=None)
        assert result["text"] == "Corrected text"

    @pytest.mark.asyncio
    async def test_empty_text_rejected(self):
        pool = _make_pool(AsyncMock())
        request = _make_request(pool)
        body = admin_atoms.AtomPatchRequest(text="   ")
        with pytest.raises(HTTPException) as exc:
            await admin_atoms.patch_atom("atom_abc1234567", body, request, owner_scope=None)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_no_fields_rejected(self):
        pool = _make_pool(AsyncMock())
        request = _make_request(pool)
        body = admin_atoms.AtomPatchRequest()
        with pytest.raises(HTTPException) as exc:
            await admin_atoms.patch_atom("atom_abc1234567", body, request, owner_scope=None)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_atom_not_found_404(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        pool = _make_pool(conn)
        request = _make_request(pool)
        body = admin_atoms.AtomPatchRequest(text="edited")
        with pytest.raises(HTTPException) as exc:
            await admin_atoms.patch_atom("atom_nonexistent", body, request, owner_scope=None)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_query_excludes_empty_marker_rows(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _atom_row()
        pool = _make_pool(conn)
        request = _make_request(pool)
        body = admin_atoms.AtomPatchRequest(text="edited")
        await admin_atoms.patch_atom("atom_abc1234567", body, request, owner_scope=None)
        query = conn.fetchrow.call_args[0][0]
        assert "NOT is_empty_marker" in query

    # AA-431: secret validation moved into _resolve_atom_owner_scope() — see
    # TestResolveAtomOwnerScope for the rejection tests (patch_atom no longer
    # takes x_admin_secret directly, only the already-resolved owner_scope).

    @pytest.mark.asyncio
    async def test_tenant_owner_scope_adds_where_clause(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _atom_row(text="edited")
        pool = _make_pool(conn)
        request = _make_request(pool)
        tenant_id = str(uuid.uuid4())
        body = admin_atoms.AtomPatchRequest(text="edited")

        await admin_atoms.patch_atom("atom_abc1234567", body, request, owner_scope=tenant_id)
        query, *params = conn.fetchrow.call_args[0]
        assert "AND owner_scope = $" in query
        assert tenant_id in params

    @pytest.mark.asyncio
    async def test_tenant_owner_scope_mismatch_404s_not_editable(self):
        """A tenant guessing another owner_scope's atom_id must get the same
        404 as a nonexistent atom_id — not a successful edit (IDOR guard)."""
        conn = AsyncMock()
        conn.fetchrow.return_value = None  # WHERE atom_id=... AND owner_scope=... matches 0 rows
        pool = _make_pool(conn)
        request = _make_request(pool)
        body = admin_atoms.AtomPatchRequest(text="edited")

        with pytest.raises(HTTPException) as exc:
            await admin_atoms.patch_atom(
                "atom_belongs_to_someone_else", body, request, owner_scope=str(uuid.uuid4()))
        assert exc.value.status_code == 404


class TestAtomsSummary:
    @pytest.mark.asyncio
    async def test_breakdown_and_totals_independent_of_list_filters(self):
        conn = AsyncMock()
        conn.fetch.side_effect = [
            [{"distinctiveness": "LOW", "c": 230}, {"distinctiveness": "HIGH", "c": 5}],
            [
                {"tour_id": uuid.uuid4(), "tour_name": "Sapa Valley Trek",
                 "atom_count": 4, "unreviewed_count": 4, "atomized_at": None,
                 "used_atom_count": 0, "lifecycle_stage": "active",
                 "owner_scopes": ["platform"]},
                {"tour_id": uuid.uuid4(), "tour_name": "Mongolia Gobi",
                 "atom_count": 12, "unreviewed_count": 0, "atomized_at": None,
                 "used_atom_count": 0, "lifecycle_stage": "active",
                 "owner_scopes": ["platform"]},
            ],
        ]
        conn.fetchrow.return_value = {"total": 235, "reviewed": 12}
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_atoms.atoms_summary(request, owner_scope=None)

        assert result["distinctiveness_breakdown"] == {"HIGH": 5, "MED": 0, "LOW": 230}
        assert result["total_count"] == 235
        assert result["reviewed_count"] == 12

    @pytest.mark.asyncio
    async def test_by_tour_marks_thin_trips(self):
        from services.acp_shared.atom_constants import THIN_TRIP_ATOM_MIN
        conn = AsyncMock()
        conn.fetch.side_effect = [
            [],
            [
                {"tour_id": uuid.uuid4(), "tour_name": "Ha Giang Loop",
                 "atom_count": 4, "unreviewed_count": 4, "atomized_at": None,  # < THIN_TRIP_ATOM_MIN=5 -> thin
                 "used_atom_count": 0, "lifecycle_stage": "active",
                 "owner_scopes": ["platform"]},
                {"tour_id": uuid.uuid4(), "tour_name": "Mongolia Gobi",
                 "atom_count": 12, "unreviewed_count": 0, "atomized_at": None,  # not thin
                 "used_atom_count": 0, "lifecycle_stage": "active",
                 "owner_scopes": ["platform"]},
            ],
        ]
        conn.fetchrow.return_value = {"total": 16, "reviewed": 0}
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_atoms.atoms_summary(request, owner_scope=None)

        by_name = {t["tour_name"]: t for t in result["by_tour"]}
        assert by_name["Ha Giang Loop"]["atom_count"] < THIN_TRIP_ATOM_MIN
        assert by_name["Ha Giang Loop"]["is_thin"] is True
        assert by_name["Mongolia Gobi"]["is_thin"] is False

    @pytest.mark.asyncio
    async def test_atomized_at_isoformatted_for_newest_first_sort(self):
        """AA-345 round 2, Việc 4: 'Newest first' sort on the curation page
        reads by_tour[i].atomized_at — must be a JSON-safe ISO string, not a
        raw datetime (which json.dumps can't serialize as-is)."""
        import datetime
        ts = datetime.datetime(2026, 8, 9, 6, 14, 45, tzinfo=datetime.timezone.utc)
        conn = AsyncMock()
        conn.fetch.side_effect = [
            [],
            [{"tour_id": uuid.uuid4(), "tour_name": "Classic Laos",
              "atom_count": 48, "unreviewed_count": 0, "atomized_at": ts,
              "used_atom_count": 0, "lifecycle_stage": "active",
              "owner_scopes": ["platform"]}],
        ]
        conn.fetchrow.return_value = {"total": 48, "reviewed": 48}
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_atoms.atoms_summary(request, owner_scope=None)

        assert result["by_tour"][0]["atomized_at"] == ts.isoformat()

    @pytest.mark.asyncio
    async def test_owner_scopes_exposed_for_new_curation_page_badge(self):
        """AA-527 — the new admin curation page needs to tell a real platform-scope tour apart
        from a pre-AA-526 legacy tenant-scoped one; by_tour must expose the real distinct
        owner_scope value(s) for each tour, not silently drop them."""
        conn = AsyncMock()
        legacy_tenant = str(uuid.uuid4())
        conn.fetch.side_effect = [
            [],
            [
                {"tour_id": uuid.uuid4(), "tour_name": "Sri Lanka Highlands",
                 "atom_count": 108, "unreviewed_count": 108, "atomized_at": None,
                 "used_atom_count": 0, "lifecycle_stage": "active",
                 "owner_scopes": ["platform"]},
                {"tour_id": uuid.uuid4(), "tour_name": "Southern Laos",
                 "atom_count": 75, "unreviewed_count": 0, "atomized_at": None,
                 "used_atom_count": 0, "lifecycle_stage": "active",
                 "owner_scopes": [legacy_tenant]},
            ],
        ]
        conn.fetchrow.return_value = {"total": 183, "reviewed": 0}
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_atoms.atoms_summary(request, owner_scope=None)

        by_name = {t["tour_name"]: t for t in result["by_tour"]}
        assert by_name["Sri Lanka Highlands"]["owner_scopes"] == ["platform"]
        assert by_name["Southern Laos"]["owner_scopes"] == [legacy_tenant]

    @pytest.mark.asyncio
    async def test_atomized_at_null_when_no_atoms_have_a_timestamp(self):
        conn = AsyncMock()
        conn.fetch.side_effect = [
            [],
            [{"tour_id": uuid.uuid4(), "tour_name": "Untouched Tour",
              "atom_count": 0, "unreviewed_count": 0, "atomized_at": None,
              "used_atom_count": 0, "lifecycle_stage": "active",
              "owner_scopes": []}],
        ]
        conn.fetchrow.return_value = {"total": 0, "reviewed": 0}
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_atoms.atoms_summary(request, owner_scope=None)

        assert result["by_tour"][0]["atomized_at"] is None

    # AA-431: secret validation moved into _resolve_atom_owner_scope() — see
    # TestResolveAtomOwnerScope.

    @pytest.mark.asyncio
    async def test_tenant_owner_scope_adds_filter_to_all_three_queries(self):
        conn = AsyncMock()
        conn.fetch.side_effect = [[], []]
        conn.fetchrow.return_value = {"total": 0, "reviewed": 0}
        pool = _make_pool(conn)
        request = _make_request(pool)
        tenant_id = str(uuid.uuid4())

        await admin_atoms.atoms_summary(request, owner_scope=tenant_id)

        breakdown_query, *breakdown_params = conn.fetch.call_args_list[0][0]
        by_tour_query, *by_tour_params = conn.fetch.call_args_list[1][0]
        totals_query, *totals_params = conn.fetchrow.call_args[0]
        assert "owner_scope = $1" in breakdown_query and tenant_id in breakdown_params
        assert "owner_scope = $1" in by_tour_query and tenant_id in by_tour_params
        assert "owner_scope = $1" in totals_query and tenant_id in totals_params


class TestDeleteTriggersRecompute:
    """AA-564 3.1 (decision 2, AA-563) — curating `deleted` fires Segment/Score/Route recompute
    for that atom's tour; a `text`-only edit never does (only `deleted` affects Segment
    eligibility, `WHERE NOT ta.deleted`). AA-609 removed the old `starred` flag entirely."""

    @pytest.mark.asyncio
    async def test_deleted_true_fires_recompute_for_that_tour(self):
        tour_id = uuid.uuid4()
        conn = AsyncMock()
        conn.fetchrow.return_value = _atom_row(deleted=True, tour_id=tour_id)
        pool = _make_pool(conn)
        request = _make_request(pool)

        with patch("services.export.handler.recompute_segment_score_route", AsyncMock()) as m_recompute:
            body = admin_atoms.AtomPatchRequest(deleted=True)
            await admin_atoms.patch_atom("atom_abc1234567", body, request, owner_scope=None)
            await asyncio.sleep(0)

        m_recompute.assert_awaited_once()
        args, kwargs = m_recompute.call_args
        assert args[0] == str(tour_id)
        assert args[1] is pool

    @pytest.mark.asyncio
    async def test_deleted_false_also_fires_recompute_undelete_restores_eligibility(self):
        # Explicitly setting deleted=False (restoring an atom) also changes Segment eligibility
        # (WHERE NOT ta.deleted) — the trigger is "deleted was included in the patch", not
        # "deleted was set to true" specifically.
        conn = AsyncMock()
        conn.fetchrow.return_value = _atom_row(deleted=False)
        pool = _make_pool(conn)
        request = _make_request(pool)

        with patch("services.export.handler.recompute_segment_score_route", AsyncMock()) as m_recompute:
            body = admin_atoms.AtomPatchRequest(deleted=False)
            await admin_atoms.patch_atom("atom_abc1234567", body, request, owner_scope=None)
            await asyncio.sleep(0)

        m_recompute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_text_only_edit_does_not_fire_recompute(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _atom_row(text="edited")
        pool = _make_pool(conn)
        request = _make_request(pool)

        with patch("services.export.handler.recompute_segment_score_route", AsyncMock()) as m_recompute:
            body = admin_atoms.AtomPatchRequest(text="edited")
            await admin_atoms.patch_atom("atom_abc1234567", body, request, owner_scope=None)
            await asyncio.sleep(0)

        m_recompute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recompute_failure_does_not_surface_as_patch_failure(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _atom_row(deleted=True)
        pool = _make_pool(conn)
        request = _make_request(pool)

        with patch("services.export.handler.recompute_segment_score_route",
                   AsyncMock(side_effect=RuntimeError("boom"))):
            body = admin_atoms.AtomPatchRequest(deleted=True)
            result = await admin_atoms.patch_atom("atom_abc1234567", body, request, owner_scope=None)
            await asyncio.sleep(0)

        assert result["deleted"] is True


class TestUnatomizedToursAndManualTrigger:
    """AA-564 3.1/3.2 (decision 1, AA-563) — manual atomize backfill for Master Content tours that
    never went through the one automatic trigger (process_export(), AA-526)."""

    @pytest.mark.asyncio
    async def test_list_unatomized_tours(self):
        tour_id = uuid.uuid4()
        conn = AsyncMock()
        conn.fetch.return_value = [{"tour_id": tour_id, "tour_name": "Sapa Valley Trek"}]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await admin_atoms.list_unatomized_tours(request, x_admin_secret=_TEST_SECRET)
        assert result["total"] == 1
        assert result["tours"][0]["tour_id"] == str(tour_id)
        query, *params = conn.fetch.call_args[0]
        assert "NOT EXISTS" in query
        assert params == [admin_atoms._MASTER_TENANT_ID]

    @pytest.mark.asyncio
    async def test_trigger_atomize_rejects_when_neither_tour_id_nor_all(self):
        pool = _make_pool(AsyncMock())
        request = _make_request(pool)
        body = admin_atoms.AtomizeTriggerRequest()
        with pytest.raises(HTTPException) as exc:
            await admin_atoms.trigger_atomize(body, request, x_admin_secret=_TEST_SECRET)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_trigger_atomize_404_when_no_matching_tour(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)
        body = admin_atoms.AtomizeTriggerRequest(tour_id=str(uuid.uuid4()))
        with pytest.raises(HTTPException) as exc:
            await admin_atoms.trigger_atomize(body, request, x_admin_secret=_TEST_SECRET)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_trigger_atomize_one_tour_fires_background_atomize(self):
        tour_id = uuid.uuid4()
        gen_content_id = uuid.uuid4()
        conn = AsyncMock()
        conn.fetch.return_value = [{
            "tour_id": tour_id, "generated_content_id": gen_content_id,
            "aa_name": "Sapa Valley Trek", "aa_summary": "A trek.",
            "aa_highlights": "[]", "aa_itineraries": "Day 1...", "country": "Vietnam",
        }]
        pool = _make_pool(conn)
        request = _make_request(pool)

        with patch("services.export.handler._run_a3_atomize_background", AsyncMock()) as m_atomize:
            body = admin_atoms.AtomizeTriggerRequest(tour_id=str(tour_id))
            result = await admin_atoms.trigger_atomize(body, request, x_admin_secret=_TEST_SECRET)
            await asyncio.sleep(0)

        assert result["accepted"] is True
        assert result["tour_count"] == 1
        assert result["tour_ids"] == [str(tour_id)]
        m_atomize.assert_awaited_once()
        _args, kwargs = m_atomize.call_args
        assert kwargs["tour_id"] == str(tour_id)
        assert kwargs["version_id"] == str(gen_content_id)
        assert kwargs["country"] == "Vietnam"
        assert kwargs["rewritten"]["name"] == "Sapa Valley Trek"

    @pytest.mark.asyncio
    async def test_trigger_atomize_all_runs_every_tour_sequentially(self):
        tours = [
            {"tour_id": uuid.uuid4(), "generated_content_id": uuid.uuid4(),
             "aa_name": f"Tour {i}", "aa_summary": "", "aa_highlights": "[]",
             "aa_itineraries": "", "country": "Vietnam"}
            for i in range(3)
        ]
        conn = AsyncMock()
        conn.fetch.return_value = tours
        pool = _make_pool(conn)
        request = _make_request(pool)

        with patch("services.export.handler._run_a3_atomize_background", AsyncMock()) as m_atomize:
            body = admin_atoms.AtomizeTriggerRequest(all=True)
            result = await admin_atoms.trigger_atomize(body, request, x_admin_secret=_TEST_SECRET)
            await asyncio.sleep(0)

        assert result["tour_count"] == 3
        assert m_atomize.await_count == 3

    @pytest.mark.asyncio
    async def test_trigger_atomize_one_failure_does_not_stop_the_rest(self):
        tours = [
            {"tour_id": uuid.uuid4(), "generated_content_id": uuid.uuid4(),
             "aa_name": f"Tour {i}", "aa_summary": "", "aa_highlights": "[]",
             "aa_itineraries": "", "country": "Vietnam"}
            for i in range(2)
        ]
        conn = AsyncMock()
        conn.fetch.return_value = tours
        pool = _make_pool(conn)
        request = _make_request(pool)

        with patch("services.export.handler._run_a3_atomize_background",
                   AsyncMock(side_effect=[RuntimeError("boom"), None])) as m_atomize:
            body = admin_atoms.AtomizeTriggerRequest(all=True)
            await admin_atoms.trigger_atomize(body, request, x_admin_secret=_TEST_SECRET)
            await asyncio.sleep(0)

        assert m_atomize.await_count == 2

    @pytest.mark.asyncio
    async def test_wrong_admin_secret_rejected(self):
        pool = _make_pool(AsyncMock())
        request = _make_request(pool)
        body = admin_atoms.AtomizeTriggerRequest(all=True)
        with pytest.raises(HTTPException) as exc:
            await admin_atoms.trigger_atomize(body, request, x_admin_secret="wrong-secret")
        assert exc.value.status_code == 403
