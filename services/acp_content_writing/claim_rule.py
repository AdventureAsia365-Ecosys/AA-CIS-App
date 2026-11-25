"""services.acp_content_writing.claim_rule — AA-1169: the switch of the blog writer's CLAIM CHECK block
(prompts._BLOG_CLAIM_RULE).

Why. Under the W5 measure a T9 blog's first draft fails when the pinned claim instrument flags any sentence; S285 passed
1 of 12. The first-pass diagnosis (services/writer_proof/first_pass.py) types the flagged sentences by policy topic;
on the local proxy (AA-1048 v7 t9_blog pool) rarity / ranking, route and time narration and the reader's-day lines
(pace, ease, what the reader sees or feels) were about 60 % of the expected flags. The repair prompt already forbids
adding a time of day, an ordinal or a pace word (generate._REPAIR_SYSTEM); the first draft had no such line.

The switch is the setting ``cis.t9.blog_claim_rule`` (CIS 490, Platform › Settings, bool, platform scope,
maker-checker, default off = the blog prompt of main byte for byte, S272 rule for writer behaviour changes). The paid
re-measure of the W5 T9 sample (prereg w5-t9-r2, about 1.5-3 USD under a 5 USD cap) runs with it on before an
administrator leaves it on. :func:`enabled` never raises: any read problem is the default (off). A caller that must pin
the mode (an A/B harness, a test) passes ``mode=True`` / ``False``.

Guard: tests/unit/test_aa1169_blog_claim_rule.py (off = main render, on = the block once, blog only).
"""
from __future__ import annotations

from typing import Optional

import structlog

from services.quality import settings_keys

logger = structlog.get_logger()

KEY = settings_keys.T9_BLOG_CLAIM_RULE   # "cis.t9.blog_claim_rule" (CIS 490)
DEFAULT = False          # the CIS 490 seed default; served whenever the setting cannot be read


def enabled(mode: Optional[bool] = None) -> bool:
    """Whether the blog writer prompt carries the CLAIM CHECK block: ``mode`` when the caller pins it, else the
    setting in force (the default on any read problem)."""
    if mode is not None:
        return bool(mode)
    try:
        from shared import settings
        return bool(settings.get(KEY))
    except Exception as exc:  # a read problem never breaks a write
        logger.warning("blog_claim_rule_setting_unreadable", error=str(exc)[:200])
        return DEFAULT


__all__ = ["DEFAULT", "KEY", "enabled"]
