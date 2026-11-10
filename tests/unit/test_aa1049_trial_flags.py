"""AA-1049 W3 — per-job opt-in of the W3 switches for a paid trial (services/quality/trial_flags.py).

Pins: no payload key = main (no read, no change); a trial switch is honoured for an audit tenant (kind demo, not the
pilot) only and fails closed to main otherwise; the value is scoped to the job's own task (a job running beside it
and the process environment never see it); the T9 / T2 switch readers read it; the jobs record it in their result.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from services.quality import trial_flags as tf

DEMO = "11111111-1111-1111-1111-111111111111"
AGENCY = "22222222-2222-2222-2222-222222222222"
PILOT = "33333333-3333-3333-3333-333333333333"
TENANTS = {DEMO: {"kind": "demo", "is_pilot": False}, AGENCY: {"kind": "agency", "is_pilot": False},
           PILOT: {"kind": "agency", "is_pilot": True}}
SWITCHES = ("T9_REGRESSION_GUARD_MODE", "T9_SEO_BUILDER", "T2_ORCHESTRATOR", "T2_INTERIM")


class _Conn:
    """Answers the tenant lookup from TENANTS and the job's own status reads; records every statement."""

    def __init__(self, *, fail_tenant: bool = False, status: str = "processing", final: str = "approved"):
        self.sql: list[str] = []
        self.fail_tenant, self.status, self.final = fail_tenant, status, final
        self.status_reads = 0

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "shared.tenants" in sql:
            if self.fail_tenant:
                raise RuntimeError("db down")
            return TENANTS.get(args[0])
        if "FROM gold_aa_internal.published_tours" in sql:
            return {"id": "pt-1", "tour_id": "t-1"}
        if "tenant_tour_versions" in sql:
            return {"status": self.final, "score": 8.0, "qa_status": "passed"}
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        self.status_reads += 1
        return self.status if self.status_reads == 1 else self.final

    async def execute(self, sql, *args):
        self.sql.append(sql)


class _Pool:
    def __init__(self, conn: _Conn):
        self.conn = conn

    @asynccontextmanager
    async def _acq(self):
        yield self.conn

    def acquire(self):
        return self._acq()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)


def _readers() -> dict:
    from services.acp_content_writing import service
    from services.content_generation import t2_orchestrator
    return {"guard": service.regression_guard_mode(), "seo": service.seo_builder_mode(),
            "t2": t2_orchestrator.mode()}


MAIN = {"guard": "gate_names", "seo": "off", "t2": "graph"}


# ── parse ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_parse_keeps_only_the_kinds_own_switches_with_known_values():
    flags, refused = tf.parse("t9_write", {"T9_SEO_BUILDER": " ON ", "T9_REGRESSION_GUARD_MODE": "stable_keys",
                                           "T2_ORCHESTRATOR": "linear", "LLM_ROUTE_AWS_FIRST": "off"})
    assert flags == {"T9_SEO_BUILDER": "on", "T9_REGRESSION_GUARD_MODE": "stable_keys"}
    assert sorted(r.split(":")[0] for r in refused) == ["LLM_ROUTE_AWS_FIRST", "T2_ORCHESTRATOR"]
    assert tf.parse("t2_rewrite", {"T2_ORCHESTRATOR": "linear2"}) == ({}, ["T2_ORCHESTRATOR: value not allowed"])
    assert tf.parse("t2_rewrite", ["T2_ORCHESTRATOR"])[0] == {}
    assert tf.parse("s1_rewrite", {"T2_ORCHESTRATOR": "linear"})[0] == {}   # no other job kind reads a trial


@pytest.mark.parametrize("kind,name", [(k, n) for k, sw in tf.ALLOWED.items() for n in sw])
def test_every_allowed_value_is_one_its_reader_knows(monkeypatch, kind, name):
    """A trial value the reader would treat as unknown (= main) would make a trial silently run main."""
    from services.acp_content_writing import length_extend, service
    from services.content_generation import t2_orchestrator
    reader = {"T9_REGRESSION_GUARD_MODE": service.regression_guard_mode, "T9_SEO_BUILDER": service.seo_builder_mode,
              "T9_LENGTH_EXTEND": length_extend.mode, "T2_ORCHESTRATOR": t2_orchestrator.mode}[name]
    for value in tf.ALLOWED[kind][name]:
        monkeypatch.setenv(name, value)
        assert reader() == value


