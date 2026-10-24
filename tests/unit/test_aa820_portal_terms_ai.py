"""AA-820 — the portal /terms §3 carries the AI acceptable-use summary (still a draft page).

- Section id="ai" links the Anthropic Usage Policy and the AWS Acceptable Use Policy.
- It names the prohibited uses of the AI Schedule (fake reviews and testimonials, astroturfing, ...).
- It no longer says the AI terms are "[to be confirmed]" (other sections keep their open points).
- The legal shell states Draft v0.2 (shared by /privacy and /terms).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _ai_section() -> str:
    src = _read("portal/app/terms/page.tsx")
    m = re.search(r'<LegalSection id="ai".*?</LegalSection>', src, re.S)
    assert m, "terms/page.tsx: section id=\"ai\" not found"
    return m.group(0)


def test_ai_section_links_the_model_provider_policies() -> None:
    ai = _ai_section()
    assert 'href="https://www.anthropic.com/legal/aup"' in ai
    assert 'href="https://aws.amazon.com/aup/"' in ai


def test_ai_section_lists_the_prohibited_uses() -> None:
    ai = re.sub(r"\s+", " ", _ai_section())
    for phrase in (
        "fake reviews, ratings or testimonials",
        "sockpuppet",
        "astroturfing",
        "sponsorship",
        "search engines or AI systems",
        "spam",
        "visas and entry rules",
        "qualified person reviews it",
        "content checks, limits or other safeguards",
        "labelled as AI-generated",
        "you are its publisher",
    ):
        assert phrase in ai, f"/terms §3 must mention: {phrase}"


def test_ai_section_keeps_the_portal_ai_notice_and_has_no_open_placeholder() -> None:
    ai = _ai_section()
    assert 'title="3. AI-generated content and acceptable use"' in ai
    assert "This content was generated with AI." in ai
    assert "[to be confirmed]" not in ai


def test_legal_shell_states_draft_v0_2() -> None:
    src = _read("portal/app/_legal/LegalPage.tsx")
    assert 'LEGAL_VERSION = "Draft v0.2"' in src
    assert 'LEGAL_UPDATED = "24 October 2026"' in src
