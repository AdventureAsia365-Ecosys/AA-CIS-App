"""AA-796 — one place that decides what a tenant may see of a tour / tour version.

AA-770 (S233) set the rule: the tenant always receives the content; quality verdicts (scores,
QA state, the score-derived ``needs_review`` status, judge output, LLM/Jev cost) stay admin-side.
Before this module the rule lived in two copies (``v1_marketplace.tenant_version_state`` and
``v1_tours._TENANT_HIDDEN_VERSION_FIELDS``) and several tenant reads skipped it. Every tenant
read in ``v1_tours.py`` and ``v1_marketplace.py`` now goes through these helpers.

AA-1188 (S295 data-boundary audit DB-01): nor AA's commercial / supplier data. A raw_tours row is projected through an
allowlist (TENANT_RAW_FIELDS), and every projection and the key-walk also refuse TENANT_COMMERCIAL_KEY_RE keys
(supplier, provider, source price text, net / cost / margin / markup).

The stored ``tenant_tour_versions.status`` is NOT changed: ``needs_review`` (score < 7.0, written
by services/jobs/t2_rewrite_job.py) remains the admin signal. It is only mapped at read time.
"""
import re
from typing import Any, Iterable, Optional

# Keys a tenant-JWT /v1/* response must never carry (AA-796 Done-when; used by the key-walk test). AA-920 WM-25 adds
# the writing-memory keys (`generation_meta`, `rule_ids`, `release` (writing_release, release_id, release_no),
# `memory`): which release and which learned rules wrote a version is AA's QA state, not the tenant's content
# (hard stop 3).
# AA-1047: `fs_id` (CIS 386) names the admin FactSheet a version was written from; matched as a whole key.
TENANT_FORBIDDEN_KEY_RE = re.compile(
    r"needs_review|quality|score|qa_|judge|llm_cost|jev_cost|generation_meta|rule_ids|release|memory|\bfs_id\b")

# AA-1188 (S295 data-boundary audit DB-01) — AA's commercial and supplier data never reaches an agency: the supplier or
# DMC named in a source sheet (`raw_tours.provider`, `supplier_id`), the source price text (`price_raw`) and any net /
# cost / margin / markup figure. Matched on whole `_`-separated parts of a key, so `net_amount`, `target_margin`,
# `supplier_id`, `llm_cost_usd` and `provider` are caught while `internet_fee`, `planet` or `costume` are not; plus
# `price_raw` anywhere in a key. Keys a tenant legitimately gets (`from_price`, `price_usd`, `price_available`,
# `price_usd_monthly`, `overage_usd`) do not match. A separate constant from TENANT_FORBIDDEN_KEY_RE (that one is the
# AA-770 quality rule) so each reads on its own; `_without` and `forbidden_keys` apply both.
TENANT_COMMERCIAL_KEY_RE = re.compile(
    r"(?:^|_)(?:net|costs?|margins?|markups?|suppliers?|providers?)(?:_|$)|price_raw")

# tenant_tour_versions columns that are gate or generation output, not content (generation_meta: CIS 297, AA-920;
# judge_breakdown: CIS 300, AA-795 — the T2 judge verdict and the brand-claim check; fs_id: CIS 386, AA-1047 — the
# admin FactSheet the version was written from. The tenant pool's table-level grant covers fs_id, so this projection,
# not a grant, is what keeps it out of tenant responses).
TENANT_HIDDEN_VERSION_FIELDS = frozenset({
    "quality_score", "qa_status", "qa_repair_count", "qa_checked_at", "qa_auto_passed", "generation_meta",
    "judge_breakdown", "fs_id",
})
# published_tours columns that are A1 gate output. AA-1188 (DB-01): and the staff / pipeline columns a `SELECT pt.*`
# (GET /v1/tours/{id}, the /full tenant branch) carried: who approved, deleted or locked the Master and why
# (`human_lock_reason` is pii:free_text), the A1 writer row, the gold S3 path and the search embedding.
TENANT_HIDDEN_TOUR_FIELDS = frozenset({
    "quality_score", "quality_score_id", "generated_content_id", "approved_by", "deleted_by", "master_status_reason",
    "human_lock_at", "human_lock_by", "human_lock_reason", "s3_gold_path", "content_embedding",
})
# AA-1188 (DB-01) — the raw_tours columns a tenant may see: an ALLOWLIST, not a list of hidden columns, so a column
# added to raw_tours later stays AA-side until someone adds it here. Only trip facts of the AA tour, the same ones the
# tenant already reads on GET /v1/tours/versions/{id} (country, duration, inclusions, exclusions) plus the other plain
# facts of the tour (country code, group size, best time to go, activities), and `tour_id`. Left out on purpose: the
# supplier named in the source sheet and its id (`provider`, `supplier_id`), the source price text (`price_raw`, ADR
# 0005 d7: reference only), the supplier's own source copy (`src_*`, `feature`, `period`: AA's input, may name the
# supplier, its contacts or its prices; the tenant gets AA's text in `published_tours.aa_*`), `links`, `manual_facts`,
# file / import metadata (`batch_id`, `source_id`, `tour_id_external`, `sku`, `ingest_at`, `entry_origin`,
# `source_group_id`, `source_version`, `source_key`, `country_raw_unresolved`, `tenant_id`) and every pipeline /
# review / lifecycle column (`pipeline_status`, `review_*`, `reviewed_*`, `source_status`, `lifecycle_stage`,
# `deleted_*`). Columns: docs/architecture/db-schema-reference.md, silver_aa_internal.raw_tours.
TENANT_RAW_FIELDS = frozenset({
    "tour_id", "country", "country_iso2", "duration", "group_size", "best_time_to_go", "activities",
    "inclusions", "exclusions",
})
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


