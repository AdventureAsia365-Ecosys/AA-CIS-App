"""AA-1002 (gateway slice S1) — read side of shared.llm_provider_connection, and the credential policy.

A connection is one way into an LLM provider: a Bedrock account (assumed role or the task role), a vendor API
(Anthropic, OpenAI, Azure OpenAI) or a mock. It never holds a credential: an API-key connection names a Secrets
Manager secret, and that name must match ``^aa-llm/<env>/<name>$`` (``check_secret_name``, the same pattern as the
table's CHECK, migration 304). Its ``base_url`` must be an https URL on the kind's host allowlist (``check_base_url``,
``BASE_URL_ALLOWLIST``). Both checks run before any secret is read, so a row edited in the admin (slice S4) can never
make the gateway read another app's secret (rds, typesafe, booking ...) or send a key to an arbitrary host.

S1 was read-only and off the routing path. Since slice S3 the gateway routes through it:
``vendor_route.deployment_plan`` (which deployment of a model on which enabled connection), ``bedrock_satellite`` (the
role, ExternalId and region of an assume-role connection, instead of a hard-coded account table),
``catalog.account_for`` and ``embed`` (which Bedrock connections serve a profile). Which modules may read it is guarded
by ``tests/unit/test_aa1002_no_behaviour_change.py``.

``BUILTIN_CONNECTIONS`` mirrors the enabled Bedrock / OpenAI rows migration 304 seeded. It is never a routing source: it
only answers "how do I reach connection X" (``connection_or_builtin``) when the registry cannot be read or has no row
for X, so a DB outage or a cold process keeps today's satellite, acc2 and OpenAI paths working (lessons: a safe code
default must run when the DB is down).

Like ``catalog.py``: the table is tiny, loaded in one query and cached in process; the sync read never raises (a DB
failure or a table that is not migrated yet returns no connections). ``resolve_api_key`` fails closed.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import asyncpg
import structlog

from shared.secrets import get_database_url

logger = structlog.get_logger()

_CACHE_TTL_SECONDS = 300.0
_FAILURE_TTL_SECONDS = 60.0
_CONNECT_TIMEOUT_SECONDS = 3.0
# Design S1: a resolved key is kept 15 minutes (shared/secrets.py keeps its own secrets 1 hour); a 401 from the
# provider drops it at once (invalidate_credential) so a rotated key is read again on the next call.
_CREDENTIAL_TTL_SECONDS = 900.0

KINDS = ("bedrock", "anthropic", "openai", "azure_openai", "mock")
AUTH_MODES = {
    "bedrock": ("assume_role", "task_role", "bedrock_api_key"),
    "anthropic": ("api_key",),
    "openai": ("api_key",),
    "azure_openai": ("api_key",),
    "mock": ("none",),
}
DATA_SCOPES = ("aws_only", "vendor_direct")

SECRET_PREFIX = "aa-llm/"
# Same pattern as llm_provider_connection_secret_name_check (migration 304).
SECRET_NAME_RE = re.compile(r"^aa-llm/[a-z0-9-]+/[a-z0-9][a-z0-9-]*$")

# The hosts a connection of each kind may call. An exact host, or a ".suffix" for a per-resource host
# (Azure OpenAI: https://<resource>.openai.azure.com). bedrock / mock take no base_url (boto3 picks the endpoint).
BASE_URL_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "anthropic": ("api.anthropic.com",),
    "openai": ("api.openai.com",),
    "azure_openai": (".openai.azure.com",),
}

# Closed lists, same as the table CHECKs (migration 304): env vars a connection may name instead of a value.
ACCOUNT_ID_ENVS = frozenset({"BEDROCK_ACCT_PRIMARY"})   # today's satellite account ID (the repo is public)
CREDENTIAL_ENVS = frozenset({"OPENAI_API_KEY"})         # today's OpenAI key from the task definition, until S9
# AA-1002 (S2): the kind each env-held key belongs to. A connection may name a credential env only of its own kind, so
# an anthropic row naming OPENAI_API_KEY can never send the OpenAI key to api.anthropic.com (a kind not listed here,
# anthropic included, takes its key from secret_name only).
CREDENTIAL_ENV_KIND: dict[str, str] = {"OPENAI_API_KEY": "openai"}


class ConnectionPolicyError(ValueError):
    """A connection field breaks the credential policy (secret prefix, base_url allowlist, env allowlist)."""


class ConnectionCredentialError(RuntimeError):
    """No usable credential for a connection: disabled, policy problem, secret unreadable or empty. Fail closed."""


def check_secret_name(name: Optional[str]) -> None:
    """Raise ConnectionPolicyError unless ``name`` is an ``aa-llm/<env>/<name>`` secret name (or None)."""
    if name is None:
        return
    if not isinstance(name, str) or not SECRET_NAME_RE.fullmatch(name):
        raise ConnectionPolicyError(f"secret_name must match aa-llm/<env>/<name>: {name!r}")


def check_base_url(kind: str, url: Optional[str]) -> None:
    """Raise ConnectionPolicyError unless ``url`` is None or an https URL on ``kind``'s host allowlist."""
    if url is None:
        return
    allowed = BASE_URL_ALLOWLIST.get(kind)
    if not allowed:
        raise ConnectionPolicyError(f"a {kind!r} connection takes no base_url")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or not host or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.port not in (None, 443)):
        raise ConnectionPolicyError(f"base_url must be a plain https URL: {url!r}")
    for entry in allowed:
        if entry.startswith("."):
            label = host[: -len(entry)] if host.endswith(entry) else ""
            if label and "." not in label and re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", label):
                return
        elif host == entry:
            return
    raise ConnectionPolicyError(f"base_url host {host!r} is not allowed for kind {kind!r}")


