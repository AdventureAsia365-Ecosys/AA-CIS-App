# AA-1069 (lane kit-testid) — kit buttons keep a plain data-testid; guard on the call sites

Lane l26 of S277 (Claude Code). Follow-up of the S277 CRM E2E finding: the kit's `domProps()` in
`frontend/app/_kit/core/Buttons.tsx` spread the native props first and then set `"data-testid": testId`, so a call
site that passed a plain `data-testid="..."` got it overwritten with `undefined`. Nine CRM web call sites lost their
test ids silently (since the kit-only web of early November) and Playwright waited for controls that were on the page.
The CRM call sites are moved to `testId` in the AA-Partner lane of the same session; this lane fixes the source.

## Decisions

- **Keep the native attribute (`testId ?? native["data-testid"]`), do not make the type refuse it.** Both close the
  silent drop; the fallback is the safer one:
  - it never breaks a build: the Kit Sync bot copies core into AA-Partner and AA-Booking, and Kit Consumers runs their
    `tsc`. A `"data-testid"?: never` in `NativePass` would turn every consumer call site still passing the attribute
    (the CRM ones until the Partner fix merges) into a type error, so the CIS PR, the sync PRs and the Partner PR
    would have to land in one exact order;
  - it fixes every existing call site at once, including ones in code the scan does not see (a wrapper that spreads
    its props into a kit button);
  - `testId` still wins when both are given, so no call site changes behaviour.
- **One documented way stays enforced by a test, not by the type.** `frontend/lib/kitButtonTestIds.test.mjs` is the
  same AST scan as the AA-Partner web guard (TypeScript compiler, every runtime `.tsx`, kit imports resolved by name
  and alias, the components whose body calls `domProps(`), and refuses a `data-testid` attribute on a kit button.
  The CIS scan also covers the canonical core (except `Buttons.tsx`): a core component passing `data-testid` to a kit
  button would ship the same mistake to every consumer.
- **CI runs the guard.** CIS CI runs a fixed list of frontend tests (not `npm run test:unit`), so the new test is
  added to both "Kit button render tests" steps of `ci.yml` (pull request and Frontend Build).
- **No hand kit sync.** The consumers' Provenance check rejects a lock whose ref is not on CIS `main`, and the Kit Sync
  bot (`kit-sync.yml`, on push to `main` touching core) opens the `bot/kit-sync` PRs in AA-Partner and AA-Booking
  after this PR merges. TripPlanner's tokens profile does not include `Buttons.tsx`: its leg is a no-op.

## Changed

- `frontend/app/_kit/core/Buttons.tsx`: `domProps()` keeps the native `data-testid` when `testId` is unset; header
  comment says call sites use `testId`. `MANIFEST.json` regenerated (`node scripts/kit-manifest.mjs`).
- `frontend/lib/kitButtons.test.mjs`: render test over every domProps button (Button, LinkButton, IconButton,
  ChipButton, Switch, ListButton, Pressable): plain id kept, `testId` wins, no attribute when neither is set. It
  fails on the old core.
- `frontend/lib/kitButtonTestIds.test.mjs`: the call-site guard (family found, scanner self-test with an alias and a
  non-kit `Button`, whole-admin scan).
- The scan found three CIS call sites on `Button` and moved them to `testId`: `app/admin/jobs/JobDrawer.tsx`
  (`job-drawer-release-jev`), `app/admin/platform/staff/StaffManage.tsx` (`staff-resend-invite`,
  `staff-preset-apply`). No spec queries them today, so nothing was failing yet.
- `.github/workflows/ci.yml`: the guard joins the kit render test steps. `CONTEXT.md`: one paragraph under the
  button variants.

## Tradeoffs

- The fallback makes the plain attribute work, so the guard is about one way of writing it rather than a live bug.
  Kept anyway: without it a future change to `domProps()` could bring the silent drop back, and the scan is cheap.
- The scan only follows named imports from kit modules; a local wrapper component is not followed. The runtime
  fallback covers that case.

## Should know

- AA-Booking: the same scan over `frontend/` (app, ui, lib; kit at `ui/core`, barrel `@/ui`) finds no kit button with
  a plain `data-testid`.
- `frontend/lib/routeControl.test.mjs` ("the messages ... name no issue ids") fails on `main` before this change: an
  indented `// AA-1052` comment in `app/admin/ai-gateway/routeControl.messages.ts` is not stripped by the test's
  `^//` regex. CI does not run that file (CIS CI runs a fixed list), so it went unnoticed; not touched here.
