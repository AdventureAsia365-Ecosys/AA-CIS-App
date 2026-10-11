"""Create or update a staff login in shared.admin_users (AA-656).

Staff (admin / reviewer / content) log in through /auth/admin-login with a bcrypt-hashed password
and get a signed JWT; the role in that verified token drives the RBAC matrix (frontend/lib/rbac.ts).
This script is the only supported way to create those accounts: it reads the password from a prompt
(never a CLI arg, never an env var) and stores only the bcrypt hash, so no secret ends up in git,
shell history, or process listings.

Run it inside the api container (it has DATABASE_URL and the deps), e.g. via ECS exec:

    python scripts/create_staff_user.py <username> <role>

<role> is one of: admin, reviewer, content. If the username already exists you are asked whether to
reset its password / role (so this doubles as a password-reset tool).
"""
from __future__ import annotations

import asyncio
import getpass
import os
import sys

import asyncpg
import bcrypt

VALID_ROLES = ("admin", "reviewer", "content")


def _read_password() -> str:
    pw = getpass.getpass("Password: ")
    if len(pw) < 10:
        sys.exit("Password too short (min 10 chars).")
    if getpass.getpass("Confirm password: ") != pw:
        sys.exit("Passwords do not match.")
    return pw


async def _run(username: str, role: str) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set — run this inside the api container.")

    pw = _read_password()
    password_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

    conn = await asyncpg.connect(dsn)
    try:
        existing = await conn.fetchrow(
            "SELECT id, role FROM shared.admin_users WHERE username = $1", username
        )
        if existing:
            ans = input(
                f"User {username!r} already exists (role {existing['role']}). "
                "Reset password and role? [y/N] "
            ).strip().lower()
            if ans != "y":
                sys.exit("Aborted; no change made.")
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


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("Usage: python scripts/create_staff_user.py <username> <role>")
    username, role = sys.argv[1], sys.argv[2]
    if role not in VALID_ROLES:
        sys.exit(f"Invalid role {role!r}; must be one of {', '.join(VALID_ROLES)}.")
    asyncio.run(_run(username, role))


if __name__ == "__main__":
    main()
