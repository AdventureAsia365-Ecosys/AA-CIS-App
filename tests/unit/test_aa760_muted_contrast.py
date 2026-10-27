"""AA-760 — the secondary text colours of the admin (frontend/) must read at WCAG AA
(≥ 4.5:1) on every surface they are used on as text, in both themes, so a later edit cannot slip back under 4.5:1.

History: light --aa-muted2 was #9099A6 (2.9:1 on card, 2.7:1 on the page background) in both apps — the portal was
fixed in S247, the admin in S248. Light --aa-muted was #6B7380 (4.43:1 on the page background) and light
--aa-neutral-fg2 #6B7280 (4.39:1 on the neutral pill) — both fixed in S248, together with the dark muted pair
(#8A93A2 was 4.29:1 on the neutral pill).

Surfaces: card, page background, the line2 tint (the "no state yet" badge, table header rows) and the neutral pill
background. --aa-line is not one: it is only a border / divider / progress-track colour, never behind text (S248 grep).
Token blocks in <app>/app/globals.css:
the first `[data-theme="light"] {` block holds the light palette, the first `[data-theme="dark"] {` block the dark
one.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# AA-979: frontend only. The portal moved to AA-Partner (its guard checks the portal tokens there); the copy left in
# portal/ is frozen, so an admin palette edit must not need a matching edit of it.
APPS = ["frontend"]
THEMES = ["light", "dark"]


def _block(app: str, theme: str) -> str:
    src = (ROOT / app / "app/globals.css").read_text()
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
    assert round(contrast("#6B7380", "#F8F6F2"), 2) == 4.43  # the old light muted on the page background


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("token", ["muted", "muted2"])
@pytest.mark.parametrize("surface", ["card", "bg", "line2", "neutral-bg"])
def test_muted_text_reads_at_aa_on_its_surfaces(app, theme, token, surface):
    block = _block(app, theme)
    fg, bg = _token(block, token), _token(block, surface)
    ratio = contrast(fg, bg)
    assert ratio >= 4.5, f"{app} {theme}: --aa-{token} {fg} on --aa-{surface} {bg} is {ratio:.2f}:1 (< 4.5:1)"


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("theme", THEMES)
def test_neutral_pill_text_reads_at_aa(app, theme):
    block = _block(app, theme)
    for fg_name in ("neutral-fg", "neutral-fg2"):
        fg, bg = _token(block, fg_name), _token(block, "neutral-bg")
        ratio = contrast(fg, bg)
        assert ratio >= 4.5, f"{app} {theme}: --aa-{fg_name} {fg} on --aa-neutral-bg {bg} is {ratio:.2f}:1"


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("theme", THEMES)
def test_muted2_stays_the_quieter_tone(app, theme):
    """muted2 is the tertiary tone: it must not end up with MORE contrast than muted on the card."""
    block = _block(app, theme)
    card = _token(block, "card")
    assert contrast(_token(block, "muted"), card) >= contrast(_token(block, "muted2"), card)


@pytest.mark.parametrize("app", APPS)
def test_brand_ts_matches_the_light_tokens(app):
    """_kit/core/brand.ts BRAND.muted/muted2 are used as raw hex on light-only pages (legal pages); keep them in step."""
    src = (ROOT / app / "app/_kit/core/brand.ts").read_text()
    light = _block(app, "light")
    for name in ("muted", "muted2"):
        m = re.search(rf'^\s*{name}:\s*"(#[0-9A-Fa-f]{{6}})"', src, re.M)
        assert m, f"BRAND.{name} not found"
        assert m.group(1).upper() == _token(light, name).upper(), (app, name)
