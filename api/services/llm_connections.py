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

AA-1002 (S10) — each connection's budget policy (``shared.llm_connection_cost_policy``, migration 317: alert percent,
hard stop, the NAME of a read-only usage key and the provider workspace / project) is part of the listing
(``cost_policy``, ``reconcile_source``) and of the same PATCH (``POLICY_EDITABLE``, upserted in the same transaction and
audit row). ``costs`` backs ``GET /admin/llm-connections/costs``: the month-to-date spend against each budget and the
latest nightly reconciliation rows (services/llm_cost/reconcile.py).

AA-1017 (registry v2, migration 322; design D9) — the secret NAME and the base URL are migration-only: they left
``EDITABLE`` (the PATCH answers 422 "not editable here", and 322 revoked cis_app's UPDATE on both columns); the
admin keeps the switch, the limits, the budget and notes, plus the two provider limits of 322 (``daily_credit_limit``,
``low_balance_usd``). The listing stays the LLM kinds (``fetch_connections``); a decide / data / guard row (typesafe,
dataforseo, geoapify, bedrock_guardrails) can be edited by key (its limits; Jev reads its timeout / retries from its
row) but never switched (its client does not read ``enabled``). Each listed row also carries the 322 fields.

AA-1002 (S4) — ``test_connection`` backs ``POST /admin/llm-connections/{connection_key}/test``: one small real call
through ``LLMClient.probe``, logged in ``shared.llm_call_log`` (stage ``adhoc_conn_test``) and audited
(``llm_connection_tested``).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import time
from decimal import Decimal
from typing import Optional

import asyncpg

from services.llm_cost import reconcile as cost_reconcile
from shared import cost_guard
from shared.llm_client import breaker
from shared.llm_client import connections as conn_registry
from shared.llm_client import deployments as dep_registry

# The connection kinds whose `enabled` flag the gateway reads (S2: anthropic; since S3 every kind with an adapter,
# vendor_route._CALLABLE). Enabling another kind (azure_openai, mock) would change nothing, so it is refused.
TOGGLE_KINDS = ("bedrock", "anthropic", "openai", "azure_openai")     # S9: azure_openai (adapters/openai_chat.py)

# AA-1002 (S4): what PATCH /admin/llm-connections/{key} may change. `enabled` is migration 306's grant; the others
# need migration 310 (until then the UPDATE answers permission denied → ConnectionEditUnavailable, 503). Never kind,
# auth_mode, account, role, ExternalId, region, credential_env or data_scope (identity / data policy: migrations only).
# AA-1017 (D9, migration 322): secret_name and base_url are no longer here (migration-only); the two provider limits
# of 322 are.
EDITABLE = ("enabled", "timeout_s", "max_retries", "max_concurrency", "rpm_limit", "monthly_budget_usd", "notes",
            "daily_credit_limit", "low_balance_usd")
# The fields a credential check depends on, as an edit could move them (none since AA-1017: kept for the check below).
CREDENTIAL_EDITS: tuple = ()
# AA-1017: the fields a decide / data / guard row (connections.PROVIDER_KINDS) may change through this PATCH.
PROVIDER_EDITABLE = ("timeout_s", "max_retries", "notes", "daily_credit_limit", "low_balance_usd")
# Fields that need migration 322 (the column grant); before it the UPDATE answers permission denied -> 503.
V2_FIELDS = ("daily_credit_limit", "low_balance_usd")

# AA-1002 (S10): the budget policy fields of the same PATCH -> their column in shared.llm_connection_cost_policy (317).
POLICY_EDITABLE = {"budget_alert_pct": "alert_pct", "budget_hard_stop": "hard_stop",
                   "usage_secret_name": "usage_secret_name", "usage_scope_id": "usage_scope_id"}
USAGE_FIELDS = ("usage_secret_name", "usage_scope_id")
_SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ConnectionToggleRefused(ValueError):
    """The change is refused (kind without an adapter, policy problem, unreadable credential, field not editable)."""


class ConnectionEditUnavailable(RuntimeError):
    """The database refused the UPDATE (permission denied): the column grant of migration 310 is not applied yet."""


def policy_view(c: conn_registry.ProviderConnection, p: cost_guard.ConnectionCostPolicy) -> dict:
    """The budget policy and reconcile source of a connection, as the listing shows it (a secret NAME, never a
    value)."""
    source = cost_reconcile.SOURCES.get(c.kind, cost_reconcile.SOURCE_NONE)
    needs_key = c.kind in cost_reconcile.VENDOR_FETCHERS
    return {
        "cost_policy": {"alert_pct": p.alert_pct, "hard_stop": p.hard_stop, "usage_secret_name": p.usage_secret_name,
                        "usage_scope_id": p.usage_scope_id, "stored": p.stored, "updated_at": p.updated_at,
                        "updated_by": p.updated_by},
        "reconcile_source": source,
        # bedrock: the stored Cost Explorer snapshot (no key); a vendor API: only with a usage key; others: none
        "reconcile_configured": (c.kind == "bedrock") or (needs_key and bool(p.usage_secret_name)),
        "usage_key_supported": needs_key,
    }


def build(connections: dict, deployments: list, policies: Optional[dict] = None) -> dict:
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
            # AA-1017 (migration 322): read-only here, except the two limits
            "failure_domain": c.failure_domain,
            "served_roles": list(c.served_roles) if c.served_roles is not None else None,
            "access_state": c.access_state, "last_probe_at": c.last_probe_at, "balance_source": c.balance_source,
            "daily_credit_limit": c.daily_credit_limit, "low_balance_usd": c.low_balance_usd,
            "editable_fields": list(EDITABLE),
            "notes": c.notes, "updated_at": c.updated_at, "updated_by": c.updated_by,
            "deployments": sorted(by_conn.get(key, []), key=lambda d: (d["model_key"], d["priority"])),
            **policy_view(c, cost_guard.policy_of(policies or {}, key)),
        })
    return {"schema_ready": True, "routing_uses_registry": True, "connections": rows,
            "cost_policy_ready": policies is not None}


