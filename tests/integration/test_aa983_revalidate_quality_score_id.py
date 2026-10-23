"""AA-983 — against real Postgres with CIS 268's FK (ON DELETE SET NULL) in place:

- the real `_revalidate_tour` (graph mocked) leaves the published tour pointing at the version's NEW score row,
  removes the old row, and a failure inside the write rolls everything back (old row + pointer kept);
- scripts/aa983_backfill_quality_score_id.py: DRY RUN counts and writes nothing; --apply re-points exactly the
  NULL rows whose version has a score row, refuses a wrong --expect, and leaves rows without a score NULL.

Every test removes its own rows.
"""
import asyncio
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_pipeline  # noqa: E402
from scripts import aa983_backfill_quality_score_id as backfill  # noqa: E402

TENANT = "00000000-0000-0000-0000-000000000001"
BATCH = "00000000-0000-0000-0000-000000000002"
# app.tenant_id as a startup setting: a local non-superuser runner is subject to FORCE RLS (CI superuser is not).
DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}?app.tenant_id={TENANT}"
_MADE: list[str] = []


@pytest.fixture
def cur(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN)
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT,))
    _MADE.clear()
    try:
        yield c
    finally:
        for t in list(_MADE):
            c.execute("DELETE FROM gold_aa_internal.published_tours WHERE tour_id = %s", (t,))
            c.execute("DELETE FROM silver_aa_internal.quality_scores WHERE tour_id = %s", (t,))
            c.execute("DELETE FROM silver_aa_internal.generated_content WHERE tour_id = %s", (t,))
            c.execute("DELETE FROM silver_aa_internal.raw_tours WHERE tour_id = %s", (t,))
        conn.close()


def _tour(c):
    tour_id, content_id = str(uuid.uuid4()), str(uuid.uuid4())
    _MADE.append(tour_id)
    c.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country) "
              "VALUES (%s, %s, %s, 'aa983 test', 'Vietnam')", (tour_id, TENANT, BATCH))
    c.execute("INSERT INTO silver_aa_internal.generated_content (id, tour_id, tenant_id, version_num, aa_name) "
              "VALUES (%s, %s, %s, 1, 'aa983')", (content_id, tour_id, TENANT))
    return tour_id, content_id


def _score(c, tour_id, content_id, evaluated_at="2026-10-01T00:00:00Z", overall=8.0):
    c.execute("INSERT INTO silver_aa_internal.quality_scores "
              "(generated_content_id, tour_id, tenant_id, score_overall, evaluated_at) "
              "VALUES (%s, %s, %s, %s, %s) RETURNING id::text", (content_id, tour_id, TENANT, overall, evaluated_at))
    return c.fetchone()[0]


def _publish(c, tour_id, content_id, score_id):
    c.execute("INSERT INTO gold_aa_internal.published_tours "
              "(tour_id, generated_content_id, tenant_id, aa_name, quality_score, quality_score_id) "
              "VALUES (%s, %s, %s, 'aa983', 8.0, %s) RETURNING id::text", (tour_id, content_id, TENANT, score_id))
    return c.fetchone()[0]


def _pointer(c, pt_id):
    c.execute("SELECT quality_score_id::text FROM gold_aa_internal.published_tours WHERE id = %s", (pt_id,))
    return c.fetchone()[0]


def _score_ids(c, content_id):
    c.execute("SELECT id::text FROM silver_aa_internal.quality_scores WHERE generated_content_id = %s", (content_id,))
    return {r[0] for r in c.fetchall()}


def _graph_result():
    return {"revalidate_passed": True, "quality_score": 9.1, "sub_scores": {"brand": 9.0}, "failure_codes": [],
            "passed_count": 5, "failed_count": 0, "brand_audit_status": "pass", "brand_audit_codes": [],
            "brand_audit_issues": [], "brand_audit_fields": [], "lessons_extracted": []}


def _revalidate(content_id, connect=None):
    graph = MagicMock()
    graph.invoke.return_value = _graph_result()
    patches = [patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(return_value=None)),
               patch("services.content_generation.graph.build_revalidation_graph", MagicMock(return_value=graph))]
    if connect is not None:
        patches.append(patch("api.routers.admin_pipeline.asyncpg.connect", connect))
    for p in patches:
        p.start()
    try:
        return asyncio.run(admin_pipeline._revalidate_tour(content_id))
    finally:
        for p in patches:
            p.stop()


# ── revalidation ────────────────────────────────────────────────────────────────────────────────────────

