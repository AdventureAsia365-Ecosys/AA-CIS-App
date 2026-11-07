# AA-911 — Vercel retirement of the CIS admin (lane l4-aa911-vercel-retire, S272)

Source: root runbook `docs/ops/runbooks/staff-web-ecs-cutover.md` section 4 steps 2-4 (approved by the tech lead on
07/11/2026), root ADR 0019 d23 and root ADR 0006a A11 / A13. The DNS cut-over (section 3) is done: aa-cis. points at
the shared ALB and the ECS service `aa-cis-dev-admin` serves it. Root notes: `docs/implementation-notes/AA-911-vercel-retire.md`.

## Decisions

- **The post-deploy checks move, they are not dropped.** `vercel-deploy.yml` ran the signed-in UI smoke and the
  visual run after each production deploy. Both now run from `deploy-admin-dev.yml` after the `verify` job passes on
  main (`smoke-production` -> `ui-smoke.yml` with `staff_session: true`, `visual-production` -> `visual.yml`). The deploy
  verdict job (`result`) still judges only config / deploy / verify, like the old Vercel run, so a red smoke shows as
  a red job without hiding whether the image went live.
- **No per-PR deployment.** Per-PR UI evidence is the required `Visual (next start, public pages + kit gallery)` check
  (root ADR 0019 d23 R7), which already runs without a deployment. The public smoke specs can also run against a
  local `next start`.
- **The API stops accepting the Preview client by default, the cap code stays.** `DEV_CIS_ADMIN_PREVIEW_CLIENT_ID` is
  now empty, so a token of the deleted `cis-admin-preview` client is 401 even before AA-CIS-Infra deletes it. The cap
  (`is_preview_client`, `resolve_staff` viewer mapping) stays for a client set through the env, so this PR changes no
  authorization logic, only a default. Deleting the dead code is a follow-up.
- **The frontend has one cis-admin client.** `frontend/lib/cognito.ts` drops `VERCEL_ENV`, `PREVIEW_ORIGIN`,
  `isPreviewSignInHost` and the preview client id / secret. The ECS image never set `VERCEL_ENV`, so the production
  path was already the one in use.
- **Smoke specs point at the production host only.** `auth.setup.ts`, `aa816-*`, `cognito-login.spec.ts` lose the
  Preview host. `aa911-bff-cross-site.spec.ts` uses `booking.lumiguides.it.com` as the same-site sibling Origin (it
  is only sent as a header value, never requested).
- **E2E Cognito has no input any more.** The Booking half always runs on `booking.lumiguides.it.com`; the
  `BOOKING_PREVIEW_BYPASS` secret is unused.

## Changed

- Deleted `.github/workflows/vercel-deploy.yml` and `frontend/vercel.json`.
- `deploy-admin-dev.yml`: header, jobs `smoke-production` and `visual-production`.
- `ui-smoke.yml`, `visual.yml`: no `VERCEL_AUTOMATION_BYPASS_SECRET`, staff host allow-list = the production host only.
- `e2e-cognito.yml` + `tests/e2e/cognito/cognito-login.spec.ts`: no `booking_base_url`, no bypass.
- `frontend/lib/cognito.ts`, `app/login/page.tsx`, `app/api/auth/login/route.ts`, `next.config.ts` (no Preview
  `X-Robots-Tag`).
- `api/core/identity_config.py` (default empty), `api/core/staff_identity.py` (docstring).
- Tests: `test_aa816_smoke_split.py` (the deploy-admin-dev wiring + a "Vercel is retired" guard),
  `test_aa885_portal_split.py`, `test_aa911_preview_client_cap.py` (the retired id is refused by default),
  `test_identity_verifier.py`, `test_aa911_bff_cross_site.py` (docstring).
- Docs: `CONTEXT.md`, `README.md`, `frontend/README.md`, `frontend/.env.example`, `.github/dependabot.yml`,
  `.github/pull_request_template.md`, comments in `deploy-dev.yml` / `release-please.yml` / `frontend/lib/apps.ts`.

## Tradeoffs

- Keeping the dormant preview cap in the API is a few lines of dead code; removing it now would have changed the
  staff resolver in the same PR as the workflow move, which needs its own review.
- `installBypassRoute` and the `x-vercel-protection-bypass` lines in about ten smoke specs stay as no-ops (no workflow
  passes the secret any more). Removing them touches ~40 call sites with no behaviour change; left for a clean-up PR.

## Should know

- Merge order (root runbook section 4): the AA-CIS-Infra apex PR first, then this PR and the AA-Booking PR, then the
  AA-CIS-Infra PR that deletes the Preview hosts and clients, then the Vercel projects are deleted.
- After merge, a push that touches `frontend/**` (this PR does) runs `Deploy Admin Dev`; expect `deploy`, `verify`,
  `smoke-production` and `visual-production` green on that run.
- Repo settings the orchestrator removes by hand: secrets `VERCEL_TOKEN`, `VERCEL_AUTOMATION_BYPASS_SECRET`, the `e2e`
  environment secret `BOOKING_PREVIEW_BYPASS`, the ruleset "preview-signin locked (AA-911)" and the branch
  `preview-signin`, the label `preview-signin`.
- `.claude/CLAUDE.md` still says the admin runs on Vercel; agents do not edit that file, so it is listed as a
  follow-up for the tech lead.
- **No pre-merge signed-in check for the CIS admin after this PR.** The fixed Preview host (`alias_only_url` dispatch,
  label `preview-signin`, viewer-capped E2E user) was the only way to see unmerged CIS code signed in. `Visual (next
  start)` covers the public pages and the kit gallery only (no session, no mocked API), and the A10b `admin-evidence`
  project signs in on the production host, so it captures deployed code. Staff pages are verified signed in after
  merge only (`smoke-production` / `visual-production` in `Deploy Admin Dev`). A mocked-API staff run in CIS Visual,
  like Booking's, is the follow-up that would close the gap.
