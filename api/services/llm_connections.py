"""AA-1002 (gateway slice S1) — the admin listing of LLM connections and their model deployments.

Served by ``GET /admin/llm-connections`` (api/routers/admin_llm_ops.py, admin only: ai.admin). Read-only: the rows of
shared.llm_provider_connection and shared.llm_model_deployment (migration 304) with, per connection, where its
credential comes from and the policy problems that would refuse it (``connections.ProviderConnection.policy_problems``).

Never a secret value: the listing reads no secret and no env var holding a key. A secret NAME is shown (it is
configuration, CHECKed to ``aa-llm/``); for an env-sourced credential only the env var's name and whether it is set.
The account ID shown for an assume-role connection is resolved at runtime (the admin shows account IDs, never the
internal acc keys as the only label; the public repo holds none).

Before migration 304 is applied the tables do not exist: the listing answers ``schema_ready: false`` and empty lists
instead of a 500. ``routing_uses_registry`` is true since slice S2: a model whose first enabled deployment (by
priority) is on an enabled Anthropic connection is called through the Anthropic Messages API
(shared/llm_client/vendor_route.py); every other row still describes today's code paths.

AA-1002 (S2) — ``set_enabled`` backs ``PATCH /admin/llm-connections/{connection_key}`` (admin only): the one switch an
admin may flip in S2, on a connection whose ``enabled`` flag the gateway reads (kind ``anthropic``). Enabling refuses a
row with a policy problem or a secret that cannot be read (the value is never returned). One ``acp_shared.audit_log``
row per change (action ``llm_connection_changed``), in the same transaction. Migration 306 grants cis_app UPDATE on
``enabled`` / ``updated_at`` / ``updated_by`` only.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os

import asyncpg

from shared.llm_client import connections as conn_registry
from shared.llm_client import deployments as dep_registry

# The connection kinds whose `enabled` flag the gateway reads in S2 (vendor_route.VENDOR_KINDS). Toggling a Bedrock
# or OpenAI row would change nothing on the routing path before slice S3, so it is refused rather than misleading.
TOGGLE_KINDS = ("anthropic",)


class ConnectionToggleRefused(ValueError):
    """The connection may not be switched (kind not routed in S2, policy problem, unreadable credential)."""


def build(connections: dict, deployments: list) -> dict:
    by_conn: dict[str, list[dict]] = {}
    for d in deployments:
        by_conn.setdefault(d.connection_key, []).append({
            "model_key": d.model_key, "wire_id": d.wire_id, "priority": d.priority, "enabled": d.enabled,
            "origin": d.origin, "batch_capable": d.batch_capable,
            "price_in_override": d.price_in_override, "price_out_override": d.price_out_override,
            "notes": d.notes,
        })
    rows = []
    for key in sorted(connections):
        c = connections[key]
        source = c.credential_source()
        rows.append({
            "connection_key": c.connection_key, "kind": c.kind, "auth_mode": c.auth_mode, "label": c.label,
            "enabled": c.enabled, "data_scope": c.data_scope,
            "account_id": c.resolved_account_id() if c.kind == "bedrock" else None,
            "region": c.region, "role_name": c.role_name,
            "credential_source": source,
            "secret_name": c.secret_name,
            "credential_env": c.credential_env,
            # only whether it is set — never the value
            "credential_env_set": bool(os.environ.get(c.credential_env)) if source == "env" else None,
            "base_url": c.base_url,
            "timeout_s": c.timeout_s, "max_retries": c.max_retries, "max_concurrency": c.max_concurrency,
            "rpm_limit": c.rpm_limit, "monthly_budget_usd": c.monthly_budget_usd,
            "policy_problems": c.policy_problems(),
            "notes": c.notes, "updated_at": c.updated_at, "updated_by": c.updated_by,
            "deployments": sorted(by_conn.get(key, []), key=lambda d: (d["model_key"], d["priority"])),
        })
    return {"schema_ready": True, "routing_uses_registry": True, "connections": rows}


async def read(conn) -> dict:
    try:
        connections = await conn_registry.fetch_connections(conn)
        deployments = await dep_registry.fetch_deployments(conn)
    except asyncpg.exceptions.UndefinedTableError:
        return {"schema_ready": False, "routing_uses_registry": False, "connections": []}
    return build(connections, deployments)


_SELECT_ONE_SQL = (f"SELECT {', '.join(conn_registry._COLUMNS)} FROM shared.llm_provider_connection "
                   "WHERE connection_key = $1 FOR UPDATE")


def _check_credential(c: conn_registry.ProviderConnection) -> None:
    """Raise ConnectionToggleRefused unless the connection, once enabled, could read its key. The key is read (the
    same fail-closed resolver the gateway uses) and dropped; it is never returned or logged."""
    try:
        conn_registry.resolve_api_key(dataclasses.replace(c, enabled=True))
    except conn_registry.ConnectionCredentialError as e:
        raise ConnectionToggleRefused(str(e)) from e
    finally:
        conn_registry.invalidate_credential(c.connection_key)


async def set_enabled(db, connection_key: str, *, enabled: bool, actor: str) -> dict:
    """Switch one connection on or off and audit it. Raises LookupError (unknown key, 404) and
    ConnectionToggleRefused (422). A request that changes nothing writes no audit row (``changed: false``)."""
    async with db.transaction():
        row = await db.fetchrow(_SELECT_ONE_SQL, connection_key)
        if row is None:
            raise LookupError(f"unknown connection: {connection_key!r}")
        c = conn_registry._row_to_connection(row)
        if c.kind not in TOGGLE_KINDS:
            raise ConnectionToggleRefused(
                f"connection {connection_key!r} is a {c.kind!r} connection: the gateway does not read its enabled "
                "flag before slice S3, so switching it would change nothing")
        if enabled:
            problems = c.policy_problems()
            if problems:
                raise ConnectionToggleRefused(f"connection {connection_key!r} refused: {'; '.join(problems)}")
            await asyncio.to_thread(_check_credential, c)
        changed = c.enabled != enabled
        if changed:
            await db.execute(
                "UPDATE shared.llm_provider_connection SET enabled = $2, updated_at = now(), updated_by = $3 "
                "WHERE connection_key = $1", connection_key, enabled, actor)
            await db.execute(
                "INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
                "VALUES ($1, $2, $3, $4, $5::jsonb)",
                actor[:64], "llm_connection_changed", "llm_provider_connection", connection_key,
                json.dumps({"before": {"enabled": c.enabled}, "after": {"enabled": enabled},
                            "kind": c.kind, "secret_name": c.secret_name, "base_url": c.base_url}))
    # this process reads the new state at once; the other processes (worker) within the 5-minute registry cache
    conn_registry.invalidate()
    dep_registry.invalidate()
    from shared.llm_client.adapters import anthropic_messages
    anthropic_messages.forget_connection(connection_key)
    return {"connection_key": connection_key, "kind": c.kind, "enabled": enabled, "changed": changed,
            "updated_by": actor if changed else c.updated_by}