@dataclass(frozen=True)
class ProviderConnection:
    connection_key: str
    kind: str
    auth_mode: str
    label: str
    data_scope: str
    enabled: bool = False
    account_id: Optional[str] = None
    account_id_env: Optional[str] = None
    region: Optional[str] = None
    role_name: Optional[str] = None
    external_id: Optional[str] = None
    secret_name: Optional[str] = None
    credential_env: Optional[str] = None
    base_url: Optional[str] = None
    timeout_s: Optional[int] = None
    max_retries: Optional[int] = None
    max_concurrency: Optional[int] = None
    rpm_limit: Optional[int] = None
    monthly_budget_usd: Optional[float] = None
    notes: Optional[str] = None
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    def resolved_account_id(self) -> Optional[str]:
        """``account_id``, else the value of the allowlisted ``account_id_env`` (None when unset or not allowed)."""
        if self.account_id:
            return self.account_id
        if self.account_id_env in ACCOUNT_ID_ENVS:
            return os.environ.get(self.account_id_env) or None
        return None

    def role_arn(self) -> Optional[str]:
        if self.auth_mode != "assume_role" or not self.role_name:
            return None
        account = self.resolved_account_id()
        return f"arn:aws:iam::{account}:role/{self.role_name}" if account else None

    def credential_source(self) -> str:
        """Where the call's credential comes from: sts | task_role | secret | env | none (missing)."""
        if self.auth_mode == "assume_role":
            return "sts"
        if self.auth_mode == "task_role":
            return "task_role"
        if self.auth_mode == "none":
            return "none"
        if self.secret_name:
            return "secret"
        if self.credential_env:
            return "env"
        return "missing"

    def policy_problems(self) -> list[str]:
        """Every reason this row may not be used (empty = usable as far as its fields go). Never raises."""
        problems: list[str] = []
        if self.kind not in KINDS:
            problems.append(f"unknown kind {self.kind!r}")
        elif self.auth_mode not in AUTH_MODES[self.kind]:
            problems.append(f"auth_mode {self.auth_mode!r} does not fit kind {self.kind!r}")
        if self.data_scope not in DATA_SCOPES:
            problems.append(f"unknown data_scope {self.data_scope!r}")
        for check in (lambda: check_secret_name(self.secret_name), lambda: check_base_url(self.kind, self.base_url)):
            try:
                check()
            except ConnectionPolicyError as e:
                problems.append(str(e))
        if self.credential_env is not None and self.credential_env not in CREDENTIAL_ENVS:
            problems.append(f"credential_env {self.credential_env!r} is not allowed")
        elif self.credential_env is not None and CREDENTIAL_ENV_KIND.get(self.credential_env) != self.kind:
            problems.append(f"credential_env {self.credential_env!r} is not a {self.kind!r} key "
                            "(this kind takes its key from secret_name)")
        if self.account_id_env is not None and self.account_id_env not in ACCOUNT_ID_ENVS:
            problems.append(f"account_id_env {self.account_id_env!r} is not allowed")
        if self.auth_mode == "assume_role":
            missing = [n for n in ("role_name", "external_id", "region") if not getattr(self, n)]
            if not self.account_id and not self.account_id_env:
                missing.append("account_id")
            if missing:
                problems.append("assume_role needs " + ", ".join(missing))
        if self.auth_mode in ("api_key", "bedrock_api_key") and self.credential_source() == "missing":
            problems.append("no secret_name or credential_env")
        return problems


