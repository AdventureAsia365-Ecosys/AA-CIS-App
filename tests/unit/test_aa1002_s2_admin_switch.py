"""AA-1002 (gateway slice S2) — PATCH /admin/llm-connections/{connection_key}: the one admin switch of S2.

Admin only (ai.admin), body ``{"enabled": bool}`` only, Anthropic connections only (the gateway reads no other kind's
flag before S3), enabling refuses a policy problem or an unreadable secret, one audit row per change in the same
transaction, caches dropped after the change. Migration 306 grants the three columns the switch writes.
"""
import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.core import staff_authz
from api.routers import admin_llm_ops as ops
from api.services import llm_connections
from shared.llm_client import connections
from shared.llm_client.adapters import anthropic_messages

MIGRATION_306 = Path(__file__).resolve().parents[2] / "api" / "migrations" / "306_llm_anthropic_deployments.sql"


def _row(**kw):
    row = {c: None for c in connections._COLUMNS}
    row.update(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key", label="Anthropic API",
               data_scope="vendor_direct", enabled=False, secret_name="aa-llm/dev/anthropic-direct",
               base_url="https://api.anthropic.com", updated_by="migration-304-aa1002")
    row.update(kw)
    return row


class FakeDB:
    def __init__(self, row):
        self.row = row
        self.executed: list = []
        self.in_tx = False
        self.committed = False

    @asynccontextmanager
    async def _tx(self):
        self.in_tx = True
        try:
            yield
            self.committed = True
        finally:
            self.in_tx = False

    def transaction(self):
        return self._tx()

    async def fetchrow(self, sql, key):
        assert "FOR UPDATE" in sql and self.in_tx
        return self.row if self.row and self.row["connection_key"] == key else None

    async def execute(self, sql, *args):
        assert self.in_tx, "write outside the transaction"
        self.executed.append((" ".join(sql.split()), args))


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    reader = MagicMock(return_value=json.dumps({"api_key": "sk-ant-test"}))
    monkeypatch.setattr(connections, "_fetch_secret", reader)
    connections._credentials.clear()
    yield reader
    connections._credentials.clear()


def _run(db, enabled=True, key="anthropic-direct"):
    return asyncio.run(llm_connections.set_enabled(db, key, enabled=enabled, actor="admin:u-1"))


def test_switch_is_admin_only():
    assert staff_authz.POLICY[("PATCH", "/admin/llm-connections/{connection_key}")] == staff_authz.AI_ADMIN
    for role in ("reviewer", "content", "viewer"):
        assert staff_authz.AI_ADMIN not in staff_authz.ROLE_PERMS[role]


def test_body_takes_only_the_switch():
    assert ops.LlmConnectionPatch(enabled=True).enabled is True
    for extra in ({"enabled": True, "secret_name": "aa-cis/dev/rds"}, {"enabled": True, "base_url": "https://x.io"},
                  {}):
        with pytest.raises(ValidationError):
            ops.LlmConnectionPatch(**extra)


def test_enable_updates_and_audits_in_one_transaction(_secret):
    db = FakeDB(_row())
    with patch.object(anthropic_messages, "forget_connection") as forget, \
            patch.object(connections, "invalidate") as inv:
        out = _run(db)
    assert out == {"connection_key": "anthropic-direct", "kind": "anthropic", "enabled": True, "changed": True,
                   "updated_by": "admin:u-1"}
    (upd_sql, upd_args), (audit_sql, audit_args) = db.executed
    assert upd_sql.startswith("UPDATE shared.llm_provider_connection SET enabled = $2, updated_at = now(), "
                              "updated_by = $3")
    assert upd_args == ("anthropic-direct", True, "admin:u-1")
    assert audit_sql.startswith("INSERT INTO acp_shared.audit_log")
    assert audit_args[:4] == ("admin:u-1", "llm_connection_changed", "llm_provider_connection", "anthropic-direct")
    details = json.loads(audit_args[4])
    assert details["before"] == {"enabled": False} and details["after"] == {"enabled": True}
    assert "sk-ant-test" not in repr(db.executed) and "sk-ant-test" not in repr(out)   # the key is never written
    assert db.committed
    _secret.assert_called_once_with("aa-llm/dev/anthropic-direct")                    # readable before enabling
    forget.assert_called_once_with("anthropic-direct")
    assert inv.called
    assert connections._credentials == {}                                            # the check keeps no key


