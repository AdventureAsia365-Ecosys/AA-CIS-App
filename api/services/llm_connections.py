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

AA-1002 (S2) — ``set_enabled`` backed the on/off switch; (S4) ``update_connection`` backs
``PATCH /admin/llm-connections/{connection_key}`` (admin only): the switch (every kind the gateway calls since S3:
bedrock, anthropic, openai) plus the secret NAME, base URL, timeout / retries, limiter, budget and notes
(``EDITABLE``; identity and data-scope fields stay migration-only). A secret name / base URL is checked against the
credential policy before anything reads it; enabling, or moving the secret / endpoint of an enabled API-key connection,
reads the key first (the value is never returned). One ``acp_shared.audit_log`` row per change (action
``llm_connection_changed``), in the same transaction. Grants: 306 (enabled / updated_at / updated_by), 310 (the rest).

AA-1002 (S4) — ``test_connection`` backs ``POST /admin/llm-connections/{connection_key}/test``: one small real call
through ``LLMClient.probe``, logged in ``shared.llm_call_log`` (stage ``adhoc_conn_test``) and audited
(``llm_connection_tested``).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from decimal import Decimal
from typing import Optional

import asyncpg

from shared.llm_client import breaker
from shared.llm_client import connections as conn_registry
from shared.llm_client import deployments as dep_registry

# The connection kinds whose `enabled` flag the gateway reads (S2: anthropic; since S3 every kind with an adapter,
# vendor_route._CALLABLE). Enabling another kind (azure_openai, mock) would change nothing, so it is refused.
TOGGLE_KINDS = ("bedrock", "anthropic", "openai")

# AA-1002 (S4): what PATCH /admin/llm-connections/{key} may change. `enabled` is migration 306's grant; the others
# need migration 310 (until then the UPDATE answers permission denied → ConnectionEditUnavailable, 503). Never kind,
# auth_mode, account, role, ExternalId, region, credential_env or data_scope (identity / data policy: migrations only).
EDITABLE = ("enabled", "secret_name", "base_url", "timeout_s", "max_retries", "max_concurrency", "rpm_limit",
            "monthly_budget_usd", "notes")
CREDENTIAL_EDITS = ("secret_name", "base_url")


class ConnectionToggleRefused(ValueError):
    """The change is refused (kind without an adapter, policy problem, unreadable credential, field not editable)."""


class ConnectionEditUnavailable(RuntimeError):
    """The database refused the UPDATE (permission denied): the column grant of migration 310 is not applied yet."""


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
                   "WHERE connection_key = $1")
_SELECT_ONE_FOR_UPDATE_SQL = _SELECT_ONE_SQL + " FOR UPDATE"

# The fields the credential check depends on: when one of them changed between the check (outside the transaction)
# and the locked re-read, the check no longer proves anything and the switch is refused.
_CREDENTIAL_FIELDS = ("kind", "auth_mode", "secret_name", "credential_env", "base_url")


def _check_credential(c: conn_registry.ProviderConnection) -> None:
    """Raise ConnectionToggleRefused unless the connection, once enabled, could read its key. The key is read (the
    same fail-closed resolver the gateway uses) and dropped; it is never returned or logged."""
    try:
        conn_registry.resolve_api_key(dataclasses.replace(c, enabled=True))
    except conn_registry.ConnectionCredentialError as e:
        raise ConnectionToggleRefused(str(e)) from e
    finally:
        conn_registry.invalidate_credential(c.connection_key)


def _validate(c: conn_registry.ProviderConnection, changes: dict) -> conn_registry.ProviderConnection:
    """The connection with ``changes`` applied, or ConnectionToggleRefused. The secret name and base URL are checked
    against the credential policy (``aa-llm/<env>/<name>``, the kind's host allowlist) BEFORE anything reads them; an
    enabled result must have no policy problem at all."""
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise ConnectionToggleRefused(f"not editable here: {', '.join(unknown)}")
    if changes.get("enabled") and c.kind not in TOGGLE_KINDS:
        raise ConnectionToggleRefused(
            f"connection {c.connection_key!r} is a {c.kind!r} connection: the gateway has no adapter for it yet, so "
            "switching it on would change nothing")
    try:
        if changes.get("secret_name") is not None:
            if c.auth_mode not in ("api_key", "bedrock_api_key"):
                raise conn_registry.ConnectionPolicyError(
                    f"a {c.auth_mode} connection takes no secret_name (its credential is not an API key)")
            conn_registry.check_secret_name(changes["secret_name"])
        if changes.get("base_url") is not None:
            conn_registry.check_base_url(c.kind, changes["base_url"])
    except conn_registry.ConnectionPolicyError as e:
        raise ConnectionToggleRefused(f"connection {c.connection_key!r} refused: {e}") from e
    merged = dataclasses.replace(c, **changes)
    if merged.enabled:
        problems = merged.policy_problems()
        if problems:
            raise ConnectionToggleRefused(f"connection {c.connection_key!r} refused: {'; '.join(problems)}")
    return merged


