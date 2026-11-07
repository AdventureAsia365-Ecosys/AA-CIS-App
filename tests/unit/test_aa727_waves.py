"""AA-727 — wave tour classification + report shape (services/waves/report.py). The SQL itself is
covered against real Postgres in tests/integration/test_aa727_waves.py."""
from services.waves import report as wr


def _t(**kw):
    base = {"tour_id": "t", "src_name": "T", "country": "Laos", "source_status": "active", "deleted": False,
            "src_chars": 2000, "s1_status": None, "s1_error": None, "any_active": False, "atomized": False,
            "jev": False, "master_status": None, "published_in_wave": False, "review_status": None,
            "failure_summary": None, "score": None}
    base.update(kw)
    return base


def test_classify_order():
    assert wr.classify(_t(master_status="active", published_in_wave=True, review_status="pending")) == "master"
    # active in Master but published before the wave = not this wave's result
    assert wr.classify(_t(master_status="active", published_in_wave=False, s1_status="succeeded")) == "unclassified"
    assert wr.classify(_t(review_status="pending", s1_status="succeeded")) == "review_pending"
    assert wr.classify(_t(source_status="trashed")) == "trashed"
    assert wr.classify(_t(deleted=True)) == "trashed"
    assert wr.classify(_t(any_active=True, s1_status="failed")) == "running"
    assert wr.classify(_t(s1_status="failed")) == "failed"
    assert wr.classify(_t(s1_status="stopped_budget")) == "failed"
    assert wr.classify(_t(s1_status="cancelled")) == "interrupted"
    assert wr.classify(_t()) == "not_started"


def test_published_in_wave_reads_updated_at_not_published_at():
    """AA-645 d06: a re-publish keeps the first published_at, so the wave window must test updated_at (mig 273)."""
    import re
    m = re.search(r"coalesce\((.*?)\)\s+AS published_in_wave", wr.TOURS_SQL, re.S)
    assert m and "pt.updated_at >= w.created_at" in m.group(1) and "pt.published_at" not in m.group(1)


def test_build_report_columns_coverage_and_completeness():
    tours = [
        _t(tour_id="a", s1_status="succeeded", master_status="active", published_in_wave=True, atomized=True, jev=True),
        _t(tour_id="b", s1_status="succeeded", review_status="pending", score=6.1,
           failure_summary="Low quality", jev=True),
        _t(tour_id="c", source_status="trashed"),
        _t(tour_id="d"),
    ]
    jobs = [{"kind": "s1_rewrite", "status": "succeeded", "n": 2}]
    rep = wr.build_report({"name": "Laos"}, tours, jobs, [{"provider": "bedrock", "calls": 9, "cost_usd": 0.12}],
                          {"calls": 2, "cost_usd": 0.03})
    assert rep["columns"] == {"s1_ok": 2, "to_master": 1, "to_review": 1, "atomized": 1}
    assert rep["states"]["not_started"] == 1 and rep["terminal"] == 3
    assert rep["jobs_finished"] is True and rep["complete"] is False
    assert [r["tour_id"] for r in rep["not_terminal"]] == ["d"]
    assert rep["review"][0]["review"]["failure_class"] == "low_quality"
    assert rep["jev_coverage"] == {"tours": 2, "expected": 4, "pct": 50.0}
    assert rep["cost"]["total_usd"] == 0.15


def test_report_not_finished_while_a_job_is_active_or_none_started():
    tours = [_t(tour_id="a", master_status="active", published_in_wave=True)]
    running = wr.build_report({}, tours, [{"kind": "a3_atomize", "status": "running", "n": 1}], [], {})
    assert running["jobs_finished"] is False and running["complete"] is False
    no_jobs = wr.build_report({}, tours, [], [], {})
    assert no_jobs["jobs_finished"] is False


# ── router (api/routers/admin_waves.py) ────────────────────────────────────────
import pytest  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from api.routers import admin_waves  # noqa: E402


def _req(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    req = MagicMock()
    req.app.state.pool.acquire = MagicMock(return_value=ctx)
    return req


@pytest.mark.asyncio
async def test_create_wave_needs_tour_ids_or_country():
    with patch.object(admin_waves, "verify_admin_secret"):
        with pytest.raises(HTTPException) as e:
            await admin_waves.create_wave(admin_waves.WaveCreate(name="x"), _req(AsyncMock()), "s", None)
    assert e.value.status_code == 422


@pytest.mark.asyncio
async def test_create_wave_rejects_unknown_tour_ids():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"tour_id": "a"}])
    with patch.object(admin_waves, "verify_admin_secret"):
        with pytest.raises(HTTPException) as e:
            await admin_waves.create_wave(admin_waves.WaveCreate(name="x", tour_ids=["a", "b"]), _req(conn), "s", None)
    assert e.value.status_code == 422 and e.value.detail["tour_ids"] == ["b"]


@pytest.mark.asyncio
async def test_create_wave_from_country_uses_active_master_raw_tours():
    from datetime import datetime, timezone
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"tour_id": "11111111-1111-1111-1111-111111111111"}])
    conn.fetchrow = AsyncMock(return_value={
        "id": "w1", "name": "Laos", "country": "Laos", "expected_tour_ids": ["1"], "status": "open",
        "notes": None, "created_by": "admin", "created_at": datetime(2026, 10, 11, tzinfo=timezone.utc),
        "closed_at": None, "last_checked_at": None, "last_check": None})
    with patch.object(admin_waves, "verify_admin_secret"):
        out = await admin_waves.create_wave(admin_waves.WaveCreate(name="Laos", country="Laos"), _req(conn), "s", None)
    sql, tenant, country = conn.fetch.call_args[0]
    assert "source_status = 'active'" in sql and "deleted_at IS NULL" in sql
    assert tenant == wr.MASTER_TENANT_ID and country == "Laos"
    assert out["expected_count"] == 1 and out["status"] == "open"
