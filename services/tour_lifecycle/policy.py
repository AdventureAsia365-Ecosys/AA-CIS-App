"""AA-1049 (tech lead decision S277, root ADR 0020 D8): the elephant-riding policy on the served state.

A Master whose product offers elephant riding is not on sale until a person reviews it; it goes to the Review Queue
and only an approval puts it back on sale. This module holds the DB reads the lifecycle service (service.py) and the
Review Queue use for that rule; the writes of the status columns stay in service.py:

- ``master_hits`` / ``version_hits``: the detector (``services/quality/elephant_policy.scan``) over a live Master's
  published text, or a version's stored text, plus the tour's raw source (the same fields and country rule as S1);
- ``human_approved``: a person approved this version in the Review Queue (a ``review_queue`` row of the version with
  ``review_status = 'approved'``; the approve claims that row before it publishes);
- ``policy_approved``: a person approved this version's own policy review (an approved row that carries the policy
  marker): the hold refuses such a version (409 ``policy_approved``) and the one-time script leaves it out, so a
  re-run never undoes a person's approval;
- ``ensure_policy_review``: the pending policy review row (``services/quality/policy_review.py`` marker) of a tour;
- ``held_master_version``: the review row is the policy review of the very version a held Master serves, so its
  approve puts back exactly what was on sale before the hold.

The guard (service.publish, service.activate_master): a version with a detector hit becomes an active Master only
with a human approval of that version; a Master held for the policy (``master_status_reason = 'policy_hold'``, CIS
398) comes back only through the Review Queue approve (its publish lifts the hold).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from services.quality import elephant_policy, policy_review

POLICY_HOLD = "policy_hold"
POLICY_HOLD_MIGRATION = "398"
POLICY_REVIEW_REQUIRED = "policy_review_required"
POLICY_APPROVED = "policy_approved"
AA_TENANT = "00000000-0000-0000-0000-000000000001"

_SOURCE_COLUMNS = ("rt.country, rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, "
                   "rt.src_highlights::text AS src_highlights, rt.src_itineraries, rt.inclusions, "
                   "rt.activities::text AS activities, rt.price_raw")

# A live Master's published text + its source (the hold script and the hold read the same columns).
MASTER_SCAN_COLUMNS = (
    "pt.tour_id::text AS tour_id, pt.generated_content_id::text AS generated_content_id, "
    "pt.master_status::text AS master_status, pt.master_status_reason, "
    "pt.aa_name, pt.aa_subtitle, pt.aa_summary, pt.aa_description, pt.aa_highlights::text AS aa_highlights, "
    "pt.aa_itineraries, pt.seo_title, pt.seo_meta, " + _SOURCE_COLUMNS)
MASTER_SCAN_FROM = ("FROM gold_aa_internal.published_tours pt "
                    "JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id")

_MASTER_SQL = f"SELECT {MASTER_SCAN_COLUMNS} {MASTER_SCAN_FROM} WHERE pt.tour_id = $1::uuid"

_VERSION_SQL = f"""
    SELECT gc.tour_id::text AS tour_id, gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description,
           gc.aa_highlights::text AS aa_highlights, gc.aa_itineraries, gc.seo_title, gc.seo_meta, {_SOURCE_COLUMNS}
      FROM silver_aa_internal.generated_content gc
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
     WHERE gc.id = $1::uuid
"""

SOURCE_SQL = f"SELECT {_SOURCE_COLUMNS} FROM silver_aa_internal.raw_tours rt WHERE rt.tour_id = $1::uuid"

_APPROVED_SQL = """
    SELECT EXISTS (SELECT 1 FROM silver_aa_internal.review_queue
                    WHERE generated_content_id = $1::uuid AND review_status = 'approved') AS ok
"""



def policy_approved_sql(version_expr: str) -> str:
    """SQL boolean: the version ``version_expr`` has an approved S1 policy review (never NULL)."""
    return (f"EXISTS (SELECT 1 FROM silver_aa_internal.review_queue a WHERE a.generated_content_id = {version_expr}"
            f" AND a.review_status = 'approved' AND {policy_review.row_is_policy_sql('a')})")


_POLICY_APPROVED_SQL = f"SELECT {policy_approved_sql('$1::uuid')} AS ok"

_PENDING_POLICY_SQL = f"""
    SELECT rq.id::text AS id FROM silver_aa_internal.review_queue rq
     WHERE rq.tour_id = $1::uuid AND rq.tenant_id = $2::uuid AND rq.review_status = 'pending'
       AND {policy_review.row_is_policy_sql('rq')}
     ORDER BY rq.created_at DESC, rq.id LIMIT 1
"""

_INSERT_POLICY_REVIEW_SQL = """
    INSERT INTO silver_aa_internal.review_queue (
        tour_id, generated_content_id, tenant_id, failure_summary, score_overall, review_status, escalate_detail)
    SELECT $1::uuid, $2::uuid, $3::uuid, $4,
           (SELECT q.score_overall FROM silver_aa_internal.quality_scores q
             WHERE q.generated_content_id = $2::uuid ORDER BY q.evaluated_at DESC LIMIT 1),
           'pending'::review_status_enum, $5::jsonb
    RETURNING id::text
