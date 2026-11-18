"""AA-876 — static checks on the front-end side of the edge hardening.

- The admin app sends the security headers on every response (and no x-powered-by). AA-1111 (S284): the list lives in
  frontend/lib/securityHeaders.ts (+ COOP), the full CSP goes out with a per-request nonce from proxy.ts on every path
  the proxy runs on and without a nonce from next.config.ts on the paths it skips (/api, /_next).
- The admin login BFF turns the API's 429 into a 429 (not the generic 502), before the !res.ok branch.
  (The portal's own checks moved to AA-Partner with the portal, AA-979.)
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"

SECURITY_HEADERS = [
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "X-Frame-Options",
    "Permissions-Policy",
    "Cross-Origin-Opener-Policy",
]


def test_admin_sets_security_headers():
    lib = (FRONTEND / "lib" / "securityHeaders.ts").read_text()
    for header in SECURITY_HEADERS:
        assert f'key: "{header}"' in lib, f"frontend/lib/securityHeaders.ts: missing {header}"
    assert 'HSTS_VALUE = "max-age=63072000; includeSubDomains";' in lib and "preload" not in lib
    directives = ("default-src", "script-src", "style-src", "img-src", "connect-src", "frame-ancestors", "object-src")
    for directive in directives:
        assert f'["{directive}"' in lib, f"CSP without {directive}"
    assert "'strict-dynamic'" in lib and "'nonce-${opts.nonce}'" in lib
    cfg = (FRONTEND / "next.config.ts").read_text()
    assert 'source: "/(.*)", headers: STATIC_SECURITY_HEADERS' in cfg
    assert 'source: "/api/:path*", headers: csp' in cfg and 'source: "/_next/:path*", headers: csp' in cfg
    assert "poweredByHeader: false" in cfg
    # still wrapped by Sentry
    assert "withSentryConfig(nextConfig" in cfg


def test_admin_proxy_sends_the_nonce_csp_on_every_path():
    gate = (FRONTEND / "proxy.ts").read_text()
    assert 'response.headers.set("Content-Security-Policy", csp)' in gate
    assert 'requestHeaders.set("Content-Security-Policy", csp)' in gate, "Next reads the nonce from the request CSP"
    assert "requestHeaders.set(NONCE_HEADER, nonce)" in gate
    matcher = gate[gate.index("export const config"):]
    assert 'matcher: ["/((?!_next/static|_next/image|api/).*)"]' in matcher
    # The staff gate still runs only where the old matcher ran ("/admin/:path*", "/login").
    assert 'pathname === "/login" || pathname === "/admin" || pathname.startsWith("/admin/")' in gate
    assert "return NextResponse.next();" not in gate, "every pass must carry the request headers with the nonce"


def test_root_layout_puts_the_nonce_on_every_inline_script():
    layout = (FRONTEND / "app" / "layout.tsx").read_text()
    assert "(await headers()).get(NONCE_HEADER)" in layout
    assert layout.count("<script") == layout.count("<script nonce={nonce}"), "an inline script without the nonce"


@pytest.mark.parametrize("route", [
    FRONTEND / "app" / "api" / "auth" / "login" / "route.ts",
])
def test_login_bff_maps_429_before_the_502_branch(route):
    src = route.read_text()
    assert "res.status === 429" in src, f"{route}: no 429 branch"
    assert src.index("res.status === 429") < src.index("if (!res.ok)"), f"{route}: 429 must be checked before !res.ok"
    assert '"Retry-After"' in src