async def read(conn) -> dict:
    try:
        connections = await conn_registry.fetch_connections(conn)
        deployments = await dep_registry.fetch_deployments(conn)
    except asyncpg.exceptions.UndefinedTableError:
        return {"schema_ready": False, "routing_uses_registry": False, "connections": []}
    policies = await _policies_or_none(conn)
    return build(connections, deployments, policies)


async def _policies_or_none(conn) -> Optional[dict]:
    """AA-1002 (S10): the stored policies, or None before migration 317 (the listing says cost_policy_ready false)."""
    out = await cost_guard.read_cost_policies(conn, missing=_NOT_READY)
    return None if out is _NOT_READY else out


_NOT_READY: dict = {"__not_ready__": None}


# AA-1017: the edit reads the 322 columns too (the two provider limits are diffed and audited against the stored
# value). Before 322 the read answers UndefinedColumnError -> ConnectionEditUnavailable (503); readiness needs 322.
_SELECT_ONE_SQL = conn_registry._SELECT_ONE_V2_SQL
_SELECT_ONE_FOR_UPDATE_SQL = _SELECT_ONE_SQL + " FOR UPDATE"
_NEEDS_322 = "editing a connection needs database migration 322 (the provider connection v2 columns)"

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
    """The connection with ``changes`` applied, or ConnectionToggleRefused. AA-1017 (D9): the secret name and base URL
    are not editable (migration only); an enabled result must have no policy problem at all."""
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        why = (" (the secret name and the endpoint change by migration only)"
               if set(unknown) & {"secret_name", "base_url"} else "")
        raise ConnectionToggleRefused(f"not editable here: {', '.join(unknown)}{why}")
    if not c.is_llm:
        refused = sorted(set(changes) - set(PROVIDER_EDITABLE))
        if refused:
            raise ConnectionToggleRefused(
                f"connection {c.connection_key!r} is a {c.kind!r} provider: only {', '.join(PROVIDER_EDITABLE)} can "
                f"be changed here (not {', '.join(refused)})")
    if changes.get("enabled") and c.kind not in TOGGLE_KINDS:
        raise ConnectionToggleRefused(
            f"connection {c.connection_key!r} is a {c.kind!r} connection: the gateway has no adapter for it yet, so "
            "switching it on would change nothing")
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
    return Decimal(str(value)) if field in ("monthly_budget_usd", "low_balance_usd") and value is not None else value


