-- Migration 160: AA-610 (Sub 1) — acp_contract.tour_atoms.evidence.
--
-- `said` (score.py's 4th rank-sum axis, ported to acp_contract.atom_ranking migration 130) is
-- currently `SUM(LENGTH(tour_atoms.text))` — near-inert, per atom_ranking.py's own disclosed
-- limitation (docstring item 2): AA-509's Decision 1 changed `text` from an LLM-written 1-2
-- sentence narrative to a terse `f"{place} — {action}"` mechanical join, so `text` length now
-- differs mostly by place/action NAME length, not by how much an itinerary elaborates on a
-- moment — the signal `said` is meant to carry.
--
-- Ms. Thư's own `Atom.evidence` (aa-social-media src/aa_social/models.py) is the fix: the exact
-- span of the day's source text the atom came from, quoted verbatim, never paraphrased
-- (stages/atoms.py's own `_checkable_evidence()` validates this). AA-CIS never ported this
-- field when T5 decompose was first built — it does not exist anywhere in this schema, so
-- there is nothing to backfill from; it is populated by T5 asking the LLM for it going
-- forward (services/acp_shared/atom_extraction.py SYSTEM_PROMPT, this build).
--
-- Nullable, no backfill — same precedent as migration 129's place/action: an atom atomized
-- before this migration keeps evidence NULL until its day is next re-atomized (the
-- SYSTEM_PROMPT change auto-invalidates every existing day_fingerprint row, so this happens on
-- the next atomize call for that day, not never). atom_ranking.py's `said` computation falls
-- back to `text` when `evidence` is NULL, so pre-migration Segments keep their (weaker, but
-- non-zero) said signal rather than dropping to 0.

BEGIN;

ALTER TABLE acp_contract.tour_atoms
    ADD COLUMN evidence TEXT NULL;

COMMENT ON COLUMN acp_contract.tour_atoms.evidence IS
    'AA-610 — the exact span of this day''s source text the atom was extracted from, quoted '
    'verbatim (never paraphrased), per Ms. Thư''s Atom.evidence (aa-social-media models.py). '
    'NULL for atoms read before this migration, until their day is next re-atomized. This is '
    'what said (acp_contract.atom_ranking, migration 130) should measure — length of text is '
    'a fallback, not the intended signal (see atom_ranking.py).';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('160', now(),
    'AA-610 (Sub 1): acp_contract.tour_atoms.evidence — verbatim source-text span per atom, '
    'ported from Ms. Thu''s Atom.evidence, fixes the near-inert said axis (SUM(LENGTH(text)) '
    'fallback kept for pre-migration atoms)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
