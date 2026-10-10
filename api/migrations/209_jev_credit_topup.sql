-- Migration 209: AA-756 — record Jev (TypeSafe) credit top-ups so admins can see a rough balance.
--
-- Decision (Nghiệp, 10/10/2026, S224): TypeSafe has no balance API, and Nghiệp does not top up Jev
-- himself (Ms. Thư does, by hand). The admin alert fires only when credit is exhausted (402
-- billing_error, AA-720) — there is no "running low" alert. This table is the one place the manual
-- top-ups are recorded, so Settings can show an estimated balance:
--     estimated_left = sum(amount_usd) − Jev spend since the first top-up
-- where Jev spend = sum(cost_usd) of shared.llm_call_log where provider = 'typesafe' and
-- created_at >= the earliest top-up date (it includes the reconcile_s224 backfill rows).
--
-- Additive. Apply before the deploy that ships GET/POST/DELETE /admin/decisions/jev-credit; with
-- the table missing the GET returns a clean empty state (the FE handles the 404).

BEGIN;

CREATE TABLE IF NOT EXISTS shared.jev_credit_topup (
    id            BIGSERIAL PRIMARY KEY,
    topped_up_on  DATE          NOT NULL,
    amount_usd    NUMERIC(10, 2) NOT NULL CHECK (amount_usd > 0),
    note          TEXT,
    created_by    TEXT,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now()
);

COMMENT ON TABLE shared.jev_credit_topup IS
    'AA-756: manual Jev (TypeSafe) credit top-ups recorded by admins; estimated balance = '
    'sum(amount_usd) − typesafe spend in shared.llm_call_log since the first top-up.';

CREATE INDEX IF NOT EXISTS idx_jev_credit_topup_date
    ON shared.jev_credit_topup (topped_up_on DESC);

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('209', now(),
    'AA-756: shared.jev_credit_topup — manual Jev credit top-ups for the Settings estimated-balance card')
ON CONFLICT (version) DO NOTHING;

COMMIT;