def _json_value(value):
    return float(value) if isinstance(value, Decimal) else value


_POLICY_FOR_UPDATE_SQL = ("SELECT alert_pct, hard_stop, usage_secret_name, usage_scope_id "
                          "FROM shared.llm_connection_cost_policy WHERE connection_key = $1 FOR UPDATE")
_POLICY_DEFAULTS = {"budget_alert_pct": cost_guard.DEFAULT_CONNECTION_ALERT_PCT, "budget_hard_stop": True,
                    "usage_secret_name": None, "usage_scope_id": None}


def _validate_policy(c: conn_registry.ProviderConnection, changes: dict) -> None:
    """AA-1002 (S10): refuse a policy change the gateway / reconcile job could not use (ConnectionToggleRefused). The
    usage secret NAME is checked against the aa-llm/ policy before anything reads it."""
    if "budget_alert_pct" in changes:
        v = changes["budget_alert_pct"]
        if v is None or not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 100:
            raise ConnectionToggleRefused("budget_alert_pct must be a whole number from 1 to 100")
    if "budget_hard_stop" in changes and not isinstance(changes["budget_hard_stop"], bool):
        raise ConnectionToggleRefused("budget_hard_stop must be true or false")
    if any(changes.get(f) is not None for f in USAGE_FIELDS) and c.kind not in cost_reconcile.VENDOR_FETCHERS:
        raise ConnectionToggleRefused(
            f"connection {c.connection_key!r} is a {c.kind!r} connection: it has no usage key "
            f"(reconcile source: {cost_reconcile.SOURCES.get(c.kind, 'none')})")
    if changes.get("usage_secret_name") is not None:
        try:
            conn_registry.check_secret_name(changes["usage_secret_name"])
        except conn_registry.ConnectionPolicyError as e:
            raise ConnectionToggleRefused(f"usage key refused: {e}") from e
    scope = changes.get("usage_scope_id")
    if scope is not None and not _SCOPE_ID_RE.fullmatch(scope):
        raise ConnectionToggleRefused("usage_scope_id must be a provider workspace / project id (letters, digits, _ -)")


def _check_usage_key(connection_key: str, name: str) -> None:
    """Read the usage key once (the reconcile job's own fail-closed resolver) and drop it."""
    try:
        conn_registry.resolve_usage_key(connection_key, name)
    except conn_registry.ConnectionCredentialError as e:
        raise ConnectionToggleRefused(str(e)) from e


async def _apply_policy(db, connection_key: str, changes: dict, actor: str) -> tuple[list[str], dict, dict]:
    """Upsert the policy row inside the caller's transaction. Returns (changed fields, before, after)."""
    try:
        row = await db.fetchrow(_POLICY_FOR_UPDATE_SQL, connection_key)
    except asyncpg.exceptions.UndefinedTableError as e:
        raise ConnectionEditUnavailable("budget policy fields need database migration 317") from e
    before = ({f: row[col] for f, col in POLICY_EDITABLE.items()} if row is not None else dict(_POLICY_DEFAULTS))
    diff = [f for f in POLICY_EDITABLE if f in changes and not _same(before[f], changes[f])]
    if not diff:
        return [], {}, {}
    after = {**before, **{f: changes[f] for f in diff}}
    cols = list(POLICY_EDITABLE.values())
    await db.execute(
        f"INSERT INTO shared.llm_connection_cost_policy (connection_key, {', '.join(cols)}, updated_by) "
        f"VALUES ($1, {', '.join(f'${i + 2}' for i in range(len(cols)))}, ${len(cols) + 2}) "
        f"ON CONFLICT (connection_key) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)}, "
        "updated_at = now(), updated_by = EXCLUDED.updated_by",
        connection_key, *[after[f] for f in POLICY_EDITABLE], actor[:128])
    return diff, {f: before[f] for f in diff}, {f: after[f] for f in diff}


