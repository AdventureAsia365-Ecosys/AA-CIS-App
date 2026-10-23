"""AA-987 — CIS side of the TripPlanner cache purge on a tour lifecycle event (services/jobs/tripplanner_purge_job.py).

No DB: a fake pool / connection. Covers the signature (a fixed vector shared with AA-TripPlanner-Web
frontend/lib/catalogPurge.test.ts), the delivery answers (2xx delivered, 4xx final, 5xx / 429 / network retried),
the not-configured skip, the event -> job enqueue (one job per event, keyed on the event, replay creates nothing),
the sweep, and the wiring (run_after_commit, the publish of process_export, the ops sampler part).
"""
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.jobs import tripplanner_purge_job as tp  # noqa: E402
from shared.jobs.registry import NonRetryable, get_kind  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# The cross-repo vector: TripPlanner's verifier test uses the same secret, timestamp, body and expected value.
VECTOR_SECRET = "aa987-test-secret"
VECTOR_TS = 1_761_200_000
VECTOR_BODY = (b'{"contract":"catalog.tour.lifecycle.v1","events":[{"event":"retired","event_id":42,'
               b'"idempotency_key":"trash_master:t1:retired:abc","occurred_at":"2026-10-23T14:33:41+00:00",'
               b'"reason":"master_trashed","replaced_by_tour_id":null,"source_id":"3f0c2a8e-0000-4000-8000-'
               b'000000000001","source_system":"cis","tour_id":"00000000-0000-4000-8000-0000000000t1"}]}')
# Pinned literal (the same string is in the TripPlanner test): a change on either side breaks one of the two suites.
VECTOR_SIGNATURE = "t=1761200000,v1=0ea3fd2093eca674137f8046bd84405dcd77280f4d7b2dad4ae8a52bff80d41a"

ROW = {
    "event_id": 42,
    "source_system": "cis",
    "source_id": "3f0c2a8e-0000-4000-8000-000000000001",
    "tour_id": "00000000-0000-4000-8000-0000000000t1",
    "event": "retired",
    "reason": "master_trashed",
    "replaced_by_tour_id": None,
    "occurred_at": datetime(2026, 10, 23, 14, 33, 41, tzinfo=timezone.utc),
    "idempotency_key": "trash_master:t1:retired:abc",
}


def _ctx(payload, pool=None, attempt=1):
    pool = pool or SimpleNamespace(fetchrow=AsyncMock(return_value=ROW))
    return SimpleNamespace(payload=payload, pool=pool, job_id="job-1", job=SimpleNamespace(attempt=attempt))


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.delenv("TRIPPLANNER_REVALIDATE_URL", raising=False)
    monkeypatch.setenv("TRIPPLANNER_REVALIDATE_SECRET", VECTOR_SECRET)
    tp._secret.reset()


# ── signature and body ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_body_and_signature_match_the_shared_vector():
    body = tp.encode_body([tp.event_body(ROW)])
    assert body == VECTOR_BODY
    assert tp.sign(VECTOR_SECRET, body, VECTOR_TS) == VECTOR_SIGNATURE


def test_the_pinned_vector_is_plain_hmac_sha256():
    mac = hmac.new(VECTOR_SECRET.encode(), b"1761200000." + VECTOR_BODY, hashlib.sha256).hexdigest()
    assert VECTOR_SIGNATURE == f"t={VECTOR_TS},v1={mac}"


def test_the_signature_covers_the_timestamp_and_the_body():
    base = tp.sign("s", b"{}", 100)
    assert tp.sign("s", b"{}", 101) != base
    assert tp.sign("s", b"{ }", 100) != base
    assert tp.sign("other", b"{}", 100) != base


def test_parse_secret_accepts_bare_or_json_and_refuses_empty():
    assert tp.parse_secret(" abc \n") == "abc"
    assert tp.parse_secret('{"secret": "abc"}') == "abc"
    assert tp.parse_secret('{"other": 1}') is None
    assert tp.parse_secret("{not json") is None
    assert tp.parse_secret("") is None and tp.parse_secret(None) is None


