"""The staff console (frontend/) and the tenant portal (portal/) are separate apps on separate hosts. Their
login pages must not link to each other (Nghiệp, S245): staff never sign in on the portal, tenants never on the
staff console. Old bookmarks are handled by the 308 redirects in frontend/next.config.ts, not by links."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAFF_LOGIN = [ROOT / "frontend/app/login/LoginForm.tsx", ROOT / "frontend/app/login/page.tsx"]
TENANT_LOGIN = [ROOT / "portal/app/tenant-login/page.tsx"]


_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _text(paths):
    """The code of the files without comments (a comment may name the other app; a link may not)."""
    return "\n".join(_COMMENTS.sub("", p.read_text(encoding="utf-8")) for p in paths if p.exists())


def test_staff_login_has_no_link_to_the_tenant_portal():
    text = _text(STAFF_LOGIN)
    assert text, "staff login files not found"
    for needle in ("tenant-login", "PORTAL_BASE_URL", "portal.lumiguides", "Tenant login"):
        assert needle not in text, needle


def test_tenant_login_has_no_link_to_the_staff_console():
    text = _text(TENANT_LOGIN)
    assert text, "tenant login page not found"
    for needle in ("STAFF_LOGIN_URL", "ADMIN_BASE_URL", "aa-cis.lumiguides", "Staff login", 'href="/login"'):
        assert needle not in text, needle
