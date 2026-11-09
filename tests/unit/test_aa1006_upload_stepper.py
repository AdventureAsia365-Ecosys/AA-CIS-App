"""AA-1006 follow-up (S264 UI check) — /admin/upload at 390 px was wider than the phone (main 675 px in a 390
viewport) because the five-step stepper was one nowrap row of labelled pills.

The CI-run copy of the source guard in ``frontend/lib/uploadStepper.test.mjs`` (``npm run test:unit``): the step row
wraps inside its container, and on a phone only the current step shows its label (the others keep it as their
accessible name and tooltip), with compact pills and connectors so the five steps fit one 358 px line.
"""
from __future__ import annotations

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "frontend" / "app" / "admin" / "upload" / "page.tsx"


def _code() -> str:
    """Page source without comments, so a comment that quotes a style never satisfies a check."""
    src = PAGE.read_text()
    src = re.sub(r"\{/\*.*?\*/\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def _stepper() -> str:
    src = _code()
    start = src.find("function StepIndicator(")
    end = src.find("\nfunction ", start + 1)
    assert 0 < start < end, "StepIndicator not found (renamed? update this guard)"
    return src[start:end]


def test_step_row_wraps_inside_its_container():
    src = _stepper()
    row = src[src.index('data-testid="upload-stepper"') : src.index("STEPS.map(")]
    assert 'flexWrap: "wrap"' in row, "the step row must wrap instead of running off the right edge"
    assert "minWidth: 0" in row
    assert 'maxWidth: "100%"' in row


def test_phone_shows_only_the_current_label_and_keeps_the_others_accessible():
    src = _stepper()
    assert "const isPhone = useIsPhone();" in src
    assert "const showLabel = active || !isPhone;" in src
    assert "{showLabel && (" in src
    assert "aria-label={showLabel ? undefined : s.label}" in src
    assert "title={showLabel ? undefined : s.label}" in src
    assert 'aria-current={active ? "step" : undefined}' in src


def test_phone_pills_and_connectors_are_compact():
    src = _stepper()
    assert 'padding: isPhone ? "7px 8px" : "7px 14px"' in src
    assert "width: isPhone ? 8 : 20" in src


def test_page_renders_the_stepper_once():
    assert _code().count("<StepIndicator step={step} />") == 1
