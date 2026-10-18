#!/usr/bin/env python3
"""AA-816 (S242, plan step C8) — link the first staff rows in `shared.admin_users` to their work email.

Migration 247 added `email`, `display_name`, `cognito_iss`, `cognito_sub`, `last_login_at` and `is_owner`.
The Cognito verifier (C3) matches a WORKFORCE access token to a staff row on `lower(email)`; on the first
real sign-in it fills `cognito_iss` / `cognito_sub`. This script only prepares the rows for that:

  * sets `email` (and optionally `display_name`) for the usernames given on the command line;
  * optionally sets `is_owner = true` for exactly one username (`--owner`); it never clears another
    row's owner flag (break-glass is an owner too);
  * NEVER writes `cognito_iss` / `cognito_sub` (a real sign-in fills them), `password_hash`, `role` or
    `is_active`.

Runs INSIDE the CIS `api` container through the S3-mediated ECS exec pattern (python3 + asyncpg + boto3).
The DSN comes from Secrets Manager `aa-cis/dev/rds` (a plain DSN string), parsed with urlparse, and the
connection always uses ssl="require".

Modes:
  (default) --dry-run   read only. Lists every staff row (no password hash) and prints the plan for the
                        given --map / --owner. Nothing is written to the DB.
  --apply               runs the same plan inside ONE transaction (rows locked FOR UPDATE, plan
                        recomputed under the lock); any error aborts with no change. Each changed row
                        gets an `acp_shared.audit_log` row in the same transaction.

Mapping: --map 'username=email[:Display Name]' (repeatable). The email must be @adventure.asia (stored
lower-case, the same rule as the DB check `admin_users_email_chk`). Leaving out ':Display Name' keeps
the current display name. Because a Vietnamese display name with spaces is awkward inside the nested
quotes of `ecs execute-command`, --map-b64 takes the same entries base64-encoded (UTF-8, one per line):

  printf 'nghiep=nghieppham@adventure.asia:Nghiệp Phạm\\n' | base64 -w0

Output: a JSON report on stdout; with --s3 it is also written to
s3://aa-cis-bronze-005097885195/scripts/s242_link_report_<mode>_<UTC timestamp>.json (long ECS exec stdout
gets cut). Each run gets its own key, so a later dry run never overwrites an apply report; the key is also
printed to stderr as `S3_REPORT s3://...`. The report holds staff emails / display names (PII): delete it
from the bucket once read. Cognito ids are reported only as set / not set.
Exit code 0 = no errors, 2 = validation / plan / runtime errors. Nothing is applied unless `applied` is true:
an unexpected exception is caught and reported in `errors`, and the DB transaction rolls back.

Run (each line one command; see skill/aa-cis-schema/references/ecs-exec.md):
  aws s3 cp scripts/link_staff_identity.py s3://aa-cis-bronze-005097885195/scripts/link_staff_identity.py --profile aa365-admin --region us-west-1
  URL=$(aws s3 presign s3://aa-cis-bronze-005097885195/scripts/link_staff_identity.py --profile aa365-admin --region us-west-1 --expires-in 300)
  TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-api --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)
  aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container api --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/s.py && cd /app && python3 /tmp/s.py --dry-run --s3'" --profile aa365-admin --region us-west-1
  aws s3 ls s3://aa-cis-bronze-005097885195/scripts/s242_link_report_ --profile aa365-admin --region us-west-1
  aws s3 cp s3://aa-cis-bronze-005097885195/scripts/<key from the listing> .tmp-session/s242_link_report.json --profile aa365-admin --region us-west-1
Then the same with: python3 /tmp/s.py --apply --map-b64 <b64> --owner <username> --s3
Afterwards delete the reports (PII):
  aws s3 rm s3://aa-cis-bronze-005097885195/scripts/ --recursive --exclude "*" --include "s242_link_report_*" --profile aa365-admin --region us-west-1
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
import json
import os
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SECRET_ID = "aa-cis/dev/rds"
REGION = "us-west-1"
S3_BUCKET = "aa-cis-bronze-005097885195"
S3_KEY_PREFIX = "scripts/s242_link_report_"
DEFAULT_ACTOR = "script:link_staff_identity"

# Stricter than the DB check admin_users_email_chk ('^[^@[:space:]]+@adventure[.]asia$' on a lower-case
# value): the local part is ASCII only, so Python .lower() and Postgres lower() can never disagree (e.g. 'İ').
EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@adventure\.asia$")
OWNER_ROLE = "admin"  # plan C7: owners are Nghiệp + break-glass, both admins
USERNAME_MAX = 100  # shared.admin_users.username is varchar(100)
DISPLAY_NAME_MAX = 100
EMAIL_MAX = 254
ACTOR_MAX = 64  # acp_shared.audit_log.actor is varchar(64)

AUDIT_RESOURCE_TYPE = "admin_user"  # acp_shared.audit_log.resource_type is varchar(32)
AUDIT_ACTION_LINKED = "admin_user.email_linked"
AUDIT_ACTION_OWNER = "admin_user.owner_granted"

# Every column read; password_hash is deliberately never selected.
ROWS_SQL = """
SELECT id, username, role::text AS role, is_active, email, display_name, cognito_iss, cognito_sub,
       last_login_at, is_owner, created_at, updated_at
  FROM shared.admin_users
 ORDER BY created_at, username
