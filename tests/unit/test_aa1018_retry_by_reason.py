"""AA-1018 (reliability part 1) — job backoff jitter, the bulk re-queue of `llm_access_denied` jobs
(shared/jobs/queue.py `retry_by_reason`), its admin route and the jobs-list `reason` filter. Fake pool, no database;
the guarded UPDATE runs against Postgres in the job-queue integration tests' style only through its SQL text here."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.jobs import queue, registry

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
ERROR_002 = ("llm_access_denied: Model 'gpt-6-luna' refused for stage 'adhoc_x': An error occurred "
             "(AccessDeniedException) when calling the Converse operation: Error 002: Access to Bedrock models is "
             "not allowed for this account")
NAMED = "llm_access_denied: Model 'gpt-6-luna' not called: circuit open on acc3 (access_denied)"
NO_CREDIT = ("llm_access_denied: RateLimitError: Error code: 429 - {'error': {'type': 'insufficient_quota', "
             "'code': 'insufficient_quota'}}")


# ── backoff jitter ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_backoff_without_jitter_is_the_old_curve():
    assert [queue.backoff_seconds(n, rand=lambda: 0.0) for n in (1, 2, 3)] == [30, 60, 120]
    assert queue.backoff_seconds(20, rand=lambda: 0.0) == queue.BACKOFF_MAX_SECONDS


def test_backoff_jitter_adds_up_to_a_quarter_and_never_less():
    assert queue.backoff_seconds(1, rand=lambda: 1.0) == 37            # 30 + 25 %
    assert queue.backoff_seconds(20, rand=lambda: 1.0) == 1125          # the cap + 25 %
    assert queue.backoff_seconds(2, rand=lambda: 5.0) == 75             # rand clamped to 0..1
    for _ in range(200):
        assert 60 <= queue.backoff_seconds(2) <= 75


def test_jobs_that_failed_together_do_not_all_return_in_the_same_second():
    assert len({queue.backoff_seconds(3) for _ in range(50)}) > 1


# ── llm_hold (pure) ─────────────────────────────────────────────────────────────────────────────────────────────────

KEYS = ["acc2", "acc3", "anthropic-direct", "openai-direct"]


def test_a_named_connection_holds_only_while_it_is_blocked():
    assert queue.llm_hold(NAMED, {"acc3": "access_denied"}, KEYS) == "connection still refused: acc3 (access_denied)"
    assert queue.llm_hold(NAMED, {"openai-direct": "quota_exhausted"}, KEYS) is None


def test_a_key_is_matched_as_a_whole_word():
    assert queue._named_connections("circuit open on acc30", KEYS) == []
    assert queue._named_connections("circuit open on acc3, acc2 (x)", KEYS) == ["acc2", "acc3"]


def test_an_error_naming_no_connection_waits_for_any_block_of_its_class():
    assert queue.llm_hold(ERROR_002, {"acc3": "access_denied"}, KEYS).startswith("a connection is still refused")
    assert queue.llm_hold(ERROR_002, {"openai-direct": "quota_exhausted"}, KEYS) is None
    assert queue.llm_hold(NO_CREDIT, {"openai-direct": "quota_exhausted"}, KEYS) is not None
    assert queue.llm_hold(ERROR_002, {}, KEYS) is None


# ── retry_by_reason against a fake pool ─────────────────────────────────────────────────────────────────────────────

def _health(conn, model, cls, open_s=300, at=T0):
    return {"connection_key": conn, "model_key": model, "last_ok_at": None, "last_error_at": at,
            "last_error_class": cls, "last_error": "x", "consecutive_failures": 1,
            "circuit_open_until": at + timedelta(seconds=open_s), "open_streak": 1, "opened_at": at,
            "probe_until": None}


class FakePool:
    def __init__(self, jobs, health=(), keys=KEYS, taken=()):
        self.jobs = jobs
        self.health = list(health)
        self.keys = keys
        self.taken = set(taken)
        self.updates = []
        self.candidate_args = None

    async def fetch(self, sql, *args):
        if "FROM shared.job j" in sql:
            self.candidate_args = args
            return self.jobs
        if "shared.llm_provider_health" in sql:
            return self.health
        if "shared.llm_provider_connection" in sql:
            return [{"connection_key": k} for k in self.keys]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        # AA-1018 part 2: the guarded UPDATE takes the status it re-checks as $3 ('failed' here)
        assert "status = $3" in sql and "attempt = 0" in sql and args[2] == "failed"
        self.updates.append(args)
        return None if args[0] in self.taken else {"id": args[0]}


def _job(i, kind="t9_write", error=ERROR_002, payload=None):
    return {"id": f"00000000-0000-0000-0000-00000000000{i}", "kind": kind, "payload": json.dumps(payload or {}),
            "error": error, "finished_at": T0}


@pytest.fixture
def now_t0(monkeypatch):
    real = queue.blocked_connections

    async def at_t0(pool, now=None):
        return await real(pool, now=T0 + timedelta(seconds=10))
    monkeypatch.setattr(queue, "blocked_connections", at_t0)


@pytest.mark.asyncio
async def test_a_dry_run_lists_and_changes_nothing(now_t0):
    pool = FakePool([_job(1), _job(2)])
    out = await queue.retry_by_reason(pool, since=T0 - timedelta(days=14))
    assert out["dry_run"] is True and out["matched"] == 2 and out["eligible"] == 2 and out["requeued"] == 0
    assert pool.updates == []
    prefix, since, kinds, limit, status = pool.candidate_args
    assert prefix == "llm_access_denied" and kinds == [] and limit == queue.RETRY_REASON_LIMIT
    assert status == "failed"


@pytest.mark.asyncio
async def test_the_apply_requeues_with_a_spread_and_skips_rows_taken_meanwhile(now_t0):
    pool = FakePool([_job(1), _job(2)], taken={_job(2)["id"]})
    vals = iter([0.0, 1.0])
    out = await queue.retry_by_reason(pool, since=T0, dry_run=False, rand=lambda: next(vals))
    assert out["requeued_ids"] == [_job(1)["id"]] and out["requeued"] == 1
    assert [u[1] for u in pool.updates] == [0, queue.RETRY_SPREAD_SECONDS]


@pytest.mark.asyncio
async def test_jobs_wait_while_their_connection_is_still_refused(now_t0):
    # acc3 refused on two models: the connection circuit is open
    health = [_health("acc3", "gpt-6-luna", "access_denied"), _health("acc3", "sonnet-5-5", "access_denied")]
    pool = FakePool([_job(1), _job(2, error=NAMED), _job(3, error=NO_CREDIT)], health=health)
    out = await queue.retry_by_reason(pool, since=T0, dry_run=False)
    assert out["blocked_connections"] == {"acc3": "access_denied"}
    assert [h["id"] for h in out["held"]] == [_job(1)["id"], _job(2)["id"]]
    assert out["requeued_ids"] == [_job(3)["id"]]


@pytest.mark.asyncio
async def test_a_quota_stop_still_holds_after_its_open_period_until_a_call_answers(now_t0):
    # the openai-direct credit refusal was 2 days ago (half-open now), nothing answered since
    health = [_health("openai-direct", "gpt-6-luna-openai", "quota_exhausted", at=T0 - timedelta(days=2))]
    pool = FakePool([_job(1, error=NO_CREDIT), _job(2)], health=health)
    out = await queue.retry_by_reason(pool, since=T0 - timedelta(days=3))
    assert out["blocked_connections"] == {"openai-direct": "quota_exhausted"}
    assert [h["id"] for h in out["held"]] == [_job(1)["id"]] and [j["id"] for j in out["jobs"]] == [_job(2)["id"]]


@pytest.mark.asyncio
async def test_an_account_block_still_holds_after_its_open_period_until_a_call_answers(now_t0):
    # review S262: acc3 refused on two models 2 days ago; its circuit is half-open (calls may probe) but nothing has
    # answered since, so the bulk re-queue must still hold its jobs
    old = T0 - timedelta(days=2)
    health = [_health("acc3", "gpt-6-luna", "access_denied", at=old),
              _health("acc3", "sonnet-5-5", "access_denied", at=old)]
    pool = FakePool([_job(1), _job(2, error=NAMED)], health=health)
    out = await queue.retry_by_reason(pool, since=T0 - timedelta(days=3), dry_run=False)
    assert out["blocked_connections"] == {"acc3": "access_denied"}
    assert out["requeued"] == 0 and pool.updates == []
    assert [h["id"] for h in out["held"]] == [_job(1)["id"], _job(2)["id"]]


@pytest.mark.asyncio
async def test_an_answered_call_since_the_block_releases_the_hold(now_t0):
    old = T0 - timedelta(days=2)
    answered = {**_health("acc3", "haiku-5-5", None, at=old), "last_error_at": None, "last_error_class": None,
                "consecutive_failures": 0, "circuit_open_until": None, "opened_at": None,
                "last_ok_at": T0 - timedelta(hours=1)}                 # e.g. the admin's Test connection
    health = [_health("acc3", "gpt-6-luna", "access_denied", at=old),
              _health("acc3", "sonnet-5-5", "access_denied", at=old), answered]
    pool = FakePool([_job(1)], health=health)
    out = await queue.retry_by_reason(pool, since=T0 - timedelta(days=3), dry_run=False)
    assert out["blocked_connections"] == {} and out["requeued"] == 1


@pytest.mark.asyncio
async def test_an_old_access_denied_alone_does_not_hold(now_t0):
    health = [_health("acc3", "gpt-6-luna", "access_denied", at=T0 - timedelta(days=2))]
    pool = FakePool([_job(1)], health=health)
    out = await queue.retry_by_reason(pool, since=T0 - timedelta(days=3))
    assert out["blocked_connections"] == {} and out["eligible"] == 1


@pytest.mark.asyncio
async def test_a_kind_that_refuses_retries_and_an_unknown_kind_are_held(now_t0):
    registry.job_kind("aa1018_test_single_use", retry_refusal=lambda row: "single use")(AsyncMock())
    try:
        pool = FakePool([_job(1, kind="aa1018_test_single_use"), _job(2, kind="no_such_kind")])
        out = await queue.retry_by_reason(pool, since=T0)
        assert {h["kind"]: h["held"] for h in out["held"]} == {"aa1018_test_single_use": "single use",
                                                               "no_such_kind": "kind is not registered"}
    finally:
        registry._KINDS.pop("aa1018_test_single_use", None)


@pytest.mark.asyncio
async def test_before_the_registry_migrations_nothing_is_blocked():
    import asyncpg

    pool = FakePool([])
    pool.fetch = AsyncMock(side_effect=asyncpg.exceptions.UndefinedTableError("missing"))
    assert await queue.blocked_connections(pool) == ({}, [])


@pytest.mark.asyncio
async def test_limit_is_capped_and_the_prefix_is_required(now_t0):
    pool = FakePool([])
    out = await queue.retry_by_reason(pool, since=T0, limit=10_000, kinds=["t9_write"])
    assert out["limit"] == queue.RETRY_REASON_MAX_LIMIT and pool.candidate_args[2] == ["t9_write"]
    # review S262: an apply re-queues a subset per call, never hundreds at once
    out = await queue.retry_by_reason(pool, since=T0, limit=10_000, dry_run=False)
    assert out["limit"] == queue.RETRY_REASON_APPLY_LIMIT == 50 and pool.candidate_args[3] == 50
    with pytest.raises(ValueError):
        await queue.retry_by_reason(pool, " ", since=T0)


# ── the admin route ─────────────────────────────────────────────────────────────────────────────────────────────────

def _request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    db = MagicMock()
    db.execute = AsyncMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=db)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire)
    return req, db


@pytest.mark.asyncio
async def test_the_route_is_a_dry_run_by_default_and_audited(monkeypatch, now_t0):
    from api.routers import admin_job_runner as r
    monkeypatch.setattr(r, "verify_admin_secret", lambda s: None)
    pool = FakePool([_job(1)])
    req, db = _request(pool)
    out = await r.retry_llm_denied(r.RetryLlmDeniedRequest(), req, x_admin_secret="s", x_admin_user_id="u1")
    assert out["dry_run"] is True and out["eligible"] == 1 and pool.updates == []
    sql, actor, action, rtype, rid, details = db.execute.call_args[0]
    assert "acp_shared.audit_log" in sql and actor == "admin:u1" and action == "jobs_retry_llm_denied"
    assert json.loads(details)["eligible"] == 1 and json.loads(details)["dry_run"] is True
    since = datetime.fromisoformat(out["since"])
    assert timedelta(days=13, hours=23) < datetime.now(timezone.utc) - since < timedelta(days=14, minutes=1)


@pytest.mark.asyncio
async def test_the_route_applies_when_asked(monkeypatch, now_t0):
    from api.routers import admin_job_runner as r
    monkeypatch.setattr(r, "verify_admin_secret", lambda s: None)
    pool = FakePool([_job(1)])
    req, _ = _request(pool)
    body = r.RetryLlmDeniedRequest(dry_run=False, since="2026-10-01T00:00:00", kinds=[" t9_write "])
    out = await r.retry_llm_denied(body, req, x_admin_secret="s", x_admin_user_id=None)
    assert out["requeued"] == 1 and out["kinds"] == ["t9_write"] and out["since"].endswith("+00:00")


def test_the_route_refuses_unknown_fields():
    from pydantic import ValidationError

    from api.routers import admin_job_runner as r
    with pytest.raises(ValidationError):
        r.RetryLlmDeniedRequest(prefix="anything")


# ── the jobs-list reason filter ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_list_filters_on_the_error_prefix_without_wildcards():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=0)
    await queue.list_jobs(pool, reason="llm_access_denied")
    sql, *args = pool.fetch.call_args[0]
    assert "left(j.error, length($8)) = $8" in sql and args[7] == "llm_access_denied"
    await queue.count_jobs(pool, reason="llm_access_denied")
    assert pool.fetchval.call_args[0][-1] == "llm_access_denied"
