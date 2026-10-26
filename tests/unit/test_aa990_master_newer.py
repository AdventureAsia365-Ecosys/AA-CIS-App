"""AA-990 scope 5 — a tenant version older than the Master: the tenant is told, nothing is overwritten.

GET /v1/tours/my-versions and GET /v1/tours/versions/{id} carry, per version, `master_updated_at`
(published_tours.updated_at, moved only by a Master publish / promote, CIS 273) and `master_newer`
(master_updated_at strictly later than the version's created_at). Computed at read time: no migration, no write.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from api.routers import v1_tours
from api.routers._tenant_projection import TENANT_FORBIDDEN_KEY_RE, master_newer, project_version
from tests.unit.test_aa859_t2_version_reads import (
    TENANT_ID, _call_my_versions, _call_version, _conn, _my_row, _request, _sqls, _version_row,
)

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


# ── the flag ──────────────────────────────────────────────────────────────────

def test_master_newer_when_master_changed_after_the_version():
    assert master_newer(T0 + timedelta(seconds=1), T0) is True


def test_not_newer_when_master_is_older():
    assert master_newer(T0 - timedelta(days=3), T0) is False


def test_equal_timestamps_are_not_newer():
    assert master_newer(T0, T0) is False


@pytest.mark.parametrize("master,created", [(None, T0), (T0, None), (None, None)])
def test_missing_side_is_not_newer(master, created):
    assert master_newer(master, created) is False


def test_naive_vs_aware_is_not_newer_rather_than_an_error():
    assert master_newer(datetime(2026, 10, 21), T0) is False


# ── projection ────────────────────────────────────────────────────────────────

def test_project_version_adds_the_flag_when_the_master_time_is_joined():
    row = {"id": uuid.uuid4(), "status": "needs_review", "created_at": T0,
           "master_updated_at": T0 + timedelta(hours=2)}
    out = project_version(row)
    assert out["master_newer"] is True
    assert out["master_updated_at"] == T0 + timedelta(hours=2)
    assert out["created_at"] == T0, "the version itself is untouched"
    assert out["status"] == "ai_generated"


def test_project_version_without_the_master_column_has_no_flag():
    out = project_version({"id": uuid.uuid4(), "status": "approved", "created_at": T0})
    assert "master_newer" not in out and "master_updated_at" not in out


def test_new_keys_pass_the_tenant_key_rule():
    for key in ("master_updated_at", "master_newer"):
        assert not TENANT_FORBIDDEN_KEY_RE.search(key)


# ── routes (tenant pool and flag-off admin pool) ──────────────────────────────

def test_both_queries_select_the_master_time():
    for sql in (v1_tours._MY_VERSIONS_ROWS_SQL, v1_tours._VERSION_SQL):
        assert "pt.updated_at AS master_updated_at" in sql


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.asyncio
async def test_my_versions_rows_carry_the_flag(flag):
    newer = {**_my_row(None), "master_updated_at": T0 + timedelta(days=1), "created_at": T0}
    older = {**_my_row(None), "master_updated_at": T0 - timedelta(days=1), "created_at": T0}
    same = {**_my_row(None), "master_updated_at": T0, "created_at": T0}
    conn = _conn(rows=[newer, older, same], total=3)
    request = _request(flag=flag, tenant_conn=conn, main_conn=conn)
    out = await _call_my_versions(request, TENANT_ID)
    assert [d["master_newer"] for d in out["data"]] == [True, False, False]
    assert [d["master_updated_at"] for d in out["data"]] == [T0 + timedelta(days=1), T0 - timedelta(days=1), T0]


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.asyncio
async def test_version_detail_carries_the_flag_and_reads_only(flag):
    row = _version_row(job_id=None, created_at=T0, master_updated_at=T0 + timedelta(minutes=5))
    conn = _conn(row=row)
    request = _request(flag=flag, tenant_conn=conn, main_conn=conn)
    out = await _call_version(request, TENANT_ID)
    assert out["master_newer"] is True
    assert out["master_updated_at"] == T0 + timedelta(minutes=5)
    assert not any(sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for sql in _sqls(conn))