"""
ROWS_FOR_UPDATE_SQL = """
SELECT id, username, role::text AS role, is_active, email, display_name, cognito_iss, cognito_sub,
       last_login_at, is_owner, created_at, updated_at
  FROM shared.admin_users
 ORDER BY created_at, username
   FOR UPDATE
"""
# The only two writes this script makes. Neither touches cognito_iss / cognito_sub (a guard test checks).
UPDATE_IDENTITY_SQL = """
UPDATE shared.admin_users
   SET email = $2, display_name = $3, updated_at = NOW()
 WHERE id = $1
"""
UPDATE_OWNER_SQL = """
UPDATE shared.admin_users
   SET is_owner = TRUE, updated_at = NOW()
 WHERE id = $1 AND is_owner = FALSE
"""
# Fallback with the exact column list of services.acp_shared.audit_log.write_audit_log, used only when
# that helper cannot be imported (e.g. the script is run outside /app).
AUDIT_INSERT_SQL = """
INSERT INTO acp_shared.audit_log (tenant_id, actor, action, resource_type, resource_id, details)
VALUES (NULL, $1, $2, $3, $4, $5::jsonb)
"""


class MapError(ValueError):
    """A --map / --map-b64 / --owner / --actor value is invalid."""


@dataclass(frozen=True)
class MapEntry:
    username: str
    email: str
    display_name: Optional[str]  # None = keep the current display name


@dataclass
class Plan:
    changes: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ── parsing / validation (pure, unit-tested) ─────────────────────────────────────────────────────────

def _has_control(s: str) -> bool:
    return any(unicodedata.category(ch).startswith("C") for ch in s)


def validate_username(raw: str) -> str:
    username = (raw or "").strip()
    if not username:
        raise MapError("username is empty")
    if len(username) > USERNAME_MAX:
        raise MapError(f"username {username[:20]!r}... is longer than {USERNAME_MAX} chars")
    if any(ch.isspace() for ch in username) or _has_control(username):
        raise MapError(f"username {username!r} contains whitespace or control characters")
    return username


def validate_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not email:
        raise MapError("email is empty")
    if len(email) > EMAIL_MAX:
        raise MapError(f"email is longer than {EMAIL_MAX} chars")
    if _has_control(email) or not EMAIL_RE.fullmatch(email):
        raise MapError(f"email {email!r} is not an @adventure.asia address")
    return email


def validate_display_name(raw: str) -> str:
    name = unicodedata.normalize("NFC", (raw or "").strip())
    if not name:
        raise MapError("display name is empty (leave out ':Name' to keep the current one)")
    if len(name) > DISPLAY_NAME_MAX:
        raise MapError(f"display name is longer than {DISPLAY_NAME_MAX} chars")
    if _has_control(name):
        raise MapError("display name contains control characters")
    return name


def validate_actor(raw: str) -> str:
    actor = (raw or "").strip()
    if not actor or len(actor) > ACTOR_MAX or _has_control(actor):
        raise MapError(f"actor must be 1..{ACTOR_MAX} printable chars")
    return actor


def parse_map_entry(spec: str) -> MapEntry:
    """'username=email[:Display Name]' -> MapEntry. The display name may itself contain ':'."""
    if spec is None or "=" not in spec:
        raise MapError(f"map entry {spec!r} must look like 'username=email[:Display Name]'")
    user_part, rest = spec.split("=", 1)
    username = validate_username(user_part)
    if ":" in rest:
        email_part, name_part = rest.split(":", 1)
        display_name: Optional[str] = validate_display_name(name_part)
    else:
        email_part, display_name = rest, None
    return MapEntry(username=username, email=validate_email(email_part), display_name=display_name)


def decode_map_b64(blob: str) -> list[str]:
    """Base64 (UTF-8 text, one map entry per line; blank lines ignored) -> entry strings."""
    try:
        text = base64.b64decode((blob or "").strip(), validate=True).decode("utf-8")
    except ValueError as exc:  # binascii.Error and UnicodeDecodeError are both ValueError
        raise MapError(f"--map-b64 is not valid base64 UTF-8 text: {exc}") from exc
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise MapError("--map-b64 decodes to no entries")
    return lines


def parse_mapping(specs: list[str]) -> list[MapEntry]:
    entries = [parse_map_entry(s) for s in specs]
    seen_users: set[str] = set()
    seen_emails: set[str] = set()
    for e in entries:
        if e.username in seen_users:
            raise MapError(f"username {e.username!r} appears more than once in the mapping")
        if e.email in seen_emails:
            raise MapError(f"email {e.email!r} is mapped to more than one username")
        seen_users.add(e.username)
        seen_emails.add(e.email)
    return entries


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="link_staff_identity.py",
        description="Set email / display_name (and optionally is_owner) on shared.admin_users rows (AA-816).",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false",
                      help="read only: list staff rows and print the plan (default)")
    mode.add_argument("--apply", dest="apply", action="store_true",
                      help="write the plan in one transaction")
    p.set_defaults(apply=False)
    p.add_argument("--map", action="append", default=[], metavar="USERNAME=EMAIL[:NAME]",
                   help="repeatable; e.g. 'nghiep=nghieppham@adventure.asia:Nghiep Pham'")
    p.add_argument("--map-b64", default=None, metavar="B64",
                   help="the same entries, base64 UTF-8, one per line (avoids ECS exec quoting)")
    p.add_argument("--owner", default=None, metavar="USERNAME",
                   help="set is_owner=true for exactly this username (never clears other owners)")
    p.add_argument("--actor", default=DEFAULT_ACTOR, help=f"audit_log actor (<= {ACTOR_MAX} chars)")
    p.add_argument("--s3", action="store_true",
                   help=f"also write the report to s3://{S3_BUCKET}/{S3_KEY_PREFIX}<mode>_<ts>.json")
    p.add_argument("--s3-key", default=None, help=argparse.SUPPRESS)
    p.add_argument("--secret-id", default=SECRET_ID, help=argparse.SUPPRESS)
    return p


@dataclass(frozen=True)
class Options:
    apply: bool
    entries: list[MapEntry]
    owner: Optional[str]
    actor: str
    s3: bool
    s3_key: Optional[str]  # None = per-run key from default_s3_key()
    secret_id: str


def parse_args(argv: Optional[list[str]] = None) -> Options:
    """argparse + validation. Raises MapError on a bad value (argparse itself exits 2 on bad flags)."""
    ns = build_parser().parse_args(argv)
    specs = list(ns.map)
    if ns.map_b64:
        specs.extend(decode_map_b64(ns.map_b64))
    entries = parse_mapping(specs)
    owner = validate_username(ns.owner) if ns.owner is not None else None
    if ns.apply and not entries and owner is None:
        raise MapError("--apply needs at least one --map / --map-b64 entry or --owner")
    return Options(apply=ns.apply, entries=entries, owner=owner, actor=validate_actor(ns.actor),
                   s3=ns.s3, s3_key=ns.s3_key, secret_id=ns.secret_id)


# ── planning (pure, unit-tested) ─────────────────────────────────────────────────────────────────────

def compute_plan(rows: list[dict[str, Any]], entries: list[MapEntry], owner: Optional[str]) -> Plan:
    """Diff the requested mapping against the current rows. Never plans a cognito_* change."""
    plan = Plan()
    by_user = {r["username"]: r for r in rows}
    email_owner = {(r["email"] or "").lower(): r["username"] for r in rows if r.get("email")}

    for e in entries:
        row = by_user.get(e.username)
        if row is None:
            plan.errors.append(f"{e.username}: no shared.admin_users row with this username")
            continue
        holder = email_owner.get(e.email)
        if holder is not None and holder != e.username:
            plan.errors.append(f"{e.username}: email {e.email} already belongs to {holder}")
            continue
        current_email = row.get("email")
        if row.get("cognito_sub") and (current_email or "").lower() != e.email:
            plan.errors.append(
                f"{e.username}: already linked to a Cognito identity (cognito_sub set); "
                "change its email through the staff directory, not this script")
            continue
        new_name = e.display_name if e.display_name is not None else row.get("display_name")
        if current_email == e.email and row.get("display_name") == new_name:
            plan.warnings.append(f"{e.username}: email and display name already set; no change")
            continue
        if not row.get("is_active"):
            plan.warnings.append(f"{e.username}: row is inactive (is_active=false); linking anyway")
        plan.changes.append({
            "kind": "identity", "id": str(row["id"]), "username": e.username,
            "before": {"email": current_email, "display_name": row.get("display_name")},
            "after": {"email": e.email, "display_name": new_name},
        })

    if owner is not None:
        row = by_user.get(owner)
        if row is None:
            plan.errors.append(f"--owner {owner}: no shared.admin_users row with this username")
        elif not row.get("is_active"):
            plan.errors.append(f"--owner {owner}: row is inactive; refusing to make it an owner")
        elif row.get("role") != OWNER_ROLE:
            plan.errors.append(
                f"--owner {owner}: role is {row.get('role')!r}; only an {OWNER_ROLE!r} row can be an owner")
        elif row.get("is_owner"):
            plan.warnings.append(f"--owner {owner}: already is_owner=true; no change")
        else:
            mapped = any(e.username == owner for e in entries)
            if not row.get("email") and not mapped:
                plan.warnings.append(f"--owner {owner}: row has no email yet, so it cannot sign in with Cognito")
            plan.changes.append({
                "kind": "owner", "id": str(row["id"]), "username": owner,
                "before": {"is_owner": False}, "after": {"is_owner": True},
            })
        others = sorted(r["username"] for r in rows if r.get("is_owner") and r["username"] != owner)
        if others:
            plan.warnings.append(f"existing owners kept unchanged: {', '.join(others)}")
    return plan


def public_row(r: dict[str, Any]) -> dict[str, Any]:
    """A row as it goes into the report (password_hash is never selected in the first place). The Cognito
    issuer / subject are reported only as set / not set, to keep online ids out of the S3 report."""
    keys = ("id", "username", "role", "is_active", "email", "display_name",
            "last_login_at", "is_owner", "created_at", "updated_at")
    out = {k: (str(r.get(k)) if k == "id" and r.get(k) is not None else r.get(k)) for k in keys}
    out["cognito_iss_set"] = bool(r.get("cognito_iss"))
    out["cognito_sub_set"] = bool(r.get("cognito_sub"))
    return out


def default_s3_key(mode: str, now: Optional[datetime.datetime] = None) -> str:
    """One key per run: scripts/s242_link_report_<mode>_<YYYYmmddTHHMMSSZ>.json."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return f"{S3_KEY_PREFIX}{mode}_{now.strftime('%Y%m%dT%H%M%SZ')}.json"


