"""AA-1026 (S267) — the LLM availability sweep: the candidate mode of services/jobs/llm_probe_job.py (disabled
deployments, a small budget, never counted on the breaker), the per-deployment probe state it writes (CIS 367), the
matrix it returns, and the gateway's uncounted probe (LLMClient.probe(count=False)). No provider call, no database."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services import llm_connections
from services.jobs import llm_probe_job as probe
from shared.jobs.registry import NonRetryable
from shared.llm_client import breaker
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.connections import BUILTIN_CONNECTIONS, ProviderConnection
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.vendor_route import VendorDeployment


def _model(key, p_in=1.0, p_out=5.0, enabled=False):
    return CatalogModel(model_key=key, label=key, vendor="openai", provider="bedrock", api_style="converse",
                        callable_via=("llm_client",), price_in_per_mtok=p_in, price_out_per_mtok=p_out,
                        enabled=enabled)


def _dep(model, conn, enabled=False):
    return ModelDeployment(model, conn, f"wire-{model}", 10, enabled, "seed")


OFF_CONN = ProviderConnection(connection_key="acc9-west", kind="bedrock", auth_mode="task_role", label="x",
                              data_scope="aws_only", enabled=False, region="us-west-2")
BROKEN = ProviderConnection(connection_key="openai-other", kind="openai", auth_mode="api_key", label="x",
                            data_scope="vendor_direct", enabled=True)          # no secret, no env: a policy problem
CONNS = {"acc2": BUILTIN_CONNECTIONS["acc2"], "acc3": BUILTIN_CONNECTIONS["acc3"], "acc9-west": OFF_CONN,
         "openai-other": BROKEN}
MODELS = {"cand-a": _model("cand-a", 3.0, 15.0), "cand-b": _model("cand-b", None, None),
          "routed-c": _model("routed-c", 0.1, 0.5, enabled=True), "west-d": _model("west-d", 0.2, 0.8)}
DEPS = [_dep("cand-a", "acc3"), _dep("cand-b", "acc3"), _dep("routed-c", "acc3", enabled=True),
        _dep("routed-c", "acc2", enabled=True), _dep("west-d", "acc9-west", enabled=True),
        _dep("cand-a", "openai-other")]


def _testable(conn, deployments):
    return [(d, MODELS[d.model_key]) for d in deployments]


def _ctx(payload):
    return SimpleNamespace(payload=payload, job_id="j1", pool=MagicMock(), set_result=MagicMock())


# ── the plan ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_candidate_plan_takes_disabled_deployments_and_switched_off_connections():
    plan = probe.build_candidate_plan(CONNS, DEPS, _testable)
    assert [(p["connection_key"], p["model_key"]) for p in plan["probes"]] == [
        ("acc3", "cand-a"), ("acc3", "cand-b"), ("acc9-west", "west-d")]
    assert all(p["candidate"] is True and p["probe"] == "text" for p in plan["probes"])
    why = {(s["connection_key"], s["model_key"]): s["why"] for s in plan["skipped"]}
    assert why[("openai-other", None)].startswith("unconfigured: ")
    assert ("acc3", "routed-c") not in why and ("acc2", "routed-c") not in why    # routed rows: the daily probe's


def test_the_candidate_estimate_uses_the_small_budget_and_counts_unpriced_rows():
    plan = probe.build_candidate_plan(CONNS, DEPS, _testable)
    est = {p["model_key"]: p["estimated_usd"] for p in plan["probes"]}
    assert est["cand-a"] == round((probe.EST_INPUT_TOKENS * 3.0 + probe.CANDIDATE_MAX_TOKENS * 15.0) / 1e6, 6)
    assert est["cand-b"] is None and plan["unpriced"] == 1
    assert plan["estimated_usd"] == round(est["cand-a"] + est["west-d"], 6)


def test_the_candidate_plan_filters_and_caps(monkeypatch):
    plan = probe.build_candidate_plan(CONNS, DEPS, _testable, connection_keys=["acc3"], model_keys=["cand-b"])
    assert [(p["connection_key"], p["model_key"]) for p in plan["probes"]] == [("acc3", "cand-b")]
    monkeypatch.setattr(probe, "MAX_PROBES", 1)
    plan = probe.build_candidate_plan(CONNS, DEPS, _testable)
    assert len(plan["probes"]) == 1 and any(s["why"].startswith("over_limit") for s in plan["skipped"])


def test_deployment_state_of_each_outcome():
    assert probe.deployment_state(True, None) == "ok"
    assert probe.deployment_state(False, "access_denied") == "access_denied"
    assert probe.deployment_state(False, "quota_exhausted") == "quota_exhausted"
    assert probe.deployment_state(False, "throttled") == "throttled"
    assert probe.deployment_state(False, "credential") == "unconfigured"
    assert probe.deployment_state(False, "request") == "error"
    assert probe.deployment_state(False, None) == "error"


# ── the job ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_candidate_dry_run_makes_no_call(monkeypatch):
    monkeypatch.setenv(probe.SWITCH_ENV, "on")
    loader = AsyncMock(return_value=probe.build_candidate_plan(CONNS, DEPS, _testable))
    monkeypatch.setattr(probe, "load_candidate_plan", loader)
    monkeypatch.setattr(probe, "load_plan", AsyncMock(side_effect=AssertionError("daily plan")))
    runner = AsyncMock()
    monkeypatch.setattr(probe, "run_probes", runner)
    out = await probe.run(_ctx({"trigger": "manual", "mode": "candidates", "connection_keys": ["acc3"]}))
    assert out["mode"] == "candidates" and out["dry_run"] is True and out["calls_made"] == 0
    assert len(out["planned"]) == 3
    assert loader.await_args.kwargs == {"connection_keys": ["acc3"], "model_keys": None}
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_candidate_mode_is_manual_only_and_known_modes_only(monkeypatch):
    monkeypatch.setenv(probe.SWITCH_ENV, "on")
    with pytest.raises(NonRetryable, match="mode"):
        await probe.run(_ctx({"trigger": "schedule", "mode": "candidates"}))
    with pytest.raises(NonRetryable, match="mode"):
        await probe.run(_ctx({"trigger": "manual", "mode": "everything"}))
    with pytest.raises(ValueError):
        await probe.enqueue_manual(MagicMock(), dry_run=True, actor="admin:x", mode="everything")


@pytest.mark.asyncio
async def test_a_live_candidate_sweep_is_uncounted_small_and_returns_the_matrix(monkeypatch):
    monkeypatch.setenv(probe.SWITCH_ENV, "on")
    plan = probe.build_candidate_plan(CONNS, DEPS, _testable)
    monkeypatch.setattr(probe, "load_candidate_plan", AsyncMock(return_value=plan))
    seen = []
    long_error = "AccessDeniedException: not available for this account " + "x" * 300

    async def fake_test(pool, key, *, model_key, actor, count_outcome=True, max_tokens=None):
        seen.append((key, model_key, count_outcome, max_tokens))
        if model_key == "cand-a":
            return {"ok": False, "error_class": "access_denied", "error": long_error, "cost_usd": 0.0,
                    "wire_id": "wire-cand-a"}
        if model_key == "west-d":
            raise LookupError("gone")
        return {"ok": True, "error_class": None, "error": None, "cost_usd": 0.00003, "wire_id": "wire-cand-b",
                "reply_usable": False}

    monkeypatch.setattr(llm_connections, "test_connection", fake_test)
    stamp = AsyncMock()
    monkeypatch.setattr(probe, "stamp_probed", stamp)
    states = AsyncMock(return_value=2)
    monkeypatch.setattr(probe, "record_deployment_states", states)
    monkeypatch.setattr(probe, "coverage_summary", AsyncMock(return_value={"ready": True, "ok": True}))
    out = await probe.run(_ctx({"trigger": "manual", "mode": "candidates", "dry_run": False}))
    assert all(c is False and m == probe.CANDIDATE_MAX_TOKENS for _, _, c, m in seen)
    assert out["probed"] == 3 and out["ok"] == 1 and out["failed"] == 2 and out["cost_usd"] == 0.00003
    rows = {m["model_key"]: m for m in out["matrix"]}
    assert rows["cand-a"]["ok"] is False and rows["cand-a"]["error_class"] == "access_denied"
    assert len(rows["cand-a"]["error"]) <= probe.MATRIX_ERROR_CHARS
    assert rows["cand-b"] == {"model_key": "cand-b", "connection_key": "acc3", "wire_id": "wire-cand-b", "ok": True,
                              "error_class": None, "error": None}
    assert rows["west-d"]["error_class"] == "unavailable" and rows["west-d"]["wire_id"] == "wire-west-d"
    assert stamp.await_args.args[1] == []                 # a candidate never stamps its connection as probed
    assert len(states.await_args.args[1]) == 3
    assert out["stage_coverage"] == {"ready": True, "ok": True}


@pytest.mark.asyncio
async def test_a_daily_probe_keeps_counting_on_the_breaker(monkeypatch):
    calls = []

    async def fake_test(pool, key, *, model_key, actor):          # the daily probe's call shape is unchanged
        calls.append(model_key)
        return {"ok": True, "error_class": None, "cost_usd": 0.0}

    monkeypatch.setattr(llm_connections, "test_connection", fake_test)
    monkeypatch.setattr(probe, "stamp_probed", AsyncMock())
    plan = {"probes": [{"connection_key": "acc3", "model_key": "routed-c", "wire_id": "w", "probe": "text"}]}
    results = await probe.run_probes(MagicMock(), plan)
    assert calls == ["routed-c"] and results[0]["candidate"] is False


@pytest.mark.asyncio
async def test_enqueue_manual_candidate_payload(monkeypatch):
    monkeypatch.setenv(probe.SWITCH_ENV, "on")
    enq = AsyncMock(return_value=("job-1", True))
    monkeypatch.setattr(probe, "enqueue", enq)
    await probe.enqueue_manual(MagicMock(), dry_run=False, actor="script:x", mode="candidates",
                               connection_keys=["acc3"])
    assert enq.await_args.args[2] == {"trigger": "manual", "dry_run": False, "requested_by": "script:x",
                                      "mode": "candidates", "connection_keys": ["acc3"], "model_keys": []}
    await probe.enqueue_manual(MagicMock(), dry_run=True, actor="admin:x")
    assert "mode" not in enq.await_args.args[2]                        # a daily-mode manual run: as before


# ── the deployment state write ─────────────────────────────────────────────────────────────────────────────────────

class _CM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def test_record_deployment_states_writes_each_reached_deployment_masked():
    db = MagicMock()
    db.executemany = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_CM(db))
    arn = "arn:aws:iam::123456789012:role/Some-Role"
    results = [
        {"model_key": "a", "connection_key": "acc3", "ok": True, "error_class": None, "probe": "text"},
        {"model_key": "b", "connection_key": "acc3", "ok": False, "error_class": "access_denied", "probe": "text",
         "error": f"AccessDenied for {arn}"},
        {"model_key": "c", "connection_key": "acc3", "ok": False, "error_class": "unavailable", "probe": "text"},
        {"model_key": "geocode-search", "connection_key": "geoapify", "ok": True, "probe": "geocode"},
        {"model_key": "e", "connection_key": "acc2", "ok": False, "error_class": "throttled", "probe": "embed",
         "error": "ThrottlingException"},
    ]
    assert asyncio.run(probe.record_deployment_states(pool, results)) == 3
    sql, rows = db.executemany.await_args.args
    assert "UPDATE shared.llm_model_deployment SET access_state = $3" in sql
    assert rows[0] == ("a", "acc3", "ok", None)
    assert rows[1][:3] == ("b", "acc3", "access_denied") and "123456789012" not in rows[1][3]
    assert rows[2] == ("e", "acc2", "throttled", "ThrottlingException")


def test_record_deployment_states_never_fails_the_probe():
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=RuntimeError("column access_state does not exist"))
    results = [{"model_key": "a", "connection_key": "acc3", "ok": True, "error_class": None, "probe": "text"}]
    assert asyncio.run(probe.record_deployment_states(pool, results)) == 0
    assert asyncio.run(probe.record_deployment_states(pool, [])) == 0


# ── the gateway: an uncounted probe ────────────────────────────────────────────────────────────────────────────────

def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        return LLMClient()


def _target():
    return VendorDeployment(_dep("cand-a", "acc3"), BUILTIN_CONNECTIONS["acc3"])


def test_an_uncounted_probe_skips_the_breaker_but_keeps_the_limiter():
    client = _client()
    held = []

    class _Slot:
        def __enter__(self):
            held.append("in")

        def __exit__(self, *exc):
            held.append("out")
            return False

    refused = RuntimeError("AccessDeniedException: not available for this account")
    with patch.object(breaker, "guarded", side_effect=AssertionError("counted")), \
            patch.object(breaker, "record_failure") as rec_fail, patch.object(breaker, "record_success") as rec_ok, \
            patch.object(breaker, "limit", return_value=_Slot()), \
            patch.object(client, "_dispatch_target", side_effect=refused):
        with pytest.raises(RuntimeError, match="not available"):
            client.probe(MagicMock(stage="adhoc_conn_test"), MODELS["cand-a"], _target(), count=False)
    rec_fail.assert_not_called() and rec_ok.assert_not_called()
    assert held == ["in", "out"]


def test_a_counted_probe_still_goes_through_the_breaker():
    client = _client()
    with patch.object(breaker, "guarded", return_value="resp") as guarded:
        assert client.probe(MagicMock(), MODELS["cand-a"], _target()) == "resp"
    assert guarded.call_args.kwargs["admit_call"] is False


def test_the_connection_test_passes_count_false_only_when_asked(monkeypatch):
    seen = []

    class FakeClient:
        def probe(self, request, model, target, *, count=True):
            seen.append((count, request.max_tokens))
            raise RuntimeError("AccessDeniedException: not available for this account")

    row = {"connection_key": "acc3"}
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=row)
    db.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _CM(db))
    monkeypatch.setattr(llm_connections.conn_registry, "_row_to_connection", lambda r: BUILTIN_CONNECTIONS["acc3"])
    monkeypatch.setattr(llm_connections.dep_registry, "fetch_deployments",
                        AsyncMock(return_value=[_dep("cand-a", "acc3")]))
    monkeypatch.setattr(llm_connections, "_testable", lambda conn, deps: [(deps[0], MODELS["cand-a"])])
    out = asyncio.run(llm_connections.test_connection(pool, "acc3", model_key="cand-a", actor="t",
                                                      client=FakeClient(), max_tokens=16, count_outcome=False))
    assert out["ok"] is False and out["error_class"] == "access_denied"
    out = asyncio.run(llm_connections.test_connection(pool, "acc3", model_key="cand-a", actor="t",
                                                      client=FakeClient()))
    assert seen == [(False, 16), (True, llm_connections.TEST_MAX_TOKENS)]
