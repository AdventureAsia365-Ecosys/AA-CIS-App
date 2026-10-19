"""AA-816 C9 — the staff BFF proxies send a Cognito session's Bearer and NO X-Admin-Secret on the staff API paths,
and the secret for a legacy HS256 session or a path outside them (frontend/lib/auth-server.ts
staffCredentialHeaders). Source checks, like test_aa882_staff_authz's BFF tests; the API side is in
test_aa816_c9_principal_secret.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from api.core.staff_authz import STAFF_PATH_PREFIXES

REPO = Path(__file__).resolve().parents[2]

# ── BFF proxies ──────────────────────────────────────────────────────────────────────────────────

_BFFS = ["frontend/app/api/tenant/[...path]/route.ts", "frontend/app/api/admin/[...path]/route.ts",
         "frontend/app/api/pipeline/[...path]/route.ts", "frontend/app/api/tour-full/[id]/route.ts",
         "frontend/app/api/tenant/pipeline/brand-identity/route.ts"]


def _auth_server() -> str:
    return (REPO / "frontend" / "lib" / "auth-server.ts").read_text()


def test_bff_staff_api_prefixes_match_the_api():
    m = re.search(r"export const STAFF_API_PREFIXES = \[([^\]]*)\]", _auth_server())
    assert m
    assert tuple("/" + p for p in re.findall(r'"([^"]+)"', m.group(1))) == STAFF_PATH_PREFIXES


def test_bff_cognito_session_sends_the_bearer_and_no_secret():
    src = _auth_server()
    body = re.search(r"export function staffCredentialHeaders\([\s\S]*?\n\}", src)
    assert body
    fn = body.group(0)
    bearer = re.search(r"if \(auth\.bearer && isStaffApiPath\(backendPath\)\) \{\s*return ([^;]*);", fn)
    assert bearer and "Admin-Secret" not in bearer.group(1) and "Authorization" in bearer.group(1)
    assert re.search(r'return \{ "X-Admin-Secret": adminSecret \};', fn)
    assert "staffBearerHeader" not in src


@pytest.mark.parametrize("bff", _BFFS)
def test_every_secret_bff_takes_its_credential_from_the_helper(bff):
    src = (REPO / bff).read_text()
    assert "staffCredentialHeaders(auth, ADMIN_SECRET," in src, bff
    assert not re.search(r'"[Xx]-[Aa]dmin-[Ss]ecret":\s*ADMIN_SECRET', src), bff
    assert "staffBearerHeader" not in src, bff