def _num(v) -> Optional[float]:
    return float(v) if v is not None else None


_COLUMNS = (
    "connection_key", "kind", "auth_mode", "label", "account_id", "account_id_env", "region", "role_name",
    "external_id", "secret_name", "credential_env", "base_url", "data_scope", "timeout_s", "max_retries",
    "max_concurrency", "rpm_limit", "monthly_budget_usd", "enabled", "notes", "updated_at", "updated_by",
)
_SELECT_SQL = f"SELECT {', '.join(_COLUMNS)} FROM shared.llm_provider_connection ORDER BY connection_key"


def _row_to_connection(row) -> ProviderConnection:
    d = {k: row[k] for k in _COLUMNS}
    d["monthly_budget_usd"] = _num(d["monthly_budget_usd"])
    d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
    return ProviderConnection(**d)


async def _connect():
    return await asyncpg.connect(get_database_url(), ssl="require", timeout=_CONNECT_TIMEOUT_SECONDS)


async def fetch_connections(conn) -> dict[str, ProviderConnection]:
    rows = await conn.fetch(_SELECT_SQL)
    return {r["connection_key"]: _row_to_connection(r) for r in rows}


async def _fetch_all() -> dict[str, ProviderConnection]:
    conn = await _connect()
    try:
        return await fetch_connections(conn)
    finally:
        await conn.close()


