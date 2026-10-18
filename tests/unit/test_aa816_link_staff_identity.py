"""AA-816 (S242, C8) — scripts/link_staff_identity.py: arg parsing, validation, planning, apply path.

Mock-only (no DB, no AWS): the DB connection and the audit writer are replaced by fakes.
"""
import asyncio
import base64
import json
import re
import sys
import uuid

import pytest

from scripts import link_staff_identity as lsi


def _row(username, *, email=None, display_name=None, sub=None, is_owner=False, is_active=True, role="admin"):
    return {
        "id": uuid.uuid4(), "username": username, "role": role, "is_active": is_active,
        "email": email, "display_name": display_name,
        "cognito_iss": "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS" if sub else None,
        "cognito_sub": sub, "last_login_at": None, "is_owner": is_owner,
        "created_at": None, "updated_at": None,
    }


# ── parsing / validation ─────────────────────────────────────────────────────────────────────────────

def test_default_mode_is_dry_run():
    opts = lsi.parse_args([])
    assert opts.apply is False and opts.entries == [] and opts.owner is None
    assert opts.actor == lsi.DEFAULT_ACTOR and opts.s3 is False


def test_map_entry_with_display_name_containing_colon_and_unicode():
    e = lsi.parse_map_entry("nghiep=NghiepPham@Adventure.Asia:Nghiệp Phạm: Owner")
    assert e == lsi.MapEntry("nghiep", "nghieppham@adventure.asia", "Nghiệp Phạm: Owner")


def test_map_entry_without_display_name_keeps_current():
    assert lsi.parse_map_entry("nghiep=nghieppham@adventure.asia").display_name is None


@pytest.mark.parametrize("spec", [
    "nghiep",                                   # no '='
    "=nghieppham@adventure.asia",               # empty username
    "ng hiep=nghieppham@adventure.asia",        # whitespace in username
    "nghiep=nghieppham@gmail.com",              # wrong domain
    "nghiep=nghieppham@adventure.asia.evil.com",
    "nghiep=nghieppham@sub.adventure.asia",
    "nghiep=@adventure.asia",
    "nghiep=a@b@adventure.asia",
    "nghiep=",                                  # empty email
    "nghiep=nghieppham@adventure.asia:",        # empty display name
    "nghiep=nghieppham@adventure.asia:   ",
    "nghiep=nghieppham@adventure.asia:bad\x07name",
    "nghiep=nghieppham@adventure.asia:" + "x" * 101,
    "u" * 101 + "=nghieppham@adventure.asia",
])
def test_bad_map_entries_rejected(spec):
    with pytest.raises(lsi.MapError):
        lsi.parse_map_entry(spec)


def test_email_rule_matches_db_check():
    # admin_users_email_chk: lower-case and '^[^@[:space:]]+@adventure[.]asia$'
    for ok in ("a@adventure.asia", "first.last+x@adventure.asia"):
        assert lsi.validate_email(ok) == ok
    assert lsi.validate_email("  Mixed@ADVENTURE.ASIA ") == "mixed@adventure.asia"
    for bad in ("a b@adventure.asia", "a@adventureXasia", "a@adventure.asia\x00", "a@adventure.asia.x"):
        with pytest.raises(lsi.MapError):
            lsi.validate_email(bad)


@pytest.mark.parametrize("bad", ["nghiệp@adventure.asia", "\u0130nfo@adventure.asia", "a\u00a0b@adventure.asia"])
def test_email_local_part_is_ascii_only(bad):
    # Python .lower() and Postgres lower() may disagree on non-ASCII (e.g. 'İ'); the DB check would then
    # raise mid-apply, so the script refuses these up front.
    with pytest.raises(lsi.MapError):
        lsi.validate_email(bad)


def test_duplicate_username_or_email_in_mapping_rejected():
    with pytest.raises(lsi.MapError, match="more than once"):
        lsi.parse_args(["--map", "a=a@adventure.asia", "--map", "a=b@adventure.asia"])
    with pytest.raises(lsi.MapError, match="more than one username"):
        lsi.parse_args(["--map", "a=x@adventure.asia", "--map", "b=X@adventure.asia"])


def test_map_b64_decodes_utf8_lines_and_merges_with_map():
    blob = base64.b64encode("nghiep=nghieppham@adventure.asia:Nghiệp Phạm\n\n".encode()).decode()
    opts = lsi.parse_args(["--apply", "--map-b64", blob, "--map", "e2e=e2e-cognito@adventure.asia"])
    assert opts.apply is True
    assert [e.username for e in opts.entries] == ["e2e", "nghiep"]
    assert opts.entries[1].display_name == "Nghiệp Phạm"


@pytest.mark.parametrize("blob", ["not base64!!", base64.b64encode(b"\n\n").decode(),
                                  base64.b64encode(b"\xff\xfe").decode()])
