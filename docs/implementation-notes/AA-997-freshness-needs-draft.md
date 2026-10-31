# AA-997 / AA-991 follow-up (S262 lane w1d) — `needs_draft` card rows and the free carry-over

The Infra DB Freshness Probe (AA-991) was red four nights in a row: `place_card_links_missing` = 111 active place
atoms in 12 tours. A read-only diagnosis (31/10/2026, role `cis_app`) found that all 111 were groups that the $0
relink after `a3_atomize` (AA-989, `max_places=0`) cut as `over_cap`. Those atoms were left with no
`atom_place_card` row at all, so the probe read "waiting for a paid draft" as "link lost". 5 of them had a replaced
predecessor of the same source linked to exactly one card. Infra companion PR: `fix/aa-991-probe-needs-draft`.

## Decisions

- **Fix A: record the cut groups.** In `ensure_place_cards`, the groups past `max_places` get the atom-level row
  through the existing `_ATOM_MISS_SQL`, with `miss_reason = 'needs_draft'` (`place_cards.NEEDS_DRAFT`) and
  `card_id` NULL. This happens for the relink (`max_places=0`) and for a `place_cards_build` over its 40-place cap.
  New counter `needs_draft_recorded` (atoms).
- **`needs_draft` is a marker, not a miss.** No `place_card_miss` row is written and the value is not in
  `MISS_RETRY`. The next `ensure_place_cards` run still treats the atom as unlinked, because the `linked` read joins
  `place_card`, and no stored miss blocks the group. So `place_cards_build` and T8 draft it. A draft replaces the row:
  `_LINK_SQL` on success, `_record_miss` (a different reason) on a miss. A later relink that finds a stored miss
  replaces the marker with that reason (the AA-989 cached-miss path).
- **Fix B: free carry-over in `relink_tour_place_cards`.** It runs after `ensure_place_cards`. It reads the tour's
  atoms that still hold a `needs_draft` row, and the non-rejected cards of the replaced atoms of the same source
  (`COALESCE(raw_tours.source_group_id, tour_id)`, atoms not in `v_active_tour_atoms`). The pure
  `plan_carry_over` uses the atom_repoint narrowing:
  1. Same `place_norm()`. If that gives several cards, keep only the same itinerary day.
  2. If no predecessor has the same name, the same day and the same `core_key()`. This catches respellings
     ("X Pass (3550m)" after "X Pass") and generic names linked by an earlier draft.

  The atom is linked only when exactly one distinct card is left. New counter `carried_over`.
- **No alias row on a carry-over.** The evidence is this tour's own history, not a name that is safe as a
  country-wide alias. A paid draft still adds aliases as before.
- **The carry-over soft-fails.** An error leaves the `needs_draft` rows and sets `carried_over = "failed: <Type>"`.
  The relink counters stay intact, and atomize never fails because of it (the handler already catches a relink
  failure).
- **Data coverage.** The `CARD` query has a new field `awaiting_draft`, and there is a new info row
  `card.awaiting_draft` ("Place atoms waiting for a paid card draft"). `card.no_row` keeps its meaning: an atom with
  no row at all.

## Changed

- `services/acp_shared/place_cards.py`: `NEEDS_DRAFT`, the cut rows in `ensure_place_cards`, `_WAITING_ATOMS_SQL`,
  `_REPLACED_CARDS_SQL`, `plan_carry_over`, `_carry_over`, and the relink docstring and counters.
- `api/services/data_coverage/queries.py` and `registry.py`: `awaiting_draft`.
- Tests:
  - `tests/unit/test_aa988_989_follow.py`: the AA-989 test now expects the `needs_draft` row. New tests cover the
    carry-over (respelling and generic name), no guess between two cards, the soft-fail, and the narrowing table.
  - `tests/unit/test_aa772_place_card_cost.py`: a build over the 40 cap records `needs_draft` and the next build
    drafts exactly those atoms. A `needs_draft` atom is drafted into a link.
  - `tests/unit/test_aa992_data_coverage.py`: the `awaiting_draft` count.
- `CONTEXT.md`: the relink paragraph and the Place Card paragraph.

## Tradeoffs

- **No migration.** `atom_place_card.miss_reason` has no CHECK constraint (only `place_card_miss.reason` has one,
  migration 221), so no migration is needed. The column COMMENT from 221 still lists the old reasons. Updating it
  would need a migration number, and lane A owns the CIS numbers this wave. It is left for a later migration.
- **Carry-over before or after the free pass.** It runs after, and only on `needs_draft` atoms, so an alias or
  core-name match always wins over the history guess. The cost is two writes for a carried atom (the marker, then the
  link), about 5 atoms on Dev.
- **The carry-over is per atom, not per group.** Two atoms of one group on different days can end up in different
  states. That is the same per-atom rule atom_repoint uses.
- **Known edge case.** A generic name on a tour whose days shifted, with two predecessor cards, narrows to the same
  day number. That can pick the other city's card. atom_repoint has the same limit. Tours with one such card are
  unaffected.

## Should know

- `needs_draft_recorded` also counts the atoms that the carry-over then links, so read both counters.
- Repair after deploy: run `relink.py APPLY=1` (orchestrator, $0) on the 12 tours. Expected probe result:
  `place_card_links_missing` 0 once the Infra PR is deployed, and `place_card_needs_draft` ≈ 106
  (111 minus the ~5 carried over).
- The probe (Infra) excludes `needs_draft` rows from `place_card_links_missing` and adds an info metric for them. A
  relink that did not run still shows as missing.