def test_secret_name_and_url_defaults(monkeypatch):
    monkeypatch.delenv("TRIPPLANNER_REVALIDATE_SECRET_NAME", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert tp.secret_name() == "aa-cis/dev/tripplanner-revalidate-hmac"
    assert not tp.secret_name().startswith("tripplanner/")      # the CIS task role is denied tripplanner/*
    assert tp.target_url() == tp.DEFAULT_URL
    monkeypatch.setenv("TRIPPLANNER_REVALIDATE_URL", "")
    assert tp.target_url() == ""                                  # explicit off switch


def test_secret_cache_reads_once_and_caches_a_failure(monkeypatch):
    monkeypatch.delenv("TRIPPLANNER_REVALIDATE_SECRET")
    calls = []

    def fetch(name):
        calls.append(name)
        raise RuntimeError("ResourceNotFoundException")

    cache = tp._SecretCache(fetch)
    assert cache.get() is None and cache.get() is None
    assert len(calls) == 1                                         # the failure is cached, not retried per job

    ok = tp._SecretCache(lambda name: "k1")
    assert ok.get() == "k1" and ok.get() == "k1"


# ── delivery ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deliver_posts_a_verifiable_signed_body():
    seen = {}

    async def post(url, body, headers):
        seen.update(url=url, body=body, headers=headers)
        return 200, json.dumps({"ok": True, "purged": {"tags": ["tp-catalog"]}})

    out = await tp.deliver(ROW, secret=VECTOR_SECRET, url="https://tp.test/api/revalidate/catalog", post=post,
                           now=VECTOR_TS)
    assert out["delivered"] is True and out["status"] == 200 and out["purged"] == {"tags": ["tp-catalog"]}
    assert seen["body"] == VECTOR_BODY
    assert seen["headers"][tp.SIGNATURE_HEADER] == VECTOR_SIGNATURE
    assert seen["headers"]["x-aa-event-key"] == ROW["idempotency_key"]
    assert VECTOR_SECRET not in json.dumps(seen["headers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 404, 413])
async def test_a_final_4xx_is_not_retried(status):
    with pytest.raises(NonRetryable):
        await tp.deliver(ROW, secret="s", url="u", post=AsyncMock(return_value=(status, "bad_signature")))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
async def test_a_5xx_or_throttle_is_retried(status):
    with pytest.raises(RuntimeError) as e:
        await tp.deliver(ROW, secret="s", url="u", post=AsyncMock(return_value=(status, "")))
    assert not isinstance(e.value, NonRetryable)


@pytest.mark.asyncio
async def test_a_network_error_is_retried_without_leaking_detail():
    with pytest.raises(RuntimeError, match="request failed: ConnectionError$"):
        await tp.deliver(ROW, secret="s", url="u", post=AsyncMock(side_effect=ConnectionError("10.0.0.1:443 refused")))


# ── the handler ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_reads_the_event_from_the_outbox_table_and_delivers():
    ctx = _ctx({"event_key": ROW["idempotency_key"], "trigger": "after_commit"})
    with patch.object(tp, "_post", AsyncMock(return_value=(200, "{}"))) as post:
        out = await tp.run(ctx)
    sql, key = ctx.pool.fetchrow.await_args.args
    assert "FROM gold_aa_internal.tour_lifecycle_event" in sql and key == ROW["idempotency_key"]
    assert out["delivered"] is True and out["event_id"] == 42 and out["lifecycle_event"] == "retired"
    assert out["trigger"] == "after_commit"
    url, body, headers = post.await_args.args
    assert url == tp.DEFAULT_URL and json.loads(body)["events"][0]["source_id"] == ROW["source_id"]


@pytest.mark.asyncio
async def test_run_is_safe_to_replay():
    """A retry or a replay sends the same event again; TripPlanner's purge is idempotent, so the outcome is equal."""
    ctx = _ctx({"event_key": ROW["idempotency_key"]})
    with patch.object(tp, "_post", AsyncMock(return_value=(200, "{}"))):
        first = await tp.run(ctx)
        second = await tp.run(ctx)
    assert {k: v for k, v in first.items() if k != "ms"} == {k: v for k, v in second.items() if k != "ms"}


@pytest.mark.asyncio
async def test_run_without_configuration_succeeds_as_skipped(monkeypatch):
    monkeypatch.delenv("TRIPPLANNER_REVALIDATE_SECRET")
    with patch.object(tp._secret, "_fetch", lambda name: (_ for _ in ()).throw(RuntimeError("missing"))), \
            patch.object(tp, "_post", AsyncMock()) as post:
        out = await tp.run(_ctx({"event_key": ROW["idempotency_key"]}))
    assert out["delivered"] is False and out["skipped"] == "not_configured"
    post.assert_not_awaited()
    monkeypatch.setenv("TRIPPLANNER_REVALIDATE_URL", "")
    out = await tp.run(_ctx({"event_key": ROW["idempotency_key"]}))
    assert out["skipped"] == "not_configured"


@pytest.mark.asyncio
async def test_run_refuses_a_bad_payload_or_an_unknown_event():
    with pytest.raises(NonRetryable):
        await tp.run(_ctx({}))
    with pytest.raises(NonRetryable):
        await tp.run(_ctx({"event_key": "x"}, pool=SimpleNamespace(fetchrow=AsyncMock(return_value=None))))


@pytest.mark.asyncio
async def test_run_reraises_a_failed_delivery_for_the_runner():
    with patch.object(tp, "_post", AsyncMock(return_value=(503, "not_configured"))):
        with pytest.raises(RuntimeError):
            await tp.run(_ctx({"event_key": ROW["idempotency_key"]}))


def test_the_kind_is_registered_with_bounded_retries():
    k = get_kind(tp.KIND)
    assert k is not None and k.max_attempts == tp.MAX_ATTEMPTS == 4 and k.concurrency == 1
    assert k.background is False and k.deploy_gate_exempt is False
    assert not tp.KIND.startswith(("bk_", "tp_"))                 # shared.job job_kind_chk


# ── enqueue and sweep ──────────────────────────────────────────────────────────────────────────────────────────────

class FakeJobs:
    """queue.enqueue over a dict: ON CONFLICT (idempotency_key) DO NOTHING."""

    def __init__(self, fail=False):
        self.rows = {}
        self.fail = fail

    async def fetchrow(self, sql, kind, payload, key, max_attempts, parent, created_by, run_after):
        if self.fail:
            raise RuntimeError("pool closed")
        if key in self.rows:
            return None
        self.rows[key] = {"id": f"j{len(self.rows)}", "kind": kind, "payload": json.loads(payload),
                          "max_attempts": max_attempts, "created_by": created_by}
        return {"id": self.rows[key]["id"]}

    async def fetchval(self, sql, key):
        return self.rows[key]["id"]


EVENTS = [
    {"event": "retired", "reason": "master_trashed", "tour_id": "t1", "idempotency_key": "trash_master:t1:retired:a"},
    {"event": "superseded", "reason": "replaced", "tour_id": "t0", "idempotency_key": "supersede:t0:superseded:b"},
    {"event": "paused", "reason": "x"},                         # an older shape without the key: skipped
]


@pytest.mark.asyncio
async def test_one_job_per_event_keyed_on_the_event_and_replay_creates_none():
    db = FakeJobs()
    assert await tp.enqueue_for_events(db, EVENTS) == 2
    assert set(db.rows) == {"tripplanner_purge:trash_master:t1:retired:a",
                            "tripplanner_purge:supersede:t0:superseded:b"}
    row = db.rows["tripplanner_purge:trash_master:t1:retired:a"]
    assert row["kind"] == tp.KIND and row["max_attempts"] == tp.MAX_ATTEMPTS
    assert row["payload"] == {"event_key": "trash_master:t1:retired:a", "trigger": "after_commit"}
    assert await tp.enqueue_for_events(db, EVENTS) == 0              # replay: same keys, nothing new
    assert len(db.rows) == 2


@pytest.mark.asyncio
async def test_enqueue_never_raises():
    assert await tp.enqueue_for_events(FakeJobs(fail=True), EVENTS) == 0
    assert await tp.enqueue_for_events(FakeJobs(), None) == 0


@pytest.mark.asyncio
async def test_sweep_is_one_idempotent_anti_join_insert():
    db = SimpleNamespace(fetchval=AsyncMock(return_value=3))
    assert await tp.sweep(db) == 3
    sql, window, lag = db.fetchval.await_args.args
    assert "FROM gold_aa_internal.tour_lifecycle_event e" in sql
    assert "NOT EXISTS (SELECT 1 FROM shared.job j WHERE j.idempotency_key = 'tripplanner_purge:' ||" in sql
    assert "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING" in sql
    assert (window, lag) == (float(tp.SWEEP_WINDOW_S), float(tp.SWEEP_LAG_S))
    db.fetchval = AsyncMock(return_value=0)
    assert await tp.sweep(db) == 0


# ── wiring ─────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_after_commit_enqueues_the_purge_first_and_survives_its_failure():
    from services.tour_lifecycle import effects
    order = []
    outcome = SimpleNamespace(events=EVENTS[:1], recompute=[("t1", "trashed")], cancelled_jobs=[])
    rec = AsyncMock(side_effect=lambda *a: order.append("recompute"))
    with patch("services.jobs.tripplanner_purge_job.enqueue_for_events",
               AsyncMock(side_effect=lambda *a, **k: order.append("purge") or 1)) as enq, \
            patch.object(effects, "recheck_trip_pages", AsyncMock()):
        await effects.run_after_commit(object(), outcome, recompute=rec)
    assert order == ["purge", "recompute"] and enq.await_args.args[1] == EVENTS[:1]
    with patch("services.jobs.tripplanner_purge_job.enqueue_for_events", AsyncMock(side_effect=RuntimeError("x"))), \
            patch.object(effects, "recheck_trip_pages", AsyncMock()):
        await effects.run_after_commit(object(), outcome, recompute=AsyncMock())   # logged, not raised


def test_the_lifecycle_event_carries_its_idempotency_key():
    src = open(os.path.join(ROOT, "services", "tour_lifecycle", "service.py")).read()
    assert '"replaced_by_tour_id": replaced_by, "idempotency_key": key}' in src


def test_the_publish_of_process_export_notifies_tripplanner():
    src = open(os.path.join(ROOT, "services", "export", "handler.py")).read()
    assert 'await notify_tripplanner(conn, published.events, trigger="publish")' in src


@pytest.mark.asyncio
async def test_the_ops_sampler_runs_the_sweep():
    from api.core import ops_metrics_sampler as sampler
    out = {}
    with patch("services.jobs.tripplanner_purge_job.sweep", AsyncMock(return_value=2)):
        await sampler._tripplanner_purge_part(object(), out)
    assert out == {"tripplanner_purge_swept": 2}
    out = {}
    with patch("services.jobs.tripplanner_purge_job.sweep", AsyncMock(return_value=0)):
        await sampler._tripplanner_purge_part(object(), out)
    assert out == {}
