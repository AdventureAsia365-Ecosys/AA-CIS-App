"""
services.acp_angle_gate.service — T8 request lifecycle.

AA-522 (04/09/2026) — Luồng B removed. Every angle_gate_request is now created by
services.acp_shared.slate.pick_subject() (atom_id/trip_id/channel/subject_id all set at INSERT
time, from the picked Subject) — this module no longer creates requests itself. create_request()
(the old atom-only, no-Subject creation path) and set_channel() (the old post-angle-choice
Channel step, AA-469 Việc 4's workflow step 8) are both DELETED along with their FE — channel is
now always fixed from the Subject at creation, never chosen here. This also means the AA-451
slot-CTA prefill (_fetch_slot_cta()/_compute_and_persist_slot_cta(), which lived inside
set_channel()) is gone with it — pick_subject() does not populate `cta` either, so it stays NULL
for every real request today; services/acp_content_writing/service.py's own ask-the-tenant
fallback (MissingCTAError) is now the ONLY way a CTA gets resolved. See AA-522's implementation
notes for the full before/after and why this is an acceptable, deliberate simplification rather
than a regression (the slot-CTA prefill was already unreachable for the current Subject-driven
flow — channel being fixed at creation meant the FE's Channel-step card, its only caller, never
rendered for a Subject-driven request).

DB tables: acp_shared.angle_gate_request / angle_gate_option (migration 113, channel now
nullable per migration 126, subject_id migration 133).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from uuid import UUID

import structlog

from services.acp_angle_gate.brand_audience import fetch_brand_audience
from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.generate import generate_angles
from services.acp_angle_gate.goals import get_goal
from services.acp_angle_gate.ranking import rank_angles, verify_answers
from services.acp_planning.tenant_pool import fetch_tenant_trips
from services.acp_shared.dfs_relevance import fetch_search_demand_signal
from services.acp_shared.piece_history import fetch_piece_history

logger = structlog.get_logger()


class AngleGateError(Exception):
    """Base class for angle-gate lifecycle errors — this module's own domain errors, distinct
    from generate.py's AngleGenerationError (which propagates through unchanged when it happens
    mid-lifecycle, e.g. inside set_goal_and_generate())."""


class AtomNotFoundError(AngleGateError):
    pass


class RequestNotFoundError(AngleGateError):
    pass


class InvalidGoalError(AngleGateError):
    pass


class WrongStatusError(AngleGateError):
    """Raised when an action is attempted on a request in the wrong lifecycle state (e.g.
    choosing an angle before a goal has been set, or setting a goal twice)."""


_ATOM_QUERY = """
    SELECT atom_id, tour_id, text
    FROM acp_contract.tour_atoms
    WHERE atom_id = $1 AND owner_scope IN ('platform', $2) AND NOT deleted AND NOT is_empty_marker