# ── DB / AWS (runs only inside the container) ────────────────────────────────────────────────────────

def _dsn_from_secret(secret_id: str) -> str:
    import boto3

    sm = boto3.client("secretsmanager", region_name=REGION)
    dsn = sm.get_secret_value(SecretId=secret_id)["SecretString"].strip()
    if not dsn.startswith(("postgres://", "postgresql://")):
        raise RuntimeError(f"secret {secret_id} is not a plain postgres DSN")
    return dsn


async def _connect(secret_id: str):
    import asyncpg
    from urllib.parse import unquote, urlparse

    u = urlparse(_dsn_from_secret(secret_id))
    return await asyncpg.connect(
        host=u.hostname, port=u.port or 5432,
        user=unquote(u.username or ""), password=unquote(u.password or ""),
        database=u.path.lstrip("/"), ssl="require",
    )


def _import_roots() -> list[str]:
    """Where the app package may live: /app in the container, the repo root when run from scripts/."""
    roots = []
    if os.path.isdir("/app"):
        roots.append("/app")
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(here) == "scripts":
        roots.append(os.path.dirname(here))
    return roots


def _audit_writer():
    """Prefer the shared helper (services.acp_shared.audit_log.write_audit_log); fall back to its exact
    INSERT when it cannot be imported (the script was fetched to /tmp and /app is not importable).
    Import roots are appended (never prepended), so nothing next to the script shadows the app."""
    for path in _import_roots():
        if path not in sys.path:
            sys.path.append(path)
    try:
        from services.acp_shared.audit_log import write_audit_log
    except Exception:  # noqa: BLE001 — any import failure means "use the inline INSERT"
        async def _inline(conn, *, tenant_id, actor, action, resource_type, resource_id, details=None):
            await conn.execute(AUDIT_INSERT_SQL, actor, action, resource_type, str(resource_id),
                               json.dumps(details or {}, default=str))
        return "inline", _inline
    return "helper", write_audit_log