"""

# AA-653: one pending review per tour; the policy review replaces older pending S1 rows of the tour. Only the master
# tenant's S1 rows (a version id): a tenant's T2 row of the same tour (tenant_tour_version_id, its agency tenant_id,
# e.g. the T2 elephant-riding policy hold) is the agency's own review and is never closed here.
_SUPERSEDE_OTHERS_SQL = """
    UPDATE silver_aa_internal.review_queue
       SET review_status = 'superseded'::review_status_enum, reviewed_at = NOW()
     WHERE tour_id = $1::uuid AND review_status = 'pending' AND id <> $2::uuid
       AND generated_content_id IS NOT NULL AND tenant_id = $3::uuid
"""

HELD_MASTER_VERSION_SQL = f"""
    SELECT EXISTS (
        SELECT 1 FROM silver_aa_internal.review_queue rq
          JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rq.tour_id
         WHERE rq.id = $1::uuid AND {policy_review.row_is_policy_sql('rq')}
           AND pt.master_status = 'inactive' AND pt.master_status_reason = '{POLICY_HOLD}'
           AND pt.generated_content_id = rq.generated_content_id) AS ok
"""


class PolicyReviewRequiredError(Exception):
    """process_export refused a version with elephant riding and no human approval: the version went back to
    'hitl' and a pending policy review row was filed (``review_id``)."""

    def __init__(self, version_id: str, review_id: Optional[str]):
        super().__init__(version_id, review_id)
        self.version_id = version_id
        self.review_id = review_id

    def __str__(self) -> str:
        return (f"{POLICY_REVIEW_REQUIRED}: this tour offers elephant riding; a person approves it in the Review "
                "Queue before it is on sale")


def refusal_message(action: str) -> str:
    return (f"this tour offers elephant riding, so a person reviews it before it is on sale (policy): approve it in "
            f"the Review Queue instead of {action}.")


def hits_from_row(row: Any, *, master: bool = False) -> list[dict]:
    """Detector hits over a row carrying the aa_* / seo_* page columns and the raw source columns. A hit on the page
    text says where it is: ``master`` (a live Master's published text) or ``version`` (a stored version)."""
    hits = elephant_policy.scan(elephant_policy.version_content_from_row(row),
                                elephant_policy.raw_source_from_row(row))
    label = "master" if master else "version"
    return [{**h, "where": label if h["where"] == "generated" else h["where"]} for h in hits]


async def master_hits(conn, tour_id) -> list[dict]:
    row = await conn.fetchrow(_MASTER_SQL, str(tour_id))
    return hits_from_row(row, master=True) if row else []


async def version_hits(conn, version_id) -> list[dict]:
    if not version_id:
        return []
    row = await conn.fetchrow(_VERSION_SQL, str(version_id))
    return hits_from_row(row) if row else []


async def source_row(conn, tour_id) -> Optional[Any]:
    return await conn.fetchrow(SOURCE_SQL, str(tour_id))


async def human_approved(conn, version_id) -> bool:
    if not version_id:
        return False
    return (await conn.fetchval(_APPROVED_SQL, str(version_id))) is True


async def policy_approved(conn, version_id) -> bool:
    if not version_id:
        return False
    return (await conn.fetchval(_POLICY_APPROVED_SQL, str(version_id))) is True


async def held_master_version(conn, review_id) -> bool:
    return (await conn.fetchval(HELD_MASTER_VERSION_SQL, str(review_id))) is True


async def ensure_policy_review(conn, tour_id, version_id, hits: list[dict], *,
                               base_summary: str | None = None) -> str:
    """The tour's pending policy review row: the existing one, else a new row for ``version_id`` (which then
    replaces the tour's other pending S1 rows, AA-653; tenants' T2 rows are left alone). Returns its id."""
    existing = await conn.fetchval(_PENDING_POLICY_SQL, str(tour_id), AA_TENANT)
    if existing:
        return existing
    review_id = await conn.fetchval(
        _INSERT_POLICY_REVIEW_SQL, str(tour_id), str(version_id), AA_TENANT, policy_review.summary(base_summary),
        json.dumps(policy_review.detail(hits)))
    await conn.execute(_SUPERSEDE_OTHERS_SQL, str(tour_id), review_id, AA_TENANT)
    return review_id


__all__ = ["HELD_MASTER_VERSION_SQL", "MASTER_SCAN_COLUMNS", "MASTER_SCAN_FROM", "POLICY_APPROVED", "POLICY_HOLD",
           "POLICY_HOLD_MIGRATION", "POLICY_REVIEW_REQUIRED", "PolicyReviewRequiredError", "SOURCE_SQL",
           "ensure_policy_review", "held_master_version", "hits_from_row", "human_approved", "master_hits",
           "policy_approved", "policy_approved_sql", "refusal_message", "source_row", "version_hits"]
