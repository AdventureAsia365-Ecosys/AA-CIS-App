"""AA-825 — retention policy, mode decision, SQL shape and archive keys (no DB).

The DB behaviour (rollup before delete, archive == deleted, FK-protected rows kept, held runs) is in
tests/integration/test_aa825_retention_jobs.py.
"""
import ast
import gzip
import os
import re
from datetime import date, datetime, timedelta, timezone
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


def test_a_retry_counts_earlier_deletions_once():
    # manual: the approved max_rows is the job total, earlier deletions count against it
    assert retention_job.rows_left("manual", 10, 4) == 6
    assert retention_job.rows_left("manual", 10, 12) == 0
    # schedule: this attempt's limit is its own candidate count (already net of earlier deletions)
    assert retention_job.rows_left("schedule", 6, 4) == 6
    # ... but the whole job stays under RUN_MAX_ROWS
    cap = retention_job.RUN_MAX_ROWS
    assert retention_job.rows_left("schedule", cap, cap - 100) == 100
    assert retention_job.rows_left("schedule", 50, cap) == 0


class _FkConn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, *args):
        return self.rows


@pytest.mark.asyncio
async def test_fk_refs_only_accepts_single_column_fks_onto_id():
    ok = [{"ref_table": "shared.job", "ref_col": "parent_job_id", "ncols": 1, "target_col": "id",
           "conname": "job_parent_job_id_fkey"}]
    assert await runner.fk_refs(_FkConn(ok), "shared.job") == [("shared.job", "parent_job_id")]
    for bad in ({"ncols": 2, "target_col": "id"}, {"ncols": 1, "target_col": "idempotency_key"}):
        rows = [{**ok[0], **bad}]
        with pytest.raises(runner.UnsupportedForeignKey):
            await runner.fk_refs(_FkConn(rows), "shared.job")


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


class _S3:
    """put_object records; head_object answers from what was put (or from `head` if given)."""

    def __init__(self, head=None):
        self.calls, self.heads, self.head = [], [], head

    def put_object(self, **kw):
        self.calls.append(kw)

    def head_object(self, **kw):
        self.heads.append(kw)
        if self.head is not None:
            return self.head
        put = next(c for c in self.calls if c["Key"] == kw["Key"])
        return {"ContentLength": len(put["Body"]), "Metadata": put["Metadata"]}


@pytest.mark.asyncio
async def test_archiver_writes_gzipped_json_lines_with_sse(monkeypatch):
    monkeypatch.setenv("BRONZE_BUCKET", "aa-cis-bronze-test")  # AA-902: bucket from the environment
    s3 = _S3()
    a = archive.S3Archiver(s3, "job1", run="run1")
    key = await a.put("job", "failed", 1, datetime(2026, 1, 9, tzinfo=timezone.utc), ['{"a": 1}', '{"a": 2}'])
    [kw] = s3.calls
    assert kw["Key"] == key == "archive/job/2026/01/job1-run1-failed-b00001.jsonl.gz"
    assert kw["Bucket"] == "aa-cis-bronze-test" and kw["ServerSideEncryption"] == "AES256"
    assert gzip.decompress(kw["Body"]).decode().splitlines() == ['{"a": 1}', '{"a": 2}']
    assert kw["Metadata"]["rows"] == "2"
    # S3 checks the body against Content-MD5; the object is HEAD-verified before the caller deletes
    assert kw["ContentMD5"] == archive.content_md5(kw["Body"])
    assert s3.heads == [{"Bucket": kw["Bucket"], "Key": key}]


@pytest.mark.asyncio
@pytest.mark.parametrize("head", [
    {"ContentLength": 3, "Metadata": {"rows": "2"}},          # truncated object
    {"ContentLength": None, "Metadata": {"rows": "2"}},       # nothing there
    {"Metadata": {"rows": "1"}},                               # wrong row count
])
async def test_archiver_refuses_an_object_it_cannot_verify(head):
    a = archive.S3Archiver(_S3(head=head), "job1", run="run1")
    with pytest.raises(archive.ArchiveNotVerified):
        await a.put("job", "failed", 1, datetime(2026, 1, 9, tzinfo=timezone.utc), ['{"a": 1}', '{"a": 2}'])


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


# ── manual override (S239): prove rollup + archive on Dev without waiting for data to age ───────