def test_the_job_kinds_match_the_jobs_that_read_them():
    from services.jobs import t2_rewrite_job, t9_write_job
    assert set(tf.ALLOWED) == {t9_write_job.KIND, t2_rewrite_job.KIND}


# ── scope ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, None, {"trial_flags": {}}, {"trial_flags": None}])
async def test_no_trial_request_reads_nothing_and_changes_nothing(payload):
    conn = _Conn()
    async with tf.scope(_Pool(conn), "t9_write", payload, DEMO) as rec:
        assert rec is None
        assert _readers() == MAIN
    assert conn.sql == []


@pytest.mark.asyncio
async def test_an_audit_tenant_gets_the_switches_for_the_body_only(monkeypatch):
    monkeypatch.setenv("T9_SEO_BUILDER", "off")
    conn = _Conn()
    payload = {"trial_flags": {"T9_SEO_BUILDER": "on", "T9_REGRESSION_GUARD_MODE": "stable_keys"}}
    async with tf.scope(_Pool(conn), "t9_write", payload, DEMO) as rec:
        assert _readers() == {"guard": "stable_keys", "seo": "on", "t2": "graph"}
        assert tf.active() == {"T9_SEO_BUILDER": "on", "T9_REGRESSION_GUARD_MODE": "stable_keys"}
    assert rec == {"requested": payload["trial_flags"], "applied": payload["trial_flags"], "refused": []}
    assert _readers() == MAIN and tf.active() == {}
    assert len(conn.sql) == 1


@pytest.mark.asyncio
async def test_t2_linear_trial_reaches_the_orchestrator_reader_and_keeps_the_interim_break_glass(monkeypatch):
    payload = {"trial_flags": {"T2_ORCHESTRATOR": "linear"}}
    async with tf.scope(_Pool(_Conn()), "t2_rewrite", payload, DEMO):
        assert _readers()["t2"] == "linear"
        monkeypatch.setenv("T2_INTERIM", "off")      # break glass still wins, as for the env switch
        assert _readers()["t2"] == "graph"


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant,conn_kw,why", [
    (AGENCY, {}, "not an audit tenant"), (PILOT, {}, "not an audit tenant"),
    ("44444444-4444-4444-4444-444444444444", {}, "unknown tenant"), (None, {}, "no tenant"),
    (DEMO, {"fail_tenant": True}, "tenant lookup failed: RuntimeError"),
])
async def test_anything_but_an_audit_tenant_fails_closed_to_main(tenant, conn_kw, why):
    payload = {"trial_flags": {"T9_SEO_BUILDER": "on"}}
    async with tf.scope(_Pool(_Conn(**conn_kw)), "t9_write", payload, tenant) as rec:
        assert _readers() == MAIN
    assert rec["applied"] == {} and rec["refused"] == [f"T9_SEO_BUILDER: {why}"]


@pytest.mark.asyncio
async def test_a_pilot_demo_row_is_refused_too():
    TENANTS["55555555-5555-5555-5555-555555555555"] = {"kind": "demo", "is_pilot": True}
    try:
        async with tf.scope(_Pool(_Conn()), "t9_write", {"trial_flags": {"T9_SEO_BUILDER": "on"}},
                            "55555555-5555-5555-5555-555555555555") as rec:
            assert _readers() == MAIN
        assert rec["refused"] == ["T9_SEO_BUILDER: pilot tenant"]
    finally:
        TENANTS.pop("55555555-5555-5555-5555-555555555555")


@pytest.mark.asyncio
async def test_only_refused_entries_read_no_tenant():
    conn = _Conn()
    async with tf.scope(_Pool(conn), "t9_write", {"trial_flags": {"T2_ORCHESTRATOR": "linear"}}, DEMO) as rec:
        assert _readers() == MAIN
    assert rec["applied"] == {} and conn.sql == []


