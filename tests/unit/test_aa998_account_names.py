"""AA-998 — the admin names an AWS account by its 12-digit account ID, never by the internal key (acc2 / acc3).

The IDs come from the running task (acc2: STS of the task's own credentials, acc3: BEDROCK_ACCT_PRIMARY); this repo
is public, so the tests use AWS documentation example accounts. A stored account that is neither (the history of the
retired fallback account) is "Retired account", with no ID.
"""
import re

import pytest
from botocore.exceptions import NoCredentialsError

from shared.aws_client import accounts
from shared.aws_client import cost_explorer as ce

ACC2 = "111122223333"   # tests/unit/conftest.py patches bronze.caller_account_id to this
ACC3 = "444455556666"


@pytest.fixture(autouse=True)
def _acc3_id(monkeypatch):
    monkeypatch.setattr("shared.llm_client.bedrock_satellite.ACC3_ACCOUNT_ID", ACC3)


def test_live_keys_resolve_to_account_ids():
    assert accounts.account_id("acc2") == ACC2
    assert accounts.account_id("acc3") == ACC3
    assert accounts.account_name("acc3") == ACC3
    assert accounts.account_label("acc3") == f"{ACC3} (LLM)"
    assert accounts.account_label("acc2") == f"{ACC2} (main infrastructure)"


@pytest.mark.parametrize("key", ["retired", "acc1", "some-old-account"])
def test_any_other_stored_account_is_retired_without_an_id(key):
    assert accounts.account_id(key) is None
    assert accounts.account_name(key) == accounts.account_label(key) == "Retired account"


@pytest.mark.parametrize("key", [None, "", "unknown", "none"])
def test_no_account_has_no_name(key):
    assert accounts.account_name(key) is None and accounts.account_label(key) is None


def test_an_id_that_is_not_12_digits_is_not_shown(monkeypatch):
    # The unit env sets BEDROCK_ACCT_PRIMARY to a placeholder; a misconfigured task must not print it as an ID.
    monkeypatch.setattr("shared.llm_client.bedrock_satellite.ACC3_ACCOUNT_ID", "acc3-test-account")
    assert accounts.account_id("acc3") is None
    assert accounts.account_name("acc3") == accounts.UNRESOLVED_ACCOUNT_LABEL


def test_sts_failure_does_not_fail_the_label(monkeypatch):
    def boom():
        raise NoCredentialsError()

    monkeypatch.setattr("shared.aws_client.bronze.caller_account_id", boom)
    assert accounts.account_name("acc2") == accounts.UNRESOLVED_ACCOUNT_LABEL


def test_directory_covers_every_key_and_never_prints_a_key():
    d = accounts.account_directory()
    assert set(d) == {"acc2", "acc3", "retired"}
    assert d["acc3"]["account_id"] == ACC3 and d["retired"]["account_id"] is None
    shown = " ".join(str(v["name"]) + " " + str(v["label"]) + " " + v["role"] for v in d.values())
    assert not re.search(r"acc[0-9]", shown)
    assert accounts.RETIRED_ACCOUNT_KEY == ce.RETIRED_ACCOUNT_KEY


@pytest.mark.asyncio
async def test_aws_accounts_route():
    from api.routers import admin_llm_ops as ops

    out = await ops.get_aws_accounts()
    assert out["accounts"]["acc2"]["name"] == ACC2 and out["accounts"]["retired"]["name"] == "Retired account"


def test_settings_route_option_is_labelled_by_id():
    from api.routers import admin_llm_ops as ops

    assert ops._account_route_options() == [{"value": "acc3", "label": f"{ACC3} (LLM)"}]
