"""shared/aws_client/accounts.py — how the admin names the ecosystem's AWS accounts (AA-998).

The code keys accounts as "acc2" (main infrastructure, the account this task runs in) and "acc3" (the LLM
account): llm_role_config.account_route, llm_call_log.account and the cost pages all use these keys, and they
stay as they are. The admin pages never print the keys: they show the 12-digit AWS account ID instead. This
repo is public, so no ID is written here; both come from the running task at run time:

  - acc2: STS GetCallerIdentity of the task's own credentials, read once per process
    (shared/aws_client/bronze.caller_account_id, lru_cache).
  - acc3: the task env BEDROCK_ACCT_PRIMARY (shared/llm_client/bedrock_satellite.ACC3_ACCOUNT_ID).

AA-1095 (S278): another Bedrock satellite is DATA, never a key in this code: every assume-role Bedrock connection of the
registry (shared.llm_provider_connection, e.g. the payer account that serves the chat models while acc3 is blocked)
is a live LLM account too, named by its ID from the connection's own env var (``resolved_account_id``). Without a
registry row (DB down, connection removed) its key falls back to "Retired account", as before.

AA-1002 (S6): a vendor-API call stores its connection key as the account ("openai-direct", "anthropic-direct"); the
admin names it by its vendor ("OpenAI API"). Any other stored account (the history of the fallback account retired
in AA-998) is "Retired account", with no ID. GET /admin/aws-accounts serves account_directory() to the frontend
(frontend/lib/awsAccounts.ts).
"""
from __future__ import annotations

import re
from typing import Any, Optional

import structlog
from botocore.exceptions import BotoCoreError, ClientError

from shared.aws_client import bronze
from shared.llm_client import bedrock_satellite, connections

logger = structlog.get_logger()

MAIN_ACCOUNT_KEY = "acc2"
LLM_ACCOUNT_KEY = "acc3"
RETIRED_ACCOUNT_KEY = "retired"
LIVE_ACCOUNT_KEYS = (LLM_ACCOUNT_KEY, MAIN_ACCOUNT_KEY)

RETIRED_ACCOUNT_LABEL = "Retired account"
UNRESOLVED_ACCOUNT_LABEL = "AWS account (ID unavailable)"
ACCOUNT_ROLES = {
    MAIN_ACCOUNT_KEY: "main infrastructure",
    LLM_ACCOUNT_KEY: "LLM",
    RETIRED_ACCOUNT_KEY: "former LLM fallback, history only",
}
_ACCOUNT_ID_RE = re.compile(r"^[0-9]{12}$")
# AA-1002 (S6): the vendor-API connections whose key a call-log row stores as its account (no AWS account behind
# them). Same keys as migration 304 / 306's connection rows; frontend/lib/awsAccounts.ts has the same map.
VENDOR_CONNECTION_LABELS = {
    "openai-direct": "OpenAI API",
    "anthropic-direct": "Anthropic API",
}


def satellite_connections() -> dict[str, connections.ProviderConnection]:
    """AA-1095: the assume-role Bedrock connections of the registry other than the built-in keys (acc3 / acc2), by key.
    Empty when the registry cannot be read. Never raises."""
    try:
        rows = connections.registry_rows_sync()
    except Exception as e:  # noqa: BLE001 - a label must never fail a page
        logger.warning("aws_account_registry_unreadable", error=str(e)[:200])
        return {}
    return {k: c for k, c in rows.items()
            if c.kind == "bedrock" and c.auth_mode == "assume_role"
            and k not in (*LIVE_ACCOUNT_KEYS, RETIRED_ACCOUNT_KEY) and k not in VENDOR_CONNECTION_LABELS}


def live_account_keys() -> tuple[str, ...]:
    """The live account keys in serving order: acc3, the registry's other satellites (by key), acc2."""
    return (LLM_ACCOUNT_KEY, *sorted(satellite_connections()), MAIN_ACCOUNT_KEY)


def is_live(key: Optional[str]) -> bool:
    return bool(key) and key in live_account_keys()


def role_of(key: str) -> str:
    """The account's role in ACCOUNT_ROLES; a registry satellite is an LLM account."""
    return ACCOUNT_ROLES.get(key) or ACCOUNT_ROLES[LLM_ACCOUNT_KEY]


def account_id(key: Optional[str]) -> Optional[str]:
    """The 12-digit AWS account ID of a live account key; None for any other key, or when it can not be read."""
    if key == LLM_ACCOUNT_KEY:
        value = bedrock_satellite.ACC3_ACCOUNT_ID
    elif key and key not in (MAIN_ACCOUNT_KEY, RETIRED_ACCOUNT_KEY) and key in satellite_connections():
        value = satellite_connections()[key].resolved_account_id()
    elif key == MAIN_ACCOUNT_KEY:
        try:
            value = bronze.caller_account_id()
        except (BotoCoreError, ClientError) as e:  # a label must never fail a page
            logger.warning("aws_account_id_unresolved", key=key, error=str(e)[:200])
            return None
    else:
        return None
    return value if value and _ACCOUNT_ID_RE.match(value) else None


def account_name(key: Optional[str]) -> Optional[str]:
    """What the admin shows for an account key: the account ID of a live account, the vendor of a vendor-API
    connection ("OpenAI API"), "Retired account" for any other stored account, None when there is no account (a Jev
    call, an OpenAI call logged before S6)."""
    if not key or key in ("unknown", "none"):
        return None
    if key in VENDOR_CONNECTION_LABELS:
        return VENDOR_CONNECTION_LABELS[key]
    if is_live(key):
        return account_id(key) or UNRESOLVED_ACCOUNT_LABEL
    return RETIRED_ACCOUNT_LABEL


def account_label(key: Optional[str]) -> Optional[str]:
    """account_name() with the account's role, e.g. "123456789012 (LLM)"; "Retired account" alone."""
    name = account_name(key)
    if name is None or not is_live(key):
        return name
    return f"{name} ({role_of(key)})"


def account_directory() -> dict[str, dict[str, Any]]:
    """Every account key the admin may meet, with its ID (None for the retired account), name, label and role (the
    registry's other Bedrock satellites included, AA-1095: the frontend treats every key here but "retired" as live)."""
    return {key: {"account_id": account_id(key), "name": account_name(key), "label": account_label(key),
                  "role": role_of(key)}
            for key in (*live_account_keys(), RETIRED_ACCOUNT_KEY)}
