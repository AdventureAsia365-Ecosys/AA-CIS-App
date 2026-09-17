"""AA-226 (original) / AA-608 (revised): seo_meta length behaviour in flag_fix_node.

AA-608 (H3 finding) changed the policy: a meta that cannot be pulled into the 140-155 band is
a FORMAT miss, not a product-truth miss, so it must NOT hard-block via manual_check any more —
it is kept as the best candidate and left to flag (the validate/revalidate -0.5 sub-score still
surfaces it to the reviewer). Only genuine product-truth findings from the brand audit block.
_rerepair_meta is now a bounded retry loop (up to _META_REREPAIR_MAX_ATTEMPTS) rather than a
single call, so tests feed enough responses for the loop and assert on the no-block policy.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from services.content_generation import flag_fix_node as ff

_LEAD = "Trek valleys meet weavers rest at lodge "
def _meta(n):
    """Complete sentence of EXACTLY n chars: lead + filler + period."""
    return _LEAD + ("a" * (n - len(_LEAD) - 1)) + "."

def _resp(payload, cost=0.004):
    return SimpleNamespace(content=json.dumps(payload), cost_usd=cost,
                           model_used="haiku", fallback_used=False,
                           satellite_account=None,
                           # AA-288: generate_node reads these off every resp now.
                           cache_read_tokens=0, cache_write_tokens=0)

def _state(seo=None, cur_meta=None):
    # brand_audit flags seo_meta so _should_fix + _build_fix_keys include it.
    return {
        "tour": {"country": "Nepal", "duration": "10 days"},
        "generated": {"name": "Annapurna Circuit", "seo_meta": cur_meta or _meta(132),
                      "seo_title": "Annapurna Circuit Trek"},
        "brand_audit_status": "flagged",
        "brand_audit_codes": ["META_TOO_SHORT"],
        "brand_audit_issues": ["seo_meta under 140 chars"],
        "model_tier": "haiku",
        "cost_usd": 0.0,
        "seo": seo if seo is not None else {"people_also_ask": ["best time to trek Annapurna"]},
    }

def _patch_llm(responses):
    inst = MagicMock()
    inst.generate.side_effect = responses
    return patch.object(ff, "LLMClient", return_value=inst)


def test_rerepair_recovers_in_band_no_hitl():
    """Main fix returns under-floor (132); the re-repair loop's first attempt returns in-band
    (148) -> accepted, no manual_check."""
    main = _resp({"seo_meta": _meta(132)})
    rerepair = _resp({"seo_meta": _meta(148)})
    with _patch_llm([main, rerepair]):
        out = ff.flag_fix_node(_state())
    assert len(out["generated"]["seo_meta"]) == 148
    assert out.get("brand_audit_status") != "manual_check"
    assert out["fix_pass_applied"] is True


def test_rerepair_still_out_of_band_flags_not_blocks():
    """AA-608: main fix + every re-repair attempt stay under-floor (132) -> the meta is kept as
    best-effort and the node does NOT escalate to manual_check (format miss must not hard-block).
    Feed enough responses for the whole bounded loop plus the main field fix."""
    responses = [_resp({"seo_meta": _meta(132)})
                 for _ in range(ff._META_REREPAIR_MAX_ATTEMPTS + 1)]
    with _patch_llm(responses):
        out = ff.flag_fix_node(_state())
    assert out.get("brand_audit_status") != "manual_check"
    assert out["fix_pass_applied"] is True


def test_in_band_first_pass_no_rerepair_no_hitl():
    """Main fix already in band (148) -> only ONE LLM call (no re-repair loop), no manual_check."""
    main = _resp({"seo_meta": _meta(148)})
    inst = MagicMock()
    inst.generate.side_effect = [main]
    with patch.object(ff, "LLMClient", return_value=inst):
        out = ff.flag_fix_node(_state())
    assert len(out["generated"]["seo_meta"]) == 148
    assert out.get("brand_audit_status") != "manual_check"
    assert inst.generate.call_count == 1


def test_no_seo_context_still_no_block():
    """AA-608: no PAA/related clue available + re-repair fails -> still no manual_check, no crash."""
    responses = [_resp({"seo_meta": _meta(132)})
                 for _ in range(ff._META_REREPAIR_MAX_ATTEMPTS + 1)]
    with _patch_llm(responses):
        out = ff.flag_fix_node(_state(seo={}))
    assert out.get("brand_audit_status") != "manual_check"
