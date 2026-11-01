"""AA-642 A1 (gaps CIS-2, CIS-4, OPS-11 stage part) — the stage kill switch and the declared degrade mode.

- ``llm_role_config.is_active`` false = switched off: the gateway (chat and embed) refuses every call of the stage
  before any provider attempt, with one 0 USD ledger row (status refused, error_class disabled); an explicit
  ``model_tier`` is refused too, and the tenant budget is not even read.
- The refusal is an LLMAccessDenied: the job fails without a retry, its error names the stage, the availability card
  does not read it as an account block, and the bulk re-queue holds it while the stage is still off.
- The failure-domain report: a stage with a declared degrade mode and no second domain is ``degrade`` (not a breach);
  a switched-off stage is listed apart.
- The admin switch: PATCH /admin/llm-config/{stage}/switch (ai.admin), a reason when switching off, audited in the
  same transaction.
No database, no AWS, no vendor: role_config rows, the job pool and the ledger writes are fakes. Invented stages only.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from api.core import staff_authz
from api.routers import admin_llm_ops as ops
from api.services import llm_availability
from shared.jobs import queue
from shared.jobs.worker import failure_outcome
from shared.llm_client import call_log, invariant, tenant_budget
from shared.llm_client import embed as embed_mod
from shared.llm_client import role_config as rc_mod
from shared.llm_client.client import attempt_outcome
from shared.llm_client.errors import (DISABLED, LLMAccessDenied, LLMStageDisabled, classify_text, disabled_stage_of,
                                      is_llm_access_denied)
from shared.llm_client.role_config import SAFE_DEFAULTS, StageConfig
from tests.unit.test_aa1002_s3_deployment_routing import World

REPO = Path(__file__).resolve().parents[2]
MIGRATION_326 = REPO / "api" / "migrations" / "326_llm_stage_switch_degrade_mode.sql"


@pytest.fixture
def world(monkeypatch):
    from shared.llm_client import catalog, vendor_route
    hook = vendor_route.circuit_open
    w = World(monkeypatch)
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


@pytest.fixture
def ledger(monkeypatch):
    rows: list = []

    async def record_calls(rows_):
        rows.extend(rows_)
    monkeypatch.setattr(call_log, "record_calls", lambda rows: record_calls(rows))
    return rows


def _stage(monkeypatch, stage, *, enabled=True, reason=None, model="gpt-6-luna"):
    cfg = StageConfig(stage=stage, role="writer", provider="claude", model_id=model, account_route="acc3",
                      enabled=enabled, disabled_reason=reason)
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda s: cfg)
    return cfg


# ── role_config: the row → StageConfig ──────────────────────────────────────────────────────────────────────────────

def _row(**kw):
    row = {"stage": "t9_write", "role": "writer", "provider": "claude", "model_id": "sonnet-5-5",
           "account_route": "acc3", "fallback_model_ids": [], "shadow_model_id": None, "shadow_sample_pct": 0}
    row.update(kw)
    return row


def test_an_inactive_row_is_a_switched_off_stage_not_the_built_in_route():
    cfg = rc_mod._row_to_config(_row(is_active=False, disabled_reason="provider drill", degrade_mode="queue_retry"))
    assert (cfg.enabled, cfg.disabled_reason, cfg.degrade_mode) == (False, "provider drill", "queue_retry")
    assert cfg.model_id == "sonnet-5-5"                     # the stored route, never SAFE_DEFAULTS


def test_a_row_before_migration_326_is_on_with_no_degrade_mode():
    cfg = rc_mod._row_to_config(_row())
    assert (cfg.enabled, cfg.disabled_reason, cfg.degrade_mode) == (True, None, None)
    assert SAFE_DEFAULTS["t9_write"].enabled is True        # a stage with no row runs


def test_the_stage_read_no_longer_filters_inactive_rows():
    seen: list = []

    class Conn:
        async def fetchrow(self, sql, *args):
            seen.append(sql)
            return _row(is_active=False, degrade_mode=None, disabled_reason="x")

        async def close(self):
            pass
    with patch.object(rc_mod.asyncpg, "connect", AsyncMock(return_value=Conn())), \
            patch.object(rc_mod, "get_database_url", return_value="postgresql://x"):
        cfg = asyncio.run(rc_mod._fetch_one("t9_write"))
    assert cfg.enabled is False
    assert "AND is_active" not in seen[0] and "is_active" in seen[0] and "degrade_mode, disabled_reason" in seen[0]


# ── the gateway: refused before any attempt ─────────────────────────────────────────────────────────────────────────

def test_a_switched_off_stage_is_refused_before_any_attempt_with_a_0_usd_row(world, monkeypatch, ledger):
    _stage(monkeypatch, "t9_write", enabled=False, reason="provider drill")
    budget = MagicMock()
    monkeypatch.setattr(tenant_budget, "check", budget)
    with pytest.raises(LLMStageDisabled) as caught:
        world.generate(stage="t9_write")
    assert world.calls == [] and budget.call_count == 0     # no provider attempt, no budget read
    assert caught.value.stage == "t9_write" and "provider drill" in str(caught.value)
    call_log.wait_for_pending_writes()
    assert len(ledger) == 1
    row = ledger[0]
    assert (row["status"], row["error_class"], row["cost_usd"]) == ("refused", DISABLED, 0.0)
    assert row["stage"] == "t9_write" and row["model"] == "gpt-6-luna" and row["attempt_no"] is None


def test_an_explicit_model_tier_is_refused_too(world, monkeypatch, ledger):
    _stage(monkeypatch, "t9_write", enabled=False, reason="maintenance")
    with pytest.raises(LLMStageDisabled):
        world.generate(stage="t9_write", model_tier="gpt-6-luna")
    assert world.calls == []


def test_a_stage_switched_on_again_calls_as_before(world, monkeypatch, ledger):
    _stage(monkeypatch, "t9_write", enabled=True)
    resp = world.generate(stage="t9_write")
    assert resp.content == "from acc3" and len(world.calls) == 1


def test_the_refusal_outcome_and_the_job_failure():
    exc = LLMStageDisabled("t9_write", "maintenance window")
    assert isinstance(exc, LLMAccessDenied) and is_llm_access_denied(exc)
    assert attempt_outcome(exc) == ("refused", DISABLED)
    assert attempt_outcome(RuntimeError("wrapped")) != ("refused", DISABLED)
    error, retry, reason = failure_outcome(exc)
    assert retry is False and reason == "llm_access_denied"
    assert error.startswith("llm_access_denied: stage 't9_write' is switched off by an admin: maintenance window")


def test_the_stored_job_error_names_the_stage_and_classifies_as_disabled():
    error = str(LLMStageDisabled("t2_generate", "AccessDenied quota drill"))   # the reason may say anything
    assert disabled_stage_of(error) == "t2_generate"
    assert classify_text(error) == DISABLED
    assert disabled_stage_of("llm_access_denied: Error 002") is None
    assert disabled_stage_of(None) is None


def test_the_availability_card_does_not_read_a_switched_off_stage_as_an_account_block():
    at = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    jobs = [{"id": "j1", "kind": "t9_write", "status": "failed", "failed_at": at,
             "error": str(LLMStageDisabled("t9_write", "drill"))}]
    assert llm_availability.summarize_failures(jobs) == []


# ── embeddings ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_switched_off_embed_stage_is_refused_before_any_attempt(monkeypatch, ledger):
    import dataclasses
    cfg = dataclasses.replace(SAFE_DEFAULTS["f10_embed"], enabled=False, disabled_reason="re-embed pause")
    runtime = MagicMock()
    with patch.object(embed_mod, "get_stage_config_sync", return_value=cfg), \
            patch.object(embed_mod, "_runtime_for", return_value=runtime):
        with pytest.raises(LLMStageDisabled):
            embed_mod.embed("f10_embed", ["a quiet lake"])
    assert runtime.invoke_model.call_count == 0
    call_log.wait_for_pending_writes()
    assert len(ledger) == 1
    assert (ledger[0]["status"], ledger[0]["error_class"], ledger[0]["role"]) == ("refused", DISABLED, "embed")
    assert ledger[0]["cost_usd"] == 0.0


# ── the bulk re-queue ───────────────────────────────────────────────────────────────────────────────────────────────

OFF = str(LLMStageDisabled("t9_write", "drill"))
KEYS = ["acc2", "acc3", "openai-direct"]


def test_a_switched_off_job_waits_only_for_its_stage():
    assert queue.llm_hold(OFF, {}, KEYS, frozenset({"t9_write"})) == "stage still switched off: t9_write"
    assert queue.llm_hold(OFF, {}, KEYS, frozenset({"t2_generate"})) is None
    # a blocked connection does not hold it: the switch, not the account, stopped it
    assert queue.llm_hold(OFF, {"acc3": "access_denied"}, KEYS, frozenset()) is None


T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class FakePool:
    def __init__(self, jobs, off=()):
        self.jobs, self.off = jobs, list(off)
        self.updates: list = []
        self.switch_reads = 0

    async def fetch(self, sql, *args):
        if "FROM shared.job j" in sql:
            return self.jobs
        if sql == queue._DISABLED_STAGES_SQL:
            self.switch_reads += 1
            return [{"stage": s} for s in self.off]
        if "shared.llm_provider_health" in sql:
            return []
        if "shared.llm_provider_connection" in sql:
            return [{"connection_key": k} for k in KEYS]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        self.updates.append(args)
        return {"id": args[0]}


def _job(i, error):
    return {"id": f"00000000-0000-0000-0000-00000000000{i}", "kind": "t9_write", "payload": "{}", "error": error,
            "finished_at": T0}


@pytest.fixture
def kinds(monkeypatch):
    from shared.jobs import registry
    monkeypatch.setattr(registry, "get_kind", lambda k: MagicMock(retry_refusal=None))
    monkeypatch.setattr("shared.jobs.worker.load_kinds", lambda: None)


def test_the_re_queue_holds_jobs_while_the_stage_is_off(kinds):
    pool = FakePool([_job(1, OFF)], off=["t9_write"])
    out = asyncio.run(queue.retry_by_reason(pool, since=T0 - timedelta(days=1), dry_run=False))
    assert out["eligible"] == 0 and out["held_count"] == 1 and pool.updates == []
    assert out["held"][0]["held"] == "stage still switched off: t9_write"


def test_the_re_queue_releases_them_once_the_stage_is_on(kinds):
    pool = FakePool([_job(1, OFF)], off=[])
    out = asyncio.run(queue.retry_by_reason(pool, since=T0 - timedelta(days=1), dry_run=False, rand=lambda: 0.0))
    assert out["requeued"] == 1


def test_the_switch_state_is_read_only_when_a_candidate_was_switched_off(kinds):
    pool = FakePool([_job(1, "llm_access_denied: Error 002 for this account")], off=["t9_write"])
    asyncio.run(queue.retry_by_reason(pool, since=T0 - timedelta(days=1)))
    assert pool.switch_reads == 0


# ── the failure-domain report ───────────────────────────────────────────────────────────────────────────────────────

CONNS = [{"connection_key": "acc3", "kind": "bedrock", "enabled": True, "data_scope": "aws_only",
          "account_id": "<account>", "account_id_env": None}]
DEPS = [{"model_key": "writer-x", "connection_key": "acc3", "priority": 1, "enabled": True}]


def _route(stage, role="writer", model="writer-x", degrade=None, active=True):
    return {"stage": stage, "role": role, "model_id": model, "fallback_model_ids": [],
            "allowed_scopes": ["aws_only"], "is_active": active, "degrade_mode": degrade}


def _status(report, stage):
    return next(s for s in report["stages"] if s["stage"] == stage)


def test_a_declared_degrade_mode_is_not_a_breach():
    report = invariant.check([_route("t_one", degrade="queue_retry"), _route("t_two"),
                              _route("t_off", active=False)], [], DEPS, CONNS)
    one, two = _status(report, "t_one"), _status(report, "t_two")
    assert (one["status"], one["ok"], one["degrade_mode"]) == ("degrade", True, "queue_retry")
    assert (two["status"], two["ok"], two["degrade_mode"]) == ("breach", False, None)
    assert report["breaches"] == 1 and report["declared_degrade"] == 1 and report["stages_checked"] == 2
    assert report["disabled"] == ["t_off"]
    writer = report["classes"][0]
    assert writer["breaches"] == ["t_two"] and writer["degrade"] == ["t_one"]


def test_a_degrade_mode_never_excuses_a_stage_that_cannot_run_at_all():
    """Review S263: ADR 0009 d5 lets a degrade mode stand in for the SECOND failure domain only. A stage with no usable
    deployment (route model missing, or its only connection unfunded) is a breach whatever it declares."""
    blocked_conn = {"acc3": "quota_exhausted"}
    for routes, blocked in (([_route("t_three", model="nothing-here", degrade="basic_narration")], {}),
                            ([_route("t_three", degrade="queue_retry")], blocked_conn)):
        report = invariant.check(routes, [], DEPS, CONNS, blocked)
        three = _status(report, "t_three")
        assert (three["status"], three["ok"], three["primary"]) == ("breach", False, None)
        assert three["degrade_mode"] is not None                         # still shown, but not as a pass
        assert report["breaches"] == 1 and report["declared_degrade"] == 0 and report["ok"] is False
        assert report["classes"][0]["breaches"] == ["t_three"] and report["classes"][0]["degrade"] == []


def test_the_report_reads_degrade_modes_and_falls_back_before_migration_326():
    import asyncpg

    class Conn:
        def __init__(self, missing):
            self.missing, self.sql = missing, []

        async def fetch(self, sql, *args):
            self.sql.append(sql)
            if self.missing and "degrade_mode" in sql:
                raise asyncpg.exceptions.UndefinedColumnError("column \"degrade_mode\" does not exist")
            return []
    for missing in (False, True):
        conn = Conn(missing)
        asyncio.run(invariant.read(conn, blocked={}))
        assert conn.sql[0] == invariant.ROUTES_V3_SQL
        assert (invariant.ROUTES_SQL in conn.sql) is missing


# ── the admin switch ────────────────────────────────────────────────────────────────────────────────────────────────

def test_switching_off_needs_a_reason_switching_on_clears_it():
    with pytest.raises(ValidationError):
        ops.LlmStageSwitch(enabled=False)
    with pytest.raises(ValidationError):
        ops.LlmStageSwitch(enabled=False, reason="  ")
    with pytest.raises(ValidationError):
        ops.LlmStageSwitch(enabled=True, extra="x")
    assert ops.LlmStageSwitch(enabled=False, reason=" drill ").reason == "drill"
    assert ops.LlmStageSwitch(enabled=True).reason is None


def test_the_switch_route_is_admin_only():
    assert staff_authz.POLICY[("PATCH", "/admin/llm-config/{stage}/switch")] == staff_authz.AI_ADMIN


def test_the_switch_route_calls_the_store_and_maps_an_unknown_stage_to_404():
    from fastapi import HTTPException
    stored = {"stage": "t9_write", "is_active": False, "disabled_reason": "drill", "degrade_mode": "queue_retry",
              "updated_at": T0, "updated_by": "admin:a1"}
    setter = AsyncMock(return_value=dict(stored))
    with patch.object(ops, "verify_admin_secret"), patch.object(ops, "set_stage_enabled", setter):
        out = asyncio.run(ops.patch_llm_stage_switch("t9_write", ops.LlmStageSwitch(enabled=False, reason="drill"),
                                                     x_admin_secret="s", x_admin_user_id="a1"))
    assert out["is_active"] is False and out["updated_at"] == T0.isoformat()
    assert setter.call_args.kwargs == {"enabled": False, "reason": "drill", "updated_by": "admin:a1",
                                       "audit_actor": "admin:a1"}
    with patch.object(ops, "verify_admin_secret"), \
            patch.object(ops, "set_stage_enabled", AsyncMock(side_effect=ValueError("unknown stage: 'nope'"))):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(ops.patch_llm_stage_switch("nope", ops.LlmStageSwitch(enabled=True),
                                                   x_admin_secret="s", x_admin_user_id="a1"))
    assert caught.value.status_code == 404


class _Txn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _conn(before, after):
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_Txn())
    conn.fetchrow = AsyncMock(side_effect=[before, after])
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    return conn


@pytest.mark.parametrize("enabled,reason,stored,action", [
    (False, "  provider drill ", "provider drill", "llm_stage_switched_off"),
    (True, "ignored when on", None, "llm_stage_switched_on"),
])
def test_the_store_updates_and_audits_in_one_transaction(enabled, reason, stored, action):
    before = {"is_active": not enabled, "disabled_reason": None if not enabled else "old"}
    after = {"stage": "t9_write", "is_active": enabled, "disabled_reason": stored, "degrade_mode": "queue_retry",
             "updated_at": None, "updated_by": "admin:a1"}
    conn = _conn(before, after)
    with patch.object(rc_mod.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(rc_mod, "get_database_url", return_value="postgresql://x"), \
            patch.object(rc_mod, "invalidate") as invalidate:
        out = asyncio.run(rc_mod.set_stage_enabled("t9_write", enabled=enabled, reason=reason,
                                                   updated_by="admin:a1", audit_actor="admin:a1"))
    assert out["is_active"] is enabled
    conn.transaction.assert_called_once()
    update_args = conn.fetchrow.call_args_list[1].args
    assert "SET is_active = $2, disabled_reason = $3" in update_args[0]
    assert update_args[1:] == ("t9_write", enabled, stored, "admin:a1")
    audit = conn.execute.call_args.args
    assert "acp_shared.audit_log" in audit[0] and audit[2] == action and audit[4] == "t9_write"
    assert json.loads(audit[5])["after"] == {"is_active": enabled, "disabled_reason": stored}
    invalidate.assert_called_once_with("t9_write")


def test_the_store_refuses_an_unknown_stage():
    conn = _conn(None, None)
    with patch.object(rc_mod.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(rc_mod, "get_database_url", return_value="postgresql://x"), \
            patch.object(rc_mod, "invalidate"):
        with pytest.raises(ValueError):
            asyncio.run(rc_mod.set_stage_enabled("nope", enabled=True, reason=None, updated_by="u", audit_actor="a"))


# ── the ledger class and its CHECK ──────────────────────────────────────────────────────────────────────────────────

def _newest_error_class_check() -> Path:
    """The newest migration that (re)defines shared.llm_call_log's error_class CHECK (326, then 338 for AA-642 A5)."""
    files = sorted((p for p in (REPO / "api" / "migrations").glob("[0-9]*.sql")
                    if "llm_call_log_error_class_check CHECK" in p.read_text()),
                   key=lambda p: int(p.name.split("_", 1)[0]))
    assert files and int(files[0].name.split("_", 1)[0]) <= 326
    return files[-1]


def test_every_ledger_error_class_is_allowed_by_the_newest_check():
    """Review S263: the code writes 'role_not_served' (AA-642 A5), so the code PR merges after migration 338 (MIG-4
    order); this test fails until that file is on main."""
    sql = _newest_error_class_check().read_text()
    check = re.search(r"error_class IN \(([^)]*)\)", sql, re.S).group(1)
    allowed = set(re.findall(r"'([a-z_]+)'", check))
    assert set(call_log.ERROR_CLASSES) <= allowed and DISABLED in allowed
    assert "disabled" in set(re.findall(r"'([a-z_]+)'", MIGRATION_326.read_text()))
