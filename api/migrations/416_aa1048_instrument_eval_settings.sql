-- Migration 416: AA-1048 (S279 lane l22) — the limits of the claim-instrument evaluation in the platform settings
-- registry (CIS 407; tech lead S279: a threshold, limit or cap is a setting on Platform › Settings, never a code
-- literal or an env var alone).
--
-- Why. The S279 blind panel found that the R1 claim instrument over-fires (precision 0.22-0.27). Before any full R1 is
-- paid for, a new prompt version is measured on a stratified subset of the claims R1 fired on
-- (services/ground_truth/instrument_eval.py, stage adhoc_aa1048). That run needs a hard cap, the most claims it may
-- take (full run and trial) and the go / no-go bars (the share of false positives it must drop, the share of true
-- positives it must keep). They are policy, so they live here.
--
-- What:
--   1. Five rows in shared.setting_definition (seed block below, read by scripts/settings/gen_compiled_defaults.py into
--      shared/settings/compiled_defaults.json in this PR):
--        cis.spend.instrument_eval_cap_usd           usd 0.01..2 (r1.HARD_CAP_USD), default 1.5, high, maker-checker
--        cis.spend.instrument_eval_max_claims        int claims 1..200, default 50, medium, maker-checker
--        cis.spend.instrument_eval_trial_claims      int claims 1..10, default 3, medium, maker-checker
--        cis.quality.instrument_eval_fp_drop_min_pct pct 0..100, default 60, medium, maker-checker, evidence required
--        cis.quality.instrument_eval_tp_keep_min_pct pct 0..100, default 90, medium, maker-checker, evidence required
--      Seeded with enforced = false (the page badge "not enforced yet"): the code that reads them ships in the next PR
--      of this lane (MIG-4), and the settings-lint guard (tests/unit/test_s279_settings_lint.py) refuses an enforced
--      key that no code reads. A later migration sets enforced = true once that code is deployed (follow-up recorded
--      in docs/implementation-notes/AA-1048-instrument-v2.md).
--   2. Register 416.
--
-- Behaviour on apply. None for running code: no value row is seeded, nothing reads the keys until the evaluation code
-- is deployed, and then every key resolves to its default (the S279 panel report's numbers) until an administrator
-- approves a change.
--
-- Class: CONTRACT (root ADR 0017 decision 3; no migrate directive line): config rows INSERTed into an existing table.
-- MIG-4: a migration-only PR (it joins the wave's CIS migration batch); applied on Dev by Infra Migration Apply when the
-- job tables are idle (ADR 0017a). DM-7.1: no new table.
--
-- Number. 412-415 are taken by other lanes of this wave; if the owner main max + 1 differs at merge, renumber (git grep
-- '416_' and '416' here, in compiled_defaults.json and in the code that names the file).
--
-- Locks. One INSERT of five rows (the definition guard trigger allows the file's role, cis_owner). lock_timeout 5s.
-- Idempotent (ON CONFLICT (key) DO NOTHING), one transaction, self-registering.
--
-- Rollback (a new forward migration, after the evaluation code is reverted): DELETE the five keys (no value row can
-- exist before the code reads them; a value row would have to go first).

BEGIN;
SET LOCAL lock_timeout = '5s';

-- settings-seed:begin (scripts/settings/gen_compiled_defaults.py reads the JSON array of the dollar-quoted seed)
INSERT INTO shared.setting_definition
    (key, app, area, label, description, value_type, unit, min_value, max_value, allowed_values, default_value,
     scope_kind, impact, approval, evidence_required, env_override, cache_s, enforced)
SELECT s.key, s.app, s.area, s.label, s.description, s.value_type, s.unit, s.min_value, s.max_value, s.allowed_values,
       s.default_value, s.scope_kind, s.impact, s.approval, s.evidence_required, s.env_override, s.cache_s, s.enforced
  FROM jsonb_to_recordset($seed$[
{"key": "cis.spend.instrument_eval_cap_usd", "app": "cis", "area": "spend", "label": "Claim instrument evaluation cap", "description": "Hard cap in USD of one claim-instrument evaluation run (a new prompt version measured on claims the R1 instrument fired on). Every call is gated on the spend so far plus its worst case; the run stops before it would pass the cap. Never above the R1 harness hard cap of 2 USD. Read by the evaluation from the release that ships it; marked enforced by a later migration.", "value_type": "usd", "unit": "USD", "min_value": 0.01, "max_value": 2, "allowed_values": null, "default_value": 1.5, "scope_kind": "platform", "impact": "high", "approval": "maker_checker", "evidence_required": false, "env_override": null, "cache_s": 30, "enforced": false},
{"key": "cis.spend.instrument_eval_max_claims", "app": "cis", "area": "spend", "label": "Claim instrument evaluation claims", "description": "The most claims one claim-instrument evaluation run may take (half false positives, half true positives of the claims the R1 instrument fired on). Read by the evaluation from the release that ships it; marked enforced by a later migration.", "value_type": "int", "unit": "claims", "min_value": 1, "max_value": 200, "allowed_values": null, "default_value": 50, "scope_kind": "platform", "impact": "medium", "approval": "maker_checker", "evidence_required": false, "env_override": null, "cache_s": 30, "enforced": false},
{"key": "cis.spend.instrument_eval_trial_claims", "app": "cis", "area": "spend", "label": "Claim instrument evaluation trial", "description": "The most claims the paid trial of a claim-instrument evaluation may take before the full subset run (check the cost per call in the ledger first). Read by the evaluation from the release that ships it; marked enforced by a later migration.", "value_type": "int", "unit": "claims", "min_value": 1, "max_value": 10, "allowed_values": null, "default_value": 3, "scope_kind": "platform", "impact": "medium", "approval": "maker_checker", "evidence_required": false, "env_override": null, "cache_s": 30, "enforced": false},
{"key": "cis.quality.instrument_eval_fp_drop_min_pct", "app": "cis", "area": "quality", "label": "Instrument evaluation: false positives dropped", "description": "Go / no-go bar of a claim-instrument evaluation: the new prompt version must clear at least this percentage of the subset's false positives (claims the panel judged ok that R1 fired on). Read by the evaluation from the release that ships it; marked enforced by a later migration.", "value_type": "pct", "unit": "%", "min_value": 0, "max_value": 100, "allowed_values": null, "default_value": 60, "scope_kind": "platform", "impact": "medium", "approval": "maker_checker", "evidence_required": true, "env_override": null, "cache_s": 30, "enforced": false},
{"key": "cis.quality.instrument_eval_tp_keep_min_pct", "app": "cis", "area": "quality", "label": "Instrument evaluation: true positives kept", "description": "Go / no-go bar of a claim-instrument evaluation: the new prompt version must still flag at least this percentage of the subset's stored violations (out of sample; the approved relabels are in-sample for a prompt drawn from them, so they are reported apart and never count toward this bar). Read by the evaluation from the release that ships it; marked enforced by a later migration.", "value_type": "pct", "unit": "%", "min_value": 0, "max_value": 100, "allowed_values": null, "default_value": 90, "scope_kind": "platform", "impact": "medium", "approval": "maker_checker", "evidence_required": true, "env_override": null, "cache_s": 30, "enforced": false}
]$seed$::jsonb) AS s (key text, app text, area text, label text, description text, value_type text, unit text,
                      min_value numeric, max_value numeric, allowed_values jsonb, default_value jsonb, scope_kind text,
                      impact text, approval text, evidence_required boolean, env_override text, cache_s integer,
                      enforced boolean)
ON CONFLICT (key) DO NOTHING;
-- settings-seed:end

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('416', now(), 'AA-1048: limits of the claim-instrument evaluation (cap, claims, trial, go / no-go bars)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
