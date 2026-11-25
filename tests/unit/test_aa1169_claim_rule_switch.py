"""AA-1169 — services/acp_content_writing/claim_rule.py: the switch of the blog writer's CLAIM CHECK block and its
setting cis.t9.blog_claim_rule (CIS 484). The prompt side is guarded by tests/unit/test_aa1169_blog_claim_rule.py."""
from unittest.mock import patch

from services.acp_content_writing import claim_rule
from services.quality import settings_keys
from shared import settings


def test_the_key_and_its_compiled_default():
    assert claim_rule.KEY == settings_keys.T9_BLOG_CLAIM_RULE == "cis.t9.blog_claim_rule"
    d = settings.definition(claim_rule.KEY)
    assert d.default_value is claim_rule.DEFAULT is False
    assert d.value_type == "bool" and d.scope_kind == "platform" and d.approval == "maker_checker"


def test_default_is_off_and_the_setting_turns_it_on():
    assert claim_rule.enabled() is False
    real_get = settings.get
    with patch.object(settings, "get", side_effect=lambda k, *a: True if k == claim_rule.KEY else real_get(k, *a)):
        assert claim_rule.enabled() is True


def test_a_pinned_mode_wins_and_a_read_problem_is_off():
    assert claim_rule.enabled(True) is True and claim_rule.enabled(False) is False
    with patch.object(settings, "get", side_effect=RuntimeError("no settings")):
        assert claim_rule.enabled() is False