TODAY = date(2026, 10, 16)
OK_DAY = (TODAY - timedelta(days=retention_job.OVERRIDE_MIN_AGE_DAYS)).isoformat()   # 2026-09-25
OVR = {"trigger": "manual_override", "older_than": OK_DAY}
LLM = "retention_llm_call_log"


def test_override_cutoff_is_utc_midnight_of_older_than_and_none_for_other_triggers():
    assert retention_job.override_cutoff(LLM, OVR, TODAY, "dev") == datetime(2026, 9, 25, tzinfo=timezone.utc)
    assert retention_job.override_cutoff("retention_tenant_api_usage", OVR, TODAY, "test") is not None
    for payload in ({}, {"trigger": "manual"}, {"trigger": "schedule"}, {"trigger": "manual", "dry_run": False}):
        assert retention_job.override_cutoff(LLM, payload, TODAY, "dev") is None


@pytest.mark.parametrize("trigger", ["schedule", "manual", None])
def test_older_than_is_refused_outside_a_manual_override(trigger):
    payload = {"older_than": OK_DAY, **({"trigger": trigger} if trigger else {})}
    with pytest.raises(NonRetryable):
        retention_job.override_cutoff(LLM, payload, TODAY, "dev")


def test_schedule_all_never_sends_an_override():
    # the only payload the daily scheduler enqueues
    src = open(os.path.join(ROOT, "services/jobs/retention_job.py")).read()
    assert '{"trigger": "schedule"}' in src.split("async def schedule_all")[1].split("def ")[0]


@pytest.mark.parametrize("kind", ["retention_job", "retention_decision_log", "retention_cost_explorer_snapshot"])
def test_override_is_refused_for_kinds_without_a_rollup(kind):
    with pytest.raises(NonRetryable):
        retention_job.override_cutoff(kind, OVR, TODAY, "dev")


def test_override_kinds_are_exactly_the_rolled_up_ones():
    assert set(retention_job.OVERRIDE_KINDS) == {k for k, s in SPECS.items() if s.rollup}
    assert all(len(SPECS[k].parts) == 1 for k in retention_job.OVERRIDE_KINDS)


@pytest.mark.parametrize("older_than", [
    None, "", "2026-9-15", "2026-09-15T00:00:00", "15/09/2026", "2026-02-30", 20260915,
])
def test_override_needs_a_plain_utc_date(older_than):
    with pytest.raises(NonRetryable):
        retention_job.override_cutoff(LLM, {"trigger": "manual_override", "older_than": older_than}, TODAY, "dev")


def test_override_cutoff_must_be_at_least_the_min_age():
    too_recent = (TODAY - timedelta(days=retention_job.OVERRIDE_MIN_AGE_DAYS - 1)).isoformat()
    with pytest.raises(NonRetryable, match="too recent"):
        retention_job.override_cutoff(LLM, {**OVR, "older_than": too_recent}, TODAY, "dev")
    with pytest.raises(NonRetryable):
        retention_job.override_cutoff(LLM, {**OVR, "older_than": TODAY.isoformat()}, TODAY, "dev")
    assert retention_job.OVERRIDE_MIN_AGE_DAYS >= 8   # outside every default 7-day reader window


def test_override_tenant_usage_cutoff_must_be_in_a_closed_month():
    tu = "retention_tenant_api_usage"
    late_month = date(2026, 10, 31)
    in_month = (late_month - timedelta(days=retention_job.OVERRIDE_MIN_AGE_DAYS)).isoformat()   # 2026-10-10
    with pytest.raises(NonRetryable, match="current month"):
        retention_job.override_cutoff(tu, {**OVR, "older_than": in_month}, late_month, "dev")
    # the first day of the current month is fine: rows strictly before it are in closed months
    assert retention_job.override_cutoff(tu, {**OVR, "older_than": "2026-10-01"}, late_month, "dev")
    assert retention_job.override_cutoff(tu, {**OVR, "older_than": "2026-09-17"}, TODAY, "dev")
    # llm_call_log rolls up by day, so the same in-month cutoff is accepted there
    assert retention_job.override_cutoff(LLM, {**OVR, "older_than": in_month}, late_month, "dev")


