"""AA-1095 (S278) — Bedrock chat models served from acc1 while AWS blocks acc3 (root ADR 0001a, Note S278).

acc1 comes back as DATA only: the registry connection 'acc1' of CIS migration 406 (assume_role, account from the env
var BEDROCK_ACCT_PAYER that AA-CIS-Infra sets on the api and worker tasks). The runtime code names no account key for
it (tests/unit/test_aa998_no_acc1_runtime.py still holds): the satellite client, the account directory of the admin
and the cost pages read the registry. Without the registry row, the key is refused / shown as "Retired account", as
before. AWS documentation example accounts only (this repo is public).
"""
import re
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.aws_client import accounts
from shared.llm_client import bedrock_satellite as bs
from shared.llm_client import connections

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "api" / "migrations"
PAYER_ID = "777788889999"
ACC3_ID = "444455556666"

PAYER = connections.ProviderConnection(
    connection_key="acc1", kind="bedrock", auth_mode="assume_role", label="Bedrock, payer account (assume role)",
    data_scope="aws_only", enabled=True, account_id_env="BEDROCK_ACCT_PAYER", region="us-west-1",
    role_name="AA1-Bedrock-Invoker", external_id="aa1095-satellite-bedrock-acc1",
    failure_domain="bedrock:BEDROCK_ACCT_PAYER:aws_only", served_roles=("writer", "judge", "validate"))


@pytest.fixture
def registry(monkeypatch):
    """The registry holds acc3 (its 304 mirror) and the 406 payer row; the payer env is set."""
    rows = {"acc3": connections.BUILTIN_CONNECTIONS["acc3"], "acc1": PAYER}
    monkeypatch.setenv("BEDROCK_ACCT_PAYER", PAYER_ID)
    monkeypatch.setattr(connections, "run_sync", lambda fn: rows if fn is connections._fetch_all else [])
    monkeypatch.setattr("shared.llm_client.bedrock_satellite.ACC3_ACCOUNT_ID", ACC3_ID)
    connections.invalidate()
    bs._cached_sessions.clear()
    bs._cached_session_expiry.clear()
    yield rows
    connections.invalidate()
    bs._cached_sessions.clear()
    bs._cached_session_expiry.clear()


# ── the connection and its env ─────────────────────────────────────────────────────────────────────────────────────

_ENV_CHECK_RE = re.compile(r"CONSTRAINT llm_provider_connection_account_id_env_check\s+"
                           r"CHECK\s*\(\s*account_id_env IS NULL OR account_id_env IN \(([^)]*)\)")


def _latest_env_check() -> tuple[str, set]:
    """(file, values) of the last migration that (re)defines the account env CHECK."""
    latest = None
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        m = _ENV_CHECK_RE.search(path.read_text(encoding="utf-8"))
        if m:
            latest = (path.name, set(re.findall(r"'([^']*)'", m.group(1))))
    assert latest
    return latest


def test_the_env_allowlist_matches_the_latest_check():
    # MIG-4: the migration (406, its own PR) widens the CHECK before this code ships; on a branch without it the code
    # may know BEDROCK_ACCT_PAYER one step ahead of the DB, never the other way round.
    assert connections.ACCOUNT_ID_ENVS == frozenset({"BEDROCK_ACCT_PRIMARY", "BEDROCK_ACCT_PAYER"})
    name, values = _latest_env_check()
    assert values <= set(connections.ACCOUNT_ID_ENVS), name
    assert set(connections.ACCOUNT_ID_ENVS) - values <= {"BEDROCK_ACCT_PAYER"}, name


def test_the_payer_row_resolves_its_account_from_the_env(monkeypatch):
    monkeypatch.setenv("BEDROCK_ACCT_PAYER", PAYER_ID)
    assert PAYER.policy_problems() == []
    assert PAYER.resolved_account_id() == PAYER_ID
    assert PAYER.role_arn() == f"arn:aws:iam::{PAYER_ID}:role/AA1-Bedrock-Invoker"
    monkeypatch.delenv("BEDROCK_ACCT_PAYER")
    assert PAYER.resolved_account_id() is None and PAYER.role_arn() is None   # unset: no ARN, the call fails loudly


def test_the_builtin_mirror_stays_acc3_only():
    # Reaching acc1 needs its registry row: the no-registry path (DB down) keeps today's acc3 / acc2 / OpenAI.
    assert {k for k, c in connections.BUILTIN_CONNECTIONS.items() if c.auth_mode == "assume_role"} == {"acc3"}


def test_satellites_order_puts_the_payer_with_acc3_before_the_task_role(registry):
    assert connections.bedrock_connection_keys() == ("acc1", "acc3", "acc2")


# ── the satellite client ───────────────────────────────────────────────────────────────────────────────────────────

