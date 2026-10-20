"""AA-886 — the tenant portal's legal pages are draft pages served by the portal itself.

- /privacy and /terms are portal pages (no redirect to adventure.asia any more).
- Both use the legal shell, which always renders the "Draft — pending legal review" banner.
- Both are kept out of search engines while they are drafts (robots index: false).
- The portal gate (proxy.ts) does not cover them, so they stay public.
- The sign-in form and the portal sidebar link both pages.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PORTAL = REPO / "portal"
PAGES = ["portal/app/privacy/page.tsx", "portal/app/terms/page.tsx"]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("page", PAGES)
def test_legal_page_uses_the_draft_shell_and_is_noindex(page: str) -> None:
    src = _read(page)
    assert "<LegalPage" in src, f"{page}: must render the LegalPage shell (it carries the draft banner)"
    assert re.search(r"robots:\s*\{\s*index:\s*false", src), f"{page}: a draft legal page must be noindex"


def test_legal_shell_always_renders_the_draft_banner() -> None:
    src = _read("portal/app/_legal/LegalPage.tsx")
    assert 'LEGAL_DRAFT_LABEL = "Draft — pending legal review"' in src
    assert "<DraftBanner />" in src


def test_privacy_is_no_longer_redirected() -> None:
    cfg = _read("portal/next.config.ts")
    assert not re.search(r"source:\s*\"/privacy\"", cfg), "next.config.ts must not redirect /privacy (AA-886)"
    assert not re.search(r"source:\s*\"/terms\"", cfg), "next.config.ts must not redirect /terms"


def test_legal_pages_stay_outside_the_portal_gate() -> None:
    matcher = re.search(r"matcher:\s*\[([^\]]*)\]", _read("portal/proxy.ts"))
    assert matcher, "proxy.ts: matcher not found"
    for path in ("/privacy", "/terms"):
        assert path not in matcher.group(1), f"proxy.ts must not gate {path}"


@pytest.mark.parametrize(
    "rel", ["portal/app/tenant-login/page.tsx", "portal/app/portal/_components/Sidebar.tsx"]
)
def test_sign_in_form_and_sidebar_link_both_pages(rel: str) -> None:
    src = _read(rel)
    assert "href={PRIVACY_PATH}" in src and "href={TERMS_PATH}" in src, f"{rel}: legal links missing"
