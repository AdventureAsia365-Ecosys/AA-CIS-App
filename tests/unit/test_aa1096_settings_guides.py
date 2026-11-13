"""AA-1096 l23 (CIS 416) — settings-guide lint: every setting on Platform › Settings explains itself to an operator.

Tech lead S279: "the thresholds on the Settings page are hard to understand; each needs an explanation or definition so I
know when to change it". Every compiled definition (``shared/settings/compiled_defaults.json``, generated from the
migration seed and guide blocks) must carry a complete guide in English and Vietnamese:

    guide = {"en": {"what", "raise", "lower", "when", "example"}, "vi": {...the same five parts}}

``what``: what the setting controls and where the effect shows in the product; ``raise`` / ``lower``: the effect and
risk of each direction (an on/off setting: turn on / off; a choice: the stricter / the looser choice); ``when``: the
signal that tells the operator to change it; ``example``: the default value in plain terms.

Rules (CONTEXT.md "Settings registry: every key ships its guide"):
  1. every key has a complete guide (both languages, five non-blank parts of 10-800 characters, the Vietnamese parts
     translated, not copies), unless it is listed in the shrink-only baseline ``snapshots/aa1096_settings_guide_baseline
     .json`` (empty on 13/11/2026; a new entry needs the tech lead's OK in review, and the entry exempts its key from
     rules 3 and 4 too, so the baseline is a real escape hatch);
  2. a baseline entry names an existing key that still lacks a guide (the list only shrinks);
  3. no issue id (AA-123) and no internal stage code (T7, A1, F13, S279 …) in a label or a guide: the page is read by
     operators (the key, shown small, keeps the code);
  4. the English example names the default value (a number, on / off, or the choice) and says it is the default
     ("With the default of ..."), never "Today ...": the value in force can change, the guide is migration data
     (review of the first pass: "Today 5.8" under "In force now: 3.364"). The page computes the value in force where
     it matters (lib/settingsRegistry nowExample).

Fix: add the guide to the migration's ``settings-guide`` block (or the seed row's ``guide``), then run
``python3 scripts/settings/gen_compiled_defaults.py``. No DB."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from shared import settings

BASELINE = Path(__file__).resolve().parent / "snapshots" / "aa1096_settings_guide_baseline.json"
LANGS = ("en", "vi")
PARTS = ("what", "raise", "lower", "when", "example")
ISSUE_ID = re.compile(r"\bAA-\d+")
STAGE_CODE = re.compile(r"\b(?!S3\b)[ATSF]\d{1,3}\b")   # Amazon S3 is a product name, not a stage
NUMERIC = ("int", "numeric", "duration_s", "usd", "pct")


def _definitions() -> list[dict]:
    return settings.load_definitions()


def _baseline() -> list[str]:
    return list(json.loads(BASELINE.read_text(encoding="utf-8"))["keys_without_guide"])


def guide_problems(guide: object) -> list[str]:
    """Why ``guide`` is not complete ([] = complete). Pure."""
    if not isinstance(guide, dict):
        return ["no guide"]
    out = []
    for lang in LANGS:
        part_map = guide.get(lang)
        if not isinstance(part_map, dict):
            out.append(f"no {lang} guide")
            continue
        for part in PARTS:
            text = part_map.get(part)
            if not isinstance(text, str) or not 10 <= len(text.strip()) <= 800:
                out.append(f"{lang}.{part} missing or not 10-800 characters")
    if not out:
        same = [p for p in PARTS if guide["en"][p].strip() == guide["vi"][p].strip()]
        out.extend(f"vi.{p} is a copy of en.{p}" for p in same)
    return out


def default_words(d: dict) -> list[str]:
    """The ways the English example may name the default (any one must appear). [] = not checked (a JSON table)."""
    v, t = d["default_value"], d["value_type"]
    if t == "bool":
        return ["on"] if v else ["off"]
    if t == "enum":
        return [str(v)]
    if t in NUMERIC:
        n = float(v)
        plain = str(int(n)) if n.is_integer() else repr(n)
        return sorted({plain, f"{n:,.0f}" if n.is_integer() else plain, f"{n:.2f}"})
    return []


def test_every_setting_has_a_complete_guide_in_both_languages():
    allowed = set(_baseline())
    missing = {d["key"]: guide_problems(d.get("guide")) for d in _definitions() if d["key"] not in allowed}
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, (
        "settings without a complete EN + VI guide (add it to the migration's settings-guide block, then run "
        f"python3 scripts/settings/gen_compiled_defaults.py): {json.dumps(missing, indent=1)}")


def test_the_baseline_only_shrinks():
    by_key = {d["key"]: d for d in _definitions()}
    for key in _baseline():
        assert key in by_key, f"{key} is in the guide baseline but is no setting: remove it from {BASELINE.name}"
        assert guide_problems(by_key[key].get("guide")), f"{key} has its guide now: remove it from {BASELINE.name}"


def test_labels_and_guides_carry_no_issue_ids_or_stage_codes():
    allowed = set(_baseline())
    for d in _definitions():
        if d["key"] in allowed:
            continue
        texts = [d["label"]] + [t for lang in (d.get("guide") or {}).values() for t in lang.values()]
        for text in texts:
            assert not ISSUE_ID.search(text), f"{d['key']}: an issue id in operator text: {text!r}"
            assert not STAGE_CODE.search(text), f"{d['key']}: an internal stage code in operator text: {text!r}"


TODAY = re.compile(r"^\s*(Today|Hiện là|Hiện đang)\b")


def test_the_example_names_the_default_and_never_says_today():
    allowed = set(_baseline())
    for d in _definitions():
        guide = d.get("guide") or {}
        if d["key"] in allowed or not guide:
            continue
        for lang in LANGS:
            example = (guide.get(lang) or {}).get("example") or ""
            assert not TODAY.search(example), f"{d['key']}: the {lang} example says 'today': {example!r}"
        words = default_words(d)
        example = (guide.get("en") or {}).get("example")
        if not words or not example:
            continue
        assert any(w in example for w in words), f"{d['key']}: the example does not name the default {words}"


@pytest.mark.parametrize("guide,expected", [
    (None, ["no guide"]),
    ({"en": {p: "long enough text" for p in PARTS}}, ["no vi guide"]),
    ({"en": {p: "long enough text" for p in PARTS}, "vi": {p: "đủ dài để đọc" for p in PARTS}}, []),
    ({"en": {p: "long enough text" for p in PARTS}, "vi": {**{p: "đủ dài để đọc" for p in PARTS}, "when": " "}},
     ["vi.when missing or not 10-800 characters"]),
    ({"en": {p: "long enough text" for p in PARTS}, "vi": {**{p: "đủ dài để đọc" for p in PARTS},
                                                           "example": "long enough text"}},
     ["vi.example is a copy of en.example"]),
])
def test_guide_problems(guide, expected):
    assert guide_problems(guide) == expected


@pytest.mark.parametrize("d,words", [
    ({"value_type": "bool", "default_value": False}, ["off"]),
    ({"value_type": "enum", "default_value": "soft"}, ["soft"]),
    ({"value_type": "int", "default_value": 2000}, ["2,000", "2000", "2000.00"]),
    ({"value_type": "usd", "default_value": 0.25}, ["0.25"]),
    ({"value_type": "json", "default_value": {"a": 1}}, []),
])
def test_default_words(d, words):
    assert default_words(d) == sorted(set(words))
