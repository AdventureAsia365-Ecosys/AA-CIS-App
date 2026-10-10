"""
tests/unit/test_aa469_viec5_a4_feedback_loop.py — AA-469 Việc 5: A4 feedback loop for T9/T10.

The A3-atomize-failure half of this file (TestEscalateT5AtomizeFailure, which covered
escalate_t5_atomize_failure) was removed at AA-757 (S224): atomize moved to
services/acp_contract/a3_atomize.py and the escalate helper was deleted (no live caller — atomize
now runs as the durable `a3_atomize` job, so failures retry and show on the Jobs page). What
remains here is the T9/T10 half:
- T9/T10 (content_piece.gate_ledger/held_reason) gets its first-ever A4 read route —
  api/routers/admin_a4.py::get_content_log(), new GET /admin/a4/content-log.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

_TEST_SECRET = "test-admin-secret"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _TEST_SECRET)


def _make_pool(fetch=None, execute=None):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.execute = AsyncMock(return_value=execute or "INSERT 0 1")

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _make_request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    return req


TENANT_ID = str(uuid.uuid4())
TOUR_ID = str(uuid.uuid4())
VERSION_ID = str(uuid.uuid4())
PIECE_ID = str(uuid.uuid4())
REQUEST_ID = str(uuid.uuid4())


# ── api.routers.admin_a4.get_content_log ────────────────────────────────────────

@pytest.mark.asyncio
class TestGetContentLog:
    async def test_requires_admin_secret(self):
        from api.routers.admin_a4 import get_content_log

        pool, _ = _make_pool(fetch=[])
        req = _make_request(pool)

        with pytest.raises(HTTPException) as exc_info:
            await get_content_log(req, tenant_id=None, limit=200, x_admin_secret="wrong")
        assert exc_info.value.status_code == 403

    @staticmethod
    def _full_row(**over):
        """AA-501 — every column the widened SELECT now returns. Held/failed rows are no longer
        the only rows this endpoint returns (see test_returns_every_status_not_just_held_failed
        below) — this fixture covers a 'held' row with a full angle/atom/tour/DFS-PAA context so
        every new field has real coverage."""
        base = {
            "piece_id": PIECE_ID, "tenant_id": TENANT_ID, "tenant_name": "WanderLux",
            "tenant_slug": "wanderlux-travel", "angle_gate_request_id": REQUEST_ID,
            "atom_id": "atom_abc123", "goal": "engagement_conversation", "cta": "Book now",
            "dfs_paa_snapshot": json.dumps(
                {"relevance": "HIGH", "people_also_ask": ["q1"], "related_keywords": ["k1"]}
            ),
            "trip_id": TOUR_ID, "channel": "tiktok",
            "status": "held", "held_reason": "F1_grounding: unsupported claim",
            "gate_ledger": json.dumps([
                {"gate": "F1_grounding", "passed": False, "violations": ["unsupported claim"]},
                {"gate": "F6_cta_present", "passed": True, "violations": []},
            ]),
            "repair_log": json.dumps([{"round": 1, "feedback": "add a source"}]),
            "attempt_number": 2, "content_text": "Some real content...",
            "atom_text": "Cross the bamboo bridge", "atom_activity_type": "adventure",
            "atom_emotional_hook": "awe", "atom_season_note": "dry season best",
            "tour_name": "Sapa Trek", "tour_destination": "Vietnam",
            "publish_id": None, "publish_external_url": None, "publish_published_at": None,
            "created_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
            # AA-561 3a — lineage fields.
            "subject_id": str(uuid.uuid4()), "segment_id": "seg_abc123", "route_id": None,
            "segment_place": "Sapa Terraces", "segment_action": "walk",
            "route_hub_name": None, "route_first_day": None, "route_last_day": None,
            "sibling_piece_count": 1, "request_first_piece_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
            "angles": json.dumps([
                {"option_id": str(uuid.uuid4()), "idx": 0, "name": "Behind the Scenes",
                 "why_it_works": "curiosity", "formula_fit": "AIDA", "best_final_style": "warm",
                 "recommended": True, "chosen": True},
                {"option_id": str(uuid.uuid4()), "idx": 1, "name": "The Local's View",
                 "why_it_works": "authenticity", "formula_fit": "PAS", "best_final_style": "candid",
                 "recommended": False, "chosen": False},
            ]),
        }
        base.update(over)
        return base

    async def test_returns_full_context_and_gate_detail(self):
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[self._full_row()])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)

        assert result["total"] == 1
        item = result["data"][0]
        assert item["status"] == "held"
        assert item["channel"] == "tiktok"
        assert len(item["gate_ledger"]) == 2
        assert item["gate_ledger"][0]["gate"] == "F1_grounding"
        assert item["gate_pass_count"] == 1
        assert item["gate_total_count"] == 2
        assert item["repair_log"] == [{"round": 1, "feedback": "add a source"}]
        # AA-561 3a — angle became a full list (was a single COALESCE'd chosen-only object).
        assert len(item["angles"]) == 2
        assert item["angles"][0]["name"] == "Behind the Scenes"
        assert item["angles"][0]["chosen"] is True
        assert item["angles"][1]["chosen"] is False
        assert item["atom"] == {
            "text": "Cross the bamboo bridge", "activity_type": "adventure",
            "emotional_hook": "awe", "season_note": "dry season best",
        }
        assert item["tour"] == {"name": "Sapa Trek", "destination": "Vietnam"}
        assert item["source"] == {
            "kind": "segment", "segment_id": "seg_abc123",
            "place": "Sapa Terraces", "action": "walk",
        }
        assert item["is_buffer_retry"] is False
        assert item["dfs_paa_snapshot"] == {
            "relevance": "HIGH", "people_also_ask": ["q1"], "related_keywords": ["k1"],
        }
        assert item["cta"] == "Book now"
        assert item["publish_status"] == "n/a"  # held — not ready to publish at all

    async def test_query_no_longer_hardcodes_held_failed_filter(self):
        """AA-501 — widened from held/failed-only to every content_piece row (Nghiệp: 'AA cần
        thấy MỌI THỨ tenant thấy, CỘNG THÊM chi tiết kỹ thuật' — not a different subset)."""
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[])
        req = _make_request(pool)
        await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)

        sql = conn.fetch.call_args[0][0]
        assert "cp.status IN ('held', 'failed')" not in sql

    async def test_publish_status_published_when_publish_log_row_exists(self):
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(status="approved", held_reason=None, publish_id=str(uuid.uuid4()))
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"][0]["publish_status"] == "published"

    async def test_publish_status_pending_when_approved_and_unpublished(self):
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(status="approved", held_reason=None, publish_id=None)
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"][0]["publish_status"] == "pending_publish"

    async def test_no_llm_cost_or_token_fields(self):
        """AA-501 build task explicitly excludes cost/token tracking (split to AA-505) — this
        endpoint must not fabricate or expose any such field."""
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[self._full_row()])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        keys = set(result["data"][0].keys())
        assert not any("cost" in k or "token" in k for k in keys)

    async def test_channel_reads_content_piece_first_coalesce_pattern(self):
        """Same COALESCE(cp.channel, agr.channel) fix AA-469 Việc 4 already applied to
        v1_publish.py's two queries on this same table, for the same reason: angle_gate_request.
        channel is no longer stable after a piece is written (set_channel() can be called again
        before the request's NEXT write)."""
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[])
        req = _make_request(pool)
        await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)

        sql = conn.fetch.call_args[0][0]
        assert "COALESCE(cp.channel, agr.channel)" in sql

    async def test_tenant_filter_scopes_query_cross_tenant_by_default(self):
        """Same cross-tenant-by-default shape as review-log/publish-log: an optional filter,
        not a hard tenant scope — A4 is cross-tenant oversight by design."""
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[])
        req = _make_request(pool)

        # No filter — every tenant's held/failed pieces are visible.
        await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        sql_unfiltered = conn.fetch.call_args[0][0]
        assert "cp.tenant_id = $" not in sql_unfiltered

        # With a filter — scoped to just that tenant.
        await get_content_log(req, tenant_id=TENANT_ID, limit=200, x_admin_secret=_TEST_SECRET)
        sql_filtered, *params = conn.fetch.call_args[0]
        assert "cp.tenant_id = $" in sql_filtered
        assert TENANT_ID in params

    async def test_source_is_direct_atom_when_no_subject(self):
        """AA-561 3a — the pre-existing atom-picker path (AA-449, never retired) creates a
        request with no Subject at all. Must read as an explicit label, not a blank/broken cell."""
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(
            subject_id=None, segment_id=None, route_id=None,
            segment_place=None, segment_action=None,
        )
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"][0]["source"] == {"kind": "direct_atom"}

    async def test_source_is_route_when_subject_points_at_a_route(self):
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(
            segment_id=None, segment_place=None, segment_action=None,
            route_id="route_xyz", route_hub_name="Nakasendo Way", route_first_day=1, route_last_day=3,
        )
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"][0]["source"] == {
            "kind": "route", "route_id": "route_xyz", "hub_name": "Nakasendo Way",
            "first_day": 1, "last_day": 3,
        }

    async def test_is_buffer_retry_true_when_later_sibling_piece(self):
        """AA-561 3a — a buffer retry (AA-485) writes a SECOND content_piece row for the same
        request; this row is not the request's earliest, so it must be flagged as a retry."""
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(
            sibling_piece_count=2,
            request_first_piece_at=datetime(2026, 8, 29, tzinfo=timezone.utc),  # earlier than created_at
        )
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"][0]["is_buffer_retry"] is True

    async def test_atom_join_accepts_platform_owner_scope(self):
        """AA-561 STEP0 finding — the old join (`ta.owner_scope = cp.tenant_id::text`) stopped
        matching anything once AA-526 made atoms platform-wide (owner_scope='platform'). Must now
        accept either."""
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[])
        req = _make_request(pool)
        await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)

        sql = conn.fetch.call_args[0][0]
        assert "ta.owner_scope = 'platform'" in sql
        assert "ta.owner_scope = cp.tenant_id::text" in sql

    async def test_empty_result_is_not_an_error(self):
        from api.routers.admin_a4 import get_content_log

        pool, _ = _make_pool(fetch=[])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"] == []
        assert result["total"] == 0


