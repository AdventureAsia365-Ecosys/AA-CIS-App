# chore: Next 16 `middleware.ts` → `proxy.ts` (S243, Refs AA-816)

No Linear issue of its own: backlog §3 row 7 "(chore)", S242 follow-up. Referenced as AA-816.

## Decisions
- Convention confirmed from the installed Next (`node_modules/next` = 16.4.2,
  `dist/docs/01-app/03-api-reference/03-file-conventions/proxy.md`): the file is `proxy.ts` at the same
  level as `app/`; it exports ONE function, either default or named `proxy`; `config` (with `matcher`) is
  exported alongside, unchanged; Proxy defaults to the Node.js runtime and a `runtime` export throws.
- Kept a named export (`export async function proxy`) — the same shape as AA-Booking's `frontend/proxy.ts`.
- Rename done by hand with `git mv` (not `npx @next/codemod@canary`), so git keeps the history and no
  canary code is pulled. The `NEXT_PUBLIC_API_URL` line was never retyped (Edit tool on other lines only).
- Log prefixes `[middleware]` → `[proxy]` in the two dev-only `console.warn` lines.
- Comment sweep limited to the literal file name `middleware.ts` (the bare word "middleware" stays where it
  means the concept, and in the backend where it is FastAPI middleware).

## Changed
- `frontend/middleware.ts` → `frontend/proxy.ts`: header line, function name, two log prefixes. Gate logic and
  `config.matcher` are byte-identical.
- `middleware.ts` → `proxy.ts` in comments: `next.config.ts`, `lib/{rbac,staffSession,cognito,cognitoRoutes}.ts`,
  `app/api/auth/{login,refresh,admin-logout}/route.ts`, `app/api/admin/[...path]/route.ts`,
  `app/admin/{platform,tenant-activity}/page.tsx`, `app/login/LoginForm.tsx`,
  `app/_kit/{SessionKeepAlive,adminNav}.tsx`, `tests/e2e/aa384-marketplace-auth.spec.ts` (historical bug note,
  kept as "middleware.ts's (now proxy.ts)"), `api/routers/admin_a4.py` (docstring).
- `lib/rbac.ts:12`: "Edge-safe … (middleware.ts imports it)" → "Dependency-free … (proxy.ts imports it)",
  since the gate no longer runs on Edge.
- `CONTEXT.md`: only the four frontend gate lines (53, 475, 477, 541). Line 53 also fixed the stale
  `PROTECTED_ROUTES` name (the list is `PAGE_RULES` in `lib/rbac.ts` since AA-656). Backend middleware lines untouched.

## Tradeoffs
- NOT swept, on purpose (critic S243: the AA-885 portal split moves / byte-vendors these files, a sweep here
  would collide with its byte guard): `app/_brand/tokens.ts`, `app/(tenant)/portal/**`,
  `app/api/auth/tenant-login/route.ts`, `lib/auth-server.ts`, and also `app/api/tenant/[...path]/route.ts`
  (AA-885 S4b copies it into `portal/`). They still say `middleware.ts` in comments; AA-885 owns those lines.
- No new test for the rename: the build output itself is the check (`ƒ Proxy (Middleware)` row,
  `.next/server/functions-config-manifest.json` → `/_middleware` `runtime: "nodejs"` with the 4 matchers),
  and the PR's ui-smoke on the preview exercises the gate.

## Should know
- Runtime moves from Edge to Node.js. On Vercel the gate now runs as a Node function in the project region,
  not at the edge. Functionally the same (it only uses `fetch`, cookies and `NextResponse`); counts as Vercel
  function invocations (negligible on Dev).
- `sentry.edge.config.ts` no longer covers the gate. No `instrumentation.ts` exists, so nothing changes in
  practice; not fixed here.
- Build no longer prints the "middleware file convention is deprecated" warning. The only remaining
  deprecation line is Sentry's pre-existing `disableLogger` notice.
- `npm run lint` reports 163 problems (54 errors, 109 warnings) — identical to `origin/main` (checked with the
  changes stashed). The Frontend Lint CI job is non-required.
- Local junk `frontend/middleware.ts:Zone.Identifier` lives only in the main checkout (untracked); this branch
  does not touch it. The main session deletes it.
- AA-885 should branch after this merges and edit `frontend/proxy.ts` (S4c), not `middleware.ts`.
- AA-TripPlanner-Web (Next 14.2.15) keeps `middleware.ts` until a Next 16 upgrade; AA-Booking already uses `proxy.ts`.