def test_the_satellite_client_assumes_the_payer_role_with_its_external_id(registry):
    sts = MagicMock()
    sts.assume_role.return_value = {"Credentials": {
        "AccessKeyId": "a", "SecretAccessKey": "s", "SessionToken": "t",
        "Expiration": MagicMock(timestamp=lambda: 4_102_444_800.0)}}
    with patch.object(bs.boto3, "client", return_value=sts), patch.object(bs.boto3, "Session") as m_session:
        bs.get_satellite_client("bedrock-runtime", account="acc1")
    kwargs = sts.assume_role.call_args.kwargs
    assert kwargs["RoleArn"] == f"arn:aws:iam::{PAYER_ID}:role/AA1-Bedrock-Invoker"
    assert kwargs["ExternalId"] == "aa1095-satellite-bedrock-acc1"
    assert m_session.call_args.kwargs["region_name"] == "us-west-1"
    assert bs.inference_profile_arn("acc1", "global.anthropic.claude-sonnet-5-5") == (
        f"arn:aws:bedrock:us-west-1:{PAYER_ID}:inference-profile/global.anthropic.claude-sonnet-5-5")


def test_without_its_registry_row_the_key_is_refused_before_sts():
    with patch.object(bs.boto3, "client") as m_client:
        with pytest.raises(ValueError, match="Unknown satellite account"):
            bs.get_satellite_client("bedrock-runtime", account="acc1")
    m_client.assert_not_called()


# ── how the admin names it ─────────────────────────────────────────────────────────────────────────────────────────

def test_the_directory_names_the_payer_by_its_id_with_the_llm_role(registry):
    assert accounts.live_account_keys() == ("acc3", "acc1", "acc2")
    assert accounts.account_id("acc1") == PAYER_ID
    assert accounts.account_name("acc1") == PAYER_ID
    assert accounts.account_label("acc1") == f"{PAYER_ID} (LLM)"
    d = accounts.account_directory()
    assert set(d) == {"acc2", "acc3", "acc1", "retired"}
    assert d["acc1"] == {"account_id": PAYER_ID, "name": PAYER_ID, "label": f"{PAYER_ID} (LLM)", "role": "LLM"}
    shown = " ".join(str(v["name"]) + " " + str(v["label"]) + " " + v["role"] for v in d.values())
    assert not re.search(r"acc[0-9]", shown)


def test_an_unset_payer_env_shows_the_unresolved_label(registry, monkeypatch):
    monkeypatch.delenv("BEDROCK_ACCT_PAYER")
    assert accounts.account_id("acc1") is None
    assert accounts.account_name("acc1") == accounts.UNRESOLVED_ACCOUNT_LABEL


def test_without_the_registry_the_key_stays_retired():
    assert accounts.account_name("acc1") == accounts.RETIRED_ACCOUNT_LABEL
    assert set(accounts.account_directory()) == {"acc2", "acc3", "retired"}


def test_a_vendor_or_task_role_row_is_never_a_live_aws_account(monkeypatch):
    rows = {"anthropic-direct": connections.ProviderConnection(
                connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key", label="a",
                data_scope="vendor_direct", enabled=True, secret_name="aa-llm/dev/anthropic-direct",
                base_url="https://api.anthropic.com"),
            "acc2-usw2": connections.ProviderConnection(
                connection_key="acc2-usw2", kind="bedrock", auth_mode="task_role", label="b",
                data_scope="aws_only", enabled=False, region="us-west-2")}
    monkeypatch.setattr(connections, "run_sync", lambda fn: rows if fn is connections._fetch_all else [])
    connections.invalidate()
    assert accounts.satellite_connections() == {}
    assert accounts.account_name("acc2-usw2") == accounts.RETIRED_ACCOUNT_LABEL
    assert accounts.account_name("anthropic-direct") == "Anthropic API"


# ── the cost pages ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_spend_split_keeps_the_payer_bucket(registry):
    from api.services import cost_ops
    assert cost_ops._account_of("bedrock", "acc1") == "acc1"
    assert cost_ops._account_of("bedrock", "acc3") == "acc3"
    assert cost_ops._account_of("bedrock", "acc9") == "retired"


def test_the_spend_split_without_the_registry_is_the_retired_bucket():
    from api.services import cost_ops
    assert cost_ops._account_of("bedrock", "acc1") == "retired"


@pytest.mark.asyncio
async def test_the_satellite_lines_add_the_payer_from_the_ledger(registry):
    from api.services import platform_cost

    async def no_ce(pool, start, end):
        return None

    with patch.object(platform_cost.ce, "read_cost_range", no_ce):
        out = await platform_cost._satellites(None, date(2026, 11, 1), date(2026, 12, 1),
                                              {"acc3": 1.0, "acc1": 2.5})
    assert [line["key"] for line in out["lines"]] == ["acc3", "acc1"]
    payer = out["lines"][1]
    assert payer["label"] == f"{PAYER_ID} (LLM)" and payer["llm_log_estimate_usd"] == 2.5
    assert payer["ce_bedrock_usd"] is None and payer["account_id"] == PAYER_ID