# ── AA-568 — merged "06 · Content Trace" page's new filters + computed fields ───────────────────

@pytest.mark.asyncio
class TestGetContentLogAA568:
    """Reuses TestGetContentLog's own `_full_row` fixture (staticmethod, no need to inherit —
    inheriting would re-collect and re-run every one of that class's own tests under this name)."""

    _full_row = staticmethod(TestGetContentLog._full_row)

    async def test_status_filter_scopes_query(self):
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[])
        req = _make_request(pool)
        await get_content_log(req, tenant_id=None, status="held", limit=200, x_admin_secret=_TEST_SECRET)

        sql, *params = conn.fetch.call_args[0]
        assert "cp.status = $" in sql
        assert "held" in params

    async def test_channel_filter_scopes_query(self):
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[])
        req = _make_request(pool)
        await get_content_log(req, tenant_id=None, channel="blog", limit=200, x_admin_secret=_TEST_SECRET)

        sql, *params = conn.fetch.call_args[0]
        assert "COALESCE(cp.channel, agr.channel) = $" in sql
        assert "blog" in params

    async def test_date_range_filter_scopes_query(self):
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[])
        req = _make_request(pool)
        await get_content_log(
            req, tenant_id=None, date_from="2026-09-01", date_to="2026-09-08",
            limit=200, x_admin_secret=_TEST_SECRET,
        )

        sql, *params = conn.fetch.call_args[0]
        assert "cp.created_at >= $" in sql
        assert "cp.created_at < (" in sql
        assert "2026-09-01" in params
        assert "2026-09-08" in params

    async def test_published_yes_filters_to_rows_with_a_publish_log_row(self):
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[])
        req = _make_request(pool)
        await get_content_log(req, tenant_id=None, published="yes", limit=200, x_admin_secret=_TEST_SECRET)

        sql = conn.fetch.call_args[0][0]
        assert "pl.publish_id IS NOT NULL" in sql

    async def test_published_no_filters_to_rows_without_a_publish_log_row(self):
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[])
        req = _make_request(pool)
        await get_content_log(req, tenant_id=None, published="no", limit=200, x_admin_secret=_TEST_SECRET)

        sql = conn.fetch.call_args[0][0]
        assert "pl.publish_id IS NULL" in sql

    async def test_content_text_is_full_not_truncated(self):
        """AA-568 — the merged page's row-click accordion needs the FULL text with no second
        fetch; the old 280-char `content_preview` alone would truncate a real article."""
        from api.routers.admin_a4 import get_content_log

        long_text = "x" * 500
        row = self._full_row(content_text=long_text)
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        item = result["data"][0]
        assert item["content_text"] == long_text
        assert item["content_preview"] == long_text[:280]

    async def test_retry_count_is_repair_log_length(self):
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(repair_log=json.dumps([
            {"attempt": 1, "gate_targeted": "F1_grounding", "violations": ["unsupported claim"]},
            {"attempt": 2, "gate_targeted": "F6_cta_present", "violations": ["missing CTA"]},
        ]))
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"][0]["retry_count"] == 2

    async def test_retry_count_zero_when_no_repair_rounds(self):
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(repair_log=json.dumps([]))
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"][0]["retry_count"] == 0

    async def test_topic_prefers_atom_text_over_goal(self):
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(atom_text="Cross the bamboo bridge", goal="engagement_conversation")
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"][0]["topic"] == "Cross the bamboo bridge"

    async def test_topic_falls_back_to_goal_when_no_atom_text(self):
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(atom_text=None, goal="engagement_conversation")
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"][0]["topic"] == "engagement_conversation"

    async def test_topic_is_untitled_when_neither_atom_text_nor_goal(self):
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(atom_text=None, goal=None)
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        assert result["data"][0]["topic"] == "Untitled"

    async def test_topic_truncates_long_atom_text(self):
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(atom_text="a" * 200)
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        topic = result["data"][0]["topic"]
        assert len(topic) == 91  # 90 chars + the "…" truncation marker
        assert topic.endswith("…")

    async def test_publish_url_and_published_at_pass_through_when_published(self):
        from api.routers.admin_a4 import get_content_log

        row = self._full_row(
            status="approved", held_reason=None, publish_id=str(uuid.uuid4()),
            publish_external_url="https://wp.example.com/post/123",
            publish_published_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        )
        pool, conn = _make_pool(fetch=[row])
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        item = result["data"][0]
        assert item["publish_external_url"] == "https://wp.example.com/post/123"
        assert item["publish_published_at"] == "2026-09-09T00:00:00+00:00"

    async def test_publish_url_is_none_when_never_published(self):
        from api.routers.admin_a4 import get_content_log

        pool, conn = _make_pool(fetch=[self._full_row()])  # base fixture: publish_id=None
        req = _make_request(pool)

        result = await get_content_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
        item = result["data"][0]
        assert item["publish_external_url"] is None
        assert item["publish_published_at"] is None
