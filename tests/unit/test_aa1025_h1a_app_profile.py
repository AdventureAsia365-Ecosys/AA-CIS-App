"""AA-1025 H1a (root ADR 0009 decision 4) — the app profile: AI on / off, bulk paused, default daily budget and
degrade mode per app (shared.llm_app_profile, CIS 345), and the CIS gateway's refusal when its app has AI off.

- The profile row → AppProfile; an unknown app, a bad degrade mode or an unreadable table never switch AI off (fail
  open, like the Jev and connection switches).
- AI off for app cis: ``generate`` (chat), the admin's connection test (``probe``), ``embed`` and Jev ``decide`` are
  refused before any provider attempt, each with one 0 USD ledger row (status refused, error_class disabled); the
  refusal is an LLMStageDisabled, so the job fails without a retry, names the stage (the T2 / T9 hold) and the bulk
  re-queue holds it while the app's AI is off.
- The admin: GET / PATCH /admin/ai-gateway/app-profiles (spend.read / ai.admin), a reason when switching off, the
  page-access hint ``app_profiles``.
No database, no AWS, no vendor: fakes only; invented stages and texts.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from api.core import staff_authz
from api.routers import admin_ai_gateway as gw_router
from shared.jobs import queue
from shared.jobs.worker import failure_outcome
from shared.llm_client import app_profile, call_log
from shared.llm_client import decide as d
from shared.llm_client import embed as embed_mod
from shared.llm_client.client import attempt_outcome
from shared.llm_client.errors import DISABLED, LLMStageDisabled, classify_text, disabled_stage_of
from shared.llm_client.role_config import SAFE_DEFAULTS, StageConfig
from tests.unit.test_aa1002_s3_deployment_routing import World

OFF_CIS = app_profile.AppProfile(app="cis", ai_enabled=False, disabled_reason="provider drill")


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


@pytest.fixture
def cis_off(monkeypatch):
    app_profile.invalidate()
    monkeypatch.setattr(app_profile, "loader", lambda: {"cis": OFF_CIS})

    async def _async(db):
        return {"cis": OFF_CIS}
    monkeypatch.setattr(app_profile, "loader_async", _async)
    yield
    app_profile.invalidate()


def _stage(monkeypatch, stage, model="gpt-6-luna"):
    cfg = StageConfig(stage=stage, role="writer", provider="claude", model_id=model, account_route="acc3")
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda s: cfg)
    return cfg


# ── the row ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_row_parses_from_jsonb_text_or_dict():
    p = app_profile.profile_of('{"app": "crm", "ai_enabled": false, "bulk_paused": true, '
                               '"default_daily_budget_usd": "2.50000", "degrade_mode": "manual_reply_needed", '
                               '"disabled_reason": "drill", "updated_by": "admin:x"}')
    assert (p.app, p.ai_enabled, p.bulk_paused, p.default_daily_budget_usd, p.degrade_mode) == \
        ("crm", False, True, 2.5, "manual_reply_needed")
    assert app_profile.profile_of({"app": "portal"}) is None            # only the four apps
    assert app_profile.profile_of("not json") is None
    odd = app_profile.profile_of({"app": "cis", "degrade_mode": "shrug", "ai_enabled": None})
    assert odd.degrade_mode == "queue_retry" and odd.ai_enabled is True  # never off by a bad value


def test_no_row_or_an_unreadable_table_is_ai_on(monkeypatch):
    app_profile.invalidate()
    assert app_profile.ai_off() is None                                  # conftest: no rows
    app_profile.invalidate()

    def boom():
        raise RuntimeError("database is down")
    monkeypatch.setattr(app_profile, "loader", boom)
    assert app_profile.ai_off() is None and app_profile.refusal("t9_write") is None
    app_profile.invalidate()


def test_the_env_switch_skips_the_check(cis_off, monkeypatch):
    assert app_profile.ai_off() == OFF_CIS
    monkeypatch.setenv(app_profile.SWITCH_ENV, "off")
    assert app_profile.ai_off() is None


def test_only_the_gateway_app_is_checked(monkeypatch):
    app_profile.invalidate()
    crm_off = app_profile.AppProfile(app="crm", ai_enabled=False, disabled_reason="crm drill")
    monkeypatch.setattr(app_profile, "loader", lambda: {"crm": crm_off})
    assert app_profile.refusal("t9_write") is None                       # this gateway is app cis
    assert app_profile.ai_off("crm") == crm_off
    app_profile.invalidate()


def test_the_cache_reads_once_per_ttl(monkeypatch):
    app_profile.invalidate()
    reads = []
    monkeypatch.setattr(app_profile, "loader", lambda: reads.append(1) or {"cis": OFF_CIS})
    for _ in range(5):
        app_profile.refusal("t9_write")
    assert len(reads) == 1
    app_profile.invalidate()


# ── the refusal ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_refusal_is_a_stage_switch_refusal_for_the_job_runner():
    exc = app_profile.LLMAppDisabled("cis", "t9_write", "provider drill")
    assert isinstance(exc, LLMStageDisabled) and exc.app == "cis" and exc.stage == "t9_write"
    assert attempt_outcome(exc) == ("refused", DISABLED)
    error, retry, reason = failure_outcome(exc)
    assert retry is False and reason == "llm_access_denied"
    assert disabled_stage_of(error) == "t9_write" and classify_text(error) == DISABLED
    assert "AI is off for app 'cis'" in error and "provider drill" in error


def test_ai_off_refuses_a_chat_call_before_any_attempt(world, monkeypatch, ledger, cis_off):
    _stage(monkeypatch, "t9_write")
    budget = MagicMock()
    monkeypatch.setattr("shared.llm_client.client.tenant_budget.check", budget)
    with pytest.raises(app_profile.LLMAppDisabled) as caught:
        world.generate(stage="t9_write")
    assert world.calls == [] and budget.call_count == 0                  # no provider attempt, no budget read
    assert caught.value.stage == "t9_write"
    call_log.wait_for_pending_writes()
    assert len(ledger) == 1
    row = ledger[0]
    assert (row["status"], row["error_class"], row["cost_usd"]) == ("refused", DISABLED, 0.0)
    assert row["stage"] == "t9_write" and row["model"] == "gpt-6-luna"


def test_ai_on_calls_as_before(world, monkeypatch, ledger):
    _stage(monkeypatch, "t9_write")
    resp = world.generate(stage="t9_write")
    assert resp.content == "from acc3" and len(world.calls) == 1


def test_ai_off_refuses_the_connection_test(monkeypatch, ledger, cis_off):
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest
    client = LLMClient()
    target = MagicMock()
    monkeypatch.setattr(client, "_call_target", MagicMock(side_effect=AssertionError("must not be called")))
    model = MagicMock(model_key="nova-lite")
    with pytest.raises(app_profile.LLMAppDisabled):
        client.probe(LLMRequest(system_prompt="s", user_prompt="u", max_tokens=8, stage="adhoc_conn_test"),
                     model, target)
    call_log.wait_for_pending_writes()
    assert len(ledger) == 1
    assert (ledger[0]["status"], ledger[0]["error_class"], ledger[0]["role"], ledger[0]["model"]) == \
        ("refused", DISABLED, "validate", "nova-lite")


def test_ai_off_refuses_an_embed_call(monkeypatch, ledger, cis_off):
    runtime = MagicMock()
    with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
            patch.object(embed_mod, "_runtime_for", return_value=runtime):
        with pytest.raises(app_profile.LLMAppDisabled):
            embed_mod.embed("f10_embed", ["a quiet lake"])
    assert runtime.invoke_model.call_count == 0
    call_log.wait_for_pending_writes()
    assert len(ledger) == 1
    assert (ledger[0]["status"], ledger[0]["error_class"], ledger[0]["role"]) == ("refused", DISABLED, "embed")


def _jev_conn():
    q = d.Question(key="kw_fits", stage="a3_research", kind="noul", instructions="Does it fit?", criteria=None,
                   mode="enforce", accept_floor=0.8, reject_ceiling=0.2, threshold_version=1)
    conn = MagicMock()

    async def fetch(sql, *args):
        if "decision_question" in sql:
            return [{"question_key": q.key, "stage": q.stage, "kind": q.kind, "instructions": q.instructions,
                     "criteria": q.criteria, "mode": q.mode, "accept_floor": q.accept_floor,
                     "reject_ceiling": q.reject_ceiling, "threshold_version": q.threshold_version,
                     "question_hash": "h-" + q.key}]
        return []
    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.executemany = AsyncMock()
    return conn


def test_ai_off_skips_jev_with_a_refused_row(cis_off):
    d._questions_loaded_at = 0.0
    d.reset_decision_memo()
    conn = _jev_conn()
    with patch.object(d, "_call_jev", new=AsyncMock()) as m_call, \
            patch.object(d, "record_call_with_pool", new=AsyncMock()) as m_log:
        dec = asyncio.run(d._decide(d._SingleConn(conn), "a3_research", "kw:XX:lagoon", "lagoon", ["kw_fits"],
                                    None, use_cache=False))
    assert m_call.await_count == 0                                       # nothing sent to TypeSafe
    v = dec.verdicts["kw_fits"]
    assert v.zone == "error" and v.error == app_profile.refusal_text("cis")
    row = m_log.await_args.kwargs
    assert (row["status"], row["error_class"], row["role"], row["cost_usd"]) == ("refused", "disabled", "decide", 0.0)
    d.reset_decision_memo()


# ── the bulk re-queue ───────────────────────────────────────────────────────────────────────────────────────────────

class _Pool:
    def __init__(self, off=()):
        self.off = list(off)

    async def fetch(self, sql, *args):
        assert sql == queue._DISABLED_STAGES_SQL
        return [{"stage": s} for s in self.off]


def test_every_stage_is_held_while_the_app_ai_is_off(cis_off):
    stages = asyncio.run(queue.disabled_stages(_Pool(off=["t2_generate"])))
    assert "t9_write" in stages and "any_stage_at_all" in stages
    off = str(app_profile.LLMAppDisabled("cis", "t9_write", "drill"))
    assert queue.llm_hold(off, {}, [], stages) == "stage still switched off: t9_write"


def test_only_the_switched_off_stages_are_held_while_ai_is_on():
    stages = asyncio.run(queue.disabled_stages(_Pool(off=["t2_generate"])))
    assert stages == frozenset({"t2_generate"}) and "t9_write" not in stages


# ── the admin ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_routes_are_in_the_policy():
    assert staff_authz.POLICY[("GET", "/admin/ai-gateway/app-profiles")] == staff_authz.SPEND_READ
    assert staff_authz.POLICY[("PATCH", "/admin/ai-gateway/app-profiles/{app}")] == staff_authz.AI_ADMIN
    assert gw_router.PAGE_WRITES["app_profiles"] == ("PATCH", "/admin/ai-gateway/app-profiles/{app}")


@pytest.mark.parametrize("body", [{}, {"reason": "only a reason"}, {"ai_enabled": None}, {"bulk_paused": None},
                                  {"degrade_mode": "shrug"}, {"default_daily_budget_usd": -1},
                                  {"default_daily_budget_usd": 100_001}, {"app": "cis", "ai_enabled": True},
                                  {"ai_enabled": False, "reason": "x" * 501}])
def test_a_bad_patch_is_refused(body):
    with pytest.raises(ValidationError):
        gw_router.AppProfilePatch(**body)


def test_a_patch_keeps_only_the_fields_sent():
    body = gw_router.AppProfilePatch(ai_enabled=False, reason="provider drill")
    assert body.fields() == {"ai_enabled": False, "disabled_reason": "provider drill"}
    assert gw_router.AppProfilePatch(default_daily_budget_usd=None).fields() == {"default_daily_budget_usd": None}


BEFORE = app_profile.AppProfile(app="cis")


def test_switching_off_needs_a_reason_and_switching_on_clears_it():
    with pytest.raises(ValueError, match="reason"):
        app_profile.merged(BEFORE, {"ai_enabled": False})
    with pytest.raises(ValueError, match="reason"):
        app_profile.merged(BEFORE, {"ai_enabled": False, "disabled_reason": " a "})
    off = app_profile.merged(BEFORE, {"ai_enabled": False, "disabled_reason": "  provider drill "})
    assert (off.ai_enabled, off.disabled_reason) == (False, "provider drill")
    edited = app_profile.merged(off, {"bulk_paused": True})                 # the stored reason is kept
    assert edited.disabled_reason == "provider drill" and edited.bulk_paused is True
    on = app_profile.merged(off, {"ai_enabled": True})
    assert (on.ai_enabled, on.disabled_reason) == (True, None)


def test_merged_refuses_what_the_table_refuses():
    with pytest.raises(ValueError):
        app_profile.merged(BEFORE, {"degrade_mode": "shrug"})
    with pytest.raises(ValueError):
        app_profile.merged(BEFORE, {"default_daily_budget_usd": 100_000.01})
    with pytest.raises(ValueError):
        app_profile.merged(BEFORE, {"app": "crm"})
    assert app_profile.merged(BEFORE, {"default_daily_budget_usd": 3.123456}).default_daily_budget_usd == 3.1235


def test_the_audit_action_names_the_switch():
    off = app_profile.AppProfile(app="cis", ai_enabled=False, disabled_reason="drill")
    assert app_profile.audit_action(BEFORE, off) == "llm_app_ai_switched_off"
    assert app_profile.audit_action(off, BEFORE) == "llm_app_ai_switched_on"
    assert app_profile.audit_action(BEFORE, app_profile.AppProfile(app="cis", bulk_paused=True)) == \
        "llm_app_profile_updated"


def test_the_central_app_caps_are_read_from_the_spend_budget_rows():
    rows = [{"row": '{"scope": "app:cis", "per_day_usd": "51.6500", "hard_stop": false}'},
            {"row": {"scope": "stage:t9_write", "per_day_usd": 1}},
            {"row": {"scope": "app:tripplanner", "per_day_usd": None, "per_month_usd": 20, "hard_stop": True}}]
    assert gw_router.central_app_caps(rows) == {
        "cis": {"per_day_usd": 51.65, "per_month_usd": None, "hard_stop": False},
        "tripplanner": {"per_day_usd": None, "per_month_usd": 20.0, "hard_stop": True}}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _UpdateConn:
    def __init__(self, before):
        self.before = before
        self.executed: list = []

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        if "FOR UPDATE" in sql:
            return None if self.before is None else {"row": self.before}
        assert "UPDATE shared.llm_app_profile" in sql
        app, ai, bulk, budget, mode, reason, by = args
        return {"row": {"app": app, "ai_enabled": ai, "bulk_paused": bulk, "default_daily_budget_usd": budget,
                        "degrade_mode": mode, "disabled_reason": reason, "updated_by": by}}

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


class _UpdatePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _A()


def test_the_store_updates_and_audits_in_one_transaction():
    conn = _UpdateConn({"app": "cis", "ai_enabled": True, "degrade_mode": "queue_retry"})
    out = asyncio.run(app_profile.update(_UpdatePool(conn), "cis", {"ai_enabled": False,
                                                                   "disabled_reason": "provider drill"},
                                         actor="admin:someone"))
    assert out["ai_enabled"] is False and out["disabled_reason"] == "provider drill"
    assert out["updated_by"] == "admin:someone"
    (sql, args), = conn.executed
    assert "acp_shared.audit_log" in sql and args[1] == "llm_app_ai_switched_off" and args[2] == "cis"
    assert '"ai_enabled": [true, false]' in args[3]


def test_the_store_refuses_an_unknown_app_or_a_missing_row():
    with pytest.raises(LookupError):
        asyncio.run(app_profile.update(_UpdatePool(_UpdateConn(None)), "portal", {"bulk_paused": True}, actor="a"))
    with pytest.raises(LookupError):
        asyncio.run(app_profile.update(_UpdatePool(_UpdateConn(None)), "crm", {"bulk_paused": True}, actor="a"))


def test_page_access_names_the_app_profile_control():
    def principal(role, read_only=False):
        return staff_authz.principal_from_role(role, SimpleNamespace(auth="cognito", is_owner=False,
                                                                    e2e_read_only=read_only))
    assert gw_router.page_access(principal("admin"))["actions"]["app_profiles"] is True
    assert gw_router.page_access(principal("admin", read_only=True))["actions"]["app_profiles"] is False
    assert gw_router.page_access(principal("viewer"))["actions"]["app_profiles"] is False


def test_the_jev_refusal_text_reads_as_refused_on_the_calls_tab():
    from api.services import ai_gateway as gw
    assert gw.jev_verdict_status({"zone": "error", "error": app_profile.refusal_text("cis")}) == "refused"


def test_ai_off_refuses_the_daily_embedding_probe_as_refused(cis_off, monkeypatch):
    from services.jobs import llm_probe_job
    model = MagicMock(model_key="embed-x", api_style="embed")
    conn = MagicMock(connection_key="acc2", kind="bedrock", auth_mode="task_role")
    with pytest.raises(app_profile.LLMAppDisabled):
        embed_mod.probe(model, conn, "wire-x", ["a quiet lake"])

    rows: list = []

    class _Db:
        async def fetch(self, *a):
            return []

        async def execute(self, *a):
            return None

    class _Pool:
        def acquire(self):
            db = _Db()

            class _A:
                async def __aenter__(self):
                    return db

                async def __aexit__(self, *exc):
                    return False
            return _A()

    async def record(pool, **row):
        rows.append(row)
    monkeypatch.setattr("shared.llm_client.connections.fetch_connections", AsyncMock(return_value={"acc2": conn}))
    monkeypatch.setattr("shared.llm_client.catalog.get_model_sync", lambda key: model)
    monkeypatch.setattr("shared.llm_client.call_log.record_call_with_pool", record)
    out = asyncio.run(llm_probe_job.probe_embedding(_Pool(), "acc2", "embed-x", "wire-x"))
    assert out["ok"] is False and out["cost_usd"] == 0.0
    assert (rows[0]["status"], rows[0]["error_class"], rows[0]["role"]) == ("refused", DISABLED, "embed")
