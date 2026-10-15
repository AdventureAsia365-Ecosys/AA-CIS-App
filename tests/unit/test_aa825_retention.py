"""AA-825 — retention policy, mode decision, SQL shape and archive keys (no DB).

The DB behaviour (rollup before delete, archive == deleted, FK-protected rows kept, held runs) is in
tests/integration/test_aa825_retention_jobs.py.
"""
import ast
import gzip
import os
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from services.jobs import retention_job
from services.retention import archive, runner
from services.retention.policy import DECISION_LOG_WINDOW_DAYS, SPECS
from shared.jobs.registry import NonRetryable, get_kind
from shared.llm_client.decide import CACHE_MAX_AGE_DAYS

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ── policy = the approved windows (E3, DM-7 retention table) ────────────────────────────────────

def _windows(kind):
    return {p.name: (p.window_days, p.archive) for p in SPECS[kind].parts}


def test_windows_and_archive_flags_match_the_approved_table():
    assert _windows("retention_llm_call_log") == {"raw": (180, True)}
    assert _windows("retention_tenant_api_usage") == {"raw": (90, True)}
    assert _windows("retention_job") == {"succeeded": (30, False), "failed": (90, True)}
    assert _windows("retention_decision_log") == {"unlabelled": (180, True)}
    assert _windows("retention_cost_explorer_snapshot") == {"superseded": (1, False)}
    assert SPECS["retention_llm_call_log"].rollup == "llm_call_daily"
    assert SPECS["retention_tenant_api_usage"].rollup == "tenant_api_usage_monthly"


def test_decision_log_window_never_drops_below_the_jev_cache_age():
    # decide._CACHE_SQL reuses a verdict for CACHE_MAX_AGE_DAYS: deleting younger rows would
    # silently turn cache hits into paid Jev calls.
    assert CACHE_MAX_AGE_DAYS == 180
    assert DECISION_LOG_WINDOW_DAYS >= CACHE_MAX_AGE_DAYS
    assert "outcome IS NULL" in SPECS["retention_decision_log"].parts[0].where


def test_job_retention_never_touches_queued_or_running_jobs():
    wheres = " ".join(p.where for p in SPECS["retention_job"].parts)
    assert "queued" not in wheres and "running" not in wheres


def test_monthly_rollup_window_leaves_the_cutoff_month_closed():
    # rollups.TENANT_USAGE_MONTHLY_INSERT_SQL rolls whole months up to the cutoff's month; with a
    # window longer than any month that month is always over.
    assert SPECS["retention_tenant_api_usage"].parts[0].window_days > 31


def test_every_kind_is_registered_background_with_concurrency_one():
    for kind in SPECS:
        k = get_kind(kind)
        assert k is not None and k.concurrency == 1 and k.background is True


# ── mode decision (DM-7.2) ──────────────────────────────────────────────────────────────────────

W = {"raw": 180}


def test_manual_runs_are_dry_runs_unless_explicitly_live():
    assert retention_job.decide_mode({}, W, 10, None)[0] == "dry_run"
    assert retention_job.decide_mode({"dry_run": True, "max_rows": 5}, W, 10, None)[0] == "dry_run"


def test_manual_live_needs_an_approved_max_rows_and_is_capped():
    with pytest.raises(NonRetryable):
        retention_job.decide_mode({"dry_run": False}, W, 10, None)
    mode, limit, _ = retention_job.decide_mode({"dry_run": False, "max_rows": 10 ** 9}, W, 10, None)
    assert mode == "live" and limit == retention_job.RUN_MAX_ROWS
    assert retention_job.decide_mode({"dry_run": False, "max_rows": 7}, W, 10, None)[:2] == ("live", 7)


def test_schedule_is_a_dry_run_until_a_live_run_with_the_same_windows():
    sched = {"trigger": "schedule"}
    assert retention_job.decide_mode(sched, W, 10, None)[0] == "dry_run"
    prev_other = {"windows": {"raw": 90}, "deleted_total": 100}
    assert retention_job.decide_mode(sched, W, 10, prev_other)[0] == "dry_run"
    # a schedule payload cannot force a live run by itself
    assert retention_job.decide_mode({**sched, "dry_run": False, "max_rows": 1}, W, 10, None)[0] == "dry_run"


def test_schedule_holds_above_twice_the_last_live_run():
    sched = {"trigger": "schedule"}
    prev = {"windows": W, "deleted_total": 10_000}
    assert retention_job.decide_mode(sched, W, 20_000, prev)[:2] == ("live", 20_000)
    assert retention_job.decide_mode(sched, W, 20_001, prev)[0] == "held"
    # a quiet previous run (0 rows) still allows one batch
    quiet = {"windows": W, "deleted_total": 0}
    assert retention_job.decide_mode(sched, W, retention_job.GROWTH_FLOOR_ROWS, quiet)[0] == "live"
    assert retention_job.decide_mode(sched, W, retention_job.GROWTH_FLOOR_ROWS + 1, quiet)[0] == "held"


