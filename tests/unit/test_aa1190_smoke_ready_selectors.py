"""AA-1190 review fix: every UI smoke ``readySelector`` names a test id the frontend still renders.

The smoke run waits for a page's ``readySelector`` before it calls the page loaded; a test id that a refactor removed
makes that page fail every signed-in smoke run, and CI on the viewer-capped account never sees it when the viewer is
refused before the check (the My work page lost ``my-work-content`` that way). This test reads
tests/e2e/smoke/ui-smoke.spec.ts and looks for each ``[data-testid="..."]`` id as a quoted literal in frontend/**/*.tsx.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "tests" / "e2e" / "smoke" / "ui-smoke.spec.ts"
SELECTOR = re.compile(r"""[rR]eadySelector:\s*'([^']+)'""")
TEST_ID = re.compile(r'^\[data-testid="([^"]+)"\]$')


def _frontend_source() -> str:
    skip = {"node_modules", ".next"}
    files = [p for p in (REPO / "frontend").rglob("*.tsx") if not skip & set(p.parts)]
    return "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in files)


def test_every_smoke_ready_selector_is_a_rendered_test_id():
    selectors = SELECTOR.findall(SPEC.read_text(encoding="utf-8"))
    assert selectors, "no readySelector found in ui-smoke.spec.ts"
    source = _frontend_source()
    missing = []
    for selector in selectors:
        match = TEST_ID.match(selector)
        assert match, f"readySelector {selector!r} is not a [data-testid=\"...\"] selector (extend this test)"
        test_id = match.group(1)
        if not re.search(r"[\"'`]" + re.escape(test_id) + r"[\"'`]", source):
            missing.append(test_id)
    assert not missing, f"smoke readySelector test ids no page renders: {missing}"


def test_my_work_waits_for_its_loaded_summary():
    spec = SPEC.read_text(encoding="utf-8")
    assert "path: '/admin/my-work', readySelector: '[data-testid=\"my-work-total\"]'" in spec