def run_sync(coro_fn):
    """Run ``coro_fn()`` to completion from sync code, also when a loop is already running (catalog.py pattern)."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_fn())
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_fn())).result(timeout=10)


_connections: dict[str, ProviderConnection] = {}
_loaded_at = 0.0
_failed_at = 0.0
# One reload at a time (AA-1002 S2: the registry is on the call path now): when the TTL runs out, the first thread
# reloads and the others wait for it and read its result instead of each opening its own DB connection.
_load_lock = threading.Lock()


def _fresh(now: float) -> bool:
    """True while a loaded registry (5 min) or a failed load (60 s) is still within its TTL."""
    return bool((_loaded_at and now - _loaded_at < _CACHE_TTL_SECONDS)
                or (_failed_at and now - _failed_at < _FAILURE_TTL_SECONDS))


def _ensure_loaded() -> None:
    if _fresh(time.monotonic()):
        return
    with _load_lock:
        now = time.monotonic()
        if _fresh(now):
            return              # another thread reloaded while this one waited
        _load(now)


def _load(now: float) -> None:
    global _connections, _loaded_at, _failed_at
    try:
        _connections = run_sync(_fetch_all)
        _loaded_at, _failed_at = now, 0.0
    except Exception as e:  # noqa: BLE001 — never raises: a missing table (before 304) or a DB outage
        _failed_at = now
        logger.warning("llm_connection_registry_read_failed", error=str(e)[:300],
                       hint="no connection registry (migration 304 not applied, or DB down)")


def get_connection_sync(connection_key: str) -> Optional[ProviderConnection]:
    _ensure_loaded()
    return _connections.get(connection_key)


def registry_rows_sync() -> dict[str, ProviderConnection]:
    """Every loaded connection row (empty when the registry is unreadable, not migrated or empty). Never raises."""
    _ensure_loaded()
    return dict(_connections)


# AA-1002 (S3): the enabled rows migration 304 seeded for today's paths, field for field (tests compare them with the
# SQL). Credentials only (see the module docstring): routing never reads this table.
BUILTIN_CONNECTIONS: dict[str, ProviderConnection] = {
    "acc3": ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role",
                               label="Bedrock, LLM account (assume role)", data_scope="aws_only", enabled=True,
                               account_id_env="BEDROCK_ACCT_PRIMARY", region="us-west-1",
                               role_name="AA3-Bedrock-Invoker", external_id="aa296-satellite-bedrock-acc3"),
    "acc2": ProviderConnection(connection_key="acc2", kind="bedrock", auth_mode="task_role",
                               label="Bedrock, main account (task role)", data_scope="aws_only", enabled=True,
                               region="us-west-1"),
    "openai-direct": ProviderConnection(connection_key="openai-direct", kind="openai", auth_mode="api_key",
                                        label="OpenAI API (direct)", data_scope="vendor_direct", enabled=True,
                                        credential_env="OPENAI_API_KEY", base_url="https://api.openai.com/v1"),
}


def connection_or_builtin(connection_key: str) -> Optional[ProviderConnection]:
    """The registry row of ``connection_key``, else its built-in mirror, else None. For reaching a connection (role,
    ExternalId, region), not for deciding whether to route to it. The mirror is used when the registry is unreadable,
    has no such row, or the row breaks the policy (``policy_problems``: e.g. an assume-role row without its role or
    account): a half-edited row never breaks today's path. Never raises."""
    row = get_connection_sync(connection_key)
    builtin = BUILTIN_CONNECTIONS.get(connection_key)
    if row is not None and builtin is not None and row.policy_problems():
        logger.warning("llm_connection_row_unusable_builtin_used", connection=connection_key,
                       problems="; ".join(row.policy_problems())[:300])
        return builtin
    return row if row is not None else builtin


def bedrock_connection_keys() -> tuple[str, ...]:
    """The Bedrock connections a catalog ``bedrock_profile_ids`` key may name (registry rows and the built-in mirror),
    satellites (assume_role) first, then task-role ones, each by key: ("acc3", "acc2") for the 304 seed. Replaces the
    hard-coded catalog.SERVING_ACCOUNTS (AA-998: acc3 before acc2, an account no connection names is never chosen).
    ``enabled`` is not read here: switching a connection off is a routing decision (vendor_route.deployment_plan), and
    this list feeds the no-registry / rollback path (catalog.account_for), which must stay today's. Never raises."""
    rows = {**BUILTIN_CONNECTIONS, **registry_rows_sync()}
    usable = [c for c in rows.values() if c.kind == "bedrock" and c.auth_mode in ("assume_role", "task_role")]
    usable.sort(key=lambda c: (0 if c.auth_mode == "assume_role" else 1, c.connection_key))
    return tuple(c.connection_key for c in usable)


def invalidate() -> None:
    global _loaded_at, _failed_at
    _loaded_at = 0.0
    _failed_at = 0.0


# ── credential resolver (no caller on the routing path before S2 / S3) ───────────────────────────────────────────

_credentials: dict[str, tuple[str, float]] = {}


def _fetch_secret(secret_name: str) -> str:
    from shared.secrets import _fetch_secret_sdk
    return _fetch_secret_sdk(secret_name)