"""

async def _fetch_atom_for_tenant(tenant_id: UUID, atom_id: str, pool) -> dict:
    """AA-567 correction: this docstring used to claim "tenant-scoped... not a new security
    pattern," following `tenant_pool.py`'s T7 precedent — that precedent does not actually apply
    here. T7's `owner_scope=tenant_id`-only convention answers a different question ("how many
    atoms has THIS tenant personally produced via their own T5 rewrite," matching Marketplace's
    inventory count) than this function does ("what is the text of the ONE atom this
    Slate-proposed Segment/Subject already resolved to, so T8/T9 can write about it"). Since
    AA-526, atomize is platform-wide by default (`owner_scope='platform'`) — this atom_id was
    already accepted by `slate.py::_resolve_representative_atom()`'s own (now-fixed, AA-567)
    `owner_scope IN ('platform', tenant_id)` check before an `angle_gate_request` was ever
    created for it, so re-checking it here with the OLD, narrower, tenant-only filter would
    reject the exact same atom a moment later — the actual bug AA-567 fixes. Kept a tenant-owned
    atom acceptable too, unchanged from before."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_ATOM_QUERY, atom_id, str(tenant_id))
    if row is None:
        raise AtomNotFoundError(f"atom_id={atom_id!r} not found (deleted, or not a live platform/tenant atom)")
    return {"atom_id": row["atom_id"], "trip_id": row["tour_id"], "text": row["text"]}


async def _fetch_request_row(tenant_id: UUID, request_id: UUID, pool):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT request_id, tenant_id, atom_id, trip_id, channel, goal, cta, status,
                   dfs_paa_snapshot, created_at, updated_at, route_segment_ids, subject_id
            FROM acp_shared.angle_gate_request
            WHERE request_id = $1 AND tenant_id = $2
            """,
            request_id, tenant_id,
        )
    if row is None:
        raise RequestNotFoundError(f"request_id={request_id} not found for this tenant")
    return row


async def set_goal_and_generate(tenant_id: UUID, request_id: UUID, goal_key: str, pool) -> dict:
    """Workflow steps 2-6: tenant picks a goal -> auto brand audience (step 3) -> formula (step
    4) -> generate 3 angles (step 5) -> recommend (step 6) — one call, matching the build task's
    own endpoint list (POST .../goal does all of steps 2-6 in a single request, no separate
    round trip between 'goal chosen' and 'angles ready')."""
    req = await _fetch_request_row(tenant_id, request_id, pool)
    if req["status"] != "pending_goal":
        raise WrongStatusError(
            f"request_id={request_id} is status={req['status']!r}, expected 'pending_goal' — "
            "a goal has already been set for this request."
        )
    goal = get_goal(goal_key)
    if goal is None:
        raise InvalidGoalError(f"Unknown goal key: {goal_key!r}")

    atom = await _fetch_atom_for_tenant(tenant_id, req["atom_id"], pool)
    brand_audience = await fetch_brand_audience(tenant_id, pool)

    trip_name = None
    destination = None
    if req["trip_id"]:
        trips = await fetch_tenant_trips(tenant_id, pool)
        trip = next((t for t in trips if t.id == req["trip_id"]), None)
        if trip:
            trip_name = trip.name
            destination = trip.destination

    # AA-469 Việc 4 — DFS/PAA search-demand signal, the confirmed real gap from both this
    # task's STEP0 and the prior AA-469 STEP0 (docs/claude_audit/
    # AA-469-viec4-step0-t8-t11-chain-investigation.md §1-2): angle generation never read
    # seo_context at any layer. Only fetched when the request has a trip_id (matches
    # trip_name/destination's own guard just above — seo_context is keyed by tour_id, no
    # signal to fetch for a tripless atom). `fetch_search_demand_signal()` returns None when
    # this tour has no seo_context row at all — that's the common case for tours DFS hasn't
    # run against, not an error; build_user_prompt() below omits the block entirely for None.
    search_demand = None
    if req["trip_id"]:
        search_demand = await fetch_search_demand_signal(req["trip_id"], pool)

    # AA-498 (Decision 4) — real prior pieces for this exact atom (this tenant), so a rewrite
    # doesn't converge on the same angle again. Empty for the common case today (first time this
    # atom is being written, or no prior piece produced a summary yet) — see fetch_piece_history's
    # own docstring for why that's not an error.
    piece_history = await fetch_piece_history(
        tenant_id, req["atom_id"], pool, exclude_request_id=request_id,
    )

    angles, recommended_index, reason, cost_usd = await generate_angles(
        content_seed=atom["text"], goal=goal,
        brand_audience=brand_audience, destination=destination, trip_name=trip_name,
        search_demand=search_demand, piece_history=piece_history,
        tenant_id=tenant_id, request_id=request_id, pool=pool,  # AA-505
    )

    # AA-512 — measurable ranking (ADR 0004), replacing the LLM's own opinion, ONLY when channel
    # is already known (a Subject-driven request — channel fixed from the Subject at creation).
    # Avoid-list violations are channel-scoped and genuinely can't be computed before a channel is
    # known — never true for the legacy atom-picker path (channel picked at step 8, AFTER angles
    # already exist) — so that path keeps the LLM's own recommended_index unchanged, no
    # regression. See docs/claude_audit/AA-512-step0-investigation.md §2.
    ranking_evidence = None
    if req["channel"]:
        channel_style = get_channel_style(req["channel"])
        avoid_text = channel_style["avoid"] if channel_style else ""
        asked_questions = search_demand.people_also_ask if search_demand else []
        ranking_evidence, recommended_index = rank_angles(
            angles, claimed_answers=[a.get("answers", []) for a in angles],
            asked_questions=asked_questions, avoid_text=avoid_text,
        )
        ranking_evidence, recommended_index, jev_dropped = await verify_answers(   # AA-700 T8-1
            angles, ranking_evidence, tenant_id,
        )
        logger.info(
            "angle_gate_measurable_ranking", request_id=str(request_id),
            recommended_index=recommended_index, jev_dropped=jev_dropped,
            scores=[e.score for e in ranking_evidence],
        )

    # AA-501 (migration 127) — snapshot, not a live re-fetch: persist exactly the
    # SearchDemandSignal the LLM saw for THIS request, so a later T2 DFS re-run on the same tour
    # can never silently change what the review screen shows for an already-generated angle. None
    # (no trip_id, or no seo_context row) stays NULL, not an empty object — same "absent means no
    # signal" convention fetch_search_demand_signal() itself uses.
    dfs_paa_snapshot = json.dumps(asdict(search_demand)) if search_demand else None

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE acp_shared.angle_gate_request
                SET goal = $2, status = 'pending_choice', updated_at = now(),
                    dfs_paa_snapshot = $3::jsonb
                WHERE request_id = $1
                """,
                request_id, goal_key, dfs_paa_snapshot,
            )
            for i, a in enumerate(angles):
                evidence = ranking_evidence[i] if ranking_evidence else None
                await conn.execute(
                    """
                    INSERT INTO acp_shared.angle_gate_option
                        (request_id, idx, name, why_it_works, formula_fit, best_final_style,
                         recommended, answers, violations)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb)
                    """,
                    request_id, i, a["name"], a["why_it_works"], a["formula_fit"],
                    a["best_final_style"], i == recommended_index,
                    json.dumps(evidence.answers) if evidence else None,
                    json.dumps(evidence.violations) if evidence else None,
                )
    logger.info(
        "angle_gate_goal_set", request_id=str(request_id), goal=goal_key,
        recommended_index=recommended_index, recommendation_reason=reason, cost_usd=cost_usd,
    )
    return await fetch_request(tenant_id, request_id, pool)


