"""AA-1168 follow-up (CIS 463) — the AI disclosure wording setting ``cis.content.ai_disclosure``
(services/acp_content_writing/ai_disclosure_setting.py): the default equals the seed, the shape check refuses a value
that would blank or break the disclosure, the reader serves the value in force and falls back to the seed default when
the setting cannot be read or holds a refused value, and the piece language picks the line. No DB; invented wording."""
from __future__ import annotations

import pytest

from services.acp_content_writing import ai_disclosure_setting as ads
from shared import settings

GOOD = {"text": {"en": "Written with help from a model.", "vi": "Viết với sự trợ giúp của mô hình."},
        "social_tag": "#MadeWithModel"}


def test_the_default_is_the_compiled_seed_and_fits_its_own_shape():
    d = settings.definition(ads.KEY)
    assert d.value_type == "json" and d.area == "content" and d.enforced and d.impact == "high"
    assert d.default_value == ads.DEFAULT
    assert ads.value_problem(ads.DEFAULT) is None
    assert ads.DEFAULT["text"]["en"] == "This content was created with the help of AI."
    assert ads.DEFAULT["social_tag"] == "#AIgenerated"


@pytest.mark.parametrize("value, needle", [
    ("This content was created with the help of AI.", "JSON object"),
    ({"text": {"en": "ok line"}, "social_tag": "#AIgenerated", "enabled": False}, "unknown field 'enabled'"),
    ({"text": {}, "social_tag": "#AIgenerated"}, "\"text\" must be an object"),
    ({"text": {"vi": "Một dòng."}, "social_tag": "#AIgenerated"}, "English line"),
    ({"text": {"en": "   "}, "social_tag": "#AIgenerated"}, "cannot be switched off"),
    ({"text": {"en": "ok line", "vi": ""}, "social_tag": "#AIgenerated"}, "the vi line must not be empty"),
    ({"text": {"en": "two\nlines"}, "social_tag": "#AIgenerated"}, "one line"),
    ({"text": {"en": "tab\tinside"}, "social_tag": "#AIgenerated"}, "one line"),
    ({"text": {"en": "line\u2028sep"}, "social_tag": "#AIgenerated"}, "one line"),
    ({"text": {"en": "=HYPERLINK(\"x\")"}, "social_tag": "#AIgenerated"}, "formula"),
    ({"text": {"en": " +1 made by AI"}, "social_tag": "#AIgenerated"}, "formula"),
    ({"text": {"en": "@AI wrote this"}, "social_tag": "#AIgenerated"}, "formula"),
    ({"text": {"en": "ok", "vi": "-AI"}, "social_tag": "#AIgenerated"}, "formula"),
    ({"text": {"en": "x" * 201}, "social_tag": "#AIgenerated"}, "longer than 200"),
    ({"text": {"en": "ok line", "EN-us": "ok"}, "social_tag": "#AIgenerated"}, "two lowercase letters"),
    ({"text": {"en": "ok line"}}, "\"social_tag\" must not be empty"),
    ({"text": {"en": "ok line"}, "social_tag": ""}, "\"social_tag\" must not be empty"),
    ({"text": {"en": "ok line"}, "social_tag": "AIgenerated"}, "one hashtag"),
    ({"text": {"en": "ok line"}, "social_tag": "#AI generated"}, "one hashtag"),
    ({"text": {"en": "ok line"}, "social_tag": "#A"}, "one hashtag"),
    ({"text": {"en": "ok line"}, "social_tag": "#" + "a" * 51}, "one hashtag"),
])
def test_value_problem_refuses_a_value_that_breaks_the_disclosure(value, needle):
    problem = ads.value_problem(value)
    assert problem and needle in problem


def test_value_problem_accepts_a_reworded_value_and_a_new_language():
    assert ads.value_problem(GOOD) is None
    assert ads.value_problem({"text": {"en": "x" * 200, "th": "Line"}, "social_tag": "#ai_made_2"}) is None


def test_config_serves_the_value_in_force(monkeypatch):
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: GOOD if key == ads.KEY else None)
    assert ads.config() == GOOD
    assert ads.text_for("vi") == GOOD["text"]["vi"]
    assert ads.text_for("en") == GOOD["text"]["en"]
    assert ads.social_tag() == "#MadeWithModel"


@pytest.mark.parametrize("stored", [
    {"text": {"en": ""}, "social_tag": "#AIgenerated"},
    {"text": {"vi": "Chỉ có tiếng Việt."}, "social_tag": "#AIgenerated"},
    ["not", "an", "object"],
    None,
])
def test_config_falls_back_to_the_seed_default_on_a_refused_value(monkeypatch, stored):
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: stored)
    assert ads.config() == ads.DEFAULT


def test_config_falls_back_when_the_setting_cannot_be_read(monkeypatch):
    def boom(key, tenant_id=None):
        raise KeyError(key)
    monkeypatch.setattr(settings, "get", boom)
    assert ads.config() == ads.DEFAULT
    assert ads.text_for(None) == ads.DEFAULT_TEXT_EN
    assert ads.social_tag() == ads.DEFAULT_SOCIAL_TAG


@pytest.mark.parametrize("lang, expected", [
    ("vi", "vi"), ("VI", "vi"), ("vi-VN", "vi"), ("vi_VN", "vi"), (" en ", "en"), (None, "en"), ("", "en"),
    ("123", "en"), ("deu", "en"),
])
def test_lang_of_normalizes_a_language_tag(lang, expected):
    assert ads.lang_of(lang) == expected


def test_a_language_without_a_line_gets_the_english_line(monkeypatch):
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: GOOD)
    assert ads.text_for("fr") == GOOD["text"]["en"]
    assert ads.text_for("vi", cfg=ads.DEFAULT) == ads.DEFAULT_TEXT_VI


def test_config_trims_the_lines(monkeypatch):
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: {"text": {"en": "  A line.  "},
                                                                      "social_tag": "#AIgenerated"})
    assert ads.text_for("en") == "A line."
