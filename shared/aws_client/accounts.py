"""shared/aws_client/accounts.py — how the admin names the ecosystem's AWS accounts (AA-998).

The code keys accounts as "acc2" (main infrastructure, the account this task runs in) and "acc3" (the LLM
account): llm_role_config.account_route, llm_call_log.account and the cost pages all use these keys, and they
stay as they are. The admin pages never print the keys: they show the 12-digit AWS account ID instead. This
repo is public, so no ID is written here; both come from the running task at run time:

  - acc2: STS GetCallerIdentity of the task's own credentials, read once per process
    (shared/aws_client/bronze.caller_account_id, lru_cache).
  - acc3: the task env BEDROCK_ACCT_PRIMARY (shared/llm_client/bedrock_satellite.ACC3_ACCOUNT_ID).

Any other stored account (the history of the fallback account retired in AA-998) is "Retired account", with no
ID. GET /admin/aws-accounts serves account_directory() to the frontend (frontend/lib/awsAccounts.ts).
"""
from __future__ import annotations

import re
from typing import Any, Optional

import structlog
from botocore.exceptions import BotoCoreError, ClientError

from shared.aws_client import bronze
from shared.llm_client import bedrock_satellite

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


def account_id(key: Optional[str]) -> Optional[str]:
    """The 12-digit AWS account ID of a live account key; None for any other key, or when it can not be read."""
    if key == LLM_ACCOUNT_KEY:
        value = bedrock_satellite.ACC3_ACCOUNT_ID
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
    """What the admin shows for an account key: the account ID of a live account, "Retired account" for any other
    stored account, None when there is no account (an OpenAI or Jev call)."""
    if not key or key in ("unknown", "none"):
        return None
    if key in LIVE_ACCOUNT_KEYS:
        return account_id(key) or UNRESOLVED_ACCOUNT_LABEL
    return RETIRED_ACCOUNT_LABEL


def account_label(key: Optional[str]) -> Optional[str]:
    """account_name() with the account's role, e.g. "111111111111 (LLM)"; "Retired account" alone."""
    name = account_name(key)
    if name is None or key not in LIVE_ACCOUNT_KEYS:
        return name
    return f"{name} ({ACCOUNT_ROLES[key]})"


def account_directory() -> dict[str, dict[str, Any]]:
    """Every account key the admin may meet, with its ID (None for the retired account), name, label and role."""
    return {key: {"account_id": account_id(key), "name": account_name(key), "label": account_label(key),
                  "role": ACCOUNT_ROLES[key]}
            for key in (*LIVE_ACCOUNT_KEYS, RETIRED_ACCOUNT_KEY)}