async def update_connection(db, connection_key: str, changes: dict, *, actor: str) -> dict:
    """AA-1002 (S4): change the editable fields of one connection (``EDITABLE``) and audit it. Raises LookupError
    (unknown key, 404), ConnectionToggleRefused (422) and ConnectionEditUnavailable (503, migration 310 missing). A
    request that changes nothing writes no audit row (``changed: false``).

    When the change enables an API-key connection or moves its secret / endpoint while on, the key is read (the
    gateway's own fail-closed resolver) BEFORE the transaction opens (a slow Secrets Manager call never holds the row
    lock); the locked re-read then refuses the change if a credential field moved in between. The key is never
    returned, logged or written.

    AA-1002 (S10): the budget policy fields (``POLICY_EDITABLE``) go to shared.llm_connection_cost_policy (upsert) in
    the same transaction and audit row; a new usage key NAME is read once before the transaction (fail closed, never
    returned). 503 before migration 317."""
    if not changes:
        raise ConnectionToggleRefused("nothing to change")
    policy_changes = {f: v for f, v in changes.items() if f in POLICY_EDITABLE}
    changes = {f: v for f, v in changes.items() if f not in POLICY_EDITABLE}
    checked = None
    if changes.get("enabled") or any(f in changes for f in CREDENTIAL_EDITS) or policy_changes:
        try:
            row = await db.fetchrow(_SELECT_ONE_SQL, connection_key)
        except asyncpg.exceptions.UndefinedColumnError as e:
            raise ConnectionEditUnavailable(_NEEDS_322) from e
        if row is None:
            raise LookupError(f"unknown connection: {connection_key!r}")
        pre = conn_registry._row_to_connection(row)
        merged_pre = _validate(pre, changes) if changes else pre
        if changes and _needs_credential_check(pre, merged_pre):
            await asyncio.to_thread(_check_credential, merged_pre)
        if policy_changes:
            _validate_policy(pre, policy_changes)
            if policy_changes.get("usage_secret_name"):
                await asyncio.to_thread(_check_usage_key, connection_key, policy_changes["usage_secret_name"])
        checked = tuple(getattr(pre, f) for f in _CREDENTIAL_FIELDS)
    policy_diff: list[str] = []
    async with db.transaction():
        try:
            row = await db.fetchrow(_SELECT_ONE_FOR_UPDATE_SQL, connection_key)
        except asyncpg.exceptions.UndefinedColumnError as e:      # the transaction rolls back as this propagates
            raise ConnectionEditUnavailable(_NEEDS_322) from e
        if row is None:
            raise LookupError(f"unknown connection: {connection_key!r}")
        c = conn_registry._row_to_connection(row)
        merged = _validate(c, changes) if changes else c
        if checked is not None and tuple(getattr(c, f) for f in _CREDENTIAL_FIELDS) != checked:
            raise ConnectionToggleRefused(
                f"connection {connection_key!r} changed while its secret was checked; send the request again")
        diff = [f for f in EDITABLE if f in changes and not _same(getattr(c, f), changes[f])]
        if diff:
            sets = ", ".join(f"{f} = ${i + 2}" for i, f in enumerate(diff))
            needs = "322" if set(diff) & set(V2_FIELDS) else "310"
            n = len(diff) + 2
            try:
                await db.execute(
                    f"UPDATE shared.llm_provider_connection SET {sets}, updated_at = now(), updated_by = ${n} "
                    "WHERE connection_key = $1",
                    connection_key, *[_db_value(f, changes[f]) for f in diff], actor)
            except asyncpg.exceptions.InsufficientPrivilegeError as e:
                raise ConnectionEditUnavailable(
                    "editing " + ", ".join(diff) + f" needs database migration {needs} (column grant); only the "
                    "on/off switch works until it is applied") from e
            except asyncpg.exceptions.UndefinedColumnError as e:
                raise ConnectionEditUnavailable(
                    "editing " + ", ".join(diff) + " needs database migration 322 (the column)") from e
        policy_before: dict = {}
        policy_after: dict = {}
        if policy_changes:
            policy_diff, policy_before, policy_after = await _apply_policy(db, connection_key, policy_changes, actor)
        if diff or policy_diff:
            await db.execute(
                "INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
                "VALUES ($1, $2, $3, $4, $5::jsonb)",
                actor[:64], "llm_connection_changed", "llm_provider_connection", connection_key,
                json.dumps({"before": {**{f: _json_value(getattr(c, f)) for f in diff}, **policy_before},
                            "after": {**{f: _json_value(changes[f]) for f in diff}, **policy_after},
                            "kind": c.kind, "secret_name": merged.secret_name, "base_url": merged.base_url}))
    if diff:
        # this process reads the new state at once; the other processes (worker) within the 5-minute registry cache
        conn_registry.invalidate()
        dep_registry.invalidate()
        from shared.llm_client.adapters import anthropic_messages, openai_chat
        anthropic_messages.forget_connection(connection_key)
        openai_chat.forget_connection(connection_key)          # S9: the OpenAI-format client of the connection
    if diff or policy_diff:
        from shared.llm_client import connection_budget
        connection_budget.invalidate()
    changed = diff + policy_diff
    return {"connection_key": connection_key, "kind": c.kind, "enabled": merged.enabled, "changed": bool(changed),
            "changed_fields": changed, "updated_by": actor if changed else c.updated_by}


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