@pytest.mark.asyncio
async def test_a_job_beside_the_trial_and_a_worker_thread_see_their_own_value():
    """Two jobs share one worker process: the trial value lives in the trial job's task (and the threads it starts
    with asyncio.to_thread), never in the other job or the environment."""
    entered, release = asyncio.Event(), asyncio.Event()
    seen: dict = {}

    async def trial_job():
        async with tf.scope(_Pool(_Conn()), "t9_write", {"trial_flags": {"T9_SEO_BUILDER": "on"}}, DEMO):
            entered.set()
            seen["thread"] = await asyncio.to_thread(lambda: _readers()["seo"])
            await release.wait()
            seen["trial"] = _readers()["seo"]

    async def other_job():
        await entered.wait()
        seen["other"] = _readers()["seo"]
        release.set()

    await asyncio.gather(trial_job(), other_job())
    assert seen == {"thread": "on", "trial": "on", "other": "off"}
    import os
    assert "T9_SEO_BUILDER" not in os.environ


# ── the two jobs ───────────────────────────────────────────────────────────────────────────────────────────────────

def _ctx(pool, kind: str, payload: dict):
    from shared.jobs import queue
    from shared.jobs.registry import JobContext
    job = queue.Job(id="job-1", kind=kind, payload=payload, status="running", attempt=1, max_attempts=2,
                    progress={}, cost_usd=0.0)
    ctx = JobContext(pool, job, {"redis": None})
    ctx.progress = AsyncMock()
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant,expect_seo,applied", [(DEMO, "on", True), (AGENCY, "off", False)])
async def test_t9_job_runs_the_write_inside_the_trial_and_records_it(tenant, expect_seo, applied):
    from services.jobs import t9_write_job
    seen = {}

    async def fake_write(*a, **k):
        seen.update(_readers())

    payload = {"request_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "piece_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
               "tenant_id": tenant, "context": {"channel": "blog"}, "trial_flags": {"T9_SEO_BUILDER": "on"}}
    with patch.object(t9_write_job, "_run_write_with_progress", fake_write):
        out = await t9_write_job.run(_ctx(_Pool(_Conn()), "t9_write", payload))
    assert seen["seo"] == expect_seo and seen["guard"] == "gate_names"
    assert out["trial_flags"]["applied"] == ({"T9_SEO_BUILDER": "on"} if applied else {})
    assert _readers() == MAIN


@pytest.mark.asyncio
async def test_t9_job_without_a_trial_is_unchanged():
    from services.jobs import t9_write_job
    conn = _Conn()
    payload = {"request_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "piece_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
               "tenant_id": DEMO, "context": {"channel": "blog"}}
    with patch.object(t9_write_job, "_run_write_with_progress", AsyncMock()):
        out = await t9_write_job.run(_ctx(_Pool(conn), "t9_write", payload))
    assert out == {"piece_id": payload["piece_id"], "piece_status": "approved"}
    assert not any("shared.tenants" in s for s in conn.sql)


@pytest.mark.asyncio
@pytest.mark.parametrize("trial,expect_mode", [({"T2_ORCHESTRATOR": "linear"}, "linear"), (None, "graph")])
async def test_t2_job_runs_the_rewrite_inside_the_trial_and_records_it(trial, expect_mode):
    from services.jobs import t2_rewrite_job
    seen = {}

    async def fake_rewrite(*a, **k):
        seen.update(_readers())
        return {"diag": 1}

    payload = {"version_id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "tenant_id": DEMO,
               "published_tour_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
               **({"trial_flags": trial} if trial else {})}
    with patch.object(t2_rewrite_job, "_prepare", AsyncMock(return_value=({}, {"_brand_loaded": True}))), \
         patch("services.content_generation.t2_facts.with_facts", AsyncMock(return_value=({}, None))), \
         patch.object(t2_rewrite_job, "_store_fact_sheet", AsyncMock(return_value=None)), \
         patch.object(t2_rewrite_job, "_rewrite_and_save", fake_rewrite):
        out = await t2_rewrite_job.run(_ctx(_Pool(_Conn(status="pending", final="ai_generated")), "t2_rewrite",
                                            payload))
    assert seen["t2"] == expect_mode
    assert ("trial_flags" in out) == bool(trial)
    if trial:
        assert out["trial_flags"]["applied"] == trial
    assert out["diag"] == 1 and _readers() == MAIN
