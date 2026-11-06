# AA-741 — S271 phase-1 finding: Blog topic starvation (T7 Slate)

Lane l6 of S271 (with the T9 blog token fix, notes `AA-741-l6-t9-tokens.md`). Phase-1 audit on 4 synthetic audit
tenants (06/11/2026). The TL-11 freeze does not cover the T7 Slate.

## Finding
Each agency got one or two Blog Subjects ever. `CHANNEL_BARS['blog']['grain'] = 'route'`: the Blog pool was the
current Routes of the tenant's tours (`acp_contract.route`, one or two per tour), while every other Channel proposes
Segments. `idx_subject_unique_route` (migration 133) and the `ON CONFLICT ... DO UPDATE ... WHERE state = 'proposed'`
upsert mean a Route the tenant used never comes back, so once a tour's one or two Routes were written the Blog lane was
empty. The origin (aa-social-media `stages/slate.py`, `_walk()`; `reference/channels.toml` `takes_route = true`) never
had a Route Subject: a Blog Subject is a Segment (a moment) that carries its Route as the `walk` material. Route grain
was AA-CIS's own deviation (AA-511 notes, STEP0 Q2).

## Decisions (option A)
- Blog is Segment grain again, with the same Bar (demand >= 1,000, questions >= 3). Two new keys on every
  `CHANNEL_BARS` row, the origin's vocabulary: `takes_route` (Blog only) and `one_piece_per_place` (Blog only).
- The Bar reads the Segment's **best market**: `demand` / `questions` of the market with the lowest `total_rank`
  (ties: market code), travelling together, the rule Route candidates already used. Before, a Segment candidate's
  demand and questions were each the MIN over the tenant's markets; only Blog reads them, and a US + UK tenant would
  have needed 1,000 searches in its weaker market. `total_rank` and `said` stay MIN (the social Bar reads `said`).
- Place rule unchanged and now applies to Blog (one place once per Channel, `MAX_CHANNELS_PER_PLACE`).
- **Held places** (`one_piece_per_place`): a place the tenant already picked / used / cut on Blog is not proposed on
  Blog again under any Segment id (stored id, the live id it merged into, and both places). Such a candidate is
  skipped before the place rule, so it takes no seat and no cross-Channel slot.
- **The walk at pick time** (`_segment_walk`): the current Routes on the tenant's tours that hold the Segment (live id
  or any id merged into it, recursive over `atom_segment_alias`). The Route must be on the request's trip, because
  `route_guard` keeps a Route Segment only when the trip has a live member in it. So: Routes on the seed atom's tour;
  when that tour has none, a member of the **same match grade** on a tour that has one (never a looser member); each
  Route read through `trip_route_segment_ids`, kept only with the Segment on it and >= 2 Segments; longest wins, then
  earliest day, then id. No Route = single-place piece (`route_segment_ids` NULL), the old Segment-pick shape.
- T8 / T9 / admin regenerate need no change: they already read any request's `route_segment_ids` through
  `route_guard` (`subject_id` points at a Segment Subject, `route_id` NULL, so regenerate copies the guarded snapshot).
- No migration: the existing indexes and columns serve Segment Subjects on Blog.

## Changed
- `services/acp_shared/slate.py`: `CHANNEL_BARS` (blog `grain: segment`, `takes_route`, `one_piece_per_place`);
  `_SEGMENT_CANDIDATES_SQL` (best-market numbers); `HeldSubjects`, `_place_key`, `_HELD_SUBJECTS_SQL`,
  `_fetch_held_subjects`; `_choose_slate(held=...)`; `propose_slate` reads the holds; `_resolve_representative_atom(...,
  channel=)`; `_SEGMENT_ROUTES_SQL`, `_segment_walk`; docstrings.
- `CONTEXT.md`: Route, Subject, Slate step 3.
- Tests: `tests/unit/test_aa741_s271_blog_segment_grain.py` (20), `tests/integration/test_aa741_s271_blog_slate_sql.py`
  (3, incl. the walk read as `aa_app_user`); `test_aa511_slate.py` / `test_aa840_slate_channels.py` updated to the new
  grain (Blog takes Segments; the Route-grain rule is still checked with a patched Channel).

## Tradeoffs
- The origin ranks walks by Route score (mean `total_rank`). `atom_ranking` has no `aa_app_user` grant and the pick
  runs on the tenant pool (`SUBJECT_DECIDE_FLAG`), so the walk is chosen by guarded length instead. A tour's Routes are
  disjoint day spans, so a Segment sits on one Route per tour in nearly every case; the length only breaks rare ties.
- Held places compare `canonical_place` case- and space-insensitively, not through place cards: two spellings of one
  place can still both get a Blog. Kept simple; the per-Channel place rule has the same limit.
- Places walked by an old Route-grain Blog Subject are not held: that piece anchored on one moment and named the rest
  in passing, and holding up to five places per old Route would starve Blog again. A new Blog on one of those places
  may walk the same Route with another focus.
- T8 picks the angle's anchor from the whole walk (the digest's ANCHOR CANDIDATES), not necessarily the Subject's place.
  Same as a Route pick today; a "focus on the picked place" line in the T8 digest is a follow-up (prompt change, needs
  its own A/B).
- Still-`proposed` Route Subjects on Blog are deleted by the next proposal (no Blog Route is chosen any more) and
  replaced by Segment Subjects; picked / used ones stay and keep working.

## Should know
- Expect each tenant's Blog lane to grow from 1-2 to (number of Bar-clearing places, minus the place rule): the
  proposal no longer fetches Routes or runs Debate on them (one fewer Debate pass per proposal).
- `cleared_bar_reason.demand` / `questions` of every Segment Subject now show best-market numbers (display only for
  the non-Blog Channels: their Bar does not read them).
- Live check after deploy: refresh the Slate of an audit tenant (`POST /v1/slate/refresh`), then `GET /v1/slate`:
  `blog.subjects[*].segment_id` set, `route_id` null, more than 2 proposed; pick one and read its
  `angle_gate_request.route_segment_ids` (>= 2 ids, or null for a place on no Route); logs `slate_walk_resolved`.
