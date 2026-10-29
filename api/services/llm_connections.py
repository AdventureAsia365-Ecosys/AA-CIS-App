"""AA-1002 (gateway slice S1) — the admin listing of LLM connections and their model deployments.

Served by ``GET /admin/llm-connections`` (api/routers/admin_llm_ops.py, admin only: ai.admin). Read-only: the rows of
shared.llm_provider_connection and shared.llm_model_deployment (migration 304) with, per connection, where its
credential comes from and the policy problems that would refuse it (``connections.ProviderConnection.policy_problems``).

Never a secret value: the listing reads no secret and no env var holding a key. A secret NAME is shown (it is
configuration, CHECKed to ``aa-llm/``); for an env-sourced credential only the env var's name and whether it is set.
The account ID shown for an assume-role connection is resolved at runtime (the admin shows account IDs, never the
internal acc keys as the only label; the public repo holds none).

Before migration 304 is applied the tables do not exist: the listing answers ``schema_ready: false`` and empty lists
instead of a 500. ``routing_uses_registry`` is false in S1: no call goes through these rows yet (slice S3).
"""
from __future__ import annotations

import os

import asyncpg

from shared.llm_client import connections as conn_registry
from shared.llm_client import deployments as dep_registry


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
    return {"schema_ready": True, "routing_uses_registry": False, "connections": rows}


async def read(conn) -> dict:
    try:
        connections = await conn_registry.fetch_connections(conn)
        deployments = await dep_registry.fetch_deployments(conn)
    except asyncpg.exceptions.UndefinedTableError:
        return {"schema_ready": False, "routing_uses_registry": False, "connections": []}
    return build(connections, deployments)
