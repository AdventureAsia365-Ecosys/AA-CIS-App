"""AA-1002 (gateway slice S4) — the admin side of the Connections tab.

- PATCH /admin/llm-connections/{key}: secret name / base URL / limits / notes, admin only, policy checked before any
  secret is read, the key read before the transaction when the credential moves on an enabled connection, one audit
  row, never the key; 503 until migration 311's grant exists.
- POST /admin/llm-connections/{key}/test: one small call through LLMClient.probe (never a live call here: the client
  is a fake), logged in llm_call_log (stage adhoc_conn_test, role validate), audited, failure = an answer.
- GET /admin/llm-availability per connection (the breaker state of each deployment).
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.core import staff_authz
from api.routers import admin_llm_ops as ops
from api.services import llm_availability as av
from api.services import llm_connections
from shared.llm_client import breaker, catalog, connections
from shared.llm_client.adapters import anthropic_messages
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import BilledUnusableReply, LLMClient
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.models import LLMResponse
from shared.llm_client.vendor_route import VendorDeployment
from tests.unit.test_aa1002_s2_admin_switch import FakeDB, _request, _row

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    reader = MagicMock(return_value=json.dumps({"api_key": "sk-ant-test"}))
    monkeypatch.setattr(connections, "_fetch_secret", reader)
    connections._credentials.clear()
    monkeypatch.setattr(anthropic_messages, "forget_connection", MagicMock())
    yield reader
    connections._credentials.clear()


def _update(db, changes, key="anthropic-direct"):
    return asyncio.run(llm_connections.update_connection(db, key, changes, actor="admin:u-1"))


# ── PATCH ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_patch_and_test_are_admin_only():
    for route in (("PATCH", "/admin/llm-connections/{connection_key}"),
                  ("POST", "/admin/llm-connections/{connection_key}/test")):
        assert staff_authz.POLICY[route] == staff_authz.AI_ADMIN
    for role in ("reviewer", "content", "viewer"):
        assert staff_authz.AI_ADMIN not in staff_authz.ROLE_PERMS[role]


def test_body_carries_only_the_fields_sent_and_clears_with_null():
    body = ops.LlmConnectionPatch(rpm_limit=60, notes="  ", secret_name=None)
    assert body.changes() == {"rpm_limit": 60, "notes": None, "secret_name": None}
    for bad in ({"timeout_s": 0}, {"timeout_s": 901}, {"max_retries": 6}, {"max_concurrency": 0},
                {"rpm_limit": 100001}, {"monthly_budget_usd": -1}, {"account_id": "111122223333"}):
        with pytest.raises(ValidationError):
            ops.LlmConnectionPatch(**bad)


def test_limits_update_in_one_statement_with_an_audit_row(_secret):
    db = FakeDB(_row(enabled=True, rpm_limit=None, max_concurrency=4))
    out = _update(db, {"rpm_limit": 120, "max_concurrency": 4, "timeout_s": 60})
    assert out["changed"] and out["changed_fields"] == ["timeout_s", "rpm_limit"]     # max_concurrency unchanged
    (upd, args), (audit, aargs) = db.executed
    assert upd == ("UPDATE shared.llm_provider_connection SET timeout_s = $2, rpm_limit = $3, updated_at = now(), "
                   "updated_by = $4 WHERE connection_key = $1")
    assert args == ("anthropic-direct", 60, 120, "admin:u-1")
    details = json.loads(aargs[4])
    assert details["before"] == {"timeout_s": None, "rpm_limit": None}
    assert details["after"] == {"timeout_s": 60, "rpm_limit": 120}
    _secret.assert_not_called()                                      # no credential field moved
    assert db.reads == [("locked", True)]


@pytest.mark.parametrize("row,changes,match", [
    (_row(), {"secret_name": "aa-cis/dev/rds"}, "aa-llm"),            # another app's secret
    (_row(), {"secret_name": "aa-llm/dev/../x"}, "aa-llm"),
    (_row(), {"base_url": "https://evil.example.com"}, "not allowed"),
    (_row(), {"base_url": "http://api.anthropic.com"}, "plain https"),
    (_row(connection_key="acc3", kind="bedrock", auth_mode="task_role", region="us-west-1", secret_name=None,
          base_url=None), {"secret_name": "aa-llm/dev/x"}, "takes no secret_name"),
    (_row(connection_key="acc3", kind="bedrock", auth_mode="task_role", region="us-west-1", secret_name=None,
          base_url=None), {"base_url": "https://api.anthropic.com"}, "takes no base_url"),
    (_row(enabled=True), {"secret_name": None}, "no secret_name or credential_env"),   # would leave it keyless
])
def test_policy_refusals_read_no_secret_and_write_nothing(row, changes, match, _secret):
    db = FakeDB(row)
    with pytest.raises(llm_connections.ConnectionToggleRefused, match=match):
        _update(db, changes, key=row["connection_key"])
    assert db.executed == []
    _secret.assert_not_called()


def test_moving_the_secret_of_an_enabled_connection_reads_the_new_one_first(_secret):
    db = FakeDB(_row(enabled=True))
    out = _update(db, {"secret_name": "aa-llm/dev/anthropic-direct-2"})
    _secret.assert_called_once_with("aa-llm/dev/anthropic-direct-2")
    assert out["changed_fields"] == ["secret_name"]
    assert "sk-ant-test" not in repr(db.executed) and "sk-ant-test" not in repr(out)
    assert db.reads == [("plain", False), ("locked", True)]          # the key was read outside the transaction


def test_an_unreadable_new_secret_is_refused(_secret):
    _secret.side_effect = RuntimeError("ResourceNotFoundException")
    db = FakeDB(_row(enabled=True))
    with pytest.raises(llm_connections.ConnectionToggleRefused, match="secret unreadable"):
        _update(db, {"secret_name": "aa-llm/dev/missing"})
    assert db.executed == []


def test_moving_the_secret_of_a_disabled_connection_reads_nothing(_secret):
    db = FakeDB(_row(enabled=False))
    _update(db, {"secret_name": "aa-llm/dev/next"})
    _secret.assert_not_called()


def test_bedrock_and_openai_connections_can_be_switched_since_s3():
    db = FakeDB(_row(connection_key="acc2", kind="bedrock", auth_mode="task_role", region="us-west-1",
                     secret_name=None, base_url=None, enabled=True))
    assert _update(db, {"enabled": False}, key="acc2")["enabled"] is False


class _DeniedDB(FakeDB):
    async def execute(self, sql, *args):
        import asyncpg
        if sql.startswith("UPDATE"):
            raise asyncpg.exceptions.InsufficientPrivilegeError("permission denied for table llm_provider_connection")
        await super().execute(sql, *args)


def test_without_migration_311_the_route_answers_503():
    db = _DeniedDB(_row(enabled=True))
    with patch.object(ops, "verify_admin_secret"):
        with pytest.raises(HTTPException) as e:
            asyncio.run(ops.patch_llm_connection(_request(db), "anthropic-direct",
                                                 ops.LlmConnectionPatch(rpm_limit=10), "s", "u-1"))
    assert e.value.status_code == 503 and "311" in e.value.detail


def test_no_change_no_audit_row():
    db = FakeDB(_row(rpm_limit=60, notes="n"))
    out = _update(db, {"rpm_limit": 60, "notes": "n"})
    assert out["changed"] is False and db.executed == []


# ── POST …/test ──────────────────────────────────────────────────────────────────────────────────────────────────

SONNET55 = CatalogModel(model_key="sonnet-5-5", label="Sonnet 5.5", vendor="anthropic", provider="bedrock",
                        api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"},
                        callable_via=("llm_client",), price_in_per_mtok=2.0, price_out_per_mtok=10.0, enabled=True)
HAIKU55 = CatalogModel(model_key="haiku-5-5", label="Haiku 5.5", vendor="anthropic", provider="bedrock",
                       api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-haiku-5-5"},
                       callable_via=("llm_client",), price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)
EMBED = CatalogModel(model_key="cohere-embed-v4", label="Cohere", vendor="cohere", provider="bedrock",
                     api_style="embed", bedrock_profile_ids={"acc2": "global.cohere.embed-v4:0"},
                     callable_via=("embed",), price_in_per_mtok=0.12, price_out_per_mtok=0.0, enabled=True)


class ConnTestDB:
    def __init__(self, row, deployments):
        self.row = row
        self.deployments = deployments
        self.executed: list = []

    async def fetchrow(self, sql, key):
        return self.row if self.row and self.row["connection_key"] == key else None

    async def fetch(self, sql):
        assert "llm_model_deployment" in sql
        return [{"model_key": d.model_key, "connection_key": d.connection_key, "wire_id": d.wire_id,
                 "priority": d.priority, "enabled": d.enabled, "origin": d.origin, "price_in_override": None,
                 "price_out_override": None, "price_cache_read_override": None, "price_cache_write_override": None,
                 "price_cache_write_1h_override": None, "batch_capable": False, "request_defaults": {}, "notes": None}
                for d in self.deployments]

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))


def _pool(db):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield db
    pool.acquire = acquire
    return pool


ANTH_DEPS = [ModelDeployment("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 100, True, "seed"),
             ModelDeployment("haiku-5-5", "anthropic-direct", "claude-haiku-5-5", 100, False, "seed"),
             ModelDeployment("cohere-embed-v4", "anthropic-direct", "x", 1, True, "seed")]


@pytest.fixture
def cat(monkeypatch):
    monkeypatch.setattr(catalog, "_models", {m.model_key: m for m in (SONNET55, HAIKU55, EMBED)})
    monkeypatch.setattr(catalog, "_loaded_at", 10 ** 12)
    yield
    catalog.invalidate()


def _answer(**kw):
    base = dict(content="OK", model_used="anthropic-api-sonnet-5-5", provider="anthropic", input_tokens=20,
                output_tokens=2, cost_usd=0.00006, stop_reason="end_turn", provider_model="claude-sonnet-5-5")
    base.update(kw)
    return LLMResponse(**base)


def _test(db, client, model_key=None, key="anthropic-direct"):
    record = AsyncMock()
    with patch("shared.llm_client.call_log.record_call_with_pool", record):
        out = asyncio.run(llm_connections.test_connection(_pool(db), key, model_key=model_key, actor="admin:u-1",
                                                          client=client))
    return out, record


def test_the_test_goes_through_llmclient_probe_on_a_disabled_connection(cat):
    db = ConnTestDB(_row(enabled=False), ANTH_DEPS)
    client = MagicMock()
    client.probe.return_value = _answer()
    out, record = _test(db, client, model_key="sonnet-5-5")
    (request, model, target), _ = client.probe.call_args
    assert request.stage == "adhoc_conn_test" and request.max_tokens == llm_connections.TEST_MAX_TOKENS
    assert request.model_tier is None                                # no route, no pin string: the explicit target
    assert model.model_key == "sonnet-5-5" and target.deployment.wire_id == "claude-sonnet-5-5"
    assert target.connection.enabled and target.deployment.enabled  # tested before switching on
    assert out["ok"] and out["reply_usable"] and out["connection_enabled"] is False and out["cost_usd"] == 0.00006
    kw = record.await_args.kwargs
    assert (kw["stage"], kw["role"], kw["account"], kw["model"]) == \
        ("adhoc_conn_test", "validate", "anthropic-direct", "anthropic-api-sonnet-5-5")
    (audit, args), = db.executed
    assert args[1] == "llm_connection_tested" and json.loads(args[4])["ok"] is True


def test_the_default_pick_is_the_cheapest_enabled_text_model(cat):
    deps = ANTH_DEPS + [ModelDeployment("haiku-5-5", "anthropic-direct", "claude-haiku-5-5b", 200, True, "seed")]
    db = ConnTestDB(_row(enabled=True), deps)
    client = MagicMock()
    client.probe.return_value = _answer()
    _test(db, client)
    (_, model, target), _ = client.probe.call_args
    assert model.model_key == "haiku-5-5" and target.deployment.wire_id == "claude-haiku-5-5b"   # embed row skipped


def test_a_provider_failure_is_the_answer_not_an_error(cat):
    db = ConnTestDB(_row(enabled=True), ANTH_DEPS)
    client = MagicMock()
    client.probe.side_effect = RuntimeError("AuthenticationError: invalid x-api-key (authentication_error)")
    out, record = _test(db, client, model_key="sonnet-5-5")
    assert out["ok"] is False and out["error_class"] == "access_denied" and out["cost_usd"] == 0.0
    record.assert_not_awaited()                                      # nothing billed, no call-log row
    assert json.loads(db.executed[0][1][4])["error_class"] == "access_denied"


def test_an_unusable_reply_means_reachable(cat):
    db = ConnTestDB(_row(enabled=True), ANTH_DEPS)
    client = MagicMock()
    err = BilledUnusableReply("empty reply")
    err.billed_usage = ({"cost_usd": 0.0001},)
    client.probe.side_effect = err
    out, record = _test(db, client, model_key="sonnet-5-5")
    assert out["ok"] is True and out["reply_usable"] is False and out["cost_usd"] == 0.0001
    record.assert_not_awaited()                                      # the gateway wrote the billed_unusable row


@pytest.mark.parametrize("row,deps,model_key,match", [
    (_row(), [ANTH_DEPS[2]], None, "no text-model deployment"),
    (_row(), ANTH_DEPS, "gpt-6-luna", "no deployment of 'gpt-6-luna'"),
    (_row(secret_name="aa-cis/dev/rds"), ANTH_DEPS, None, "cannot be tested"),
])
def test_nothing_testable_is_422(cat, row, deps, model_key, match):
    client = MagicMock()
    with pytest.raises(llm_connections.ConnectionToggleRefused, match=match):
        _test(ConnTestDB(row, deps), client, model_key=model_key)
    client.probe.assert_not_called()


def test_route_maps_unknown_connection_to_404(cat):
    req = MagicMock()
    req.app.state.pool = _pool(ConnTestDB(None, []))
    with patch.object(ops, "verify_admin_secret"), pytest.raises(HTTPException) as e:
        asyncio.run(ops.test_llm_connection(req, "nope", None, "s", "u-1"))
    assert e.value.status_code == 404


def test_probe_is_the_breaker_wrapped_call_without_admission():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        client = LLMClient()
    target = VendorDeployment(ANTH_DEPS[0], connections.ProviderConnection(
        connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key", label="x",
        data_scope="vendor_direct", enabled=True, secret_name="aa-llm/dev/anthropic-direct"))
    with patch.object(breaker, "guarded", return_value="resp") as guarded:
        assert client.probe(MagicMock(), SONNET55, target) == "resp"
    assert guarded.call_args.kwargs["admit_call"] is False


def test_probe_logs_a_billed_unusable_reply():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        client = LLMClient()
    err = BilledUnusableReply("bad")
    err.billed_usage = ({"model": "sonnet-5-5", "cost_usd": 0.0001},)
    target = MagicMock()
    target.connection.connection_key = "anthropic-direct"
    with patch.object(client, "_call_target", side_effect=err), \
            patch("shared.llm_client.client.log_gateway_billed_failure") as log:
        with pytest.raises(BilledUnusableReply):
            client.probe(MagicMock(stage="adhoc_conn_test"), SONNET55, target)
    assert log.call_args.kwargs["role"] == "validate"


# ── availability per connection ──────────────────────────────────────────────────────────────────────────────────

def _h(conn, model=None, enabled=True, **kw):
    base = {"connection_key": conn, "kind": "bedrock", "label": conn, "enabled": enabled, "model_key": model,
            **{f: None for f in av._HEALTH_FIELDS}, "consecutive_failures": 0, "open_streak": 0}
    base.update(kw)
    return base


def test_connection_status_per_breaker_state():
    rows = [
        _h("acc3", "gpt-6-luna", last_error_class="access_denied", last_error_at=NOW, consecutive_failures=1,
           circuit_open_until=NOW + timedelta(minutes=5)),
        _h("acc3", "sonnet-5-5", last_ok_at=NOW - timedelta(hours=1)),
        _h("acc2", "cohere-embed-v4", last_ok_at=NOW - timedelta(minutes=5)),
        _h("org", "x", consecutive_failures=2, last_error_class="throttled", last_error_at=NOW),
        _h("anthropic-direct", enabled=False),
        _h("openai-direct"),
        _h("old", "y", last_ok_at=NOW - timedelta(hours=7)),
        _h("half", "z", circuit_open_until=NOW - timedelta(seconds=1), last_error_class="other",
           last_error_at=NOW - timedelta(minutes=2)),
    ]
    out = {c["connection_key"]: c for c in av.build_connections(rows, now=NOW)}
    assert {k: c["status"] for k, c in out.items()} == {
        "acc3": "open", "acc2": "ok", "org": "degraded", "anthropic-direct": "disabled", "openai-direct": "unknown",
        "old": "unknown", "half": "open"}
    acc3 = out["acc3"]
    assert acc3["open_models"] == 1 and acc3["last_error_class"] == "access_denied"
    assert [m["state"] for m in acc3["models"]] == ["open", "closed"]
    assert out["half"]["models"][0]["state"] == "half_open"
    assert out["openai-direct"]["models"] == []
    assert all("_last_ok" not in m for c in out.values() for m in c["models"])


def test_read_without_the_tables_says_so():
    import asyncpg
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[[], [], asyncpg.exceptions.UndefinedTableError("missing")])
    out = asyncio.run(av.read(conn, hours=6, now=NOW))
    assert out["health_ready"] is False and out["connections"] == []