@pytest.mark.parametrize("env", ["prod", "production", "staging", "PROD"])
def test_override_is_refused_outside_dev_and_test(env):
    with pytest.raises(NonRetryable, match="environment"):
        retention_job.override_cutoff(LLM, OVR, TODAY, env)


def test_override_reads_environment_from_the_process(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(NonRetryable):
        retention_job.override_cutoff(LLM, OVR, TODAY)
    monkeypatch.delenv("ENVIRONMENT")                     # unset = dev (sentry.py convention)
    assert retention_job.override_cutoff(LLM, OVR, TODAY) is not None


def test_override_is_a_dry_run_unless_explicitly_live():
    assert retention_job.decide_mode(OVR, W, 10, None)[0] == "dry_run"
    assert retention_job.decide_mode({**OVR, "dry_run": True, "max_rows": 5}, W, 10, None)[0] == "dry_run"


@pytest.mark.parametrize("max_rows", [None, 0, -1, True, "200", retention_job.OVERRIDE_MAX_ROWS + 1, 10 ** 6])
def test_live_override_needs_a_small_max_rows_and_is_refused_not_capped(max_rows):
    payload = {**OVR, "dry_run": False}
    if max_rows is not None:
        payload["max_rows"] = max_rows
    with pytest.raises(NonRetryable):
        retention_job.decide_mode(payload, W, 10, None)


def test_live_override_deletes_at_most_max_rows_and_ignores_the_schedule_rules():
    payload = {**OVR, "dry_run": False, "max_rows": 200}
    # no previous live run, 10^6 candidates: still live (an override is never held), limited to 200
    assert retention_job.decide_mode(payload, W, 10 ** 6, None)[:2] == ("live", 200)
    # a retry counts earlier deletions against the approved total, like a manual run
    assert retention_job.rows_left("manual_override", 200, 150) == 50


def test_an_override_live_run_never_unlocks_scheduled_live_runs():
    sql = " ".join(retention_job._LAST_LIVE_SQL.split())
    assert "result ->> 'mode' = 'live'" in sql and "<> 'manual_override'" in sql
    status_sql = " ".join(retention_job._STATUS_JOBS_SQL.split())
    assert "result ->> 'trigger' = 'manual_override' THEN 'override'" in status_sql


@pytest.mark.asyncio
async def test_retention_run_endpoint_refuses_bad_overrides(monkeypatch):
    from fastapi import HTTPException
    from api.routers import admin_job_runner as r
    monkeypatch.setattr(r, "verify_admin_secret", lambda s: None)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=None)))
    today = datetime.now(timezone.utc).date()
    ok = (today - timedelta(days=retention_job.OVERRIDE_MIN_AGE_DAYS)).isoformat()
    recent = (today - timedelta(days=5)).isoformat()
    bad = [
        dict(kind="retention_job", older_than=ok),                                    # no rollup
        dict(kind=LLM, older_than=recent),                                            # too recent
        dict(kind=LLM, older_than="yesterday"),                                       # not a date
        dict(kind=LLM, older_than=ok, dry_run=False),                                 # live, no max_rows
        dict(kind=LLM, older_than=ok, dry_run=False, max_rows=retention_job.OVERRIDE_MAX_ROWS + 1),
    ]
    for body in bad:
        with pytest.raises(HTTPException) as e:
            await r.retention_run(r.RetentionRunRequest(**body), req, "s", None)
        assert e.value.status_code == 422, body


@pytest.mark.asyncio
async def test_retention_run_endpoint_enqueues_an_override_as_manual_override(monkeypatch):
    from api.routers import admin_job_runner as r
    from shared.jobs import registry
    monkeypatch.setattr(r, "verify_admin_secret", lambda s: None)
    seen = {}

    async def fake_enqueue(pool, kind, payload, **kw):
        seen.update(kind=kind, payload=payload, **kw)
        return "job-1", True
    monkeypatch.setattr(registry, "enqueue", fake_enqueue)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=None)))
    ok = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    out = await r.retention_run(r.RetentionRunRequest(kind=LLM, older_than=ok, dry_run=False, max_rows=200),
                                req, "s", "nghiep")
    assert out["trigger"] == "manual_override" and out["job_id"] == "job-1"
    assert seen["payload"] == {"trigger": "manual_override", "older_than": ok, "dry_run": False, "max_rows": 200}
    assert seen["created_by"] == "admin:nghiep"
