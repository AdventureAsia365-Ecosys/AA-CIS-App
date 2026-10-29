# AA-994 S3s — kit sync automation (S257)

Slice S3s of the AA-994 plan (root `docs/audits/2026-10-27-S252-aa994-design-system/plan.md`, root ADR 0016
decisions 5 and 6, guards G14 and G17): the bot that copies `frontend/app/_kit/core/` into the four consumers after a
CIS merge, the "Kit consumers" check on CIS PRs, and the nightly freshness report.

## Decisions

- **The copy code moves into CIS** (`.github/scripts/kit-sync.sh`, ported from the root `tools/kit/kit-sync.sh`
  with the same lock format, profiles and exit codes). The workflows cannot read the root repo once it goes private
  in November, and two copies of the copy logic would drift. The root tool becomes a wrapper that runs this file
  (root PR in the same lane). New flag `--list` prints the consumer table, so the table exists once and
  `kit_sync_bot.py` reads it from there. Defaults change only in how they are found: `--cis` is the checkout the
  script is in; `--dest-repo` is `$AA_WORKSPACE/apps/<repo>` or the sibling `<repo>` folder of the CIS main checkout
  (worktrees resolve through the common git dir).
- **One PR per consumer repo, not per consumer.** portal and crm-web live in AA-Partner and share branch
  `bot/kit-sync` (ADR 0016 decision 6 names one branch per repo), so both locks move together.
- **No-op rule:** a repo is synced only when a copied file other than `MANIFEST.json` changes. `MANIFEST.json` lists
  every core file's hash, so without this rule TripPlanner (tokens profile) would get a PR for every DataTable edit.
  A lock-ref-only bump is never opened either. A stale open sync PR is closed as superseded on a no-op.
- **No push when the tree is the same** as the remote `bot/kit-sync`: re-runs (manual dispatch, a re-pushed CIS
  commit) do not restart the consumer's CI.
- **Debounce:** workflow-level concurrency group `kit-sync`, `cancel-in-progress: false` — a running sync finishes,
  a newer push replaces the pending run. Each run syncs `github.sha` (always on CIS main, so Provenance accepts it).
- **Token:** `actions/create-github-app-token` per matrix leg with `repositories: <that consumer repo>`,
  `permission-contents: write`, `permission-pull-requests: write`, in the `release` environment (main-only branch
  policy, where `AA_CI_BOT_PRIVATE_KEY` already lives for release-please). Nothing for AA-CIS-Infra or the root repo.
- **Commit identity:** `<app-slug>[bot]` with the `<id>+<slug>[bot]@users.noreply.github.com` address read from the
  public users API, so the commit shows as the App on GitHub.
- **Dry run:** `workflow_dispatch` input `dry_run` (default `true`): copy + local commit in the runner, the PR body
  printed, no push and no `gh` call. The token is still minted, which proves the key and the installation.
- **Kit Consumers** is its own workflow (`kit-consumers.yml`, `pull_request` only, read-only token, no secret), so a
  PR run can never reach the App key. Matrix legs from `kit_sync_bot.py plan` (the same table); the stable check name
  is the result job "Kit consumers", green when the plan says unchanged (legs skipped). Lint is the app's own
  `npm run lint` plus `--pass-on-unpruned-suppressions` (a core fix that removes a frozen violation must not fail
  here; the consumer's own CI prunes later). `next typegen` runs before `tsc --noEmit` so route types exist as in
  `next build` (portal has no `typecheck` script); its failure is only a notice.
- **Freshness** counts CIS commits that touch the consumer's profile paths since its lock ref (tokens profile: the
  token files, not `MANIFEST.json`), and lists the open sync PR. It runs nightly and after every sync, with
  `github.token` only (the consumer repos are public).

## Changed

- New: `.github/workflows/kit-sync.yml` (jobs Plan, Sync <repo> ×3, Freshness report),
  `.github/workflows/kit-consumers.yml` (Plan, Kit consumers (<consumer>) ×4, Kit consumers),
  `.github/scripts/kit-sync.sh` (canonical copy script), `.github/scripts/kit_sync_bot.py` (plan / sync / report),
  `.github/scripts/test_kit_sync.sh` (the root fixture suite, moved with the script; every call passes `--cis`),
  `tests/unit/test_aa994_kit_sync_bot.py` (18 tests, real git repos in tmp_path, no network, no `gh`).
- `CONTEXT.md`: the kit core paragraph names the new scripts and workflows.
- The plan row says "Kit consumers job on CIS PRs"; it is a separate workflow file for the secret boundary above.

## Tradeoffs

- **ADR 0011a decision 3 says "no app workflow mints a token for another repo"** (written for AA-CIS-Infra and the
  root repo: a token there could change the DB runner). ADR 0016 decision 6 (accepted earlier) prescribes exactly this
  cross-repo token for the kit. The token here is narrowed to the three consumer app repos, Contents + Pull requests,
  never Infra or root; the key itself could mint any installation token already (it sits in CIS for release-please),
  so narrowing is the control. The root PR of this lane rewords ADR 0011a decision 3 and `docs/security/github-apps.md`
  §8 to state the one exception.
- `paths:` on the PR workflow: fine while the check is not required; ADR 0016 makes it required after S4b, and then
  the filter must go (the plan job already handles "unchanged").
- Branch `bot/kit-sync` is force-pushed every time it changes: a human commit on it is lost by design (the copy is
  byte-checked; fixes belong in CIS).
- The consumer checkout is shallow (`fetch-depth: 1`); the remote branch comparison fetches only `bot/kit-sync`.

## Should know

- Verified locally: the bot's dry run against fresh clones of AA-Partner, AA-Booking and AA-TripPlanner-Web at CIS
  `origin/main` is a no-op for all three (they carry core `3994a1a8`, and no core commit landed since); `report`
  against GitHub prints 0 commits behind and no open sync PR. The portal leg of Kit Consumers ran locally end to end
  (overlay, integrity, `npm ci`, `next typegen`, `tsc --noEmit` clean, lint 0 errors).
- Not runnable before merge: a new workflow file can only be dispatched once it is on `main` (lesson S220). After the
  merge: Actions → Kit Sync → Run workflow (main, dry_run true) must end green with "no-op" per repo, then the first
  real sync PR appears with the next core merge (ADR 0016 Done-when: one real sync PR in Partner with CI + preview).
- Sync PRs are merged by the orchestrating agent through each merge queue (`tools/github/mergeq.sh`; TripPlanner by
  `gh pr merge --squash`). The bot never merges (ADR 0016 decision 6; auto-merge needs the tech lead after ~5 clean
  syncs).
- The S256 lesson (a first-time core file broke a consumer's own lint override / guard) is surfaced in the PR body:
  added files are listed under "First-time core files in this app".
