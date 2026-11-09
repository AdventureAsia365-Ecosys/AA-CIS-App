# AA-1022 — Dependabot for GitHub Actions (AA-CIS-App slice)

The Infra side of AA-1022 (S3 gateway endpoint, NAT recover alarm, OIDC ref pins, action SHA pins, Identity Center
administrator sign-in alert) is in AA-CIS-Infra `docs/implementation-notes/AA-1022.md`. This repo gets one change.

## Decisions

- `.github/dependabot.yml` gains a `github-actions` entry: weekly (Monday), all updates grouped into one PR, at most 3
  open, commit prefix `chore(deps)` like the npm/pip entries. Same shape as AA-CIS-Infra's entry.
- Version updates stay off for npm and pip (S242 reason unchanged); only the actions ecosystem gets version updates.

## Changed

- `.github/dependabot.yml` only. No workflow is edited here: pinning this repo's actions to SHAs is not in the issue
  scope (the issue pins AA-CIS-Infra only); Dependabot handles both tag and SHA pins.

## Tradeoffs

- A Dependabot actions PR edits workflows, so it runs CI without repository secrets; the Vercel deploy jobs are
  skipped for Dependabot (AA-903), the rest of CI runs as usual. A check that needs a secret is re-run from a normal
  branch with the same change.

## Should know

- If such a PR touches a workflow that the secret-scan baseline guard watches, the reviewer label
  `secret-scan-baseline-reviewed` is needed after the last push (lessons log, S246/S256).
- The first run after merge appears under Insights > Dependency graph > Dependabot; a grouped PR "chore(deps): bump
  the actions group" is expected on the next Monday if any action has a newer release.
