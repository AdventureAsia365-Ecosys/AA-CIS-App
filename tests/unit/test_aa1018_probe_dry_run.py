"""AA-1018 (reliability part 1) — the daily LLM connection probe (services/jobs/llm_probe_job.py: off by default, a
dry run that lists deployments and the estimated cost with no provider call), its sampler part and admin route, the
OPS-12 digest provider section, and the Bedrock client settings from the connection row (bedrock_satellite)."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.jobs import llm_probe_job as probe
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.connections import BUILTIN_CONNECTIONS, ProviderConnection
from shared.llm_client.deployments import ModelDeployment
from shared.jobs.registry import NonRetryable, get_kind

T0 = datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)


def _model(key, p_in=1.0, p_out=5.0, style="converse", max_out=None):
    return CatalogModel(model_key=key, label=key, vendor="anthropic", provider="bedrock", api_style=style,
                        callable_via=("llm_client",), price_in_per_mtok=p_in, price_out_per_mtok=p_out,
                        max_output_tokens=max_out, enabled=True)


def _dep(model, conn, enabled=True, **kw):
    return ModelDeployment(model, conn, f"wire-{model}", 10, enabled, "seed", **kw)


ANTHROPIC = ProviderConnection(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key",
                               label="x", data_scope="vendor_direct", enabled=False,
                               secret_name="aa-llm/dev/anthropic-direct", base_url="https://api.anthropic.com")
BROKEN = ProviderConnection(connection_key="openai-other", kind="openai", auth_mode="api_key", label="x",
                            data_scope="vendor_direct", enabled=True)          # no secret, no env: a policy problem
CONNS = {"acc2": BUILTIN_CONNECTIONS["acc2"], "acc3": BUILTIN_CONNECTIONS["acc3"], "anthropic-direct": ANTHROPIC,
         "openai-other": BROKEN}
MODELS = {"writer-a": _model("writer-a", 3.0, 15.0), "judge-b": _model("judge-b", 0.1, 0.5),
          "free-c": _model("free-c", None, None), "embed-d": _model("embed-d", 0.12, 0.0, style="embed")}
DEPS = [_dep("writer-a", "acc3"), _dep("judge-b", "acc3"), _dep("free-c", "acc3"), _dep("judge-b", "acc2", False),
        _dep("embed-d", "acc2"), _dep("writer-a", "anthropic-direct")]


def _testable(conn, deployments):
    return [(d, MODELS[d.model_key]) for d in deployments if MODELS[d.model_key].api_style != "embed"]


def test_the_kind_is_single_attempt_background_and_gate_exempt():
    from shared.jobs.worker import load_kinds
    load_kinds()
    k = get_kind(probe.KIND)
    assert k.max_attempts == 1 and k.background and k.deploy_gate_exempt and not k.deploy_blocking


def test_the_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv(probe.SWITCH_ENV, raising=False)
    assert probe.schedule_enabled() is False
    monkeypatch.setenv(probe.SWITCH_ENV, "on")
    assert probe.schedule_enabled() is True


def test_the_plan_probes_enabled_text_deployments_and_says_why_others_are_skipped():
    plan = probe.build_plan(CONNS, DEPS, _testable)
    assert [(p["connection_key"], p["model_key"]) for p in plan["probes"]] == [
        ("acc3", "writer-a"), ("acc3", "judge-b"), ("acc3", "free-c")]
    why = {(s["connection_key"], s["model_key"]): s["why"] for s in plan["skipped"]}
    assert why[("acc2", "judge-b")] == "deployment disabled"
    assert why[("anthropic-direct", None)] == "disabled"
    assert why[("openai-other", None)].startswith("unconfigured: ")
    assert ("acc2", "embed-d") not in why                               # an embedding row is not a text test


def test_the_estimate_is_catalog_price_times_the_test_size():
    plan = probe.build_plan(CONNS, DEPS, _testable)
    est = {p["model_key"]: p["estimated_usd"] for p in plan["probes"]}
    assert est["writer-a"] == round((60 * 3.0 + 32 * 15.0) / 1e6, 6)
    assert est["free-c"] is None and plan["unpriced"] == 1
    assert plan["estimated_usd"] == round(est["writer-a"] + est["judge-b"], 6)
    assert plan["estimated_usd"] < 0.03                                  # the S262 bound for a daily run


def test_a_deployment_price_override_wins_and_a_small_output_cap_counts():
    dep = _dep("writer-a", "acc3", price_in_override=1.0, price_out_override=2.0)
    assert probe.estimate_usd(dep, _model("writer-a", 3.0, 15.0), 8) == round((60 + 16) / 1e6, 6)


def test_the_plan_is_capped(monkeypatch):
    monkeypatch.setattr(probe, "MAX_PROBES", 2)
    plan = probe.build_plan(CONNS, DEPS, _testable)
    assert len(plan["probes"]) == 2
    assert any(s["why"].startswith("over_limit") for s in plan["skipped"])


def _ctx(payload):
    ctx = MagicMock()
    ctx.payload = payload
    ctx.job_id = "j1"
    ctx.pool = MagicMock()
    return ctx


@pytest.mark.asyncio
async def test_a_dry_run_makes_no_call(monkeypatch):
    monkeypatch.delenv(probe.SWITCH_ENV, raising=False)
    monkeypatch.setattr(probe, "load_plan", AsyncMock(return_value=probe.build_plan(CONNS, DEPS, _testable)))
    runner = AsyncMock()
    monkeypatch.setattr(probe, "run_probes", runner)
    out = await probe.run(_ctx({"trigger": "manual"}))                  # manual defaults to a dry run
    assert out["dry_run"] is True and out["calls_made"] == 0 and len(out["planned"]) == 3
    assert out["switch_on"] is False and out["estimated_usd"] < 0.03
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_live_run_is_refused_while_the_switch_is_off(monkeypatch):
    monkeypatch.delenv(probe.SWITCH_ENV, raising=False)
    with pytest.raises(NonRetryable, match="LLM_DAILY_PROBE"):
        await probe.run(_ctx({"trigger": "manual", "dry_run": False}))
    with pytest.raises(PermissionError):
        await probe.enqueue_manual(MagicMock(), dry_run=False, actor="admin:x")


@pytest.mark.asyncio
async def test_a_live_run_probes_each_planned_deployment_through_the_connection_test(monkeypatch):
    monkeypatch.setenv(probe.SWITCH_ENV, "on")
    plan = probe.build_plan(CONNS, DEPS, _testable)
    monkeypatch.setattr(probe, "load_plan", AsyncMock(return_value=plan))
    answers = {"writer-a": {"ok": False, "error_class": "access_denied", "cost_usd": 0.0},
               "judge-b": {"ok": True, "error_class": None, "cost_usd": 0.00002}}

    async def fake_test(pool, key, *, model_key, actor):
        assert actor == probe.ACTOR
        if model_key == "free-c":
            raise LookupError("gone")
        return answers[model_key]
    from api.services import llm_connections
    monkeypatch.setattr(llm_connections, "test_connection", fake_test)
    out = await probe.run(_ctx({"trigger": "schedule"}))
    assert out["dry_run"] is False and out["probed"] == 3 and out["ok"] == 1 and out["failed"] == 2
    assert {r["model_key"]: r["error_class"] for r in out["results"]} == {
        "writer-a": "access_denied", "judge-b": None, "free-c": "unavailable"}
    assert out["cost_usd"] == 0.00002


@pytest.mark.asyncio
async def test_the_daily_enqueue_needs_the_switch_and_the_hour(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    enq = AsyncMock(return_value=("job-1", True))
    monkeypatch.setattr(probe, "enqueue", enq)
    monkeypatch.delenv(probe.SWITCH_ENV, raising=False)
    assert await probe.enqueue_daily(pool, now=T0) == (None, False, date(2026, 10, 31))
    monkeypatch.setenv(probe.SWITCH_ENV, "on")
    early = T0.replace(hour=probe.SCHEDULE_HOUR_UTC - 1)
    assert (await probe.enqueue_daily(pool, now=early))[1] is False
    assert await probe.enqueue_daily(pool, now=T0) == ("job-1", True, date(2026, 10, 31))
    assert enq.await_args.kwargs["idempotency_key"] == "llm_connection_probe:2026-10-31"
    assert enq.await_args.args[2] == {"trigger": "schedule", "dry_run": False}


@pytest.mark.asyncio
async def test_the_sampler_part_queues_nothing_while_off(monkeypatch):
    from api.core import ops_metrics_sampler as sampler
    monkeypatch.delenv(probe.SWITCH_ENV, raising=False)
    daily = AsyncMock()
    monkeypatch.setattr(probe, "enqueue_daily", daily)
    out: dict = {}
    await sampler._llm_probe_part(MagicMock(), {probe.KIND: object()}, T0, out)
    daily.assert_not_awaited()
    assert out == {}


@pytest.mark.asyncio
async def test_the_admin_route_queues_a_dry_run_by_default(monkeypatch):
    from api.routers import admin_job_runner as r
    monkeypatch.setattr(r, "verify_admin_secret", lambda s: None)
    monkeypatch.delenv(probe.SWITCH_ENV, raising=False)
    enq = AsyncMock(return_value=("job-9", True))
    monkeypatch.setattr(probe, "enqueue", enq)
    req = MagicMock()
    out = await r.llm_probe_run(r.LlmProbeRunRequest(), req, x_admin_secret="s", x_admin_user_id="u1")
    assert out == {"job_id": "job-9", "kind": probe.KIND, "dry_run": True, "switch_on": False}
    assert enq.await_args.args[2]["dry_run"] is True
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        await r.llm_probe_run(r.LlmProbeRunRequest(dry_run=False), req, x_admin_secret="s", x_admin_user_id="u1")
    assert e.value.status_code == 409


# ── OPS-12 digest: the provider section ────────────────────────────────────────────────────────────────────────────

def _conn_entry(key, enabled=True, state="closed", cls=None, refusing=False, until=None):
    return {"connection_key": key, "kind": "bedrock", "enabled": enabled, "status": "open" if cls else "ok",
            "failure_domain": f"bedrock:{key}:aws_only", "open_models": 2 if cls else 0, "last_ok_at": None,
            "circuit": {"model_key": "*", "state": state, "refusing": refusing, "error_class": cls,
                        "open_until": until, "models": []}}


def test_provider_rows_and_attention_lines():
    from services.reports import digest
    llm = {"connections": digest._provider_rows([
        _conn_entry("acc3", state="open", cls="access_denied", refusing=True, until="2026-10-31T06:05:00+00:00"),
        _conn_entry("anthropic-direct", enabled=False, state="half_open", cls="credential", refusing=True),
        _conn_entry("acc2")]),
        "probe": {"status": "succeeded", "dry_run": False, "probed": 3, "ok": 1, "failed": 2, "finished_at": "t",
                  "failures": [{"connection_key": "acc3", "model_key": "writer-a", "error_class": "access_denied"}]},
        "invariant": {"ready": True, "ok": False, "stages_checked": 4, "breaches": 3, "domains": ["d1"],
                      "classes": [{"role": "judge", "stages": 1, "ok": 1, "breaches": []},
                                  {"role": "writer", "stages": 3, "ok": 0, "breaches": ["a", "b", "c"]}]}}
    items = digest.provider_attention(llm)
    assert items[0] == ("LLM connection acc3 paused (access_denied, circuit open until "
                        "2026-10-31T06:05:00+00:00)")
    assert not any("anthropic-direct" in i for i in items)               # switched off: not an attention item
    assert items[1].startswith("Daily LLM probe: 2 of 3 deployment(s) failed: acc3/writer-a (access_denied)")
    assert items[2] == ("Failure-domain rule (report only): 3 stage(s) have no enabled, funded fallback in another "
                        "failure domain (writer 3 of 3)")
    md = "\n".join(digest.render_providers(llm))
    assert "### Providers" in md and "| acc3 | bedrock | yes | open | open (access_denied, refusing) |" in md
    assert "Daily probe (live, succeeded, t): 3 deployment(s), 1 answered, 2 failed." in md
    assert "4 stage(s) checked, 3 without a fallback" in md


def test_an_old_digest_without_connections_renders_as_before():
    from services.reports import digest
    assert digest.render_providers({"status": "ok"}) == [] and digest.provider_attention({"status": "ok"}) == []


def test_no_probe_run_says_the_schedule_is_off():
    from services.reports import digest
    md = digest.render_providers({"connections": [], "probe": None, "invariant": {"ready": False}})
    assert any("no run in the last 24 h" in line for line in md)


@pytest.mark.asyncio
async def test_load_llm_carries_connections_invariant_and_the_last_probe(monkeypatch):
    from api.services import llm_availability
    from services.reports import digest
    monkeypatch.setattr(llm_availability, "read", AsyncMock(return_value={
        "status": "blocked", "last_ok_at": None, "cause": None, "providers": [], "failures": [],
        "connections": [_conn_entry("acc3", state="open", cls="access_denied", refusing=True)],
        "invariant": {"ready": True, "ok": False, "stages_checked": 2, "breaches": 2, "classes": [], "domains": [],
                      "stages": ["dropped from the digest"]}}))
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": "j1", "status": "succeeded", "finished_at": T0,
                                            "result": '{"dry_run": true, "probed": 0, "ok": 0, "failed": 0}'})
    out = await digest.load_llm(conn, T0)
    assert out["connections"][0]["refusing"] is True and out["connections"][0]["circuit"] == "open"
    assert out["invariant"]["breaches"] == 2 and "stages" not in out["invariant"]
    assert out["probe"]["dry_run"] is True and out["probe"]["job_id"] == "j1"
    assert "llm_connection_probe" in conn.fetchrow.await_args.args[0]


# ── Bedrock client settings from the connection row ────────────────────────────────────────────────────────────────

def test_a_task_role_runtime_keeps_todays_config_when_the_row_is_null(monkeypatch):
    from shared.llm_client import bedrock_satellite as bs
    built = []
    monkeypatch.setattr(bs.boto3, "client", lambda *a, **kw: built.append(kw) or SimpleNamespace())
    monkeypatch.setattr(bs, "_native_runtimes", {})
    bs.native_runtime(BUILTIN_CONNECTIONS["acc2"])
    cfg = built[0]["config"]
    assert cfg.read_timeout == 300 and cfg.connect_timeout == 10
    assert cfg.retries == {"total_max_attempts": 2, "mode": "standard"}
    bs.native_runtime(BUILTIN_CONNECTIONS["acc2"], read_timeout=30, max_attempts=1)     # the embed client
    assert built[1]["config"].read_timeout == 30 and built[1]["config"].retries["total_max_attempts"] == 1


def test_a_task_role_runtime_takes_the_rows_timeout_and_retries(monkeypatch):
    from dataclasses import replace

    from shared.llm_client import bedrock_satellite as bs
    built = []
    monkeypatch.setattr(bs.boto3, "client", lambda *a, **kw: built.append(kw) or SimpleNamespace())
    monkeypatch.setattr(bs, "_native_runtimes", {})
    bs.native_runtime(replace(BUILTIN_CONNECTIONS["acc2"], timeout_s=120, max_retries=0))
    cfg = built[0]["config"]
    assert cfg.read_timeout == 120 and cfg.retries["total_max_attempts"] == 1
    bs.native_runtime(replace(BUILTIN_CONNECTIONS["acc2"], timeout_s=None, max_retries=3), read_timeout=30)
    assert built[1]["config"].read_timeout == 30 and built[1]["config"].retries["total_max_attempts"] == 4


def test_the_satellite_runtime_has_no_config_when_the_row_is_null(monkeypatch):
    from shared.llm_client import bedrock_satellite as bs
    session = MagicMock()
    monkeypatch.setattr(bs, "_get_satellite_session", lambda account: session)
    monkeypatch.setattr(bs, "satellite_connection", lambda account: BUILTIN_CONNECTIONS["acc3"])
    bs.get_satellite_client("bedrock-runtime", account="acc3")
    assert "config" not in session.client.call_args.kwargs                # byte for byte today's client


def test_the_satellite_runtime_takes_the_rows_settings_but_not_the_control_plane(monkeypatch):
    from dataclasses import replace

    from shared.llm_client import bedrock_satellite as bs
    session = MagicMock()
    conn = replace(BUILTIN_CONNECTIONS["acc3"], timeout_s=90, max_retries=1)
    monkeypatch.setattr(bs, "_get_satellite_session", lambda account: session)
    monkeypatch.setattr(bs, "satellite_connection", lambda account: conn)
    bs.get_satellite_client("bedrock-runtime", account="acc3")
    cfg = session.client.call_args.kwargs["config"]
    assert cfg.read_timeout == 90 and cfg.retries == {"total_max_attempts": 2, "mode": "standard"}
    bs.get_satellite_client("bedrock", account="acc3")                    # Batch control plane: unchanged
    assert "config" not in session.client.call_args.kwargs
