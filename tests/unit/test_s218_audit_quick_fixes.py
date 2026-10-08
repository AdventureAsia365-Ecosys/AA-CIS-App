"""S218 audit quick fixes: atom action strip, brand-audit status from the judge path."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.acp_produce import tenant_pipeline as tp
from services.content_generation.brand_audit_node import _audit_from_judge


def test_strip_atom_action_substitutes_brand_words():
    from services.content_generation.forbidden_words import all_forbidden
    words = all_forbidden(["explore", "vibrant"])
    assert tp._strip_atom_action("explore the old town", words) == "visit the old town"
    assert tp._strip_atom_action("walk the vibrant market", words) == "walk the lively market"
    assert tp._strip_atom_action("", words) == ""


def _pool_returning(value):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=value)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


def test_atom_forbidden_words_platform_uses_master_brand():
    pool, conn = _pool_returning('["explore"]')
    words = asyncio.run(tp._atom_forbidden_words(pool, "platform"))
    assert "explore" in words and "curated" in words          # brand + AA core
    assert conn.fetchval.call_args.args[1] == tp._MASTER_TENANT_ID


def test_atom_forbidden_words_never_raises():
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=RuntimeError("db down"))
    words = asyncio.run(tp._atom_forbidden_words(pool, "platform"))
    assert "curated" in words                                   # core list still applies


def test_judge_path_marks_fact_check_as_manual_check():
    # Elephant riding fires FACT_CHECK_MANUAL_CHECK as a pre-code; it must read as manual_check.
    gen = {"name": "Jaipur Day", "subtitle": "", "summary": "", "seo_meta": "",
           "highlights": ["Amber Fort with elephant ride to the entrance"], "itineraries": ""}
    out = _audit_from_judge({"tour": {}}, gen)
    assert "FACT_CHECK_MANUAL_CHECK" in out["brand_audit_codes"]
    assert out["brand_audit_status"] == "manual_check"
