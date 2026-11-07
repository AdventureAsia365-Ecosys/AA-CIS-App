"""S271 W0 (root ADR 0020 "one exit contract", AA-900 l16) — the server-side gate of a Review Queue approve.

Before S271 the approve endpoint (``api.routers.v1_pipeline.approve_review``, also behind the admin alias) checked
only that a human-edited version had passed its re-validation and that the source was live. An UNEDITED version was
published as it was, whatever kept it out of Master: a surviving hard code (a forbidden word, a degraded detector
hold), unsupported sentences grounding could not repair, a grounding check that never ran (the old hitl exit), a
score below the floor. The admin page hides the Approve button for most of these, but the API did not refuse them.

``blockers(row)`` applies the auto-publish checks (``admin_pipeline._is_publishable`` + grounding) to the stored
version and returns why it cannot be approved unedited ([] = it can). One exemption is policy: a tour held only by
the elephant-riding rule (root ADR 0020 D8) is approved as ``policy_keep`` — the human review the policy asks for is
this approve. "Held only by the rule" is ``elephant_policy.is_policy_only_hold``: manual_check, the detector
hitting the stored version text or its raw source, and the policy's own code ``POLICY_ELEPHANT_RIDING`` (AA-1047)
as the only brand code, or next to the LLM audit's ``FACT_CHECK_MANUAL_CHECK`` (or the pre-AA-1047
``FACT_CHECK_MANUAL_CHECK`` alone) only when every audit issue is about the ride or a narrow voice / SEO / format
issue. Any other issue keeps the block (fail closed): the LLM audit also emits that code for other fact checks
(rare wildlife + location mismatch, an uncertain claim), and those stay blocked. Any other blocker can be overridden
only with an explicit reason (recorded on the review row); the usual path is Edit → Re-validate → Approve (an
edited elephant tour passes the re-validation gate, graph.human_edit_gate_node).

``row`` is the stored version as ``v1_pipeline._APPROVE_GATE_SQL`` reads it: score_overall, failure_codes,
brand_audit_status, brand_audit_codes (quality_scores, latest row), fix_pass_applied, metadata and the aa_* / seo_*
text (generated_content), the raw source src_* / inclusions / activities / price_raw / country (raw_tours).

Pure: no DB, no LLM, no I/O.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from services.quality import elephant_policy
from services.quality.elephant_policy import LEGACY_CODE as ELEPHANT_LEGACY_CODE
from services.quality.elephant_policy import POLICY_CODE as ELEPHANT_CODE
from services.quality.registry import threshold

ERROR_CODE = "approve_blocked"
OVERRIDE_MIN_CHARS = 10

# The auto-publish score floor (thresholds.yaml a1.publish_floor, the one admin_pipeline._is_publishable reads).
_PUBLISH_FLOOR = threshold("a1.publish_floor")


def _json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return default
    return out if isinstance(out, type(default)) else default


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def stored_result(row: Any) -> dict:
    """The stored version in the shape ``admin_pipeline._is_publishable`` / ``graph.hard_block_codes`` read."""
    metadata = _json(_get(row, "metadata"), {})
    score = _get(row, "score_overall")
    return {
        "quality_score": float(score) if score is not None else None,
        "failure_codes": [str(c) for c in _json(_get(row, "failure_codes"), [])],
        "brand_audit_status": _get(row, "brand_audit_status"),
        "brand_audit_codes": [str(c) for c in _json(_get(row, "brand_audit_codes"), [])],
        # AA-1047: the LLM audit's issues (the elephant rule reads them to tell its own restatement from another check)
        "brand_audit_issues": list(_json(_get(row, "brand_audit_issues"), [])),
        "fix_pass_applied": bool(_get(row, "fix_pass_applied")),
        "writer_version": metadata.get("writer_version"),
        "metadata": metadata,
        # the elephant-policy inputs (is_policy_keep): the stored page text and the raw source
        "content": elephant_policy.version_content_from_row(row),
        "source": elephant_policy.raw_source_from_row(row),
    }


def is_policy_keep(result: dict) -> bool:
    """Held only by the elephant-riding policy (``elephant_policy.is_policy_only_hold``): manual_check, the detector
    hits the stored text or the raw source, and the brand codes are the policy code alone, or the policy code (or the
    legacy FACT_CHECK_MANUAL_CHECK) with issues that are all about the ride or voice / SEO / format. A
    FACT_CHECK_MANUAL_CHECK the LLM audit raised for any other fact check is not a policy keep."""
    return elephant_policy.is_policy_only_hold(result.get("brand_audit_status"), result.get("brand_audit_codes"),
                                               result.get("content"), result.get("source"),
                                               issues=result.get("brand_audit_issues"))


def blockers(row: Any) -> list[dict]:
    """[{code, detail}]: why this UNEDITED version cannot be approved as it is ([] = it can)."""
    from services.content_generation.graph import hard_block_codes

    r = stored_result(row)
    out: list[dict] = []
    score = r["quality_score"]
    if score is None:
        out.append({"code": "no_score", "detail": "the version has no quality score"})
    elif score < _PUBLISH_FLOOR:
        out.append({"code": "score_below_floor", "detail": f"score {score:.1f} is below {_PUBLISH_FLOOR:g}"})
    hard = sorted(set(r["failure_codes"]) & set(hard_block_codes(r)))
    if hard:
        out.append({"code": "hard_codes", "detail": "hard codes still present: " + ", ".join(hard)})
    grounding = r["metadata"].get("grounding")
    if not isinstance(grounding, dict):
        out.append({"code": "grounding_not_run",
                    "detail": "the text was never checked against the source (grounding did not run)"})
    elif grounding.get("violations"):
        out.append({"code": "unsupported_sentences",
                    "detail": f"{len(grounding['violations'])} sentence(s) the source does not support"})
    audit = r["brand_audit_status"]
    keep = is_policy_keep(r)
    codes = [c for c in r["brand_audit_codes"] if not (keep and c in (ELEPHANT_CODE, ELEPHANT_LEGACY_CODE))]
    if audit == "flagged" and not r["fix_pass_applied"]:
        out.append({"code": "brand_flagged_unfixed",
                    "detail": "brand audit flags not repaired" + (": " + ", ".join(codes) if codes else "")})
    elif audit == "manual_check" and not keep:
        detail = "the brand audit asks for a manual check"
        if codes:
            detail += ": " + ", ".join(codes)
        out.append({"code": "manual_check", "detail": detail})
    return out


def message(found: list[dict]) -> str:
    """The 409 detail the admin page shows (its toast prints ``detail``)."""
    reasons = "; ".join(b["detail"] for b in found)
    return (f"{ERROR_CODE}: this version cannot be approved unedited ({reasons}). Edit it and re-validate, "
            "or approve with an override reason.")


def valid_override(reason: Optional[str]) -> Optional[str]:
    """The stripped override reason when it is long enough to say something, else None."""
    text = (reason or "").strip()
    return text if len(text) >= OVERRIDE_MIN_CHARS else None
