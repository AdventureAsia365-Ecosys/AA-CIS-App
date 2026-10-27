"""AA-977 (S3) — the admin editor (frontend/lib/aaContent.ts) mirrors the backend rules it warns about.

The editor shows the field rules before a save (countries, kinds, locales, limits); the backend
(services/aa_content/validate.py) stays the authority. These checks keep the two lists from drifting, and pin the page
to its RBAC rule and nav entry.
"""
from __future__ import annotations

import re
from pathlib import Path

from services.aa_content import store, validate

REPO = Path(__file__).resolve().parents[2]
TS = (REPO / "frontend" / "lib" / "aaContent.ts").read_text()


def _ts_array(name: str) -> list[str]:
    m = re.search(rf"export const {name}[^=]*=\s*\[([^\]]*)\]", TS)
    assert m, name
    return re.findall(r'"([a-z_]+)"', m.group(1))


def test_vocabulary_matches_the_backend():
    assert tuple(_ts_array("KINDS")) == validate.KINDS
    assert tuple(_ts_array("LOCALES")) == validate.LOCALES
    assert tuple(_ts_array("STATUSES")) == store.STATUSES


def test_country_quick_picks_are_iso_countries():
    """AA-990: the backend takes any ISO 3166-1 code; each quick pick is one, under its shared.countries name."""
    from shared import countries
    block = re.search(r"export const AA_COUNTRIES[^=]*=\s*\{(.*?)\};", TS, re.S)
    assert block
    picks = dict(re.findall(r'\b([A-Z]{2}): "([^"]+)"', block.group(1)))
    assert len(picks) >= 20
    assert set(picks) <= validate.COUNTRY_CODES
    assert {code: countries.by_iso2(code).name for code in picks} == picks


def test_limits_match():
    for name, value in (("TITLE_MAX", validate.TITLE_MAX), ("BODY_MAX", validate.BODY_MAX),
                        ("SOURCE_ID_MAX", validate.SOURCE_ID_MAX)):
        assert re.search(rf"export const {name} = {value};", TS), name
    assert f"export const GROUP_KEY_RE = /{validate.GROUP_KEY_RE.pattern}/;" in TS


def test_page_rule_nav_and_role_gated_controls():
    rbac = (REPO / "frontend" / "lib" / "rbac.ts").read_text()
    assert '{ prefix: "/admin/aa-content", roles: ALL_STAFF }' in rbac       # the viewer reads
    nav = (REPO / "frontend" / "app" / "_kit" / "adminNav.tsx").read_text()
    assert 'href: "/admin/aa-content"' in nav
    editor = (REPO / "frontend" / "app" / "admin" / "aa-content" / "AaContentEditor.tsx").read_text()
    # drafts are content.write, publish / retire / restore master.admin (api/core/staff_authz.py POLICY)
    assert 'can(role, "content.write")' in editor and 'can(role, "master.admin")' in editor