async def reopen_request(tenant_id: UUID, request_id: UUID, pool) -> dict:
    """AA-497 (AA-494 Decision 3) — the tenant-triggered action that moves an 'approved' request
    back to 'reusable', unlocking choose_angle() below to re-point `chosen` at a different one of
    the 3 already-generated angle_gate_option rows — no new LLM call, they were generated once by
    set_goal_and_generate() and have sat in the DB ever since (AA-449's original design already
    persisted all 3, just never exposed a way back to pick a different one after 'approved').

    Only valid from 'approved' — a request that hasn't been approved yet has nothing to reopen
    (call set_goal_and_generate()/choose_angle() normally instead), and a request already
    'reusable' is already reopened. Raising WrongStatusError on a double-call (rather than a
    silent no-op) makes that visible instead of hiding it."""
    req = await _fetch_request_row(tenant_id, request_id, pool)
    if req["status"] != "approved":
        raise WrongStatusError(
            f"request_id={request_id} is status={req['status']!r}, expected 'approved' — only "
            "an approved request can be reopened for re-selection."
        )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE acp_shared.angle_gate_request SET status = 'reusable', updated_at = now() "
            "WHERE request_id = $1",
            request_id,
        )
    return await fetch_request(tenant_id, request_id, pool)


async def choose_angle(tenant_id: UUID, request_id: UUID, idx: int, pool) -> dict:
    """Workflow step 7 — the real 'gate': tenant picks 1 of the 3 (recommended or not, both
    valid — workflow: 'có thể chọn theo đề xuất... hoặc chọn khác').

    AA-497 — also callable from status='reusable' (reopen_request() above), not just the
    original 'pending_choice' — re-points `chosen` at a different already-generated option, no
    LLM call, and lands back on 'approved' exactly like the first-time choice does below (the
    final UPDATE already sets 'approved' unconditionally, so that part needed no change)."""
    req = await _fetch_request_row(tenant_id, request_id, pool)
    if req["status"] not in ("pending_choice", "reusable"):
        raise WrongStatusError(
            f"request_id={request_id} is status={req['status']!r}, expected 'pending_choice' or "
            "'reusable'."
        )
    if idx not in (0, 1, 2):
        raise AngleGateError(f"idx must be 0, 1, or 2, got {idx!r}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.execute(
                "UPDATE acp_shared.angle_gate_option SET chosen = true "
                "WHERE request_id = $1 AND idx = $2",
                request_id, idx,
            )
            if updated == "UPDATE 0":
                raise AngleGateError(f"No angle option idx={idx} for request_id={request_id}")
            # AA-494 prerequisite fix (PR #253) — the other 2 options were never unset. Was
            # harmless while the status guard blocked calling this twice; now that AA-497's
            # 'reusable' reopen makes a second choose_angle() call real and reachable, this
            # explicit unset is what makes T9 (services/acp_content_writing/service.py::
            # start_write(), reads angles where chosen=true) pick up the LATEST choice instead
            # of silently reading the first chosen=true row by idx.
            await conn.execute(
                "UPDATE acp_shared.angle_gate_option SET chosen = false "
                "WHERE request_id = $1 AND idx != $2",
                request_id, idx,
            )
            await conn.execute(
                "UPDATE acp_shared.angle_gate_request SET status = 'approved', updated_at = now() "
                "WHERE request_id = $1",
                request_id,
            )
    # AA-448's own live-verify lesson (finalize response showed stale approved=false because the
    # in-memory object was never re-read after the DB write) — re-fetch fresh from the DB here
    # instead of hand-mutating an in-memory dict, so this can't repeat that bug class.
    return await fetch_request(tenant_id, request_id, pool)


