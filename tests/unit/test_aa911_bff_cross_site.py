"""AA-911 — every BFF write route refuses a cross-site request before it reads a cookie or a body.

`it.com` is on the Public Suffix List, so `lumiguides.it.com` is the site: every `*.lumiguides.it.com` host (the
other apps, and the fixed Preview hosts `booking-preview` / `aa-cis-preview` that serve unmerged PR code) is the
same site as the admin and portal hosts, and its requests carry their SameSite=Lax session cookies. A `text/plain`
POST needs no CORS preflight and the proxies forward it as JSON with the victim's credential, so each write route
must call `refuseCrossSiteWrite(req)` (lib/crossSiteGuard.ts → lib/sameOrigin.ts isCrossSiteWrite) first.
The behaviour itself is checked live by tests/e2e/smoke/aa911-bff-cross-site.spec.ts.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APPS = [REPO / "frontend" / "app" / "api", REPO / "portal" / "app" / "api"]

WRITE_EXPORT = re.compile(r"export\s+(?:const\s+|(?:async\s+)?function\s+)(POST|PUT|PATCH|DELETE)\b")
# a re-export (`export { handler as POST }`) would hide the handler from this scan: refused outright
REEXPORT = re.compile(r"export\s*\{[^}]*\bas\s+(POST|PUT|PATCH|DELETE)\b")
GUARD_CALL = "refuseCrossSiteWrite(req)"
GUARD_IMPORT = 'import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";'
# The first thing a route may do that the guard must come before.
FIRST_WORK = re.compile(r"requireAdmin\(|requireTenant\(|\.cookies\.get\(|req\.(?:text|json|arrayBuffer|formData)\(|handleRefreshKeepAlive\(")

# Write routes that are deliberately not on this guard, with the reason.
EXEMPT = {
    # Own strict check (isSameOrigin(request, { strict: true })) since AA-816.
    "frontend/app/api/auth/admin-logout/route.ts": "isSameOrigin(request, { strict: true })",
    # Sign-in routes read no session cookie, so a cross-site POST cannot act as a signed-in user; login CSRF
    # (signing the victim into the attacker's account) is accepted for the dev phase (AA-911 notes).
    "frontend/app/api/auth/login/route.ts": None,
    "portal/app/api/auth/tenant-login/route.ts": None,
}


def _write_routes() -> list[Path]:
    routes = []
    for root in APPS:
        for p in sorted(root.rglob("route.ts")):
            if WRITE_EXPORT.search(p.read_text()):
                routes.append(p)
    return routes


WRITE_ROUTES = _write_routes()


def test_write_routes_found():
    rels = {str(p.relative_to(REPO)) for p in WRITE_ROUTES}
    # The secret-bearing proxies the AA-911 review named must be in the scan.
    for must in (
        "frontend/app/api/admin/[...path]/route.ts",
        "frontend/app/api/pipeline/[...path]/route.ts",
        "frontend/app/api/tenant/[...path]/route.ts",
        "frontend/app/api/tenant/pipeline/brand-identity/route.ts",
        "portal/app/api/tenant/[...path]/route.ts",
        "portal/app/api/playground/proxy/route.ts",
    ):
        assert must in rels, f"{must} not found as a write route"


def _strip_comments(src: str) -> str:
    src = re.sub(r"(?m)^\s*//.*$", "", src)  # whole-line comments first: they may hold "/admin/*"
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def _function_body(src: str, header: re.Match) -> str:
    """The text of a top-level function from its header to the next top-level declaration."""
    rest = src[header.end():]
    nxt = re.search(r"\n(?:export\s|async\s+function\s|function\s|const\s)", rest)
    return rest[: nxt.start()] if nxt else rest


def _write_handlers(src: str) -> list[tuple[str, str]]:
    """(name, body) of every function that serves POST/PUT/PATCH/DELETE in a route file."""
    handlers = []
    assert not REEXPORT.search(src), "write handler exported through `export { … as … }`: export it directly"
    for m in re.finditer(r"export\s+(?:async\s+)?function\s+(POST|PUT|PATCH|DELETE)\s*\(", src):
        handlers.append((m.group(1), _function_body(src, m)))
    aliased = {m.group(2) for m in re.finditer(r"export\s+const\s+(POST|PUT|PATCH|DELETE)\s*=\s*(\w+)\s*;", src)}
    for name in sorted(aliased):
        m = re.search(rf"async\s+function\s+{name}\s*\(", src)
        assert m, f"handler {name} not found"
        handlers.append((name, _function_body(src, m)))
    return handlers


@pytest.mark.parametrize("route", WRITE_ROUTES, ids=lambda p: str(p.relative_to(REPO)))
def test_every_write_handler_refuses_cross_site_first(route: Path):
    rel = str(route.relative_to(REPO))
    src = _strip_comments(route.read_text())
    if rel in EXEMPT:
        needle = EXEMPT[rel]
        if needle:
            assert needle in src, f"{rel}: exempt because of {needle!r}, which is gone"
        return
    assert GUARD_IMPORT in src, f"{rel}: write route without {GUARD_IMPORT}"
    handlers = _write_handlers(src)
    assert handlers, f"{rel}: no write handler found — update _write_handlers"
    for name, body in handlers:
        assert GUARD_CALL in body, f"{rel}: {name} does not call {GUARD_CALL}"
        first_work = FIRST_WORK.search(body)
        assert first_work, f"{rel}: {name}: no auth/body read found — update FIRST_WORK"
        assert body.index(GUARD_CALL) < first_work.start(), (
            f"{rel}: {name}: {GUARD_CALL} must run before {first_work.group(0)!r} "
            "(refuse before any cookie or body is read)"
        )


def test_guard_rule():
    src = (REPO / "frontend" / "lib" / "sameOrigin.ts").read_text()
    assert 'const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);' in src
    assert "return !isSameOrigin(req);" in src
    guard = (REPO / "frontend" / "lib" / "crossSiteGuard.ts").read_text()
    assert "status: 403" in guard and "isCrossSiteWrite(req)" in guard