def _needs_credential_check(before: conn_registry.ProviderConnection, after: conn_registry.ProviderConnection) -> bool:
    """An API-key connection that is (or stays) on after the change and was off, or whose secret / endpoint moved."""
    if not after.enabled or after.auth_mode not in ("api_key", "bedrock_api_key"):
        return False
    return not before.enabled or any(getattr(before, f) != getattr(after, f) for f in CREDENTIAL_EDITS)


def _same(a, b) -> bool:
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)) and not isinstance(a, bool):
        return Decimal(str(a)) == Decimal(str(b))
    return a == b


def _db_value(field: str, value):
    return Decimal(str(value)) if field == "monthly_budget_usd" and value is not None else value


def _json_value(value):
    return float(value) if isinstance(value, Decimal) else value


async def update_connection(db, connection_key: str, changes: dict, *, actor: str) -> dict:
    """AA-1002 (S4): change the editable fields of one connection (``EDITABLE``) and audit it. Raises LookupError
    (unknown key, 404), ConnectionToggleRefused (422) and ConnectionEditUnavailable (503, migration 310 missing). A
    request that changes nothing writes no audit row (``changed: false``).

    When the change enables an API-key connection or moves its secret / endpoint while on, the key is read (the
    gateway's own fail-closed resolver) BEFORE the transaction opens (a slow Secrets Manager call never holds the row
    lock); the locked re-read then refuses the change if a credential field moved in between. The key is never
    returned, logged or written."""
    if not changes:
        raise ConnectionToggleRefused("nothing to change")
    checked = None
    if changes.get("enabled") or any(f in changes for f in CREDENTIAL_EDITS):
        row = await db.fetchrow(_SELECT_ONE_SQL, connection_key)
        if row is None:
            raise LookupError(f"unknown connection: {connection_key!r}")
        pre = conn_registry._row_to_connection(row)
        merged_pre = _validate(pre, changes)
        if _needs_credential_check(pre, merged_pre):
            await asyncio.to_thread(_check_credential, merged_pre)
        checked = tuple(getattr(pre, f) for f in _CREDENTIAL_FIELDS)
    async with db.transaction():
        row = await db.fetchrow(_SELECT_ONE_FOR_UPDATE_SQL, connection_key)
        if row is None:
            raise LookupError(f"unknown connection: {connection_key!r}")
        c = conn_registry._row_to_connection(row)
        merged = _validate(c, changes)
        if checked is not None and tuple(getattr(c, f) for f in _CREDENTIAL_FIELDS) != checked:
            raise ConnectionToggleRefused(
                f"connection {connection_key!r} changed while its secret was checked; send the request again")
        diff = [f for f in EDITABLE if f in changes and not _same(getattr(c, f), changes[f])]
        if diff:
            sets = ", ".join(f"{f} = ${i + 2}" for i, f in enumerate(diff))
            n = len(diff) + 2
            try:
                await db.execute(
                    f"UPDATE shared.llm_provider_connection SET {sets}, updated_at = now(), updated_by = ${n} "
                    "WHERE connection_key = $1",
                    connection_key, *[_db_value(f, changes[f]) for f in diff], actor)
            except asyncpg.exceptions.InsufficientPrivilegeError as e:
                raise ConnectionEditUnavailable(
                    "editing " + ", ".join(diff) + " needs database migration 310 (column grant); only the on/off "
                    "switch works until it is applied") from e
            await db.execute(
                "INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
                "VALUES ($1, $2, $3, $4, $5::jsonb)",
                actor[:64], "llm_connection_changed", "llm_provider_connection", connection_key,
                json.dumps({"before": {f: _json_value(getattr(c, f)) for f in diff},
                            "after": {f: _json_value(changes[f]) for f in diff},
                            "kind": c.kind, "secret_name": merged.secret_name, "base_url": merged.base_url}))
    if diff:
        # this process reads the new state at once; the other processes (worker) within the 5-minute registry cache
        conn_registry.invalidate()
        dep_registry.invalidate()
        from shared.llm_client.adapters import anthropic_messages
        anthropic_messages.forget_connection(connection_key)
    return {"connection_key": connection_key, "kind": c.kind, "enabled": merged.enabled, "changed": bool(diff),
            "changed_fields": diff, "updated_by": actor if diff else c.updated_by}


async def set_enabled(db, connection_key: str, *, enabled: bool, actor: str) -> dict:
    """Switch one connection on or off and audit it (S2's switch, now one case of ``update_connection``)."""
    out = await update_connection(db, connection_key, {"enabled": enabled}, actor=actor)
    out.pop("changed_fields", None)
    return out


# ── AA-1002 (S4): the admin's connection test ───────────────────────────────────────────────────────────────────

TEST_STAGE = "adhoc_conn_test"
TEST_ROLE = "validate"
TEST_MAX_TOKENS = 32
TEST_SYSTEM = "This is a connectivity test. Reply with the single word OK."
TEST_USER = "Are you there?"
TEXT_STYLES = ("converse", "anthropic_messages", "anthropic_native", "openai_chat")


