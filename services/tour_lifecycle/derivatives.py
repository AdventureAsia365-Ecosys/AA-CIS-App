"""AA-986 L2: what depends on a CIS tour, per tenant (ADR 0015 decision 2 step 5, Q10).

A tenant "has a derivative" of a tour when it holds any of: a T2 copy (tenant_tour_versions on the tour's Master), a
Slate subject (on a route of the tour, or on a Segment that would have no served member left without the tour), an
angle-gate request (on the tour, or on one of its atoms), a content piece of such a request, or a publish-log row of
such a piece. The AA internal tenant is never counted (its own catalog is not a derivative). The same joins as the
S248 deletion probe (root .tmp-session/S248-lanes/tour_deletion_probe.py), so the probe and the service agree.

Every query takes the tour ids of the change ($1 uuid[]). Read-only.
"""
from __future__ import annotations

import json
from typing import Any

AA_TENANT = "00000000-0000-0000-0000-000000000001"

# angle-gate requests / pieces / live posts of the tours: shared CTEs.
_REQ_CTE = """
    t AS (SELECT unnest($1::uuid[]) AS tour_id),
    req AS (
        SELECT r.request_id, r.tenant_id, r.status
          FROM acp_shared.angle_gate_request r
          LEFT JOIN acp_contract.tour_atoms ta ON ta.atom_id = r.atom_id
         WHERE COALESCE(r.trip_id, ta.tour_id) IN (SELECT tour_id FROM t)),
    piece AS (
        SELECT p.piece_id, p.tenant_id, p.status
          FROM acp_shared.content_piece p
          JOIN req ON req.request_id = p.angle_gate_request_id)"""

# A Segment is a derivative of the tours only when, without them, it has no served member left: a Segment shared
# with other served tours keeps serving the tenant (one tour leaving does not end it).
_DEAD_SEGMENTS_CTE = """
    dead_seg AS (
        SELECT DISTINCT m.segment_id
          FROM acp_contract.atom_segment_member m
          JOIN acp_contract.tour_atoms ta ON ta.atom_id = m.atom_id
         WHERE ta.tour_id IN (SELECT tour_id FROM t)
           AND NOT EXISTS (
                SELECT 1 FROM acp_contract.atom_segment_member m2
                  JOIN acp_contract.v_active_tour_atoms v ON v.atom_id = m2.atom_id
                 WHERE m2.segment_id = m.segment_id
                   AND v.tour_id NOT IN (SELECT tour_id FROM t)))"""

DERIVATIVES_SQL = f"""
WITH {_REQ_CTE},
{_DEAD_SEGMENTS_CTE},
    d AS (
        SELECT ttv.tenant_id, 't2_copies' AS kind, count(*) AS n
          FROM gold_aa_internal.tenant_tour_versions ttv
          JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
         WHERE pt.tour_id IN (SELECT tour_id FROM t)
         GROUP BY ttv.tenant_id
        UNION ALL
        SELECT s.tenant_id, 'subjects', count(DISTINCT s.subject_id)
          FROM acp_shared.subject s
         WHERE s.route_id IN (SELECT r.route_id FROM acp_contract.route r WHERE r.tour_id IN (SELECT tour_id FROM t))
            OR s.segment_id IN (SELECT segment_id FROM dead_seg)
         GROUP BY s.tenant_id
        UNION ALL
        SELECT req.tenant_id, 'angle_requests', count(*) FROM req GROUP BY req.tenant_id
        UNION ALL
        SELECT piece.tenant_id, 'pieces', count(*) FROM piece GROUP BY piece.tenant_id
        UNION ALL
        SELECT pl.tenant_id, 'live_posts', count(*)
          FROM acp_shared.publish_log pl
          JOIN piece ON piece.piece_id = pl.piece_id
         WHERE pl.status = 'published' AND pl.unpublished_at IS NULL
         GROUP BY pl.tenant_id)
SELECT d.tenant_id::text AS tenant_id, jsonb_object_agg(d.kind, d.n) AS kinds
  FROM d
 WHERE d.tenant_id <> '{AA_TENANT}'::uuid
 GROUP BY d.tenant_id
 ORDER BY d.tenant_id
"""

# Queued tenant jobs of the tours (the kinds that spend LLM money on one tour: T2 rewrite, T8 angles, T9 write).
# Queued only: a running job finishes or fails on its own and the L3 409s stop new ones (ADR 0015 decision 5).
QUEUED_TENANT_JOBS_WHERE = f"""
    j.status = 'queued'
    AND (
        (j.kind = 't2_rewrite' AND j.payload ->> 'published_tour_id' IN (
            SELECT pt.id::text FROM gold_aa_internal.published_tours pt WHERE pt.tour_id = ANY($1::uuid[])))
     OR (j.kind = 't8_angles' AND j.payload ->> 'request_id' IN (
            WITH {_REQ_CTE} SELECT request_id::text FROM req))
     OR (j.kind = 't9_write' AND j.payload ->> 'piece_id' IN (
            WITH {_REQ_CTE} SELECT piece_id::text FROM piece)))
"""

CANCEL_QUEUED_JOBS_SQL = f"""
UPDATE shared.job j SET
    status = 'cancelled', finished_at = now(), cancel_requested = true, updated_at = now(),
    error = 'cancelled: the tour is no longer served by AA (' || $2 || ')'
 WHERE {QUEUED_TENANT_JOBS_WHERE}
RETURNING j.id::text AS id, j.kind
"""

COUNT_QUEUED_JOBS_SQL = f"SELECT count(*) FROM shared.job j WHERE {QUEUED_TENANT_JOBS_WHERE}"


def _kinds(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw)


async def tenant_derivatives(conn, tour_ids: list[str]) -> dict[str, dict]:
    """{tenant_id: {kind: count}} for every tenant (not AA) with a derivative of the tours."""
    if not tour_ids:
        return {}
    rows = await conn.fetch(DERIVATIVES_SQL, [str(t) for t in tour_ids])
    return {r["tenant_id"]: {k: int(v) for k, v in _kinds(r["kinds"]).items()} for r in rows}


def summarize(derivs: dict[str, dict]) -> dict:
    """Totals for the confirm dialog: tenants, and the sum of each kind over them."""
    out = {"tenants": len(derivs), "t2_copies": 0, "subjects": 0, "angle_requests": 0, "pieces": 0,
           "live_posts": 0}
    for kinds in derivs.values():
        for k, n in kinds.items():
            out[k] = out.get(k, 0) + int(n)
    return out