def test_bad_map_b64_rejected(blob):
    with pytest.raises(lsi.MapError):
        lsi.parse_args(["--map-b64", blob])


def test_apply_needs_something_to_do():
    with pytest.raises(lsi.MapError, match="needs at least one"):
        lsi.parse_args(["--apply"])
    assert lsi.parse_args(["--apply", "--owner", "nghiep"]).owner == "nghiep"


def test_dry_run_and_apply_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        lsi.parse_args(["--dry-run", "--apply"])


def test_actor_length_limited_to_audit_column():
    with pytest.raises(lsi.MapError):
        lsi.parse_args(["--actor", "x" * 65])


def test_main_returns_2_and_json_on_bad_args(capsys):
    assert lsi.main(["--map", "nghiep=nghiep@gmail.com"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and "adventure.asia" in out["errors"][0]


# ── planning ─────────────────────────────────────────────────────────────────────────────────────────

E_NGHIEP = lsi.MapEntry("nghiep", "nghieppham@adventure.asia", "Nghiệp Phạm")


def test_plan_sets_email_and_name():
    plan = lsi.compute_plan([_row("nghiep"), _row("e2e-claude-code")], [E_NGHIEP], None)
    assert plan.ok and len(plan.changes) == 1
    ch = plan.changes[0]
    assert ch["kind"] == "identity" and ch["after"] == {"email": "nghieppham@adventure.asia",
                                                          "display_name": "Nghiệp Phạm"}
    assert not any("cognito" in k for k in ch["after"])


def test_plan_unknown_username_is_error():
    plan = lsi.compute_plan([_row("other")], [E_NGHIEP], None)
    assert not plan.ok and "no shared.admin_users row" in plan.errors[0]


def test_plan_email_taken_by_other_row_is_error():
    plan = lsi.compute_plan([_row("nghiep"), _row("old", email="nghieppham@adventure.asia")], [E_NGHIEP], None)
    assert not plan.ok and "already belongs to old" in plan.errors[0]


def test_plan_refuses_email_change_on_linked_row():
    rows = [_row("nghiep", email="old@adventure.asia", sub="abc-123")]
    plan = lsi.compute_plan(rows, [E_NGHIEP], None)
    assert not plan.ok and "already linked" in plan.errors[0]


def test_plan_linked_row_same_email_may_update_display_name():
    rows = [_row("nghiep", email="nghieppham@adventure.asia", sub="abc-123")]
    plan = lsi.compute_plan(rows, [E_NGHIEP], None)
    assert plan.ok and plan.changes[0]["after"]["display_name"] == "Nghiệp Phạm"


def test_plan_noop_is_warning_not_change():
    rows = [_row("nghiep", email="nghieppham@adventure.asia", display_name="Nghiệp Phạm")]
    plan = lsi.compute_plan(rows, [E_NGHIEP], None)
    assert plan.ok and plan.changes == [] and "no change" in plan.warnings[0]


def test_plan_keeps_display_name_when_not_given():
    rows = [_row("nghiep", display_name="Keep Me")]
    plan = lsi.compute_plan(rows, [lsi.MapEntry("nghiep", "nghieppham@adventure.asia", None)], None)
    assert plan.changes[0]["after"]["display_name"] == "Keep Me"


def test_plan_owner_sets_only_that_row_and_keeps_existing_owners():
    rows = [_row("nghiep", email="nghieppham@adventure.asia"), _row("breakglass", is_owner=True)]
    plan = lsi.compute_plan(rows, [], "nghiep")
    assert plan.ok
    assert [(c["kind"], c["username"]) for c in plan.changes] == [("owner", "nghiep")]
    assert any("existing owners kept unchanged: breakglass" in w for w in plan.warnings)


@pytest.mark.parametrize("rows,needle", [
    ([_row("other")], "no shared.admin_users row"),
    ([_row("nghiep", is_active=False)], "inactive"),
    ([_row("nghiep", role="reviewer")], "only an 'admin' row"),
    ([_row("nghiep", role="content")], "only an 'admin' row"),
])
def test_plan_owner_errors(rows, needle):
    plan = lsi.compute_plan(rows, [], "nghiep")
    assert not plan.ok and needle in plan.errors[0]


def test_plan_owner_already_owner_is_noop():
    plan = lsi.compute_plan([_row("nghiep", is_owner=True)], [], "nghiep")
    assert plan.ok and plan.changes == []


# ── guard: the script never writes cognito_iss / cognito_sub or the password hash ───────────────────

def test_sql_never_writes_cognito_identity_or_password():
    for sql in (lsi.UPDATE_IDENTITY_SQL, lsi.UPDATE_OWNER_SQL):
        set_clause = re.search(r"SET(.*?)WHERE", sql, re.S).group(1)
        assert "cognito" not in set_clause and "password" not in set_clause
        assert "role" not in set_clause and "is_active" not in set_clause
    for sql in (lsi.ROWS_SQL, lsi.ROWS_FOR_UPDATE_SQL):
        assert "password_hash" not in sql


def test_public_row_reports_cognito_ids_only_as_set_flags():
    out = lsi.public_row(_row("nghiep", email="nghieppham@adventure.asia", sub="abc-123"))
    assert "cognito_sub" not in out and "cognito_iss" not in out
    assert out["cognito_sub_set"] is True and out["cognito_iss_set"] is True
    assert "abc-123" not in json.dumps(out, default=str)
    assert lsi.public_row(_row("x"))["cognito_sub_set"] is False


def test_default_s3_key_is_per_mode_and_timestamp():
    import datetime as dt
    now = dt.datetime(2026, 10, 18, 9, 5, 7, tzinfo=dt.timezone.utc)
    assert lsi.default_s3_key("apply", now) == "scripts/s242_link_report_apply_20261018T090507Z.json"
    assert lsi.default_s3_key("dry-run", now) != lsi.default_s3_key("apply", now)
    assert lsi.parse_args(["--s3"]).s3_key is None


def test_import_roots_never_add_the_parent_of_a_tmp_script(monkeypatch):
    monkeypatch.setattr(lsi, "__file__", "/tmp/s.py")
    roots = lsi._import_roots()
    assert "/" not in roots and "/tmp" not in roots and set(roots) <= {"/app"}


# ── apply path with a fake connection ────────────────────────────────────────────────────────────────

class FakeConn:
    """rows: the table. fetch_seq: optional list of row lists returned by successive fetch() calls
    (then falls back to the table). owner_status: the status the owner UPDATE returns."""

    def __init__(self, rows, *, fetch_seq=None, owner_status="UPDATE 1"):
        self.rows = rows
        self.fetch_seq = list(fetch_seq or [])
        self.owner_status = owner_status
        self.executed = []
        self.closed = False

    async def fetch(self, sql, *args):
        src = self.fetch_seq.pop(0) if self.fetch_seq else self.rows
        return [dict(r) for r in src]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "UPDATE shared.admin_users" in sql:
            row = next(r for r in self.rows if str(r["id"]) == args[0])
            if "is_owner = TRUE" in sql:
                if self.owner_status != "UPDATE 1":
                    return self.owner_status
                row["is_owner"] = True
            else:
                row["email"], row["display_name"] = args[1], args[2]
            return "UPDATE 1"
        return "INSERT 0 1"

    def transaction(self):
        return _NullTx()

    async def close(self):
        self.closed = True


class _NullTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch(monkeypatch, conn, audits):
    async def fake_connect(secret_id):
        return conn

    async def fake_audit(c, **kw):
        audits.append(kw)

    monkeypatch.setattr(lsi, "_connect", fake_connect)
    monkeypatch.setattr(lsi, "_audit_writer", lambda: ("helper", fake_audit))


def test_run_dry_run_lists_usernames_and_writes_nothing(monkeypatch):
    conn, audits = FakeConn([_row("nghiep"), _row("e2e-claude-code")]), []
    _patch(monkeypatch, conn, audits)
    report, code = asyncio.run(lsi.run(lsi.parse_args(["--map", "nghiep=nghieppham@adventure.asia:N P"])))
    assert code == 0 and report["mode"] == "dry-run" and report["applied"] is False
    assert report["usernames"] == ["nghiep", "e2e-claude-code"]
    assert len(report["plan"]) == 1 and conn.executed == [] and audits == [] and conn.closed


def test_run_apply_updates_and_audits_each_change(monkeypatch):
    conn, audits = FakeConn([_row("nghiep"), _row("breakglass", is_owner=True)]), []
    _patch(monkeypatch, conn, audits)
    opts = lsi.parse_args(["--apply", "--map", "nghiep=nghieppham@adventure.asia:Nghiệp Phạm", "--owner", "nghiep"])
    report, code = asyncio.run(lsi.run(opts))
    assert code == 0 and report["applied"] is True and report["audit_writer"] == "helper"
    after = {r["username"]: r for r in report["rows_after"]}
    assert after["nghiep"]["email"] == "nghieppham@adventure.asia"
    assert after["nghiep"]["is_owner"] is True
    assert after["nghiep"]["cognito_sub_set"] is False and after["nghiep"]["cognito_iss_set"] is False
    assert after["breakglass"]["is_owner"] is True
    assert [a["action"] for a in audits] == [lsi.AUDIT_ACTION_LINKED, lsi.AUDIT_ACTION_OWNER]
    assert all(a["tenant_id"] is None and a["resource_type"] == "admin_user" for a in audits)
    json.dumps(report, default=str)  # serialisable


def test_run_apply_with_errors_writes_nothing(monkeypatch):
    conn, audits = FakeConn([_row("nghiep")]), []
    _patch(monkeypatch, conn, audits)
    opts = lsi.parse_args(["--apply", "--map", "nghiep=nghieppham@adventure.asia", "--owner", "ghost"])
    report, code = asyncio.run(lsi.run(opts))
    assert code == 2 and report["applied"] is False and conn.executed == [] and audits == []
    assert "rows_after" not in report


def test_run_apply_replans_under_lock_and_writes_nothing_when_rows_changed(monkeypatch):
    nghiep = _row("nghiep")
    # first read: email free; locked re-read: another row took the email in between
    locked = [dict(nghiep), _row("intruder", email="nghieppham@adventure.asia")]
    conn, audits = FakeConn([nghiep], fetch_seq=[[nghiep], locked]), []
    _patch(monkeypatch, conn, audits)
    report, code = asyncio.run(lsi.run(lsi.parse_args(["--apply", "--map", "nghiep=nghieppham@adventure.asia"])))
    assert code == 2 and report["applied"] is False
    assert "already belongs to intruder" in report["errors"][0]
    assert conn.executed == [] and audits == []
    assert [r["email"] for r in report["rows_after"]] == [None]  # table unchanged


def test_run_apply_owner_update_0_raises_reports_and_audits_nothing(monkeypatch):
    conn, audits = FakeConn([_row("nghiep", email="nghieppham@adventure.asia")], owner_status="UPDATE 0"), []
    _patch(monkeypatch, conn, audits)
    report, code = asyncio.run(lsi.run(lsi.parse_args(["--apply", "--owner", "nghiep"])))
    assert code == 2 and report["applied"] is False and audits == []
    assert len(conn.executed) == 1 and "is_owner = TRUE" in conn.executed[0][0]
    assert report["errors"][0].startswith("RuntimeError: owner update for nghiep")
    assert conn.closed


def test_run_connect_failure_still_returns_a_report(monkeypatch):
    async def boom(secret_id):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(lsi, "_connect", boom)
    report, code = asyncio.run(lsi.run(lsi.parse_args([])))
    assert code == 2 and report["applied"] is False
    assert report["errors"] == ["ConnectionError: no route to host"] and report["plan"] == []


def test_main_writes_s3_report_even_after_a_crash(monkeypatch, capsys):
    async def boom(secret_id):
        raise RuntimeError("secret is not a plain postgres DSN")

    written = {}
    monkeypatch.setattr(lsi, "_connect", boom)
    monkeypatch.setattr(lsi, "_write_s3", lambda report, key: written.setdefault(key, report) and f"s3://b/{key}")
    assert lsi.main(["--apply", "--owner", "nghiep", "--s3"]) == 2
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    (key,) = written
    assert key.startswith("scripts/s242_link_report_apply_") and key.endswith("Z.json")
    assert out["s3_report"] == f"s3://b/{key}" and "RuntimeError" in out["errors"][0]
    assert f"S3_REPORT s3://b/{key}" in captured.err


def test_main_keeps_stdout_report_when_s3_write_fails(monkeypatch, capsys):
    conn = FakeConn([_row("nghiep")])
    _patch(monkeypatch, conn, [])

    def fail(report, key):
        raise PermissionError("AccessDenied")

    monkeypatch.setattr(lsi, "_write_s3", fail)
    assert lsi.main(["--s3"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["s3_error"] == "PermissionError: AccessDenied" and out["usernames"] == ["nghiep"]


class RecordingConn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "INSERT 0 1"


def test_inline_audit_fallback_argument_order(monkeypatch):
    # Make the helper import fail so _audit_writer() falls back to the inline INSERT.
    monkeypatch.setitem(sys.modules, "services.acp_shared.audit_log", None)
    name, write = lsi._audit_writer()
    assert name == "inline"
    conn = RecordingConn()
    asyncio.run(write(conn, tenant_id=None, actor="script:x", action=lsi.AUDIT_ACTION_OWNER,
                      resource_type=lsi.AUDIT_RESOURCE_TYPE, resource_id=uuid.UUID(int=7),
                      details={"username": "nghiep"}))
    ((sql, args),) = conn.calls
    assert sql == lsi.AUDIT_INSERT_SQL
    cols = re.search(r"\((.*?)\)\s*VALUES", sql, re.S).group(1).replace(" ", "").split(",")
    assert cols == ["tenant_id", "actor", "action", "resource_type", "resource_id", "details"]
    assert args[:4] == ("script:x", lsi.AUDIT_ACTION_OWNER, "admin_user", str(uuid.UUID(int=7)))
    assert json.loads(args[4]) == {"username": "nghiep"}