def test_disable_needs_no_secret(_secret):
    db = FakeDB(_row(enabled=True))
    out = _run(db, enabled=False)
    assert out["changed"] is True and db.executed[0][1] == ("anthropic-direct", False, "admin:u-1")
    _secret.assert_not_called()


def test_no_change_writes_no_audit_row():
    db = FakeDB(_row(enabled=False))
    out = _run(db, enabled=False)
    assert out["changed"] is False and db.executed == []


@pytest.mark.parametrize("row,match", [
    (_row(connection_key="acc3", kind="bedrock", auth_mode="assume_role", secret_name=None, base_url=None),
     "slice S3"),
    (_row(connection_key="openai-direct", kind="openai", secret_name=None, credential_env="OPENAI_API_KEY",
          base_url="https://api.openai.com/v1"), "slice S3"),
    (_row(secret_name="aa-cis/dev/rds"), "aa-llm"),
    (_row(base_url="https://evil.example.com"), "not allowed"),
])
def test_refused_switches_write_nothing(row, match, _secret):
    db = FakeDB(row)
    with pytest.raises(llm_connections.ConnectionToggleRefused, match=match):
        _run(db, key=row["connection_key"])
    assert db.executed == []
    _secret.assert_not_called()


def test_enable_refused_when_the_secret_cannot_be_read(_secret):
    _secret.side_effect = RuntimeError("ResourceNotFoundException")
    db = FakeDB(_row())
    with pytest.raises(llm_connections.ConnectionToggleRefused, match="secret unreadable"):
        _run(db)
    assert db.executed == []


def test_unknown_connection_is_a_lookup_error():
    with pytest.raises(LookupError):
        _run(FakeDB(None), key="nope")


def _request(db):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield db
    pool.acquire = acquire
    req = MagicMock()
    req.app.state.pool = pool
    return req


@pytest.mark.parametrize("db,status", [(FakeDB(None), 404),
                                       (FakeDB(_row(connection_key="acc3", kind="bedrock")), 422)])
def test_route_maps_errors(db, status):
    with patch.object(ops, "verify_admin_secret"):
        with pytest.raises(HTTPException) as e:
            asyncio.run(ops.patch_llm_connection(_request(db), "acc3" if status == 422 else "nope",
                                                 ops.LlmConnectionPatch(enabled=True), "s", "u-1"))
    assert e.value.status_code == status


def test_route_switches_and_names_the_admin():
    db = FakeDB(_row())
    with patch.object(ops, "verify_admin_secret"), patch.object(anthropic_messages, "forget_connection"):
        out = asyncio.run(ops.patch_llm_connection(_request(db), "anthropic-direct",
                                                   ops.LlmConnectionPatch(enabled=True), "s", "u-7"))
    assert out["enabled"] is True and out["updated_by"] == "admin:u-7"


# ── migration 306 (text checks; the SQL itself runs in tests/integration/test_aa1002_migration_306.py) ─────────

def test_migration_306_grants_exactly_the_columns_the_switch_writes():
    sql = MIGRATION_306.read_text()
    grants = re.findall(r"^GRANT (.+?) ON (\S+) TO (\w+);", sql, re.MULTILINE)
    assert grants == [("UPDATE (enabled, updated_at, updated_by)", "shared.llm_provider_connection", "cis_app")]
    source = re.sub(r'"\s*\n\s*"', "", Path(llm_connections.__file__).read_text())   # join split literals
    update_sql = re.search(r"UPDATE shared\.llm_provider_connection SET (.+?) WHERE", source).group(1)
    assert set(re.findall(r"(\w+) =", update_sql)) == {"enabled", "updated_at", "updated_by"}


def test_migration_306_seeds_behind_acc3_and_leaves_the_connection_off():
    sql = MIGRATION_306.read_text()
    assert "SELECT s.model_key, 'anthropic-direct', s.wire_id, 100, true, 'seed'" in sql
    assert not re.search(r"UPDATE shared\.llm_provider_connection", sql)
    assert "'306'" in sql and not re.search(r"\b\d{12}\b", sql)
