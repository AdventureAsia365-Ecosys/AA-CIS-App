"""AA-816 C6 — staff directory writes: api/services/staff_directory.py + the admin_platform write routes.

Covers:
- the Cognito wrapper on a real botocore client under Stubber: AdminCreateUser always carries
  MessageAction=SUPPRESS + email + email_verified=true, an existing user counts as created, nothing outside the
  writer allow-list can be called;
- the service against an in-memory shared.admin_users / staff_app_grant / audit_log: DB first then Cognito in the
  documented order (Stubber enforces the order), non-domain and break-glass addresses refused before any write,
  the pending path (DB kept, 502 body, a pending audit row) and the idempotent retry, the guards (self demote,
  self disable, last active owner, break-glass row, a stale owner flag);
- the routes through the real app with a Cognito Bearer (staff bearer middleware): owner writes, a legacy session /
  a forged x-admin-role / a non-owner / a reviewer get 403 before the DB, and the list says who may write.
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

import boto3
import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from api.core import identity_config, staff_identity
from api.main import app
from api.routers import admin, admin_platform, admin_settings
from api.services import staff_directory as sd
from tests.unit.aa854_cognito_staff import ISS, Directory, install_issuer

POOL = "us-west-1_TESTPOOL"
SECRET = "aa816-c6-test-admin-secret"
NEW_EMAIL = "new.person@adventure.asia"


# ── Fakes ────────────────────────────────────────────────────────────────────────────────────────

class StaffDB(Directory):
    """shared.admin_users + shared.staff_app_grant + acp_shared.audit_log, answering the staff_identity SQL (via
    Directory), the staff_directory SQL and the admin_platform reads. ``transaction()`` rolls back on error."""

    def __init__(self) -> None:
        super().__init__()
        self.grants: dict[tuple[str, str], str] = {}
        self.audit: list[dict[str, Any]] = []
        self.locked_owners = 0

    def add(self, *, owner: bool = False, booking: Optional[str] = None, **kw: Any) -> str:  # type: ignore[override]
        kw.setdefault("username", f"user-{len(self.rows) + 1}")
        uid = super().add(**kw)
        self.row(uid)["is_owner"] = owner
        if booking:
            self.grants[(uid, "booking")] = booking
        return uid

    @asynccontextmanager
    async def transaction(self):
        snapshot = ([dict(r) for r in self.rows], dict(self.grants), list(self.audit))
        try:
            yield
        except BaseException:
            self.rows, self.grants, self.audit = snapshot
            raise

    def _find(self, uid: Any) -> Optional[dict[str, Any]]:
        return next((r for r in self.rows if r["id"] == str(uid)), None)

    def _target(self, r: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if r is None:
            return None
        return {k: r.get(k) for k in ("id", "username", "role", "email", "display_name", "is_active")} | {
            "is_owner": r.get("is_owner", False)}

    def _listed(self, r: dict[str, Any]) -> dict[str, Any]:
        return {**{k: r.get(k) for k in ("id", "username", "role", "is_active", "email", "display_name",
                                         "last_login_at")},
                "is_owner": r.get("is_owner", False), "created_at": None, "updated_at": None,
                "linked": r.get("cognito_sub") is not None, "booking_role": self.grants.get((r["id"], "booking"))}

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if sql == sd.SQL_LOCK_OWNERS:
            self.locked_owners += 1
            return [{"id": r["id"]} for r in self.rows if r.get("is_owner")]
        if sql.startswith(admin_platform._SELECT):
            return [self._listed(r) for r in self.rows]
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        if sql == sd.SQL_ACTOR:
            self.sql.append(sql)
            r = self._find(args[0])
            return None if r is None else {"id": r["id"], "is_active": r["is_active"],
                                           "is_owner": r.get("is_owner", False), "role": r["role"]}
        if sql == sd.SQL_TARGET:
            self.sql.append(sql)
            return self._target(self._find(args[0]))
        if sql == sd.SQL_TARGET_BY_EMAIL:
            self.sql.append(sql)
            return self._target(next((r for r in self.rows if (r["email"] or "").lower() == args[0]), None))
        if sql.startswith(admin_platform._SELECT):
            self.sql.append(sql)
            r = self._find(args[0])
            return None if r is None else self._listed(r)
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        if sql == sd.SQL_GRANT:
            return self.grants.get((str(args[0]), args[1]))
        if sql == sd.SQL_OTHER_ACTIVE_OWNERS:
            return sum(1 for r in self.rows if r.get("is_owner") and r["is_active"] and r["role"] == "admin"
                       and r["id"] != str(args[0]))
        if sql == sd.SQL_USERNAME_TAKEN:
            return 1 if any(r["username"] == args[0] for r in self.rows) else None
        if sql == sd.SQL_INSERT:
            username, role, email, display_name = args
            uid = str(uuid.uuid4())
            self.rows.append({"id": uid, "username": username, "role": role, "email": email,
                              "display_name": display_name, "is_active": True, "cognito_iss": None,
                              "cognito_sub": None, "last_login_at": None, "is_owner": False, "password_hash": None})
            return uid
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO acp_shared.audit_log" in sql:
            self.sql.append(sql)
            tenant_id, actor, action, resource_type, resource_id, details = args
            self.audit.append({"tenant_id": tenant_id, "actor": actor, "action": action,
                               "resource_type": resource_type, "resource_id": resource_id,
                               "details": json.loads(details)})
            return "INSERT 0 1"
        if sql == sd.SQL_SET_ROLE:
            self.sql.append(sql)
            self._find(args[0])["role"] = args[1]
            return "UPDATE 1"
        if sql == sd.SQL_SET_ACTIVE:
            self.sql.append(sql)
            self._find(args[0])["is_active"] = args[1]
            return "UPDATE 1"
        if sql == sd.SQL_UPSERT_GRANT:
            self.sql.append(sql)
            self.grants[(str(args[0]), args[1])] = args[2]
            return "INSERT 0 1"
        if sql == sd.SQL_DELETE_GRANT:
            self.sql.append(sql)
            self.grants.pop((str(args[0]), args[1]), None)
            return "DELETE 1"
        return await super().execute(sql, *args)

    def actions(self) -> list[str]:
        return [a["action"] for a in self.audit]


def _client():
    return boto3.session.Session().client(
        "cognito-idp", region_name="us-west-1", aws_access_key_id="testing", aws_secret_access_key="testing")


@pytest.fixture
def stubbed():
    client = _client()
    with Stubber(client) as stub:
        yield sd.CognitoDirectory(client=client, pool_id=POOL), stub
        stub.assert_no_pending_responses()


def _create(email: str = NEW_EMAIL) -> dict:
    return {"UserPoolId": POOL, "Username": email, "MessageAction": "SUPPRESS",
            "UserAttributes": [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}]}


def _group(email: str, group: str) -> dict:
    return {"UserPoolId": POOL, "Username": email, "GroupName": group}


def _user(email: str) -> dict:
    return {"UserPoolId": POOL, "Username": email}


def _expect(stub: Stubber, op: str, params: dict, response: Optional[dict] = None) -> None:
    stub.add_response(op, response if response is not None else {}, params)


def _expect_error(stub: Stubber, op: str, code: str, params: Optional[dict] = None) -> None:
    stub.add_client_error(op, service_error_code=code, http_status_code=400, expected_params=params)


@pytest.fixture
def db():
    staff_identity.clear_cache()
    d = StaffDB()
    d.owner = d.add(owner=True, email="owner@adventure.asia", username="owner", role="admin")
    yield d
    staff_identity.clear_cache()


# ── Cognito wrapper (Stubber) ────────────────────────────────────────────────────────────────────

def test_create_user_always_sends_suppress_and_a_verified_email(stubbed):
    cognito, stub = stubbed
    _expect(stub, "admin_create_user", _create(), {"User": {"Username": NEW_EMAIL}})
    assert cognito.create_user(NEW_EMAIL) is True


def test_existing_cognito_user_counts_as_created(stubbed):
    cognito, stub = stubbed
    _expect_error(stub, "admin_create_user", "UsernameExistsException", _create())
    assert cognito.create_user(NEW_EMAIL) is False


def test_other_create_errors_propagate(stubbed):
    from botocore.exceptions import ClientError

    cognito, stub = stubbed
    _expect_error(stub, "admin_create_user", "UserLambdaValidationException", _create())
    with pytest.raises(ClientError):
        cognito.create_user(NEW_EMAIL)


def test_only_the_writer_operations_can_be_called():
    cognito = sd.CognitoDirectory(client=_client(), pool_id=POOL)
    for op in ("AdminGetUser", "AdminSetUserPassword", "AdminDeleteUser", "AdminUpdateUserAttributes",
               "AdminInitiateAuth", "ListUsers"):
        with pytest.raises(RuntimeError):
            cognito._call(op, Username=NEW_EMAIL)
    assert sd.COGNITO_OPERATIONS == {
        "AdminCreateUser", "AdminAddUserToGroup", "AdminRemoveUserFromGroup", "AdminDisableUser",
        "AdminEnableUser", "AdminUserGlobalSignOut"}
    client = _client()
    assert all(hasattr(client, sd._snake(op)) for op in sd.COGNITO_OPERATIONS)


def test_missing_user_is_fine_for_disable_sign_out_and_group_removal(stubbed):
    cognito, stub = stubbed
    _expect_error(stub, "admin_disable_user", "UserNotFoundException", _user(NEW_EMAIL))
    _expect_error(stub, "admin_user_global_sign_out", "UserNotFoundException", _user(NEW_EMAIL))
    _expect_error(stub, "admin_remove_user_from_group", "UserNotFoundException", _group(NEW_EMAIL, "cis-admin"))
    cognito.disable(NEW_EMAIL)
    cognito.global_sign_out(NEW_EMAIL)
    cognito.remove_from_group(NEW_EMAIL, "cis-admin")


def test_sign_out_after_disable_tolerates_user_is_disabled_only_when_asked(stubbed):
    from botocore.exceptions import ClientError

    cognito, stub = stubbed
    _expect_error(stub, "admin_user_global_sign_out", "NotAuthorizedException", _user(NEW_EMAIL))
    cognito.global_sign_out(NEW_EMAIL, user_disabled=True)   # tokens already revoked by AdminDisableUser
    _expect_error(stub, "admin_user_global_sign_out", "NotAuthorizedException", _user(NEW_EMAIL))
    with pytest.raises(ClientError):
        cognito.global_sign_out(NEW_EMAIL)


def test_one_attempt_per_cognito_call_within_the_gateway_budget():
    cfg = sd._BOTO_CONFIG
    assert cfg.retries["max_attempts"] == 1
    assert sd.STEPS_DEADLINE_S + cfg.connect_timeout + cfg.read_timeout < 29


@pytest.mark.asyncio
async def test_no_step_starts_after_the_deadline(monkeypatch):
    clock = iter([0.0, 0.0, 25.0])
    monkeypatch.setattr(sd, "_monotonic", lambda: next(clock))
    ran: list[str] = []
    result = sd.WriteResult(staff_id="x", operation="create")
    await sd._run_steps(result, [("first", lambda: ran.append("first")), ("second", lambda: ran.append("second"))])
    assert ran == ["first"] and result.steps == ["first"]
    assert result.pending and (result.failed_step, result.error_code) == ("second", "DeadlineExceeded")


def test_pool_id_and_region_come_from_identity_config(monkeypatch):
    monkeypatch.delenv(identity_config.ENV_WORKFORCE_POOL_ID, raising=False)
    monkeypatch.delenv(identity_config.ENV_WORKFORCE_ISSUER, raising=False)
    assert identity_config.workforce_pool_id() == identity_config.DEV_WORKFORCE_POOL_ID == "us-west-1_bCcFvbSOS"
    assert identity_config.workforce_region() == "us-west-1"
    assert sd.CognitoDirectory().pool_id == "us-west-1_bCcFvbSOS"
    monkeypatch.setenv(identity_config.ENV_WORKFORCE_POOL_ID, "ap-southeast-1_Other")
    assert identity_config.workforce_region() == "ap-southeast-1"


def test_get_cognito_builds_no_client():
    sd._cognito = None
    cognito = sd.get_cognito()
    assert cognito is sd.get_cognito() and cognito._client is None


# ── Service: create ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_writes_the_row_then_creates_the_user_then_the_groups(db, stubbed):
    cognito, stub = stubbed
    _expect(stub, "admin_create_user", _create(), {"User": {"Username": NEW_EMAIL}})
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-reviewer"))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-sales"))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email="  New.Person@Adventure.Asia ",
                                   display_name="  New   Person ", role="reviewer", booking_role="sales")
    assert result.status == sd.STATUS_OK and result.created and result.cognito == sd.COGNITO_DONE
    assert result.steps == ["create_user", "add_group:cis-reviewer", "add_group:booking-sales"]
    row = db.row(result.staff_id)
    assert (row["username"], row["email"], row["display_name"], row["role"], row["is_owner"]) == (
        "new.person", NEW_EMAIL, "New Person", "reviewer", False)
    assert row["password_hash"] is None
    assert db.grants[(result.staff_id, "booking")] == "sales"
    assert db.actions() == [sd.StaffAudit.CREATED]
    assert db.audit[0]["actor"] == f"admin:{db.owner}" and db.audit[0]["tenant_id"] is None


@pytest.mark.asyncio
async def test_create_without_booking_role_adds_only_the_cis_group(db, stubbed):
    cognito, stub = stubbed
    _expect(stub, "admin_create_user", _create(), {"User": {"Username": NEW_EMAIL}})
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-content"))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N",
                                   role="content")
    assert result.steps == ["create_user", "add_group:cis-content"]
    assert (result.staff_id, "booking") not in db.grants


@pytest.mark.parametrize("email", ["someone@gmail.com", "x@adventure.asia.evil.com", "x@sub.adventure.asia",
                                   "no-at-sign", "", "a b@adventure.asia", "@adventure.asia"])
@pytest.mark.asyncio
async def test_non_domain_address_is_refused_before_any_write(db, stubbed, email):
    cognito, _ = stubbed   # no stubbed response: any Cognito call would fail the test
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email=email, display_name="X", role="content")
    assert err.value.status == 400
    assert len(db.rows) == 1 and db.audit == []


@pytest.mark.asyncio
async def test_breakglass_address_can_not_be_created(db, stubbed):
    cognito, _ = stubbed
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email="admin@adventure.asia", display_name="X",
                              role="admin")
    assert err.value.status == 403 and err.value.code == "breakglass"


@pytest.mark.asyncio
async def test_username_is_unique_and_never_the_breakglass_name(db, stubbed):
    cognito, stub = stubbed
    db.add(username="new.person", email=None)
    email = "new.person+x@adventure.asia"
    _expect(stub, "admin_create_user", _create(email), {"User": {"Username": email}})
    _expect(stub, "admin_add_user_to_group", _group(email, "cis-content"))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=email, display_name="N", role="content")
    assert db.row(result.staff_id)["username"] == "new.person-x"
    assert sd.username_base("admin.ops@adventure.asia") == "admin.ops"
    assert await sd._free_username(db, "admin") == "admin-2"


@pytest.mark.asyncio
async def test_cognito_failure_keeps_the_row_and_reports_pending(db, stubbed):
    cognito, stub = stubbed
    _expect(stub, "admin_create_user", _create(), {"User": {"Username": NEW_EMAIL}})
    _expect_error(stub, "admin_add_user_to_group", "TooManyRequestsException", _group(NEW_EMAIL, "cis-admin"))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N", role="admin")
    assert result.pending and result.cognito == sd.COGNITO_PENDING
    assert (result.failed_step, result.error_code) == ("add_group:cis-admin", "TooManyRequestsException")
    assert result.steps == ["create_user"]
    assert db.row(result.staff_id)["is_active"] is True
    assert db.actions() == [sd.StaffAudit.CREATED, sd.StaffAudit.COGNITO_PENDING]
    assert db.audit[1]["details"]  # the failed step is recorded


@pytest.mark.asyncio
async def test_re_post_of_the_same_create_is_an_idempotent_retry(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="reviewer", booking="sales", username="new.person")
    _expect_error(stub, "admin_create_user", "UsernameExistsException", _create())
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-reviewer"))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-sales"))
    _expect(stub, "admin_enable_user", _user(NEW_EMAIL))
    for g in ("cis-admin", "cis-content", "booking-admin", "booking-contracting", "booking-content_creator"):
        _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, g))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N",
                                   role="reviewer", booking_role="sales")
    assert result.staff_id == uid and not result.created and result.status == sd.STATUS_OK
    assert len(db.rows) == 2 and db.audit == []


@pytest.mark.asyncio
async def test_create_over_an_existing_cognito_user_drops_its_stale_groups_and_enables_it(db, stubbed):
    """Review finding 1: a WORKFORCE user left with cis-admin (bootstrap / old offboarding) and possibly disabled,
    but no directory row. Created as "content", it must end with cis-content only, enabled."""
    cognito, stub = stubbed
    _expect_error(stub, "admin_create_user", "UsernameExistsException", _create())
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-content"))   # add before any remove
    _expect(stub, "admin_enable_user", _user(NEW_EMAIL))
    for g in ("cis-admin", "cis-reviewer") + sd.BOOKING_GROUPS:
        _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, g))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N",
                                   role="content")
    assert result.created and result.status == sd.STATUS_OK
    assert result.steps[:3] == ["create_user", "add_group:cis-content", "enable_user"]
    assert "remove_group:cis-admin" in result.steps and "remove_group:cis-content" not in result.steps


@pytest.mark.asyncio
async def test_same_address_with_other_roles_is_409(db, stubbed):
    cognito, _ = stubbed
    db.add(email=NEW_EMAIL, role="reviewer")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N", role="admin")
    assert err.value.status == 409


@pytest.mark.asyncio
async def test_a_stale_owner_flag_is_refused_inside_the_transaction(db, stubbed):
    cognito, _ = stubbed
    db.row(db.owner)["is_owner"] = False   # revoked after the route policy read the cached row
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N", role="admin")
    assert err.value.status == 403 and len(db.rows) == 1
    assert db.locked_owners == 1   # owner rows locked before the actor is read


# ── Service: roles ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_role_change_adds_the_new_group_before_removing_the_old(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="reviewer", booking="sales")
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-admin"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "cis-reviewer"))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-contracting"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "booking-sales"))
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, role="admin",
                                   booking_role="contracting")
    assert result.status == sd.STATUS_OK
    assert db.row(uid)["role"] == "admin" and db.grants[(uid, "booking")] == "contracting"
    assert db.actions() == [sd.StaffAudit.ROLES_CHANGED]
    assert db.audit[0]["details"] == {"role": {"before": "reviewer", "after": "admin"},
                                      "booking_role": {"before": "sales", "after": "contracting"}}


@pytest.mark.asyncio
async def test_booking_grant_removed_and_added(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content", booking="content_creator")
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "booking-content_creator"))
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, booking_role=None)
    assert result.steps == ["remove_group:booking-content_creator"]
    assert (uid, "booking") not in db.grants and db.row(uid)["role"] == "content"


@pytest.mark.asyncio
async def test_role_change_on_a_row_without_email_is_db_only(db, stubbed):
    cognito, _ = stubbed
    uid = db.add(email=None, role="content")
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, role="reviewer")
    assert result.cognito == sd.COGNITO_NOT_APPLICABLE and db.row(uid)["role"] == "reviewer"


@pytest.mark.asyncio
async def test_you_can_not_demote_yourself(db, stubbed):
    cognito, _ = stubbed
    db.add(owner=True, email="other.owner@adventure.asia", role="admin")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=db.owner, role="reviewer")
    assert err.value.status == 400 and db.row(db.owner)["role"] == "admin"


@pytest.mark.asyncio
async def test_the_last_active_owner_can_not_be_demoted_or_disabled(db, stubbed):
    cognito, stub = stubbed
    other = db.add(owner=True, email="other.owner@adventure.asia", role="admin")
    # Two active admin owners: one may demote the other (an owner remains).
    _expect(stub, "admin_add_user_to_group", _group("other.owner@adventure.asia", "cis-reviewer"))
    _expect(stub, "admin_remove_user_from_group", _group("other.owner@adventure.asia", "cis-admin"))
    await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=other, role="reviewer")
    # Now db.owner is the last active admin owner. "other" is still an owner row but no longer a CIS admin, while
    # its access token may still carry cis-admin for up to 15 minutes (review finding 3): the actor re-read
    # refuses it before any guard runs, so it can neither demote nor disable the last owner.
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.disable_staff(db, cognito, actor_id=other, staff_id=db.owner)
    assert (err.value.status, err.value.code) == (403, "not_owner")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=other, staff_id=db.owner, role="content")
    assert err.value.code == "not_owner"
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=other, email=NEW_EMAIL, display_name="N", role="admin")
    assert err.value.code == "not_owner" and len(db.rows) == 2
    assert db.row(db.owner)["is_active"] is True and db.row(db.owner)["role"] == "admin"


@pytest.mark.asyncio
async def test_last_owner_guard_directly(db, stubbed):
    cognito, _ = stubbed
    actor = db.add(owner=True, email="actor@adventure.asia", role="admin")
    target = db.add(owner=True, email="target@adventure.asia", role="admin")
    db.row(db.owner)["is_owner"] = False
    db.row(actor)["is_owner"] = True
    # actor is an owner but not active+admin after a demotion elsewhere -> target is the last active owner
    db.row(actor)["role"] = "admin"
    db.row(actor)["is_active"] = True
    # two active owners: guard passes for target when actor remains
    target_row = db._target(db.row(target))
    await sd._guard_last_owner(db, target_row)
    # make actor non-admin: target is now the last active owner
    db.row(actor)["role"] = "reviewer"
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd._guard_last_owner(db, target_row)
    assert err.value.status == 409 and err.value.code == "last_owner"


@pytest.mark.asyncio
async def test_the_breakglass_row_is_never_changed(db, stubbed):
    cognito, _ = stubbed
    bg = db.add(username="admin", email="admin@adventure.asia", role="admin")
    for op, kwargs in ((sd.change_roles, {"role": "content"}), (sd.disable_staff, {}), (sd.enable_staff, {}),
                       (sd.sign_out_staff, {}), (sd.sync_staff, {})):
        with pytest.raises(sd.StaffDirectoryError) as err:
            await op(db, cognito, actor_id=db.owner, staff_id=bg, **kwargs)
        assert err.value.status == 403 and err.value.code == "breakglass"
    legacy = db.add(username="admin2", email=None, role="admin")
    db.row(legacy)["username"] = "admin"   # username alone marks the break-glass row too
    with pytest.raises(sd.StaffDirectoryError):
        await sd.disable_staff(db, cognito, actor_id=db.owner, staff_id=legacy)
    assert db.audit == []


# ── Service: disable / enable / sign-out / sync ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disable_is_db_then_disable_then_global_sign_out(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content")
    _expect(stub, "admin_disable_user", _user(NEW_EMAIL))
    _expect(stub, "admin_user_global_sign_out", _user(NEW_EMAIL))
    result = await sd.disable_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.steps == ["disable_user", "global_sign_out"]
    assert db.row(uid)["is_active"] is False and db.actions() == [sd.StaffAudit.DISABLED]


@pytest.mark.asyncio
async def test_you_can_not_disable_yourself(db, stubbed):
    cognito, _ = stubbed
    db.add(owner=True, email="other.owner@adventure.asia", role="admin")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.disable_staff(db, cognito, actor_id=db.owner, staff_id=db.owner)
    assert err.value.status == 400 and db.row(db.owner)["is_active"] is True


@pytest.mark.asyncio
async def test_disable_pending_when_sign_out_fails(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content")
    _expect(stub, "admin_disable_user", _user(NEW_EMAIL))
    _expect_error(stub, "admin_user_global_sign_out", "InternalErrorException", _user(NEW_EMAIL))
    result = await sd.disable_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.pending and result.failed_step == "global_sign_out" and result.steps == ["disable_user"]
    assert db.row(uid)["is_active"] is False


@pytest.mark.asyncio
async def test_disable_is_ok_when_cognito_refuses_sign_out_of_the_disabled_user(db, stubbed):
    """Review finding 2: AdminUserGlobalSignOut on a just-disabled user may answer "User is disabled"."""
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content")
    _expect(stub, "admin_disable_user", _user(NEW_EMAIL))
    _expect_error(stub, "admin_user_global_sign_out", "NotAuthorizedException", _user(NEW_EMAIL))
    result = await sd.disable_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK and result.steps == ["disable_user", "global_sign_out"]


@pytest.mark.asyncio
async def test_disable_failure_never_tolerates_the_sign_out_refusal(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content")
    _expect_error(stub, "admin_disable_user", "InternalErrorException", _user(NEW_EMAIL))
    result = await sd.disable_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.pending and result.failed_step == "disable_user" and result.steps == []


@pytest.mark.asyncio
async def test_enable_is_db_then_enable(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content", active=False)
    _expect(stub, "admin_enable_user", _user(NEW_EMAIL))
    result = await sd.enable_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.steps == ["enable_user"] and db.row(uid)["is_active"] is True
    assert db.actions() == [sd.StaffAudit.ENABLED]


@pytest.mark.asyncio
async def test_sign_out_ends_sessions_and_re_enables_a_disabled_account(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content", active=False)
    _expect(stub, "admin_enable_user", _user(NEW_EMAIL))
    _expect(stub, "admin_user_global_sign_out", _user(NEW_EMAIL))
    result = await sd.sign_out_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.steps == ["enable_user", "global_sign_out"]
    assert db.row(uid)["is_active"] is True
    assert db.audit[0]["action"] == sd.StaffAudit.SIGNED_OUT and db.audit[0]["details"] == {"reenabled": True}


@pytest.mark.asyncio
async def test_reset_of_a_disabled_account_enables_it_even_when_sign_out_fails(db, stubbed):
    """Review finding 2: enable runs first, so a failed sign-out never leaves the row active and the pool
    disabled."""
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content", active=False)
    _expect(stub, "admin_enable_user", _user(NEW_EMAIL))
    _expect_error(stub, "admin_user_global_sign_out", "InternalErrorException", _user(NEW_EMAIL))
    result = await sd.sign_out_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.pending and result.steps == ["enable_user"] and result.failed_step == "global_sign_out"
    assert db.row(uid)["is_active"] is True


@pytest.mark.asyncio
async def test_sync_makes_cognito_match_an_active_row(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="reviewer", booking="sales")
    _expect_error(stub, "admin_create_user", "UsernameExistsException", _create())
    _expect(stub, "admin_enable_user", _user(NEW_EMAIL))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-reviewer"))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-sales"))
    for g in ("cis-admin", "cis-content", "booking-admin", "booking-contracting", "booking-content_creator"):
        _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, g))
    result = await sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK and db.actions() == [sd.StaffAudit.COGNITO_SYNCED]


@pytest.mark.asyncio
async def test_sync_of_an_inactive_row_disables_and_never_creates(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="reviewer", active=False)
    _expect(stub, "admin_disable_user", _user(NEW_EMAIL))
    _expect_error(stub, "admin_user_global_sign_out", "NotAuthorizedException", _user(NEW_EMAIL))
    result = await sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.steps == ["disable_user", "global_sign_out"] and result.status == sd.STATUS_OK


@pytest.mark.asyncio
async def test_sync_refuses_a_row_without_email(db, stubbed):
    cognito, _ = stubbed
    uid = db.add(email=None)
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert err.value.status == 400


# ── Routes through the real app (Cognito Bearer -> staff bearer middleware -> staff_policy) ──────

@pytest.fixture
def issuer():
    iss, _ = install_issuer()
    yield iss
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()


@pytest.fixture
def api(monkeypatch, db, issuer):
    cognito = sd.CognitoDirectory(client=_client(), pool_id=POOL)
    stub = Stubber(cognito.client())
    stub.activate()
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(app.state, "pool", db, raising=False)
    app.dependency_overrides[sd.get_cognito] = lambda: cognito
    owner_sub = "aaaaaaaa-0000-4000-8000-00000000000a"
    db.row(db.owner).update(cognito_iss=ISS, cognito_sub=owner_sub)
    try:
        yield TestClient(app), stub, issuer.token(sub=owner_sub, email="owner@adventure.asia",
                                                  **{"cognito:groups": ["cis-admin"]})
    finally:
        app.dependency_overrides.pop(sd.get_cognito, None)
        stub.deactivate()


def _h(token: Optional[str] = None, role: Optional[str] = "admin") -> dict:
    h = {"X-Admin-Secret": SECRET}
    if role is not None:
        h["x-admin-role"] = role
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def test_owner_creates_from_the_api(api, db):
    client, stub, token = api
    _expect(stub, "admin_create_user", _create(), {"User": {"Username": NEW_EMAIL}})
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-content"))
    resp = client.post("/admin/platform/staff", headers=_h(token),
                       json={"email": NEW_EMAIL, "display_name": "New Person", "role": "content"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "ok" and body["cognito"] == "done"
    assert body["staff"]["email"] == NEW_EMAIL and body["staff"]["username"] == "new.person"
    assert "password" not in resp.text and "cognito_sub" not in resp.text
    stub.assert_no_pending_responses()


def test_pending_answers_502_with_the_saved_row(api, db):
    client, stub, token = api
    _expect_error(stub, "admin_create_user", "InternalErrorException", _create())
    resp = client.post("/admin/platform/staff", headers=_h(token),
                       json={"email": NEW_EMAIL, "display_name": "New Person", "role": "content"})
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["status"] == "saved_cognito_pending" and body["failed_step"] == "create_user"
    assert body["staff"]["email"] == NEW_EMAIL
    assert body["retry"] == f"/admin/platform/staff/{body['staff']['id']}/sync"
    assert isinstance(body["detail"], str) and "Cognito" in body["detail"]


def test_legacy_session_is_refused_before_the_db(api, db):
    client, _, _ = api
    before = len(db.sql)
    resp = client.post("/admin/platform/staff", headers=_h(None, role="admin"),
                       json={"email": NEW_EMAIL, "display_name": "N", "role": "content"})
    assert resp.status_code == 403 and "Cognito" in resp.json()["detail"]
    # AA-884: the staff audit middleware logs the refused request itself; nothing else reaches the DB.
    assert [s for s in db.sql[before:] if "acp_shared.audit_log" not in s] == [] and len(db.rows) == 1


def test_non_owner_cognito_admin_is_refused(api, db, issuer):
    client, _, _ = api
    sub = "bbbbbbbb-0000-4000-8000-00000000000b"
    db.add(email="plain.admin@adventure.asia", role="admin", iss=ISS, sub=sub)
    token = issuer.token(sub=sub, email="plain.admin@adventure.asia", **{"cognito:groups": ["cis-admin"]})
    resp = client.post("/admin/platform/staff/" + db.owner + "/disable", headers=_h(token))
    assert resp.status_code == 403 and resp.json()["detail"] == "Only a platform owner can change staff accounts"


def test_owner_with_only_a_reviewer_group_is_refused(api, db, issuer):
    client, _, _ = api
    token = issuer.token(sub=db.row(db.owner)["cognito_sub"], email="owner@adventure.asia",
                         **{"cognito:groups": ["cis-reviewer"]})
    resp = client.post("/admin/platform/staff", headers=_h(token),
                       json={"email": NEW_EMAIL, "display_name": "N", "role": "content"})
    assert resp.status_code == 403 and "staff.write" in resp.json()["detail"]


def test_forged_role_header_does_not_help_a_reviewer(api, db, issuer):
    client, _, _ = api
    sub = "cccccccc-0000-4000-8000-00000000000c"
    db.add(owner=True, email="rev@adventure.asia", role="reviewer", iss=ISS, sub=sub)
    token = issuer.token(sub=sub, email="rev@adventure.asia", **{"cognito:groups": ["cis-reviewer"]})
    resp = client.post("/admin/platform/staff", headers=_h(token, role="admin"),
                       json={"email": NEW_EMAIL, "display_name": "N", "role": "content"})
    assert resp.status_code == 403


def test_route_guards_map_to_http_statuses(api, db):
    client, stub, token = api
    resp = client.post(f"/admin/platform/staff/{db.owner}/disable", headers=_h(token))
    assert resp.status_code == 400 and "yourself" in resp.json()["detail"]
    resp = client.patch(f"/admin/platform/staff/{db.owner}/roles", headers=_h(token), json={"role": "content"})
    assert resp.status_code == 400
    resp = client.patch(f"/admin/platform/staff/{db.owner}/roles", headers=_h(token), json={})
    assert resp.status_code == 400
    resp = client.patch(f"/admin/platform/staff/{db.owner}/roles", headers=_h(token), json={"role": None})
    assert resp.status_code == 400
    resp = client.patch(f"/admin/platform/staff/{db.owner}/roles", headers=_h(token), json={"role": "boss"})
    assert resp.status_code == 422
    resp = client.post("/admin/platform/staff/not-a-uuid/enable", headers=_h(token))
    assert resp.status_code == 404
    resp = client.post(f"/admin/platform/staff/{uuid.uuid4()}/enable", headers=_h(token))
    assert resp.status_code == 404
    bg = db.add(username="admin", email="admin@adventure.asia", role="admin")
    resp = client.post(f"/admin/platform/staff/{bg}/sign-out", headers=_h(token))
    assert resp.status_code == 403
    resp = client.post("/admin/platform/staff", headers=_h(token),
                       json={"email": "x@gmail.com", "display_name": "N", "role": "content"})
    assert resp.status_code == 400 and "adventure.asia" in resp.json()["detail"]
    resp = client.post("/admin/platform/staff", headers=_h(token),
                       json={"email": NEW_EMAIL, "display_name": "N", "role": "content", "is_owner": True})
    assert resp.status_code == 422   # the owner flag is never writable here
    resp = client.post("/admin/platform/staff", headers=_h(token),
                       json={"email": NEW_EMAIL, "display_name": "x" * 121, "role": "content"})
    assert resp.status_code == 422   # same 120-char limit as the service


def test_roles_route_sends_only_the_given_fields(api, db):
    client, stub, token = api
    uid = db.add(email=NEW_EMAIL, role="content", booking="sales")
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "booking-sales"))
    resp = client.patch(f"/admin/platform/staff/{uid}/roles", headers=_h(token), json={"booking_role": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["staff"]["booking_role"] is None and resp.json()["staff"]["role"] == "content"


def test_list_tells_the_ui_who_may_write(api, db, issuer):
    client, _, token = api
    body = client.get("/admin/platform/staff", headers=_h(token)).json()
    assert body["viewer"] == {"auth": "cognito", "is_owner": True, "can_write": True, "admin_id": db.owner}
    legacy = client.get("/admin/platform/staff", headers=_h(None, role="admin")).json()
    assert legacy["viewer"]["can_write"] is False and legacy["viewer"]["auth"] == "legacy"
