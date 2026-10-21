"""AA-876 — static checks on the front-end side of the edge hardening.

- The admin app sends the same security headers as the portal on every response (and no x-powered-by).
- The admin and portal login BFFs turn the API's 429 into a 429 (not the generic 502), before the !res.ok branch.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
PORTAL = REPO / "portal"

SECURITY_HEADERS = [
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "X-Frame-Options",
    "Content-Security-Policy",
    "Permissions-Policy",
]


def test_admin_sets_security_headers():
    cfg = (FRONTEND / "next.config.ts").read_text()
    for header in SECURITY_HEADERS:
        assert f'key: "{header}"' in cfg, f"frontend/next.config.ts: missing {header}"
    assert "frame-ancestors 'none'" in cfg
    assert 'source: "/(.*)"' in cfg
    assert "poweredByHeader: false" in cfg
    # still wrapped by Sentry
    assert "withSentryConfig(nextConfig" in cfg


@pytest.mark.parametrize("route", [
    FRONTEND / "app" / "api" / "auth" / "login" / "route.ts",
    PORTAL / "app" / "api" / "auth" / "tenant-login" / "route.ts",
])
def test_login_bff_maps_429_before_the_502_branch(route):
    src = route.read_text()
    assert "res.status === 429" in src, f"{route}: no 429 branch"
    assert src.index("res.status === 429") < src.index("if (!res.ok)"), f"{route}: 429 must be checked before !res.ok"
    assert '"Retry-After"' in src
