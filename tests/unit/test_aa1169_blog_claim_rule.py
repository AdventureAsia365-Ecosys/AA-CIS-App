"""AA-1169 — the blog writer's CLAIM CHECK block (prompts._BLOG_CLAIM_RULE) behind the setting cis.t9.blog_claim_rule
(services/acp_content_writing/claim_rule.py, CIS 490).

Parity (root ADR 0020 change control, the t9_write v10 bump): with the switch off (the seed default) every T9 render is
the render of main byte for byte; the sha256 of the main renders are pinned by tests/unit/
test_aa1167_prompt_fence_parity.py on the same invented fixtures, and are checked here again with claim_rule=False.
On: a blog prompt carries the block once, after the length block and before the JSON envelope; no other channel
changes. The first draft and the rewrite read the setting. Invented text only."""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import get_goal
from services.acp_content_writing import claim_rule
from services.acp_content_writing import prompts as t9
from shared import settings
from tests.unit import test_aa1167_prompt_fence_guard as g
from tests.unit import test_aa1167_prompt_fence_parity as parity


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _renders(claim: bool) -> list[str]:
    real = t9.build_user_prompt
    with patch.object(t9, "build_user_prompt", side_effect=lambda **kw: real(**kw, claim_rule=claim)):
        return g._t9_prompts(False)


def test_off_render_is_main():
    assert [_h(p) for p in _renders(False)] == parity.MAIN_T9
    assert _h(t9.SYSTEM_PROMPT) == parity.MAIN_T9_SYSTEM


def test_on_adds_the_block_to_the_blog_prompts_only():
    off, on = _renders(False), _renders(True)
    for i in (0, 1):                                                    # blog: flat seed, route moments
        assert on[i].count(t9._BLOG_CLAIM_RULE) == 1
        assert on[i].replace(t9._BLOG_CLAIM_RULE, "", 1) == off[i]
        at = on[i].index(t9._BLOG_CLAIM_RULE)
        assert on[i].index("LENGTH AND EFFORT FOR THIS BLOG") < at < on[i].index("OUTPUT FORMAT FOR THIS CHANNEL ONLY")
    assert on[2] == off[2]                                              # facebook: unchanged
    assert "CLAIM CHECK" not in off[0] + off[1] + off[2]


def test_the_block_keeps_the_background_rule_and_names_no_number():
    """The block narrows claims; it never contradicts SOURCE FIDELITY's place background or adds a length number."""
    text = t9._BLOG_CLAIM_RULE
    assert "place background" in text and not any(ch.isdigit() for ch in text)
    for must in ("by evening", "one of the most", "who it suits", "seed's own nouns"):
        assert must in text


def _switch(on):
    real_get = settings.get
    return patch.object(settings, "get", side_effect=lambda k, *a: on if k == claim_rule.KEY else real_get(k, *a))


@pytest.mark.parametrize("on", [False, True])
def test_write_and_rewrite_follow_the_setting(on):
    from services.acp_content_writing import generate
    sent = []

    def fake_full(request, channel, **kw):
        sent.append(request)
        return MagicMock(content='{"body": "## Corran\\n\\nCorran market sells mats.", "seo_title": "t", '
                                 '"meta_description": "m.", "slug": "s"}'), 0.0

    kw = dict(content_seed="Corran reed market. Traders sell woven mats.", goal=get_goal("promotion"),
              channel_style=get_channel_style("blog"), brand_audience=g.AUDIENCE, angle=g.ANGLE, cta="Ask us",
              atom_id="a1")
    with _switch(on), patch.object(generate, "_generate_full", side_effect=fake_full):
        generate.write_content(**kw)
        generate.rewrite_with_feedback(revision_feedback=["shorter"], **kw)
    assert len(sent) == 2
    for request in sent:
        assert ("CLAIM CHECK FOR THIS BLOG" in request.user_prompt) is on


def test_an_unreadable_setting_is_off():
    with patch.object(settings, "get", side_effect=RuntimeError("no settings")):
        assert claim_rule.enabled() is False
    assert claim_rule.enabled(True) is True and claim_rule.enabled(False) is False


def test_the_prompt_fingerprint_counts_the_switch():
    from services.quality import quality_settings
    assert quality_settings.prompt_fp() == ""
    with _switch(True):
        assert quality_settings.prompt_fp() != ""