def test_schedule_live_is_capped_per_run():
    prev = {"windows": W, "deleted_total": retention_job.RUN_MAX_ROWS}
    mode, limit, _ = retention_job.decide_mode({"trigger": "schedule"}, W, retention_job.RUN_MAX_ROWS + 5, prev)
    assert (mode, limit) == ("live", retention_job.RUN_MAX_ROWS)


# ── SQL shape (C5.4c) ───────────────────────────────────────────────────────────────────────────

def test_batches_use_the_ts_id_keyset_lock_rows_and_respect_fk_refs():
    spec = SPECS["retention_job"]
    part = spec.parts[1]
    refs = [("acp_shared.content_piece", "job_id"), ("shared.job", "parent_job_id")]
    first = runner.batch_sql(spec, part, refs, after=False)
    nxt = runner.batch_sql(spec, part, refs, after=True)
    assert "ORDER BY t.finished_at, t.id LIMIT $2 FOR UPDATE OF t" in first
    assert "(t.finished_at, t.id) > ($3, $4::uuid)" in nxt
    assert 'NOT EXISTS (SELECT 1 FROM acp_shared.content_piece r0 WHERE r0."job_id" = t.id)' in first
    assert 'r1."parent_job_id" = t.id' in first
    assert "to_jsonb(t)::text AS doc" in first                      # failed jobs are archived
    assert "to_jsonb" not in runner.batch_sql(spec, spec.parts[0], refs, after=False)
    assert runner.delete_sql(SPECS["retention_decision_log"]) == \
        "DELETE FROM shared.decision_log WHERE id = ANY($1::bigint[])"


def test_batch_size_is_capped_at_5000():
    assert runner.BATCH_SIZE_MAX == 5000


def _sql_strings(path):
    """String constants of a module, docstrings excluded (they may say "never VACUUM FULL")."""
    with open(os.path.join(ROOT, path)) as f:
        tree = ast.parse(f.read())
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_retention_code_never_vacuums():
    for path in ("services/retention/runner.py", "services/retention/rollups.py",
                 "services/retention/archive.py", "services/jobs/retention_job.py"):
        assert not [s for s in _sql_strings(path) if re.search(r"\bVACUUM\b", s, re.I)], path


# ── archive objects ─────────────────────────────────────────────────────────────────────────────

def _r(ts):
    return {"ts": ts, "id": "x"}


def test_one_archive_object_holds_one_utc_month():
    rows = [_r(datetime(2026, 3, 31, 23, 0, tzinfo=timezone.utc)),
            _r(datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc)),
            _r(datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc))]
    assert runner.same_month_prefix(rows) == rows[:2]
    assert runner.same_month_prefix([]) == []


def test_object_key_is_partitioned_by_table_year_month():
    key = archive.object_key("llm_call_log", datetime(2026, 3, 5, tzinfo=timezone.utc), "J", "R", "raw", 7)
    assert key == "archive/llm_call_log/2026/03/J-R-raw-b00007.jsonl.gz"


@pytest.mark.asyncio
async def test_archiver_writes_gzipped_json_lines_with_sse():
    calls = []
    s3 = SimpleNamespace(put_object=lambda **kw: calls.append(kw))
    a = archive.S3Archiver(s3, "job1", run="run1")
    key = await a.put("job", "failed", 1, datetime(2026, 1, 9, tzinfo=timezone.utc), ['{"a": 1}', '{"a": 2}'])
    [kw] = calls
    assert kw["Key"] == key == "archive/job/2026/01/job1-run1-failed-b00001.jsonl.gz"
    assert kw["Bucket"] == "aa-cis-bronze-005097885195" and kw["ServerSideEncryption"] == "AES256"
    assert gzip.decompress(kw["Body"]).decode().splitlines() == ['{"a": 1}', '{"a": 2}']
    assert kw["Metadata"]["rows"] == "2"


def test_two_attempts_of_one_job_never_share_a_key():
    a1, a2 = archive.S3Archiver(None, "J"), archive.S3Archiver(None, "J", run="other")
    assert a1.run != a2.run


# ── admin endpoint validation ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retention_run_endpoint_requires_max_rows_for_a_live_run(monkeypatch):
    from fastapi import HTTPException
    from api.routers import admin_job_runner as r
    monkeypatch.setattr(r, "verify_admin_secret", lambda s: None)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=None)))
    with pytest.raises(HTTPException) as e:
        await r.retention_run(r.RetentionRunRequest(kind="retention_job", dry_run=False), req, "s", None)
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await r.retention_run(r.RetentionRunRequest(kind="retention_nope"), req, "s", None)
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await r.retention_run(r.RetentionRunRequest(kind="retention_job", dry_run=False,
                                                    max_rows=retention_job.RUN_MAX_ROWS + 1), req, "s", None)
    assert e.value.status_code == 422