def tenant_forbidden_key(key: Any) -> bool:
    """True when a key may never reach a tenant: the AA-770 quality rule or the AA-1188 commercial rule."""
    k = str(key)
    return bool(TENANT_FORBIDDEN_KEY_RE.search(k) or TENANT_COMMERCIAL_KEY_RE.search(k))


def _without(row: Any, hidden: Iterable[str]) -> dict:
    """Drop the named columns, plus any column whose name matches TENANT_FORBIDDEN_KEY_RE or
    TENANT_COMMERCIAL_KEY_RE (a safety net for a column added to a SELECT * table later)."""
    hidden = set(hidden)
    return {k: v for k, v in dict(row).items() if k not in hidden and not tenant_forbidden_key(k)}


def _only(row: Any, allowed: Iterable[str]) -> dict:
    """Keep only the named columns (an allowlist), and never a forbidden key even if it is named."""
    allowed = set(allowed)
    return {k: v for k, v in dict(row).items() if k in allowed and not tenant_forbidden_key(k)}


def master_newer(master_updated_at: Any, version_created_at: Any) -> bool:
    """AA-990 — True when Adventure Asia changed the tour's Master text after this version was made.

    ``master_updated_at`` is ``published_tours.updated_at`` (moved only by a Master publish / promote, CIS 273);
    ``version_created_at`` is ``tenant_tour_versions.created_at``. Strictly later only: equal timestamps, or a
    missing value on either side, are False (no notice when we cannot tell)."""
    if master_updated_at is None or version_created_at is None:
        return False
    try:
        return master_updated_at > version_created_at
    except TypeError:  # naive vs aware datetime, or a type that does not compare: no notice
        return False


def project_version(row: Any) -> dict:
    """A tenant_tour_versions row (possibly joined) as the tenant may see it.

    AA-990: a row joined with ``pt.updated_at AS master_updated_at`` also gets ``master_newer``
    (see master_newer()). Rows without that column (version history) are unchanged."""
    out = _without(row, TENANT_HIDDEN_VERSION_FIELDS)
    if "status" in out:
        out["status"] = tenant_version_status(out["status"])
    if "master_updated_at" in out:
        out["master_newer"] = master_newer(out["master_updated_at"], out.get("created_at"))
    if "edit_source" in out:
        # AA-1040 — the machine-readable AI mark of the version (EU AI Act Art. 50(2)), from its edit_source.
        from services.acp_content_writing.ai_marking import version_mark
        out["ai"] = version_mark(out["edit_source"], out.get("created_at"))
    return out


def project_tour(row: Any) -> dict:
    """A published_tours row as the tenant may see it.

    AA-1040: a row carrying the Master catalog text (any `aa_*` text column) also gets `ai`, the machine-readable mark
    of that AI-written text (EU AI Act Art. 50(2)), from `updated_at` (last Master publish) else `published_at`."""
    out = _without(row, TENANT_HIDDEN_TOUR_FIELDS)
    from services.acp_content_writing.ai_marking import CATALOG_TEXT_FIELDS, tour_mark
    if CATALOG_TEXT_FIELDS.intersection(out):
        out["ai"] = tour_mark(out.get("updated_at"), out.get("published_at"))
    return out


def project_raw(row: Any) -> dict:
    """A raw_tours row as the tenant may see it: only the TENANT_RAW_FIELDS columns (AA-1188)."""
    return _only(row, TENANT_RAW_FIELDS)


def project_billing(row: Any) -> dict:
    """A v_tenant_monthly_usage row as the tenant may see it."""
    return _without(row, TENANT_HIDDEN_BILLING_FIELDS)


def forbidden_keys(obj: Any, path: str = "") -> list:
    """Every key path in a JSON-like value whose key matches TENANT_FORBIDDEN_KEY_RE or TENANT_COMMERCIAL_KEY_RE."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if tenant_forbidden_key(k):
                hits.append(p)
            hits.extend(forbidden_keys(v, p))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits.extend(forbidden_keys(v, f"{path}[{i}]"))
    return hits
