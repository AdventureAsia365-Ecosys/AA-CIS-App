# AA-544 Stage 0 — infra prep: aa_app_user password → Secrets Manager + missing GRANTs

Part of AA-544's approved 7-stage rollout (2-pool split: Admin keeps `aa_cis_admin` bypass,
Tenant Portal eventually moves to `aa_app_user` + real RLS). This is Stage 0 only — infra prep,
**0 behavior change**. No route switched pool. No RLS became enforced. `api/main.py` still opens
exactly one pool, as `aa_cis_admin`, same as before this stage.

## Decisions

- **Which 7 tables**: AA-544's Round 2 Linear comment flagged 5 `acp_shared` tables (using the
  real `app.tenant_id` GUC, migrations 112/113/115/116/145) as GRANT-missing for `aa_app_user`:
  `content_metric_snapshot`, `angle_gate_request`, `content_piece`, `publish_log`, `facts`. The
  Stage-0 go-ahead comment says "7 bảng" without re-listing them. Reconciled by adding the two
  tables that ship in the same migrations and are functionally required alongside the 5 already
  named: `angle_gate_option` (113 — `angle_gate_request`'s own child row set, no `tenant_id`/RLS
  of its own, isolated only via its parent) and `year_plan` (112 — `content_metric_snapshot`'s
  migration-112 sibling, also no RLS of its own). This reconciliation is inference, not something
  Nghiep confirmed line-by-line — flagged in the Linear report for correction if wrong.
- **Scope explicitly excludes the other ~30 GRANT-missing `acp_*` tables** live-DB-confirmed
  during this task (37 total tables across `acp_contract`/`acp_silver_s2`/`acp_silver_s3`/
  `acp_gold_output`/other `acp_shared` tables — `tour_atoms`, `atom_segment`, `route`, `hub`,
  etc.). Those are read exclusively by Admin routes, which keep the `aa_cis_admin` pool under
  AA-544's design — granting them now would be unused privilege creep, the same class of mistake
  AA-517 already cost 2 wrong-account Secrets Manager writes over.
- **Password rotation not done via a migration file.** The existing `aa_app_user` password
  (`cisappuser2026`) has sat in git-tracked `api/migrations/007b_create_app_user.sql` in
  plaintext since the file was first committed — writing the *new* password into another
  committed file would repeat the same mistake. Instead: generated a 32-char random password,
  stored only in a new Secrets Manager secret (`aa-cis/dev/rds-app-user`, plain DSN string, same
  convention as `aa-cis/dev/rds`), and rotated it live via `ALTER ROLE aa_app_user WITH PASSWORD
  ...` run through the S3-mediated ECS exec pattern — the value never touched the S3 script body
  or any git-tracked file. `007b_create_app_user.sql` itself was left unedited except a comment
  flagging the literal as stale (its `CREATE ROLE ... IF NOT EXISTS` guard already no-ops on
  every environment where it previously ran, so editing the literal would do nothing for
  already-applied environments and would be misleading to leave un-flagged).

## Changed

- New `api/migrations/147_grant_aa_app_user_acp_shared_gaps.sql` — `GRANT SELECT, INSERT, UPDATE`
  (role-existence guarded, matching the 094-098 convention) on the 7 tables above. Applied live
  on RDS dev ([acc2 id]) via ECS exec, `schema_versions` row inserted.
- `api/migrations/007b_create_app_user.sql` — comment-only addition noting the password is
  rotated/stale; no SQL statement changed.
- New Secrets Manager secret `aa-cis/dev/rds-app-user` ([acc2 id], us-west-1).
- `aa_app_user`'s live RDS password rotated to the value now in that secret.

## Tradeoffs

- Reconciling "5" (Round 2's explicit list) against "7" (the go-ahead comment's count) by
  inference carries real risk of picking the wrong 2 extra tables — flagged explicitly in the
  Linear report rather than silently assumed correct, per the standing pattern on this issue
  (report, don't guess past a genuine ambiguity in a security-scoped task).
- Did not add `ALTER DEFAULT PRIVILEGES IN SCHEMA acp_shared GRANT ... TO aa_app_user` to stop
  this gap from recurring on every future `acp_shared` table. Would be low-risk (no behavior
  change either way, since nothing connects as `aa_app_user` yet) but is scope beyond what Stage
  0 asked for — noted in the Linear report as a suggestion for later, not built.

## Should know

- Live-DB audit before this change (read-only, no writes): 37 tables across `acp_*` schemas,
  only 7 already GRANTed to `aa_app_user` (`acp_deliver.packets`/`pieces`,
  `acp_shared.acp_v2_runs`/`acp_v2_slots`/`marketplace_portfolios`/`tenant_atom_state`/
  `tenant_onboarding` — migrations 094-098). After this migration: 14.
- Post-change verify: connected live as `aa_app_user` with the new password
  (`current_user=aa_app_user`, `rolbypassrls=false` confirmed), confirmed all 7 target tables
  show `SELECT/INSERT/UPDATE` in `information_schema.role_table_grants`. `GET /health` on the
  real domain stayed 200 throughout (admin pool, unaffected as expected).
- `acp_shared.quarter_plan`/`quarter_plan_version` (092) are still GRANT-missing for
  `aa_app_user` and were NOT touched here — they predate the `app.tenant_id`-GUC cohort AA-544's
  Round 2 report scoped to, and weren't named in either the "5" or reconciled-"7" set. Whether T7
  needs them granted too is a Stage-1+ question (which routes actually move to the tenant pool),
  not resolved by this migration.
