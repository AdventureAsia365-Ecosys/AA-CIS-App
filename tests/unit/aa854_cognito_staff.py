"""AA-816 / AA-854 C3 test helpers: a local WORKFORCE issuer wired into the CIS staff verifier, and a fake
shared.admin_users that answers the staff_identity SQL. No network, no AWS, no DB."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from api.core import identity_config, staff_identity
from api.core.identity_verifier import IssuerConfig, MultiIssuerVerifier
from tests.unit.test_identity_verifier import _DROP, FakeFetcher, LocalIssuer

ISS = identity_config.DEV_WORKFORCE_ISSUER
CIS_ADMIN = identity_config.DEV_CIS_ADMIN_CLIENT_ID
BREAKGLASS = identity_config.DEV_BREAKGLASS_CLIENT_ID
BOOKING_STAFF = "5ifl9dgkr8a5outnmsjc8v1kt3"
SUB = "4a1b2c3d-0000-4000-8000-000000000001"
EMAIL = "staff@adventure.asia"

__all__ = ["ISS", "CIS_ADMIN", "BREAKGLASS", "BOOKING_STAFF", "SUB", "EMAIL", "DROP",
           "StaffIssuer", "Directory", "install_issuer"]

DROP = _DROP


class StaffIssuer(LocalIssuer):
    """The WORKFORCE issuer; tokens default to the cis-admin client and a CIS admin + Booking admin user."""

    def token(self, **claims: Any) -> str:  # type: ignore[override]
        claims.setdefault("client_id", CIS_ADMIN)
        claims.setdefault("sub", SUB)
        claims.setdefault("username", claims["sub"])
        claims.setdefault("email", EMAIL)
        return super().token(**claims)


def install_issuer() -> tuple[StaffIssuer, FakeFetcher]:
    """Replace the process-wide CIS staff verifier with one that trusts a local WORKFORCE issuer."""
    issuer = StaffIssuer(ISS)
    fetcher = FakeFetcher(issuer)
    cfg = IssuerConfig(issuer=ISS, client_ids=frozenset({CIS_ADMIN, BREAKGLASS}), name="workforce")
    identity_config._verifier = MultiIssuerVerifier([cfg], fetcher)
    staff_identity.clear_cache()
    return issuer, fetcher


class Directory:
    """shared.admin_users as staff_identity (and the admin_platform list) read it."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.sql: list[str] = []
        self.links = 0
        self.touches: list[str] = []
        self.fail = False
        self.fail_touch = False

    def add(self, *, role: str = "admin", email: Optional[str] = EMAIL, active: bool = True,
            iss: Optional[str] = None, sub: Optional[str] = None, username: str = "staff-1",
            display_name: Optional[str] = "Staff One") -> str:
        uid = str(uuid.uuid4())
        self.rows.append({
            "id": uid, "username": username, "role": role, "email": email, "display_name": display_name,
            "is_active": active, "cognito_iss": iss, "cognito_sub": sub, "last_login_at": None,
        })
        return uid

    def row(self, uid: str) -> dict[str, Any]:
        return next(r for r in self.rows if r["id"] == uid)

    @asynccontextmanager
    async def acquire(self):
        if self.fail:
            raise OSError("RDS unreachable")
        yield self

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        self.sql.append(sql)
        if sql == staff_identity.SQL_BY_IDENTITY:
            return next((dict(r) for r in self.rows
                         if r["cognito_iss"] == args[0] and r["cognito_sub"] == args[1]), None)
        if sql == staff_identity.SQL_FIRST_LINK:
            for r in self.rows:
                if ((r["email"] or "").lower() == args[2] and r["cognito_iss"] is None
                        and r["cognito_sub"] is None and r["is_active"]):
                    r["cognito_iss"], r["cognito_sub"] = args[0], args[1]
                    self.links += 1
                    return dict(r)
            return None
        if sql == staff_identity.SQL_BY_EMAIL:
            r = next((r for r in self.rows if (r["email"] or "").lower() == args[0]), None)
            return None if r is None else {"id": r["id"], "is_active": r["is_active"],
                                           "bound": r["cognito_sub"] is not None}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        self.sql.append(sql)
        if sql == staff_identity.SQL_TOUCH_LOGIN:
            if self.fail_touch:
                raise OSError("connection reset during last_login_at write")
            self.touches.append(str(args[0]))
            self.row(str(args[0]))["last_login_at"] = "now"
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.sql.append(sql)
        return [{k: r[k] for k in ("id", "username", "role", "is_active")} | {"created_at": None, "updated_at": None}
                for r in self.rows]