def _key_from_secret(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise ConnectionCredentialError("secret is not valid JSON") from e
        raw = str(data.get("api_key") or "").strip() if isinstance(data, dict) else ""
    return raw


def resolve_api_key(conn: ProviderConnection) -> str:
    """The API key of an api-key connection. Fails closed (ConnectionCredentialError) when the connection is disabled,
    breaks the policy, names no credential, or the secret / env value is unreadable or empty. The secret name and the
    env var are checked BEFORE anything is read. Never logs the value."""
    if not conn.enabled:
        raise ConnectionCredentialError(f"connection {conn.connection_key!r} is disabled")
    if conn.auth_mode not in ("api_key", "bedrock_api_key"):
        raise ConnectionCredentialError(f"connection {conn.connection_key!r} ({conn.auth_mode}) has no API key")
    problems = conn.policy_problems()
    if problems:
        raise ConnectionCredentialError(f"connection {conn.connection_key!r} refused: {'; '.join(problems)}")
    if conn.secret_name:
        cache_key = f"{conn.connection_key}|{conn.secret_name}"
        hit = _credentials.get(cache_key)
        if hit and time.monotonic() - hit[1] < _CREDENTIAL_TTL_SECONDS:
            return hit[0]
        try:
            key = _key_from_secret(_fetch_secret(conn.secret_name))
        except ConnectionCredentialError:
            raise
        except Exception as e:  # noqa: BLE001 — AccessDenied, ResourceNotFound, network: all fail closed
            raise ConnectionCredentialError(
                f"connection {conn.connection_key!r}: secret unreadable ({type(e).__name__})") from e
        if not key:
            raise ConnectionCredentialError(f"connection {conn.connection_key!r}: secret holds no key")
        _credentials[cache_key] = (key, time.monotonic())
        return key
    key = (os.environ.get(conn.credential_env or "") or "").strip()
    if not key:
        raise ConnectionCredentialError(f"connection {conn.connection_key!r}: env {conn.credential_env} is empty")
    return key


def invalidate_credential(connection_key: str) -> None:
    """Drop the cached key of a connection (after a 401), so the next call reads the secret again."""
    for k in [k for k in _credentials if k.split("|", 1)[0] == connection_key]:
        _credentials.pop(k, None)


# ── AA-1002 (S10): the read-only usage key of a connection's provider (nightly cost reconciliation) ─────────────────

def _usage_key_from_secret(raw: str) -> str:
    """The key in a usage secret: a JSON object's ``admin_key`` (else ``api_key``), or the raw string."""
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise ConnectionCredentialError("usage secret is not valid JSON") from e
        if not isinstance(data, dict):
            return ""
        raw = str(data.get("admin_key") or data.get("api_key") or "").strip()
    return raw


def resolve_usage_key(connection_key: str, usage_secret_name: Optional[str]) -> str:
    """The read-only usage / admin key a reconcile source reads the provider's cost with
    (``shared.llm_connection_cost_policy.usage_secret_name``, migration 316). Fails closed
    (ConnectionCredentialError): no name, a name outside ``aa-llm/<env>/<name>`` (checked BEFORE anything is read), the
    secret unreadable or empty. Never cached, never logged: it runs once per connection per nightly run, and the key is
    a high-privilege secret (it can read the whole organisation's usage)."""
    if not usage_secret_name:
        raise ConnectionCredentialError(f"connection {connection_key!r}: no usage key configured")
    try:
        check_secret_name(usage_secret_name)
    except ConnectionPolicyError as e:
        raise ConnectionCredentialError(f"connection {connection_key!r}: usage key refused: {e}") from e
    try:
        key = _usage_key_from_secret(_fetch_secret(usage_secret_name))
    except ConnectionCredentialError:
        raise
    except Exception as e:  # noqa: BLE001 — AccessDenied, ResourceNotFound, network: all fail closed
        raise ConnectionCredentialError(
            f"connection {connection_key!r}: usage secret unreadable ({type(e).__name__})") from e
    if not key:
        raise ConnectionCredentialError(f"connection {connection_key!r}: usage secret holds no key")
    return key
