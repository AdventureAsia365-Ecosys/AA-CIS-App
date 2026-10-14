"""AA-796 — one place that decides what a tenant may see of a tour / tour version.

AA-770 (S233) set the rule: the tenant always receives the content; quality verdicts (scores,
QA state, the score-derived ``needs_review`` status, judge output, LLM/Jev cost) stay admin-side.
Before this module the rule lived in two copies (``v1_marketplace.tenant_version_state`` and
``v1_tours._TENANT_HIDDEN_VERSION_FIELDS``) and several tenant reads skipped it. Every tenant
read in ``v1_tours.py`` and ``v1_marketplace.py`` now goes through these helpers.

The stored ``tenant_tour_versions.status`` is NOT changed: ``needs_review`` (score < 7.0, written
by services/jobs/t2_rewrite_job.py) remains the admin signal. It is only mapped at read time.
"""
import re
from typing import Any, Iterable, Optional

# Keys a tenant-JWT /v1/* response must never carry (AA-796 Done-when; used by the key-walk test).
TENANT_FORBIDDEN_KEY_RE = re.compile(r"needs_review|quality|score|qa_|judge|llm_cost|jev_cost")

# tenant_tour_versions columns that are gate output, not content.
TENANT_HIDDEN_VERSION_FIELDS = frozenset({
    "quality_score", "qa_status", "qa_repair_count", "qa_checked_at", "qa_auto_passed",
})
# published_tours columns that are A1 gate output.
TENANT_HIDDEN_TOUR_FIELDS = frozenset({"quality_score", "quality_score_id"})
# raw_tours columns that are the admin's A1 review state / notes.
TENANT_HIDDEN_RAW_FIELDS = frozenset({"review_status", "reviewed_by", "reviewed_at", "review_notes"})
# /v1/billing — LLM and Jev spend are AA's cost, not the tenant's bill (AA-796 reverses AA-636).
TENANT_HIDDEN_BILLING_FIELDS = frozenset({"llm_cost_usd", "jev_cost_usd"})

# Stored statuses the tenant sees as one "finished AI rewrite" status.
_AI_FINISHED = ("ai_generated", "needs_review")


def tenant_version_status(status: Optional[str]) -> Optional[str]:
    """Stored tenant_tour_versions.status -> the status a tenant sees.

    ``needs_review`` only means "the T2 score was < 7.0"; for the tenant it is a finished
    rewrite like any other, so it reads as ``ai_generated`` ("Ready to review" in the portal).
    Every other value (pending / failed / approved / rejected / ai_generated) is the tenant's
    own workflow state and passes through unchanged."""
    return "ai_generated" if status == "needs_review" else status


def tenant_status_filter(status: str) -> list:
    """Stored statuses matching a tenant's ``?status=`` filter. Asking for either finished-AI
    value returns both, so a low-score version is never hidden from the tenant's own list."""
    return list(_AI_FINISHED) if status in _AI_FINISHED else [status]


def tenant_version_state(status: Optional[str]) -> str:
    """Coarse Marketplace state (AA-770): the tenant sees whether a version is written, not how
    a gate scored it. ai_generated / needs_review are both 'ready'; the tenant's own
    approve/reject decisions are kept."""
    if status in ("pending", "processing"):
        return "writing"
    if status == "failed":
        return "failed"
    if status in ("approved", "rejected"):
        return status
    return "ready"


def _without(row: Any, hidden: Iterable[str]) -> dict:
    """Drop the named columns, plus any column whose name matches TENANT_FORBIDDEN_KEY_RE (a
    safety net for a column added to a SELECT * table later)."""
    hidden = set(hidden)
    return {k: v for k, v in dict(row).items()
            if k not in hidden and not TENANT_FORBIDDEN_KEY_RE.search(str(k))}


def project_version(row: Any) -> dict:
    """A tenant_tour_versions row (possibly joined) as the tenant may see it."""
    out = _without(row, TENANT_HIDDEN_VERSION_FIELDS)
    if "status" in out:
        out["status"] = tenant_version_status(out["status"])
    return out


def project_tour(row: Any) -> dict:
    """A published_tours row as the tenant may see it."""
    return _without(row, TENANT_HIDDEN_TOUR_FIELDS)


def project_raw(row: Any) -> dict:
    """A raw_tours row as the tenant may see it."""
    return _without(row, TENANT_HIDDEN_RAW_FIELDS)


def project_billing(row: Any) -> dict:
    """A v_tenant_monthly_usage row as the tenant may see it."""
    return _without(row, TENANT_HIDDEN_BILLING_FIELDS)


def forbidden_keys(obj: Any, path: str = "") -> list:
    """Every key path in a JSON-like value whose key matches TENANT_FORBIDDEN_KEY_RE."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if TENANT_FORBIDDEN_KEY_RE.search(str(k)):
                hits.append(p)
            hits.extend(forbidden_keys(v, p))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits.extend(forbidden_keys(v, f"{path}[{i}]"))
    return hits