def test_revalidate_points_published_tour_at_the_new_score_row(cur):
    tour_id, content_id = _tour(cur)
    old = _score(cur, tour_id, content_id)
    pt = _publish(cur, tour_id, content_id, old)

    out = _revalidate(content_id)

    new = out["quality_score_id"]
    assert new and new != old
    assert _pointer(cur, pt) == new                       # not NULL, not the deleted row
    assert _score_ids(cur, content_id) == {new}           # the old row is gone, exactly one left
    assert out["published_repointed"] == 1
    cur.execute("SELECT revalidate_passed FROM silver_aa_internal.generated_content WHERE id = %s", (content_id,))
    assert cur.fetchone()[0] is True


def test_revalidate_twice_keeps_pointing_at_the_latest(cur):
    tour_id, content_id = _tour(cur)
    pt = _publish(cur, tour_id, content_id, _score(cur, tour_id, content_id))
    _revalidate(content_id)
    second = _revalidate(content_id)["quality_score_id"]
    assert _pointer(cur, pt) == second
    assert _score_ids(cur, content_id) == {second}


def test_revalidate_unpublished_version_has_nothing_to_repoint(cur):
    tour_id, content_id = _tour(cur)
    _score(cur, tour_id, content_id)
    out = _revalidate(content_id)
    assert out["published_repointed"] == 0
    assert _score_ids(cur, content_id) == {out["quality_score_id"]}


def test_failure_inside_the_write_rolls_back(cur):
    """The last statement of the write fails: the new row, the re-point and the DELETE all roll back."""
    tour_id, content_id = _tour(cur)
    old = _score(cur, tour_id, content_id)
    pt = _publish(cur, tour_id, content_id, old)

    real_connect = asyncpg.connect

    class _FailingConn:
        """Real connection; its execute() fails on the revalidate_passed UPDATE (the write's last statement)."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def execute(self, sql, *args, **kwargs):
            if "SET revalidate_passed" in sql:
                raise RuntimeError("boom")
            return await self._conn.execute(sql, *args, **kwargs)

    async def failing_connect(*a, **kw):
        return _FailingConn(await real_connect(*a, **kw))

    with pytest.raises(RuntimeError):
        _revalidate(content_id, connect=failing_connect)
    assert _pointer(cur, pt) == old
    assert _score_ids(cur, content_id) == {old}


# ── backfill script ─────────────────────────────────────────────────────────────────────────────────────

def _script(*argv):
    return asyncio.run(backfill.main(["--dsn", DSN, "--no-ssl", *argv]))


def _mine(rows):
    return [r for r in rows if r["tour_id"] in _MADE]


def test_dry_run_lists_null_pointers_with_a_score_and_writes_nothing(cur):
    t1, c1 = _tour(cur)
    _score(cur, t1, c1, "2026-10-01T00:00:00Z", 7.0)
    latest = _score(cur, t1, c1, "2026-10-02T00:00:00Z", 9.0)
    pt1 = _publish(cur, t1, c1, None)                      # NULL + score rows -> to fix (latest row)
    t2, c2 = _tour(cur)
    pt2 = _publish(cur, t2, c2, None)                      # NULL, no score row -> stays NULL

    out = _script()

    assert out["mode"] == "dry_run"
    mine = _mine(out["to_fix"])
    assert [(r["published_id"], r["target_score_id"]) for r in mine] == [(pt1, latest)]
    assert out["counts"]["null_without_score"] >= 1
    assert _pointer(cur, pt1) is None and _pointer(cur, pt2) is None


def test_apply_repoints_exactly_the_dry_run_rows(cur, tmp_path):
    t1, c1 = _tour(cur)
    s1 = _score(cur, t1, c1)
    pt1 = _publish(cur, t1, c1, None)
    t2, c2 = _tour(cur)
    pt2 = _publish(cur, t2, c2, None)

    n = _script()["counts"]["to_fix"]
    restore = tmp_path / "restore.json"
    out = _script("--apply", "--expect", str(n), "--restore-file", str(restore))

    assert out["changed"] == n
    assert out["verify_after"]["to_fix"] == 0
    assert _pointer(cur, pt1) == s1
    assert _pointer(cur, pt2) is None
    assert pt1 in json.loads(restore.read_text())["published_ids"]


def test_apply_refuses_a_wrong_expect_and_writes_nothing(cur):
    t1, c1 = _tour(cur)
    _score(cur, t1, c1)
    pt1 = _publish(cur, t1, c1, None)
    n = _script()["counts"]["to_fix"]
    with pytest.raises(SystemExit):
        _script("--apply", "--expect", str(n + 1))
    assert _pointer(cur, pt1) is None
