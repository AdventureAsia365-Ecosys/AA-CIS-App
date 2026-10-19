"""Create or update a staff login in shared.admin_users (AA-656).

Staff (admin / reviewer / content / viewer) log in through /auth/admin-login with a bcrypt-hashed password
and get a signed JWT; the role in that verified token drives the RBAC matrix (frontend/lib/rbac.ts).
This script is the only supported way to create those accounts: it reads the password from a prompt
(never a CLI arg, never an env var) and stores only the bcrypt hash, so no secret ends up in git,
shell history, or process listings.

Run it inside the api container (it has DATABASE_URL and the deps), e.g. via ECS exec:

    python scripts/create_staff_user.py <username> <role>

<role> is one of: admin, reviewer, content, viewer. If the username already exists you are asked whether to
reset its password / role (so this doubles as a password-reset tool).

Non-interactive mode (AA-878, for the read-only E2E account created through the S3-mediated ECS exec, where no
prompt is available):

    python scripts/create_staff_user.py --password-from-secret <secret-name> [--reset] <username> <role>

The password comes from Secrets Manager (the task role's GetSecretValue): a JSON SecretString
{"username": ..., "password": ...} whose username must equal <username>. The value is never printed or logged.
An existing username is left unchanged (exit 1) unless --reset is given, which sets the password hash and the
role and re-activates the row. No email, display name or owner flag is set (the row is a legacy password
account, not a Cognito one).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from typing import Optional

import asyncpg
import bcrypt

VALID_ROLES = ("admin", "reviewer", "content", "viewer")
MIN_PASSWORD_LEN = 10
# The Secrets Manager region when the container does not say (acc2, us-west-1).
DEFAULT_REGION = "us-west-1"


def _read_password() -> str:
    pw = getpass.getpass("Password: ")
    if len(pw) < MIN_PASSWORD_LEN:
        sys.exit(f"Password too short (min {MIN_PASSWORD_LEN} chars).")
    if getpass.getpass("Confirm password: ") != pw:
        sys.exit("Passwords do not match.")
    return pw


def password_from_secret_string(secret_string: Optional[str], username: str) -> str:
    """The password inside a {"username", "password"} SecretString. Exits (without echoing any value) when the
    secret is not that shape, names another user, or holds a short password."""
    try:
        data = json.loads(secret_string or "")
    except ValueError:
        sys.exit("Secret is not JSON {\"username\": ..., \"password\": ...}.")
    if not isinstance(data, dict):
        sys.exit("Secret is not JSON {\"username\": ..., \"password\": ...}.")
    if data.get("username") != username:
        sys.exit("Secret username does not match the <username> argument; no change made.")
    pw = data.get("password")
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD_LEN:
        sys.exit(f"Secret password missing or too short (min {MIN_PASSWORD_LEN} chars).")
    return pw


def _password_from_secret(secret_id: str, username: str) -> str:
    import boto3  # the api image has it; imported here so the interactive path does not need it

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION
    client = boto3.client("secretsmanager", region_name=region)
    try:
        resp = client.get_secret_value(SecretId=secret_id)
    except Exception as exc:  # noqa: BLE001 — report the class only, never the response
        sys.exit(f"Could not read secret {secret_id!r}: {type(exc).__name__}")
    return password_from_secret_string(resp.get("SecretString"), username)


async def _run(username: str, role: str, *, secret_id: Optional[str] = None, reset: bool = False) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set — run this inside the api container.")

    interactive = secret_id is None
    pw = _read_password() if interactive else _password_from_secret(secret_id, username)
    password_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    del pw

    conn = await asyncpg.connect(dsn)
    try:
        existing = await conn.fetchrow(
            "SELECT id, role FROM shared.admin_users WHERE username = $1", username
        )
        if existing:
            if interactive:
                ans = input(
                    f"User {username!r} already exists (role {existing['role']}). "
                    "Reset password and role? [y/N] "
                ).strip().lower()
                if ans != "y":
                    sys.exit("Aborted; no change made.")
            elif not reset:
                sys.exit(f"User {username!r} already exists (role {existing['role']}); "
                         "pass --reset to set its password and role. No change made.")
            await conn.execute(
                "UPDATE shared.admin_users "
                "SET password_hash = $2, role = $3::shared.admin_role, "
                "    is_active = TRUE, updated_at = NOW() "
                "WHERE username = $1",
                username, password_hash, role,
            )
            print(f"Updated {username} (role {role}).")
        else:
            row = await conn.fetchrow(
                "INSERT INTO shared.admin_users (username, password_hash, role) "
                "VALUES ($1, $2, $3::shared.admin_role) RETURNING id",
                username, password_hash, role,
            )
            print(f"Created {username} (role {role}, id {row['id']}).")
    finally:
        await conn.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or update a staff login (shared.admin_users).")
    parser.add_argument("username")
    parser.add_argument("role", choices=VALID_ROLES)
    parser.add_argument("--password-from-secret", dest="secret_id", metavar="SECRET_NAME",
                        help="read {username, password} from this Secrets Manager secret (no prompt)")
    parser.add_argument("--reset", action="store_true",
                        help="with --password-from-secret: update an existing username instead of refusing")
    args = parser.parse_args(argv)
    if args.reset and not args.secret_id:
        parser.error("--reset only applies with --password-from-secret (the prompt asks instead)")
    return args


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    asyncio.run(_run(args.username, args.role, secret_id=args.secret_id, reset=args.reset))


if __name__ == "__main__":
    main()
