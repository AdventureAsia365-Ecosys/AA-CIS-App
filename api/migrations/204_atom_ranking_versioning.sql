-- Migration 204: AA-734 — acp_contract.atom_ranking gains version/superseded_at (versioned-swap),
-- so a recompute never dips the live row count. Mirrors acp_contract.route (migration 144, AA-532).
--
-- Problem: run_atom_ranking(market, pool) rebuilds the table one market at a time with
-- `DELETE FROM atom_ranking WHERE market = $1` immediately followed by an INSERT, inside one
-- transaction PER market (6 markets = 6 transactions). Between the DELETE and the INSERT of a
-- market, a concurrent reader sees that market's rows gone — the headline Score count on
-- /admin/overview (SELECT count(*) FROM atom_ranking) dips and recovers 6 times during a full
-- recompute. route already avoids this with a superseded_at current-pointer (AA-532).
--
-- Fix (AA-734, phuong an 1 — Nghiep chot S214): give atom_ranking the SAME current-pointer model.
-- run_atom_ranking writes the new market rows at version N+1 (superseded_at NULL) and, in the SAME
-- transaction, marks the market's previous current rows superseded — so a reader filtering
-- `superseded_at IS NULL` always sees exactly one complete set for every market, never a gap. The
-- writer then deletes the just-superseded rows AFTER the swap commits (outside the swap txn, so it
-- can never reintroduce a dip): unlike route (whose superseded rows must live forever to keep
-- acp_shared.subject.route_id's FK resolving), atom_ranking has NO downstream FK into it
-- ("derived, never accumulated" — run_atom_ranking docstring), so retaining history here would only
-- bloat a ~10k-row table for no consumer.
--
-- Unlike route, each atom_ranking identity is per-market: the swap is scoped `WHERE market = $1`, so
-- the version counter and the "one current row" invariant are per (market, tour_id, segment_id).

BEGIN;

ALTER TABLE acp_contract.atom_ranking
    ADD COLUMN version       INT NOT NULL DEFAULT 1,
    ADD COLUMN superseded_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN acp_contract.atom_ranking.version IS
    'AA-734 — 1 for a (market, tour_id, segment_id) identity''s first ranking pass; incremented '
    'each full recompute of that market. Part of the PK so every version ever written stays '
    'unique. Existing rows backfilled to 1 by the DEFAULT.';
COMMENT ON COLUMN acp_contract.atom_ranking.superseded_at IS
    'AA-734 — NULL means this is the CURRENT row for its (market, tour_id, segment_id) identity; a '
    'non-NULL timestamp means a later recompute of this market replaced it. Every reader meaning '
    '"the ranking as it stands now" (Slate, overview count, admin panels, v1_route_hub, '
    'route_detection''s ranked-pair read) must filter `WHERE superseded_at IS NULL`. Unlike '
    'route.superseded_at, a superseded atom_ranking row is NOT kept — the writer deletes it right '
    'after the swap commits, since nothing FKs into this table.';

-- Old PK was (market, tour_id, segment_id); a (market, tour, segment) can now have more than one
-- row at a time only transiently (new current + just-superseded, before the post-swap cleanup), so
-- version joins the key to keep it unique across that window and any future retention change.
ALTER TABLE acp_contract.atom_ranking DROP CONSTRAINT atom_ranking_pkey;
ALTER TABLE acp_contract.atom_ranking ADD PRIMARY KEY (market, tour_id, segment_id, version);

-- At most one CURRENT row per (market, tour_id, segment_id) — the invariant every reader depends on
-- (exactly one "current ranking row" per identity per market), enforced as a real constraint so a
-- bug in the writer's supersede-then-insert surfaces as a violation, not silent duplicate rows.
CREATE UNIQUE INDEX idx_atom_ranking_current_identity
    ON acp_contract.atom_ranking (market, tour_id, segment_id)
    WHERE superseded_at IS NULL;

-- Every "current rankings" reader filters (market, superseded_at IS NULL); replace the non-partial
-- idx_atom_ranking_market_tour / idx_atom_ranking_market_total_rank with partial equivalents that
-- skip superseded rows (there are only ever a few superseded rows live at a time, between a swap and
-- its cleanup, but keep the indexes partial for consistency with route's model).
DROP INDEX IF EXISTS acp_contract.idx_atom_ranking_market_tour;
DROP INDEX IF EXISTS acp_contract.idx_atom_ranking_market_total_rank;
CREATE INDEX idx_atom_ranking_market_tour_current
    ON acp_contract.atom_ranking (market, tour_id)
    WHERE superseded_at IS NULL;
CREATE INDEX idx_atom_ranking_market_total_rank_current
    ON acp_contract.atom_ranking (market, total_rank)
    WHERE superseded_at IS NULL AND excluded_reason IS NULL;

COMMENT ON TABLE acp_contract.atom_ranking IS
    'AA-545: platform-wide (dropped tenant_id). One CURRENT row per (market, tour, Segment) — a '
    'Segment gets up to 6 current rows (one per finite market). AA-734: versioned-swap '
    '(version + superseded_at) so a recompute never dips the live count; readers filter '
    'superseded_at IS NULL. Superseded rows are deleted right after each swap (no downstream FK, '
    'unlike route).';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('204', now(),
    'AA-734: acp_contract.atom_ranking.version/superseded_at — run_atom_ranking switches from '
    'DELETE+INSERT-per-market to versioned-swap (new current rows + supersede old in one txn, '
    'delete superseded after commit), fixing the headline Score count dipping during recompute')
ON CONFLICT (version) DO NOTHING;

COMMIT;