async def _apply(conn, opts: Options, report: dict[str, Any]) -> Plan:
    writer_name, write_audit = _audit_writer()
    report["audit_writer"] = writer_name
    async with conn.transaction():
        locked = [dict(r) for r in await conn.fetch(ROWS_FOR_UPDATE_SQL)]
        plan = compute_plan(locked, opts.entries, opts.owner)
        if not plan.ok:
            return plan  # nothing written; leaving the block commits an empty transaction
        for ch in plan.changes:
            if ch["kind"] == "identity":
                await conn.execute(UPDATE_IDENTITY_SQL, ch["id"], ch["after"]["email"],
                                   ch["after"]["display_name"])
                action = AUDIT_ACTION_LINKED
            else:
                status = await conn.execute(UPDATE_OWNER_SQL, ch["id"])
                if status != "UPDATE 1":
                    raise RuntimeError(f"owner update for {ch['username']} touched {status!r}, expected 1 row")
                action = AUDIT_ACTION_OWNER
            await write_audit(conn, tenant_id=None, actor=opts.actor, action=action,
                              resource_type=AUDIT_RESOURCE_TYPE, resource_id=ch["id"],
                              details={"username": ch["username"], "before": ch["before"],
                                       "after": ch["after"], "issue": "AA-816"})
    return plan


