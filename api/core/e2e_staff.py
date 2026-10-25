"""AA-816 A10b (S252, root ADR 0006a A10b) — the Dev-only E2E CIS admin: constants and rules shared by the staff
directory (api/services/staff_directory.py), the identity resolver (api/core/staff_identity.py), the bearer
middleware (api/middleware/staff_bearer.py) and the route policy (api/core/staff_authz.py).

A tiny module of its own so staff_identity can read the address without importing staff_directory (which imports
staff_identity).

TEMPORARY, removed at the end of project code with A10 / A10a (AA-816):

- :data:`E2E_CIS_ADMIN_TEST_STAFF_EMAIL` is the one address. Its directory row is an ``admin``, not an owner, without
  a password (migration 283 CHECK) and without a Booking grant; the WORKFORCE pre token trigger issues its tokens
  only with exactly ``cis-admin`` and only where ``E2E_DEV_ONLY_STAFF_ENABLED`` is ``"true"`` (Terraform: dev only).
- :func:`dev_only_e2e_environment`: the API's ``ENVIRONMENT`` must be exactly ``"dev"`` (no default, no strip, no
  lower-casing: unset, ``"Dev "``, ``"prod"`` all refuse) to create, re-enable or resolve this account.
- Its session is **read-only** (``e2e_read_only`` on the identity): every non-GET/HEAD/OPTIONS on a staff path is
  refused with 403 ``e2e_read_only`` before the route runs, EXCEPT the explicit allow-list
  :data:`E2E_ALLOWED_WRITES` — the two content-quality sentence-apply routes behind ``/admin/content-quality`` Cut and
  manual Edit with a reason chip (S252 orchestrator decision: the AA-916 WM-07 sample of admin sentence actions on
  synthetic Dev tenants). On those routes only ``mode`` ``cut`` / ``manual`` is accepted (:data:`E2E_SENTENCE_MODES`):
  ``rewrite`` records a model suggestion, and ``.../sentences/suggest`` (the paid model call) stays refused.
"""
from __future__ import annotations

import os
import re
from typing import Any

E2E_CIS_ADMIN_TEST_STAFF_EMAIL = "e2e-cis-admin@e2e-mail.lumiguides.it.com"
E2E_CIS_ADMIN_TEST_STAFF_ROLE = "admin"

# The one ENVIRONMENT value under which the account may exist (compared exactly).
DEV_ONLY_ENVIRONMENT = "dev"

# The 403 details (the BFF and the agent's helpers match on them).
E2E_READ_ONLY = "e2e_read_only"
E2E_NOT_ALLOWED_HERE = "e2e_not_allowed_here"

READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# The writes an A10b session may make: (method, route template). The route policy (staff_authz) matches on the
# template; the middleware, which runs before routing, matches the concrete path with E2E_ALLOWED_WRITE_PATHS.
E2E_ALLOWED_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/admin/content-quality/pieces/{piece_id}/sentences/apply"),
    ("POST", "/admin/content-quality/tour-versions/{version_id}/sentences/apply"),
})
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
E2E_ALLOWED_WRITE_PATHS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(rf"/admin/content-quality/pieces/{_UUID}/sentences/apply")),
    ("POST", re.compile(rf"/admin/content-quality/tour-versions/{_UUID}/sentences/apply")),
)
# The sentence-apply modes an A10b session may use (Cut, manual Edit). Not "rewrite".
E2E_SENTENCE_MODES: frozenset[str] = frozenset({"cut", "manual"})


def normalise(email: Any) -> str:
    return email.strip().lower() if isinstance(email, str) else ""


def is_e2e_cis_admin(email: Any) -> bool:
    """True for the A10b address (after trimming and lower-casing)."""
    return normalise(email) == E2E_CIS_ADMIN_TEST_STAFF_EMAIL


def dev_only_e2e_environment() -> bool:
    """True only when ``ENVIRONMENT`` is exactly ``"dev"`` (read per call; unset refuses, unlike the
    ``os.getenv("ENVIRONMENT", "dev")`` default used elsewhere)."""
    return os.environ.get("ENVIRONMENT") == DEV_ONLY_ENVIRONMENT


def is_allowed_e2e_write_path(method: str, path: str) -> bool:
    """The middleware's check: the concrete (method, path) is one of :data:`E2E_ALLOWED_WRITE_PATHS`, matched in full
    (no prefix, no trailing slash, no extra segment)."""
    return any(method == m and pattern.fullmatch(path or "") for m, pattern in E2E_ALLOWED_WRITE_PATHS)


def is_allowed_e2e_write_route(method: str, route: str) -> bool:
    """The route policy's check, on the matched route template."""
    return (method.upper(), route) in E2E_ALLOWED_WRITES
