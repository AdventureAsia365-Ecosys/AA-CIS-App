"""AA-760 — the portal's tertiary text colour (--aa-muted2) must read at WCAG AA (≥ 4.5:1) on every surface it is
used on as text: card, page background, and the line2 tint (the "no state yet" badge on the Activity page). The
light value was #9099A6 (2.9:1 on card, 2.7:1 on the page background). Dark is checked too, so a later edit to
either theme cannot slip back under 4.5:1.

Token blocks in portal/app/globals.css: the first `[data-theme="light"] {` block holds the light palette, the
first `[data-theme="dark"] {` block the dark one.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parents[2] / "portal/app/globals.css"


def _block(theme: str) -> str:
    src = CSS.read_text()
    start = src.index(f'[data-theme="{theme}"] {{')
    return src[start:src.index("\n}", start)]


def _token(block: str, name: str) -> str:
    m = re.search(rf"--aa-{re.escape(name)}:\s*(#[0-9A-Fa-f]{{6}})\b", block)
    assert m, f"--aa-{name} not found as a #RRGGBB literal"
    return m.group(1)


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_helper_matches_known_values():
    assert round(contrast("#000000", "#FFFFFF"), 1) == 21.0
    assert round(contrast("#9099A6", "#FFFFFF"), 2) == 2.88  # the old light muted2 the audit flagged


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("surface", ["card", "bg", "line2"])
def test_muted2_reads_at_aa_on_its_surfaces(theme, surface):
    block = _block(theme)
    fg, bg = _token(block, "muted2"), _token(block, surface)
    ratio = contrast(fg, bg)
    assert ratio >= 4.5, f"{theme}: --aa-muted2 {fg} on --aa-{surface} {bg} is {ratio:.2f}:1 (< 4.5:1)"
