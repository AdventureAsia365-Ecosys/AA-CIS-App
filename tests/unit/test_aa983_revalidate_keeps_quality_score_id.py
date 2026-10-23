"""AA-983 — revalidation must leave a published tour pointing at the version's NEW score row.

`_revalidate_tour` used to DELETE the version's quality_scores rows and then INSERT a new one, as autocommit
statements. Since CIS 268 the FK published_tours.quality_score_id -> quality_scores(id) is ON DELETE SET NULL, so
the DELETE nulled the published tour's pointer and nothing re-pointed it. The fix writes, in one transaction:
INSERT new row (RETURNING id) -> UPDATE published_tours to it -> DELETE the other rows -> revalidate_passed.

These tests run the real `_revalidate_tour` with a recording fake connection and check that order. The real
FK behaviour is covered against Postgres in tests/integration/test_aa983_revalidate_quality_score_id.py.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import admin_pipeline

CONTENT_ID = "22222222-2222-2222-2222-222222222222"
TOUR_ID = "33333333-3333-3333-3333-333333333333"
NEW_SCORE_ID = "44444444-4444-4444-4444-444444444444"


@pytest.fixture(autouse=True)
def _db_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")


def _gc_row():
    return {
        "id": CONTENT_ID, "tour_id": TOUR_ID, "tenant_id": "00000000-0000-0000-0000-000000000001",
        "version_num": 2, "aa_name": "T", "aa_subtitle": None, "aa_summary": None, "aa_description": None,
        "aa_highlights": json.dumps([]), "aa_itineraries": None, "mobile_card_text": None,
        "seo_title": None, "seo_meta": None, "seo_keywords_used": json.dumps([]), "og_tags": json.dumps({}),
        "brand_rules_version": 1, "metadata": json.dumps({}),
        "src_name": "T", "country": "Vietnam", "duration": "2 days", "src_subtitle": None, "src_summary": None,
        "src_description": None, "src_highlights": json.dumps([]), "src_itineraries": None, "price_raw": None,
        "inclusions": None, "exclusions": None,
    }


class _Recorder:
    """Fake asyncpg connection that records every call in order, transaction enter/exit included."""

    def __init__(self, fail_on: str | None = None, repoint_status: str = "UPDATE 1"):
        self.calls: list[tuple[str, str, tuple]] = []
        self.fail_on = fail_on
        self.repoint_status = repoint_status
        self.fetchrow = AsyncMock(side_effect=[_gc_row(), None])
        self.close = AsyncMock()

    def transaction(self):
        rec = self

        class _Tx:
            async def __aenter__(self):
                rec.calls.append(("tx", "begin", ()))

            async def __aexit__(self, exc_type, exc, tb):
                rec.calls.append(("tx", "rollback" if exc_type else "commit", ()))
                return False

        return _Tx()

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", " ".join(sql.split()), args))
        return NEW_SCORE_ID

    async def execute(self, sql, *args):
        flat = " ".join(sql.split())
        self.calls.append(("execute", flat, args))
        if self.fail_on and self.fail_on in flat:
            raise RuntimeError("boom")
        if flat.startswith("UPDATE gold_aa_internal.published_tours"):
            return self.repoint_status
        return "OK"


def _graph_result():
    return {"revalidate_passed": True, "quality_score": 8.5, "sub_scores": {}, "failure_codes": [],
            "passed_count": 4, "failed_count": 0, "brand_audit_status": "pass", "brand_audit_codes": [],
            "brand_audit_issues": [], "brand_audit_fields": [], "lessons_extracted": []}


def _run(conn):
    graph = MagicMock()
    graph.invoke.return_value = _graph_result()
    with patch("api.routers.admin_pipeline.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(return_value=None)), \
         patch("services.content_generation.graph.build_revalidation_graph", MagicMock(return_value=graph)):
        return asyncio.run(admin_pipeline._revalidate_tour(CONTENT_ID))


def _index(calls, prefix):
    hits = [i for i, (_, sql, _) in enumerate(calls) if sql.startswith(prefix)]
    assert len(hits) == 1, f"expected one '{prefix}', got {len(hits)}"
    return hits[0]


def test_insert_repoint_delete_in_one_transaction_in_that_order():
    conn = _Recorder()
    out = _run(conn)
    calls = conn.calls
    begin = [i for i, c in enumerate(calls) if c[:2] == ("tx", "begin")]
    commit = [i for i, c in enumerate(calls) if c[:2] == ("tx", "commit")]
    assert len(begin) == 1 and len(commit) == 1
    ins = _index(calls, "INSERT INTO silver_aa_internal.quality_scores")
    rep = _index(calls, "UPDATE gold_aa_internal.published_tours")
    dele = _index(calls, "DELETE FROM silver_aa_internal.quality_scores")
    flag = _index(calls, "UPDATE silver_aa_internal.generated_content SET revalidate_passed")
    assert begin[0] < ins < rep < dele < flag < commit[0]
    assert "RETURNING id" in calls[ins][1]
    assert out["quality_score_id"] == NEW_SCORE_ID
    assert out["published_repointed"] == 1


def test_repoint_targets_the_new_row_and_the_version():
    conn = _Recorder()
    _run(conn)
    _, sql, args = conn.calls[_index(conn.calls, "UPDATE gold_aa_internal.published_tours")]
    assert "SET quality_score_id = $1::uuid" in sql and "WHERE generated_content_id = $2::uuid" in sql
    assert args == (NEW_SCORE_ID, CONTENT_ID)


def test_delete_keeps_the_new_row():
    """Deleting every row of the version (the old statement) would remove the new one too, and with the FK's
    ON DELETE SET NULL null the pointer just set."""
    conn = _Recorder()
    _run(conn)
    _, sql, args = conn.calls[_index(conn.calls, "DELETE FROM silver_aa_internal.quality_scores")]
    assert "id <> $2::uuid" in sql
    assert args == (CONTENT_ID, NEW_SCORE_ID)


def test_failure_after_insert_rolls_back_the_whole_write():
    conn = _Recorder(fail_on="DELETE FROM silver_aa_internal.quality_scores")
    with pytest.raises(RuntimeError):
        _run(conn)
    assert ("tx", "rollback", ()) in conn.calls
    assert ("tx", "commit", ()) not in conn.calls
    conn.close.assert_awaited()


def test_unpublished_version_reports_zero_repointed():
    conn = _Recorder(repoint_status="UPDATE 0")
    out = _run(conn)
    assert out["published_repointed"] == 0


@pytest.mark.parametrize("status,expected", [("UPDATE 3", 3), ("UPDATE 0", 0), (None, 0), ("weird", 0)])
def test_rowcount_parses_command_status(status, expected):
    assert admin_pipeline._rowcount(status) == expected


def test_export_reads_the_versions_latest_score_row():
    """The export sets quality_score_id from the version's score row; with a plain LEFT JOIN a version with two
    rows got an arbitrary one. It must use the same rule as the re-point: latest evaluated_at, then id."""
    from services.export import handler

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("services.export.handler.get_database_url", return_value="postgresql://test/test"):
        with pytest.raises(ValueError):
            asyncio.run(handler.process_export(CONTENT_ID))
    sql = " ".join(conn.fetchrow.call_args[0][0].split())
    assert "LEFT JOIN LATERAL" in sql
    assert "ORDER BY q.evaluated_at DESC, q.id DESC LIMIT 1" in sql


def test_backfill_summary_survives_an_ecs_exec_transcript():
    from scripts import aa983_backfill_quality_score_id as backfill

    out = {"mode": "dry_run", "counts": {"to_fix": 2, "stale_pointer": 0}}
    transcript = ("\r\nStarting session with SessionId: ecs-execute-command-x\r\n"
                  + backfill.render(out).replace("\n", "\r\n")
                  + "\r\n\r\nExiting session with sessionId: ecs-execute-command-x.\r\n")
    assert backfill.extract(transcript) == out
    with pytest.raises(ValueError):
        backfill.extract("Starting session ...\nAccessDeniedException\n")