async def run(opts: Options) -> tuple[dict[str, Any], int]:
    report: dict[str, Any] = {
        "script": "link_staff_identity", "issue": "AA-816",
        "mode": "apply" if opts.apply else "dry-run",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "secret_id": opts.secret_id,
        "requested": {"map": [asdict(e) for e in opts.entries], "owner": opts.owner, "actor": opts.actor},
        "applied": False,
        "plan": [], "errors": [], "warnings": [],
    }
    conn = None
    try:
        conn = await _connect(opts.secret_id)
        before = [dict(r) for r in await conn.fetch(ROWS_SQL)]
        report["usernames"] = [r["username"] for r in before]
        report["rows_before"] = [public_row(r) for r in before]
        plan = compute_plan(before, opts.entries, opts.owner)
        report.update(plan=plan.changes, errors=plan.errors, warnings=plan.warnings)
        if opts.apply and plan.ok and plan.changes:
            plan = await _apply(conn, opts, report)
            report.update(plan=plan.changes, errors=plan.errors, warnings=plan.warnings)
            report["applied"] = plan.ok and bool(plan.changes)
            report["rows_after"] = [public_row(dict(r)) for r in await conn.fetch(ROWS_SQL)]
    except Exception as exc:  # noqa: BLE001 — keep the report (and its S3 copy) whatever fails
        # An exception inside _apply's transaction block rolls it back, so `applied` stays False there;
        # it is only True when the commit already happened (e.g. the rows_after read failed).
        report["errors"] = list(report["errors"]) + [f"{type(exc).__name__}: {exc}"]
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception as exc:  # noqa: BLE001
                report["warnings"] = list(report["warnings"]) + [f"close failed: {type(exc).__name__}: {exc}"]
    return report, (0 if not report["errors"] else 2)


def _write_s3(report: dict[str, Any], key: str) -> str:
    import boto3

    body = json.dumps(report, default=str, ensure_ascii=False, indent=2).encode("utf-8")
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=S3_BUCKET, Key=key, Body=body, ContentType="application/json")
    return f"s3://{S3_BUCKET}/{key}"


def main(argv: Optional[list[str]] = None) -> int:
    try:
        opts = parse_args(argv)
    except MapError as exc:
        print(json.dumps({"script": "link_staff_identity", "errors": [str(exc)], "applied": False}))
        return 2
    report, code = asyncio.run(run(opts))
    if opts.s3:
        key = opts.s3_key or default_s3_key(report["mode"])
        try:
            report["s3_report"] = _write_s3(report, key)
            print(f"S3_REPORT {report['s3_report']}", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 — stdout still carries the report
            report["s3_error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(report, default=str, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