async def fetch_request(tenant_id: UUID, request_id: UUID, pool) -> dict:
    req = await _fetch_request_row(tenant_id, request_id, pool)
    async with pool.acquire() as conn:
        option_rows = await conn.fetch(
            """
            SELECT option_id, idx, name, why_it_works, formula_fit, best_final_style,
                   recommended, chosen, answers, violations
            FROM acp_shared.angle_gate_option
            WHERE request_id = $1
            ORDER BY idx
            """,
            request_id,
        )
        # AA-512 — fixed header data (Subject + Channel, "không sửa được ở đây"): joined live,
        # same LEFT JOIN/stale-but-harmless convention services/acp_shared/slate.py::fetch_slate()
        # already documents for a Segment/Route that's since been rebuilt away. NULL subject_id
        # (the legacy atom-picker path) simply returns no row — every field below stays None.
        subject_row = None
        if req["subject_id"] is not None:
            subject_row = await conn.fetchrow(
                """
                SELECT s.score, asg.canonical_place, asg.canonical_action, r.hub_name
                FROM acp_shared.subject s
                LEFT JOIN acp_contract.atom_segment asg ON asg.segment_id = s.segment_id
                LEFT JOIN acp_contract.route r ON r.route_id = s.route_id
                WHERE s.subject_id = $1
                """,
                req["subject_id"],
            )
    # AA-501 — dfs_paa_snapshot (migration 127) arrives as a raw JSON string (no jsonb codec
    # registered on this app's connections, same gap admin_a4.py's _parse_jsonb already works
    # around for gate_ledger/escalate_detail) — parse defensively, NULL stays None.
    snapshot = req["dfs_paa_snapshot"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)

    # AA-511 Gap A (migration 134) — arrives as a raw JSON string, same no-jsonb-codec gap
    # dfs_paa_snapshot already works around above. NULL for a Segment pick / pre-Slate request.
    route_segment_ids = req["route_segment_ids"]
    if isinstance(route_segment_ids, str):
        route_segment_ids = json.loads(route_segment_ids)

    def _parse_option(o) -> dict:
        d = dict(o)
        # AA-512 — same no-jsonb-codec gap as dfs_paa_snapshot/route_segment_ids above. NULL
        # (never ranked — see migration 135's header) stays None, not [].
        for key in ("answers", "violations"):
            if isinstance(d[key], str):
                d[key] = json.loads(d[key])
        return d

    return {
        "request_id": str(req["request_id"]),
        "tenant_id": str(req["tenant_id"]),
        "atom_id": req["atom_id"],
        "trip_id": str(req["trip_id"]) if req["trip_id"] else None,
        "channel": req["channel"],
        "goal": req["goal"],
        "cta": req["cta"],
        "status": req["status"],
        "dfs_paa_snapshot": snapshot,
        "route_segment_ids": route_segment_ids,
        "created_at": req["created_at"].isoformat(),
        "updated_at": req["updated_at"].isoformat(),
        "angles": [_parse_option(o) for o in option_rows],
        # AA-512 — fixed header (Subject + Channel, not editable here). All None for the legacy
        # atom-picker path (no subject_id) or a Subject whose Segment/Route was rebuilt away
        # since picking (LEFT JOIN, see this function's own comment above).
        "subject_id": str(req["subject_id"]) if req["subject_id"] else None,
        "subject_score": float(subject_row["score"]) if subject_row and subject_row["score"] is not None else None,
        "subject_place": subject_row["canonical_place"] if subject_row else None,
        "subject_action": subject_row["canonical_action"] if subject_row else None,
        "subject_hub_name": subject_row["hub_name"] if subject_row else None,
    }


__all__ = [
    "AngleGateError", "AtomNotFoundError", "RequestNotFoundError", "InvalidGoalError",
    "WrongStatusError", "set_goal_and_generate", "choose_angle", "reopen_request", "fetch_request",
]
