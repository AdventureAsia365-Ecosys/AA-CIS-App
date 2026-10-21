"""AA-816 (root ADR 0006a A10) — the temporary E2E test-mailbox exception is gone from api/services/staff_directory.py.

The old test address e2e@e2e-mail.lumiguides.it.com is now refused like any other off-domain address (400
``email_domain``), create refuses it before any DB write or Cognito call, and the exception's names no longer exist.
The E2E helper signs in as an ordinary ``@adventure.asia`` reviewer, which the directory accepts. Migration 252
(the DB half) is tested in tests/integration/test_aa816_migration_252.py.
"""
from __future__ import annotations

import os
import re

import pytest

from api.core import staff_identity
from api.services import staff_directory as sd
from botocore.stub import Stubber

from tests.unit.test_aa816_staff_directory_writes import POOL, StaffDB, _client, _expect

OLD_E2E = "e2e@e2e-mail.lumiguides.it.com"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture
def stubbed():
    client = _client()
    with Stubber(client) as stub:
        yield sd.CognitoDirectory(client=client, pool_id=POOL), stub
        stub.assert_no_pending_responses()


@pytest.fixture
def db():
    staff_identity.clear_cache()
    d = StaffDB()
    d.owner = d.add(owner=True, email="owner@adventure.asia", username="owner", role="admin")
    yield d
    staff_identity.clear_cache()


@pytest.mark.parametrize("name", ["E2E_TEST_STAFF_EMAIL", "E2E_TEST_STAFF_ROLE", "is_e2e_test_email",
                                  "require_e2e_test_scope"])
def test_the_exception_names_are_gone(name):
    assert not hasattr(sd, name)


def test_no_production_module_names_the_old_address():
    hits = []
    for top in ("api", "shared", "services", "worker"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, top)):
            if os.sep + "migrations" in dirpath:
                continue   # 250 (history) and 252 (its guard) name it on purpose
            for f in files:
                if f.endswith(".py"):
                    with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                        if OLD_E2E in fh.read():
                            hits.append(os.path.relpath(os.path.join(dirpath, f), ROOT))
    assert hits == []


@pytest.mark.parametrize("value", [OLD_E2E, "  E2E@E2E-Mail.Lumiguides.IT.com  ", "e2e@lumiguides.it.com",
                                   "e2e-cognito@e2e-mail.lumiguides.it.com"])
def test_the_old_address_is_refused_like_any_other_domain(value):
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_staff_email(value)
    assert (err.value.status, err.value.code) == (400, "email_domain")


def test_the_e2e_helper_address_is_an_ordinary_company_address():
    with open(os.path.join(ROOT, "tests", "e2e", "helpers", "mailbox.ts"), encoding="utf-8") as f:
        m = re.search(r"export const E2E_COGNITO_EMAIL = '([^']+)';", f.read())
    assert m, "E2E_COGNITO_EMAIL not found in the helper"
    assert re.fullmatch(r"[^@\s]+@adventure[.]asia", m.group(1))
    assert sd.require_staff_email(m.group(1)) == m.group(1)


@pytest.mark.parametrize("role", ["reviewer", "admin"])
@pytest.mark.asyncio
async def test_create_with_the_old_address_is_refused_before_any_write(db, stubbed, role):
    cognito, _ = stubbed   # no stubbed response: any Cognito call would fail the test
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email=OLD_E2E, display_name="E2E Cognito",
                              role=role)
    assert (err.value.status, err.value.code) == (400, "email_domain")
    assert len(db.rows) == 1 and db.audit == [] and db.grants == {}


@pytest.mark.asyncio
async def test_a_stored_old_row_can_still_be_disabled(db, stubbed):
    # H3 of the removal disables the old row before it is deleted; disable never re-validates the address.
    cognito, stub = stubbed
    uid = db.add(email=OLD_E2E, role="reviewer")
    _expect(stub, "admin_disable_user", {"UserPoolId": POOL, "Username": OLD_E2E})
    _expect(stub, "admin_user_global_sign_out", {"UserPoolId": POOL, "Username": OLD_E2E})
    result = await sd.disable_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK and db.row(uid)["is_active"] is False


def test_migration_252_restores_the_247_rule_text():
    with open(os.path.join(ROOT, "api", "migrations", "252_admin_users_e2e_exception_removed.sql"),
              encoding="utf-8") as f:
        sql = f.read()
    assert "email IS NULL OR (email = lower(email) AND email ~ '^[^@[:space:]]+@adventure[.]asia$')" in sql
    # No `-- migrate:` directive: not destructive, it only narrows a CHECK every row already passes.
    assert not re.search(r"^--\s*migrate:", sql, re.MULTILINE)
    assert sql.count("BEGIN;") == 1 and sql.rstrip().endswith("COMMIT;")