def _testable(conn: conn_registry.ProviderConnection, deployments: list) -> list:
    """(deployment, catalog model) pairs of ``conn`` the gateway can call for a text test (embedding rows excluded)."""
    from shared.llm_client.catalog import get_model_sync
    from shared.llm_client.vendor_route import _callable

    out = []
    for d in deployments:
        model = get_model_sync(d.model_key)
        if model is None or model.api_style not in TEXT_STYLES or _callable(model, conn) is not None:
            continue
        out.append((d, model))
    return out


def _pick(pairs: list, model_key: Optional[str]):
    if model_key:
        return next((p for p in pairs if p[0].model_key == model_key), None)
    # an enabled deployment first, then the cheapest output price, then priority: one call of a few tokens
    return min(pairs, default=None, key=lambda p: (not p[0].enabled, p[1].price_out_per_mtok or float("inf"),
                                                   p[0].priority, p[0].model_key))


async def test_connection(pool, connection_key: str, *, model_key: Optional[str] = None, actor: str,
                          client=None) -> dict:
    """AA-1002 (S4): one small real call (``TEST_MAX_TOKENS`` output tokens) of one text model on the connection,
    through ``LLMClient.probe`` (the gateway's own adapters: same credential resolver, same limiter; the outcome
    counts on the deployment's circuit, a success closes it). Works on a switched-off connection or deployment: the
    point is to test before enabling. An answered call writes its ``shared.llm_call_log`` row (stage
    ``adhoc_conn_test``, role ``validate``, account = the connection); a billed but unusable reply is logged by the
    gateway. One audit row (``llm_connection_tested``). Raises LookupError (404) and ConnectionToggleRefused (422);
    a provider failure is the result (``ok: false`` + its class), not an exception."""
    from shared.llm_client.call_log import billed_fields, record_call_with_pool
    from shared.llm_client.client import LLMClient, UnusableReply, unusable_reply_cost
    from shared.llm_client.models import LLMRequest
    from shared.llm_client.vendor_route import VendorDeployment

    async with pool.acquire() as db:
        row = await db.fetchrow(_SELECT_ONE_SQL, connection_key)
        if row is None:
            raise LookupError(f"unknown connection: {connection_key!r}")
        conn = conn_registry._row_to_connection(row)
        deployments = [d for d in await dep_registry.fetch_deployments(db) if d.connection_key == connection_key]
    problems = dataclasses.replace(conn, enabled=True).policy_problems()
    if problems:
        raise ConnectionToggleRefused(f"connection {connection_key!r} cannot be tested: {'; '.join(problems)}")
    pairs = await asyncio.to_thread(_testable, conn, deployments)
    picked = _pick(pairs, model_key)
    if picked is None:
        raise ConnectionToggleRefused(
            f"no deployment of {model_key!r} on {connection_key!r} that the gateway can call" if model_key else
            f"connection {connection_key!r} has no text-model deployment the gateway can call")
    dep, model = picked
    target = VendorDeployment(dataclasses.replace(dep, enabled=True), dataclasses.replace(conn, enabled=True))
    max_tokens = min(TEST_MAX_TOKENS, model.max_output_tokens or TEST_MAX_TOKENS)
    request = LLMRequest(system_prompt=TEST_SYSTEM, user_prompt=TEST_USER, max_tokens=max_tokens, stage=TEST_STAGE)
    client = client or LLMClient()
    started = time.monotonic()
    result = {"connection_key": connection_key, "kind": conn.kind, "model_key": model.model_key,
              "wire_id": dep.wire_id, "deployment_enabled": dep.enabled, "connection_enabled": conn.enabled}
    try:
        resp = await asyncio.to_thread(client.probe, request, model, target)
    except UnusableReply as e:      # the provider answered and billed; the gateway logged the billed row
        result.update(ok=True, reply_usable=False, error_class=None, error=str(e)[:300],
                      cost_usd=round(unusable_reply_cost(e), 6))
    except Exception as e:  # noqa: BLE001 — a failed provider call is the test's answer
        result.update(ok=False, reply_usable=False, error_class=breaker.failure_class(e),
                      error=f"{type(e).__name__}: {e}"[:300], cost_usd=0.0)
    else:
        result.update(ok=True, reply_usable=True, error_class=None, error=None, cost_usd=round(resp.cost_usd, 6),
                      tokens_in=resp.input_tokens, tokens_out=resp.output_tokens,
                      provider_model=resp.provider_model, reply=(resp.content or "")[:40])
        await record_call_with_pool(
            pool, stage=TEST_STAGE, role=TEST_ROLE, model=resp.model_used, tokens_in=resp.input_tokens,
            tokens_out=resp.output_tokens, cost_usd=resp.cost_usd,
            quality_signal={"connection_test": connection_key, "actor": actor[:64]},
            stop_reason=resp.stop_reason, account=connection_key, fallback_used=False, **billed_fields(resp))
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            actor[:64], "llm_connection_tested", "llm_provider_connection", connection_key,
            json.dumps({k: result.get(k) for k in ("model_key", "wire_id", "ok", "reply_usable", "error_class",
                                                    "cost_usd", "latency_ms")}))
    return result
