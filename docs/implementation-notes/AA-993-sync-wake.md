# AA-993 — Migration sync wake-up (S258)

`.github/workflows/migration-sync-wake.yml`: after a push to `main` that touches this repo's migrations folder, comment
(as `aa-ecosys-ci-bot`) on the open AA-CIS-Infra issue labelled `migration-sync-wake`; that comment starts Infra's
Migration Sync at once instead of waiting for its best-effort `*/10` cron (which fired once in ~6 h on 2026-10-29).

## Decisions

- Token minted in the environment `release`, restricted to `main` by its deployment branch policy (holds
  `AA_CI_BOT_PRIVATE_KEY`, as `release-please.yml`), narrowed to AA-CIS-Infra and **Issues write only**: it cannot
  change Infra code, open or merge a PR, or dispatch an Infra workflow. It can comment and label (including the Infra
  `secret-scan-baseline-reviewed` override label); root ADR 0011a d3 explains why that is no new reach. No checkout,
  `permissions: {}` for `GITHUB_TOKEN`.
- The comment is a doorbell, not a payload: Infra never reads its text and still pulls this repo's `main` anonymously
  (root ADR 0017 d2, critic 1). Infra runs the sync only for a ci-bot comment on the labelled issue.
- The comment is a plain REST call (`gh api --method POST repos/<Infra>/issues/<n>/comments`), not `gh issue comment`
  (GraphQL): the most predictable call for an Issues-write-only token.
- This workflow never creates the issue (Infra does, on a scheduled or manual sync run). No open issue = a warning and
  a green run; the Infra cron copies the file later.

## Changed

- New workflow and its shape test `tests/unit/test_migration_sync_wake.py` (stdlib, text: `permissions: {}`, no checkout, push to `main` on the
  migrations folder only, token for AA-CIS-Infra with `permission-issues: write` only, one REST comment as the only
  write). The workflow and the test are identical in the four owner repos except the migrations path.
- Infra side: AA-CIS-Infra `migration-sync.yml` (`issue_comment` + `workflow_run` triggers), root ADR 0011a d3 /
  ADR 0017 d2 amended (same S258 change set).

## Tradeoffs

- `repository_dispatch` (Contents write on Infra) and `workflow_dispatch` (Actions write: could dispatch DB Migrate or
  Terraform Apply, and ci-bot has no Actions permission today) were rejected; Issues write is already granted to the
  App, so no new permission or secret.

## Should know

- Merge only after the Infra change is on Infra `main` and the wake-up issue exists; before that the run just warns.