# ── AA-1002 (S10): budgets and the nightly cost reconciliation ──────────────────────────────────────────────────

async def costs(conn, *, days: int = 14) -> dict:
    """``GET /admin/llm-connections/costs``: per connection, the month-to-date spend against its monthly budget
    (``budget``: None without a budget) and the reconciliation rows of the last ``days`` days, newest first
    (``reconcile.last`` = the newest day). ``schema_ready`` false before migration 317 (budgets still shown: they need
    only 304). Reads no secret."""
    try:
        connections = await conn_registry.fetch_connections(conn)
    except asyncpg.exceptions.UndefinedTableError:
        return {"schema_ready": False, "connections": [], "tolerance": None}
    policies = await _policies_or_none(conn)
    budgets = await cost_guard.connection_budgets(conn, {k: c.monthly_budget_usd for k, c in connections.items()},
                                                  policies or {})
    recent = await cost_reconcile.read_recent(conn, days=days)
    rows = []
    for key in sorted(connections):
        c = connections[key]
        b = budgets.get(key)
        view = policy_view(c, cost_guard.policy_of(policies or {}, key))
        history = (recent or {}).get(key, [])
        rows.append({
            "connection_key": key, "kind": c.kind, "label": c.label, "enabled": c.enabled,
            "budget": b.summary() if b is not None else None,
            "reconcile": {"source": view["reconcile_source"], "configured": view["reconcile_configured"],
                          "last": history[0] if history else None, "days": history},
        })
    return {"schema_ready": policies is not None and recent is not None, "connections": rows,
            "tolerance": {"usd": cost_reconcile.TOLERANCE_USD, "pct": cost_reconcile.TOLERANCE_PCT},
            "window_days": days}


async def start_reconcile(pool, *, days: int, end_day, actor: str) -> dict:
    """``POST /admin/llm-connections/reconcile``: queue one llm_cost_reconcile run ($0; see the job's docstring)."""
    from services.jobs.llm_cost_reconcile_job import KIND, enqueue_manual

    cost_reconcile.window(days, end_day)          # ValueError (422) before anything is queued
    job_id, created = await enqueue_manual(pool, days=days, end_day=end_day, actor=actor)
    return {"job_id": job_id, "created": created, "kind": KIND, "days": days,
            "end_day": end_day.isoformat() if end_day else None}
