-- Migration 161: AA-610 (Sub 2) — acp_contract.atom_embedding + acp_contract.atom_matches.
--
-- `questions` (score.py's 3rd rank-sum axis, ported to atom_ranking.py's `compute_questions()`)
-- currently uses word-overlap claim-by-name — the SAME test `compute_demand()` uses — because
-- this codebase had no embedding-match infrastructure (atom_ranking.py's own disclosed
-- adaptation, item 1). This migration adds that infrastructure so a future build can land PAA
-- questions on a Segment by nearest-vector match instead, matching Ms. Thư's own `_questions()`
-- mechanism (`match_queries_to_atoms()`, aa-social-media src/aa_social/matching.py, ADR-0002/
-- 0018).
--
-- pgvector already exists in this schema (migration 041, `CREATE EXTENSION IF NOT EXISTS
-- vector`) — no new extension needed. Embeddings here reuse services/acp_shared/
-- content_embedding.py's existing model (Cohere Embed v4, 1536-dim, confirmed live the only
-- embedding-capable model this account actually offers — migration 041/124's own comment
-- assumed Titan Embed, which that module's docstring found is not available at all).
--
-- Two views per atom (ADR-0018's own design, ported as-is): "place" alone, and "place + action"
-- — a query lands wherever it's closer, without blending the two into one vector. Platform-wide
-- (acp_contract.tour_atoms already is, AA-545) — atom_embedding is keyed by atom_id alone, no
-- market/tenant column, same as tour_atoms itself.
--
-- atom_matches carries `matched_by` ('vector' | 'tokens') so a Bedrock outage's fallback to the
-- existing claim-by-name test is visible in the data, not silently indistinguishable from a real
-- embedding match (content_embedding.py's own soft-fail convention: compute_embedding() returns
-- None on any failure, never raises).

BEGIN;

CREATE TABLE acp_contract.atom_embedding (
    atom_id    TEXT NOT NULL REFERENCES acp_contract.tour_atoms(atom_id),
    view       TEXT NOT NULL CHECK (view IN ('place', 'place_action')),
    embedding  VECTOR(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (atom_id, view)
);

COMMENT ON TABLE acp_contract.atom_embedding IS
    'AA-610 (Sub 2) — 2 views per atom (place alone, place+action), Cohere Embed v4 1536-dim '
    '(services/acp_shared/content_embedding.py). Ported from Ms. Thu''s Atom embedding '
    '(aa-social-media ADR-0018) — platform-wide, no market/tenant column, same as tour_atoms.';

CREATE INDEX idx_atom_embedding_vector
    ON acp_contract.atom_embedding
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

CREATE TABLE acp_contract.atom_matches (
    id          BIGSERIAL PRIMARY KEY,
    query       TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind = 'question'),
    atom_id     TEXT NOT NULL REFERENCES acp_contract.tour_atoms(atom_id),
    distance    DOUBLE PRECISION NOT NULL,
    matched_by  TEXT NOT NULL CHECK (matched_by IN ('vector', 'tokens')),
    matched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (query, atom_id)
);

COMMENT ON TABLE acp_contract.atom_matches IS
    'AA-610 (Sub 2) — which atom a PAA question landed on (nearest-vector, k=1, fanned out to '
    'every atom in the same Segment by the caller — Ms. Thu''s own match_queries_to_atoms() '
    'mechanism, aa-social-media src/aa_social/matching.py). matched_by=''tokens'' means the '
    'vector path was unavailable (Bedrock error) and this row is really a claim-by-name '
    'fallback, not a real vector match — visible in the data, not silently indistinguishable.';

CREATE INDEX idx_atom_matches_atom ON acp_contract.atom_matches(atom_id);

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('161', now(),
    'AA-610 (Sub 2): acp_contract.atom_embedding (2 views/atom, Cohere Embed v4 1536-dim) + '
    'atom_matches (PAA question -> nearest atom, vector or tokens fallback) — infrastructure '
    'for embedding-match questions, ported from Ms. Thu''s match_queries_to_atoms()')
ON CONFLICT (version) DO NOTHING;

COMMIT;
