-- Migration 181: AA-660 — Jev (TypeSafe) decision layer: questions, ledger, tenant allow-list.
--
-- Design: docs/architecture/at-series-v2-design.md §3 (S203, 29/09/2026). Nghiệp decided Jev
-- ENFORCES (not shadow-only) in the single clean rerun, with thresholds we calibrate ourselves.
--
-- shared.decision_question   — one row per question a stage asks Jev. The question wording, its mode
--                              (off / shadow / enforce) and its two floors live here, editable in
--                              admin. `enforce` needs a calibration record (CHECK below).
-- shared.decision_log        — one row per question per subject, every probability kept, so floors
--                              can move later without paying Jev again.
-- shared.jev_tenant_allowlist— tenants whose content may be sent to TypeSafe before the DPA/ZDR is
--                              confirmed (design C2). Seeded with the AA-owned test tenants.
-- llm_model_catalog          — api_style 'decide' + the `jev-latest` row ($0.042 / 1M input, output free).
--
-- Additive. Apply before the deploy that ships shared/llm_client/decide.py (the seam reads these
-- tables; with them missing it logs zone='error' and callers keep their existing rule).

BEGIN;

CREATE TABLE IF NOT EXISTS shared.decision_question (
    question_key      TEXT PRIMARY KEY,
    stage             TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('noul', 'choice', 'score')),
    instructions      TEXT NOT NULL,
    criteria          JSONB,
    mode              TEXT NOT NULL DEFAULT 'shadow' CHECK (mode IN ('off', 'shadow', 'enforce')),
    -- noul: p >= accept_floor -> accept; p <= reject_ceiling -> reject; else grey.
    -- choice/score: confidence >= accept_floor -> accept (the pick is trusted); else grey.
    accept_floor      NUMERIC(5, 4) CHECK (accept_floor IS NULL OR accept_floor BETWEEN 0 AND 1),
    reject_ceiling    NUMERIC(5, 4) CHECK (reject_ceiling IS NULL OR reject_ceiling BETWEEN 0 AND 1),
    threshold_version INTEGER NOT NULL DEFAULT 0,
    calibration_ref   TEXT,
    notes             TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by        TEXT NOT NULL DEFAULT 'migration-181',
    CONSTRAINT decision_question_floors_ordered
        CHECK (accept_floor IS NULL OR reject_ceiling IS NULL OR reject_ceiling < accept_floor),
    CONSTRAINT decision_question_enforce_needs_calibration
        CHECK (mode <> 'enforce'
               OR (calibration_ref IS NOT NULL AND (accept_floor IS NOT NULL OR reject_ceiling IS NOT NULL)))
);

COMMENT ON TABLE shared.decision_question IS
    'AA-660 — questions stages ask Jev, with mode and calibrated floors. Owned by AA-CIS-App.';

CREATE TABLE IF NOT EXISTS shared.decision_log (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    stage             TEXT NOT NULL,
    question_key      TEXT NOT NULL,
    subject_key       TEXT NOT NULL,
    tenant_id         UUID,
    job_id            UUID,
    mode              TEXT NOT NULL CHECK (mode IN ('off', 'shadow', 'enforce')),
    zone              TEXT NOT NULL CHECK (zone IN ('accept', 'grey', 'reject', 'error', 'skipped')),
    probability       NUMERIC(6, 5),
    choice            TEXT,
    probabilities     JSONB,
    threshold_version INTEGER,
    model             TEXT,
    latency_ms        INTEGER,
    cost_usd          NUMERIC(12, 8),
    error             TEXT,
    outcome           TEXT
);

CREATE INDEX IF NOT EXISTS decision_log_stage_created_idx ON shared.decision_log (stage, created_at DESC);
CREATE INDEX IF NOT EXISTS decision_log_question_created_idx ON shared.decision_log (question_key, created_at DESC);
CREATE INDEX IF NOT EXISTS decision_log_subject_idx ON shared.decision_log (subject_key);

COMMENT ON TABLE shared.decision_log IS
    'AA-660 — every Jev verdict (probabilities kept). `outcome` = what the calling stage then did.';

CREATE TABLE IF NOT EXISTS shared.jev_tenant_allowlist (
    tenant_id  UUID PRIMARY KEY REFERENCES shared.tenants (tenant_id),
    reason     TEXT NOT NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_by   TEXT NOT NULL DEFAULT 'migration-181'
);

COMMENT ON TABLE shared.jev_tenant_allowlist IS
    'AA-660 — tenants whose content may go to TypeSafe before the DPA/ZDR (design C2). Platform '
    '(aa_internal) content is always allowed and is not listed here.';

INSERT INTO shared.jev_tenant_allowlist (tenant_id, reason)
SELECT tenant_id, 'AA-owned test tenant (S203)'
FROM shared.tenants
WHERE slug IN ('wanderlux-travel', 'exploreasia-co', 'test-1')
ON CONFLICT (tenant_id) DO NOTHING;

-- Catalog: allow api_style 'decide' (CHECK from 169 has a generated name; find it by definition).
DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT conname FROM pg_constraint
        WHERE contype = 'c' AND conrelid = 'shared.llm_model_catalog'::regclass
          AND pg_get_constraintdef(oid) LIKE '%api_style%anthropic_native%'
    LOOP
        EXECUTE format('ALTER TABLE shared.llm_model_catalog DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;

ALTER TABLE shared.llm_model_catalog
    ADD CONSTRAINT llm_model_catalog_api_style_check
        CHECK (api_style IN ('anthropic_native', 'converse', 'openai_chat', 'embed', 'decide'));

INSERT INTO shared.llm_model_catalog
    (model_key, label, vendor, provider, api_style, wire_model, callable_via, supports_temperature,
     price_in_per_mtok, price_out_per_mtok, price_source, enabled, notes, updated_by)
VALUES
    ('jev-latest', 'Jev (TypeSafe System One)', 'typesafe', 'typesafe', 'decide', 'jev-latest',
     '{decide}', false, 0.042, 0, 'typesafe.ai pricing (AA-634, 24/09/2026): input only, output free',
     true, 'Called only through shared/llm_client/decide.py (AA-660).', 'migration-181')
ON CONFLICT (model_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('181', now(), 'AA-660: Jev decision layer — decision_question, decision_log, tenant allow-list, jev-latest catalog row')
ON CONFLICT (version) DO NOTHING;

COMMIT;
