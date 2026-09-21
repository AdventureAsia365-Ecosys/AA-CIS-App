"""
services.acp_planning.allocator — N6 Slot Allocator (1x/month + invalidation).

Ported from aamc/planning.py's allocate_month()/D5 (aa-marketing-v2 research
build). Output: a calendar grid, each cell already filled with atom_ids +
framework + funnel_stage + CTA. Pure Python, $0 LLM.

AA-516 (03/09/2026): `allocate_month()`, `allocate_month_from_db()`, and
`allocate_and_persist_week()` — the async DB-wiring wrappers the historical fix notes below
refer to — were deleted. Slate (T7, AA-511) replaced the admin-triggered N4-N6 quarter-plan flow
they backed; `services/acp_produce/slot_runner.py` (N7, live) already documented deliberately
never calling this layer. `compute_slot_grid()` (the pure core every fix below actually lives
in) is kept, but AA-609 (17/09/2026) confirmed it has NO production caller — only unit tests
(`test_aa377_aa378_run_slot_persist.py`, `test_aa449_channel_extension.py`) exercise it. The
persistence layer (`create_weekly_produce_run()`/`persist_slot_grid()`/`fetch_due_slots()`/
`mark_slot_status()`) is kept. The historical notes below describe bugs found IN
`allocate_month()`'s calling convention at the time — kept for context
(`_deterministic_slot_id()`/`make_slot()`'s own fixes still apply unchanged inside
`compute_slot_grid()`), not because that function still exists.

Fixes applied during the port (see docs/implementation-notes/AA-301.md):
  B7 — cooldown was only applied post-publish (a.cooldown_until), so multiple
       slots for the same trip+channel within one month picked the SAME top-N
       atoms (near-duplicate content). Now tracks a used_this_month set per
       (trip_id, channel) and excludes it from the pool on every subsequent
       slot in the same allocate_month() call.
  B6  — keyword was assigned per-TRIP (all slots of a trip shared one
       keyword), causing SERP self-competition. Slot.keyword_seed is now
       derived from that specific slot's own top chosen atom — since B7
       guarantees no atom repeats within the month for the same trip+channel,
       keyword_seed is naturally distinct slot-to-slot.
  AA-379 — B6's "derived from the top chosen atom" was chosen[0].text[:60], a
       raw char-index slice of a full sentence, not a real keyword — live
       verify (AA-375) showed DataForSEO finding no data for the resulting
       gibberish and the topic being demand-rejected. keyword_seed is now
       built via seed_builder.build_seed() (the "{activity} in {country}"
       convention already proven against real DataForSEO traffic, AA-197/
       AA-251) fed with the atom's own activity_type + the trip's
       destination — see make_slot() for the coverage/tradeoff notes.

  AA-377 — slot_id was uuid.uuid4().hex[:10], a fresh id every allocate_month() call, so
       persisting a slot or retrying an allocation always produced a NEW row instead of
       resuming the one already there. make_slot() now derives slot_id deterministically
       from (tenant_id, week, trip_id, channel) via _deterministic_slot_id() — see that
       function's docstring. reactive_hold slots (no trip_id) keep a random id (see AA-377.md
       Tradeoffs — nothing meaningful to key on, and de-duplicating them isn't the bug this
       fixes).
  AA-378 — allocate_month()'s in-memory SlotGrid now has a real persistence layer:
       create_weekly_produce_run()/persist_slot_grid()/fetch_due_slots()/mark_slot_status()/
       allocate_and_persist_week() (bottom of this file) write to the new
       acp_shared.acp_v2_runs/acp_v2_slots tables (migration 096) instead of the S1-S4
       acp_shared.acp_runs table — see docs/implementation-notes/AA-377.md for why a
       separate table.

Also implements (not bug fixes, new requirements):
  - Atom floor (AA-300): a trip+channel whose live atom pool cannot cover
    its planned slots without repeating gets its slots dropped (not
    silently repeated) and a note logged — this falls out of the same
    used_this_month/empty-pool check as B7.
  - Reactive-hold slots (10% per SLOT_MIX) stay empty with a note that
    Mode-B (agency-message-fills-slot) is not yet defined — not designed
    here, per issue instruction.
  - Gate B: refuses to allocate from an unapproved QuarterPlan
    (QuarterPlanNotApprovedError).
  - phasing_out trips (raw_tours.lifecycle_stage) are only allocated slots
    in the real current calendar month — excluded from any other month's
    grid (interpretation of the issue's "N6 tháng hiện tại" trigger; the
    issue text is ambiguous on the exact mechanism, self-chosen, see AA-301
    implementation notes).
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date
from typing import Optional
from uuid import UUID

from services.seo_intelligence.seed_builder import build_seed

from .constants import FRAMEWORK_TABLE, SLOT_MIX
from .models import (AtomRecord, QuarterPlan, QuarterPlanNotApprovedError,
                     RunwayMap, Slot, SlotGrid, Trip)


def _deterministic_slot_id(
    tenant_id: UUID, year: int, month: int, week: int, trip_id: Optional[UUID], channel: str,
) -> str:
    """AA-377 fix — was uuid.uuid4().hex[:10] (a new id every allocate_month() call, so
    persisting or retrying a slot always produced a fresh row instead of resuming the same
    one). Hashes the fields that actually identify 'this slot': re-allocating the same
    (tenant, year, month, week, trip, channel) now always yields the same slot_id, which is
    what makes persist_slot_grid()'s `ON CONFLICT (slot_id) DO NOTHING` an idempotent no-op on
    retry instead of a duplicate row / duplicate content. Not used for reactive_hold slots
    (trip_id is always None there) — see module docstring / AA-377.md Tradeoffs.

    AA-410 fix — `year` and `month` were BOTH missing from this hash. `year` was missing from
    day one: the original AA-377 spec (migration 096 header comment, AA-377.md Decision #3)
    documented `sha256(tenant_id|year|week|tour_id|channel)`, but the shipped code never
    included it. `month` was never in the spec at all because acp_v2_runs/acp_v2_slots had no
    `month` column until this migration (103) — without it, the same (tenant, week, trip,
    channel) in two different calendar months collided onto ONE slot_id, so
    `ON CONFLICT (slot_id) DO NOTHING` would silently drop the second month's slot instead of
    creating it (AA-410's actual live-blocking symptom, plus a time-bomb for any future month
    once the run-level UNIQUE constraint below stopped masking it)."""
    raw = f"{tenant_id}|{year}|{month}|{week}|{trip_id}|{channel}"
    return f"slot_{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _add_note(notes: list[str], message: str) -> None:
    """Dedupe — the evergreen round-robin can retry an exhausted
    destination/channel many times in one call (matches the original
    aamc design: 'atoms exhausted — grid stays short, honestly'), which
    would otherwise repeat the same log line dozens of times."""
    if message not in notes:
        notes.append(message)


def _eligible_atoms(atoms: list[AtomRecord], channel: str, used_this_month: set[str],
                     today: date) -> list[AtomRecord]:
    pool = []
    for a in atoms:
        if a.deleted or a.atom_id in used_this_month:
            continue
        cd = a.cooldown_until.get(channel)
        if cd and cd > today.isoformat():
            continue
        # AA-609: the old `starred` 1.5x boost was removed — "star" was a curation flag with no
        # live effect (the only reader was here, and compute_slot_grid() has no production caller;
        # the repo now ranks by deterministic score, not a human star). Weight + distinctiveness
        # only.
        w = a.weight * {"HIGH": 1.5, "MED": 1.0, "LOW": 0.6}[a.distinctiveness]
        pool.append((w, a))
    return [a for _, a in sorted(pool, key=lambda x: -x[0])]


def compute_slot_grid(
    tenant_id: UUID, year: int, month: int, channels: list[str],
    capacity_posts_per_week: int, quarter_plan: QuarterPlan, runway: RunwayMap,
    trips_by_id: dict[UUID, Trip], atoms_by_trip: dict[UUID, list[AtomRecord]],
    primary_market: str, today: Optional[date] = None,
) -> SlotGrid:
    """Pure computation — no DB, no LLM, 100% unit-testable."""
    if not quarter_plan.approved:
        # AA-448 Gate B Option A: a tenant's own quarter plan auto-approves the instant they
        # finalize it (services/acp_planning/quarter.py::save_quarter_plan_version() ->
        # approve_quarter_plan_version(), called back-to-back by v1_planning.py's finalize
        # endpoint) — no human ever clicks approve for a tenant plan. This check/exception TYPE
        # still means exactly what it always meant: "no plan has been created+finalized yet for
        # this tenant/year/quarter" — it just never blocks on a pending-awaiting-a-human state
        # anymore. See docs/implementation-notes/AA-448-t7-content-planning.md "STOP point" for
        # the full reasoning (admin_atoms.py's preview-slotgrid demo path and
        # admin_produce.py's real N7 trigger both still depend on this exact check/type; do not
        # remove it, only its old wording changed).
        raise QuarterPlanNotApprovedError(
            "Gate B: no quarter plan has been finalized yet for this tenant/quarter — allocation refused.")
    if quarter_plan.tenant_id != tenant_id:
        raise ValueError("quarter_plan.tenant_id does not match tenant_id — refusing cross-tenant allocation.")

    today = today or date.today()
    is_current_month = (year, month) == (today.year, today.month)

    weeks = [1, 2, 3, 4]
    total_slots = capacity_posts_per_week * len(weeks)
    n_hold = max(1, round(total_slots * SLOT_MIX["reactive_held_empty"]))
    n_campaign = round(total_slots * SLOT_MIX["campaign"]) if quarter_plan.forced_specials else 0
    n_evergreen = total_slots - n_hold - n_campaign

    notes: list[str] = []
    used_this_month: dict[tuple[UUID, str], set[str]] = {}

    trips_by_dest: dict[str, list[UUID]] = {}
    excluded_phasing = []
    for tid in quarter_plan.trip_ids:
        t = trips_by_id.get(tid)
        if t is None:
            continue
        if t.lifecycle_stage == "phasing_out" and not is_current_month:
            excluded_phasing.append(t.name)
            continue
        trips_by_dest.setdefault(t.destination or t.name, []).append(tid)
    if excluded_phasing:
        notes.append(
            f"Excluded phasing_out trips from {year}-{month:02d} (not the current month): {excluded_phasing}")

    share_order = sorted(quarter_plan.destination_shares.items(), key=lambda x: -x[1])
    dest_cycle = [d for d, _ in share_order if d in trips_by_dest] or list(trips_by_dest)

    grid = SlotGrid(tenant_id=tenant_id, year=year, month=month, trips_hash=quarter_plan.trips_hash)
    slot_n = 0
    campaign_trip_ids = quarter_plan.forced_specials or quarter_plan.trip_ids[:1]

    def make_slot(kind: str, trip_id: Optional[UUID]) -> Optional[Slot]:
        nonlocal slot_n
        week = weeks[slot_n % len(weeks)]
        channel = channels[slot_n % len(channels)]
        slot_n += 1
        if kind == "reactive_hold":
            return Slot(
                slot_id=f"slot_{uuid.uuid4().hex[:10]}", week=week, channel=channel,
                kind="reactive_hold", funnel_stage="TOFU",
                topic_hint="HELD EMPTY for reactive content (Mode-B process not yet defined)")
        t = trips_by_id.get(trip_id)
        if t is None:
            return None
        dest = t.destination or t.name
        stage = runway.stage(dest, primary_market, month)
        if stage == "OFF":
            stage = "TOFU"  # off-window content still captures; never converts
        atoms = atoms_by_trip.get(trip_id, [])
        key = (trip_id, channel)
        used = used_this_month.setdefault(key, set())
        pool = _eligible_atoms(atoms, channel, used, today)
        # AA-449 — extended from ("facebook","tiktok") to also cover the 3 new short-form/
        # single-focus channels (linkedin, instagram, ads) added for T8's channel extension
        # (STEP0 §5). Self-chosen from Bang 2's own "Structure" column: linkedin/instagram/ads
        # are all single-idea/single-hook formats there (LinkedIn: "1 insight rõ"; Instagram:
        # "3-5 chi tiết cụ thể" inside one short caption, not 4 separate atoms; Ads: "1 hook...1
        # benefit...1 CTA") — same shape as facebook/tiktok's existing 1-atom treatment.
        # landing_page stays in the multi-atom (up to 4) group with blog/email — Bang 2's own
        # structure for it is explicitly multi-section (value prop / audience / why / what AA
        # does / trust signal / CTA), the same "long-form, several ideas" shape blog/email
        # already get 4 atoms for.
        n_atoms = 1 if channel in ("facebook", "tiktok", "linkedin", "instagram", "ads") else min(4, len(pool))
        if not pool:
            _add_note(
                notes,
                f"Trip '{t.name}' has no eligible atoms left for {channel} this month "
                f"(atom floor reached or all on cooldown) — slot dropped, not repeated.")
            return None
        live_count = sum(1 for a in atoms if not a.deleted)
        if live_count < 2 * max(1, n_atoms):
            _add_note(
                notes,
                f"Trip '{t.name}' atom floor: {live_count} live atoms < 2x{n_atoms} planned "
                f"slots for {channel} — capacity implicitly reduced, no silent atom repeat.")
        chosen = pool[:n_atoms]
        used.update(a.atom_id for a in chosen)
        fw_key = (stage, "blog") if channel == "blog" else ("ANY", channel)
        fw = FRAMEWORK_TABLE.get(fw_key, {"framework": "hub"})["framework"]
        cta = t.trip_url if t.url_alive else None
        top_atom = chosen[0]
        # AA-379 — keyword_seed was chosen[0].text[:60], an arbitrary char-index slice of a
        # full sentence (not a real keyword phrase); DataForSEO's exact-match search-volume
        # lookup legitimately found no data for it, and C3's demand-law gate then rejected the
        # topic. Reuses seed_builder.build_seed() — the one proven "{activity} in {country}"
        # convention already validated against real DataForSEO traffic (AA-197/AA-251) — fed
        # with the atom's own activity_type (the decompose LLM contract's required enum,
        # verified 100%-filled live for every atom that can actually reach a slot) instead of
        # raw_tours.activities. Falls back to build_seed()'s own tour_name+destination chain
        # when activity_type is absent (legacy atoms). Known tradeoff: activity_type is only 7
        # coarse buckets, so two slots of the same trip that land on the same activity_type in
        # the same month can still produce an identical keyword_seed — narrower than pre-B6's
        # trip-wide sharing, but not zero. Flagged for Nghiep, not solved here.
        keyword_seed = build_seed(
            country_raw=t.destination or "",
            activities=[top_atom.activity_type] if top_atom.activity_type else None,
            tour_name=t.name,
        ) or None
        return Slot(
            slot_id=_deterministic_slot_id(tenant_id, year, month, week, trip_id, channel),
            week=week, channel=channel, kind=kind,
            trip_id=trip_id, atom_ids=[a.atom_id for a in chosen],
            funnel_stage=stage, framework=fw, cta_target=cta,
            topic_hint=top_atom.text[:80],
            keyword_seed=keyword_seed,
        )

    i = 0
    guard = total_slots * 4
    while (sum(1 for s in grid.slots if s.kind == "evergreen") < n_evergreen
           and dest_cycle and i < guard):
        dest = dest_cycle[i % len(dest_cycle)]
        tids = trips_by_dest.get(dest, [])
        if tids:
            s = make_slot("evergreen", tids[i % len(tids)])
            if s:
                grid.slots.append(s)
        i += 1

    for j in range(n_campaign):
        s = make_slot("campaign", campaign_trip_ids[j % len(campaign_trip_ids)])
        if s:
            grid.slots.append(s)

    for _ in range(n_hold):
        s = make_slot("reactive_hold", None)
        if s:
            grid.slots.append(s)

    if notes:
        grid.capacity_note = " | ".join(notes)
    return grid


# AA-603 (21/09/2026) — the acp_shared.acp_v2_runs/acp_v2_slots persistence layer
# (create_weekly_produce_run / persist_slot_grid / fetch_due_slots / mark_slot_status, plus the
# _row_to_slot helper) was REMOVED. Its only caller was the deleted N7 slot_runner.py, and both
# backing tables are being dropped (dead N7/N8 produce flow, replaced by T-series). compute_slot_grid()
# (the pure core above) is kept — still exercised by unit tests, no DB dependency.


__all__ = [
    "compute_slot_grid",
]
