# AA-1111 — lane l10 frontend dependency hygiene: CIS admin (frontend/)

S294. Dependency hygiene for the staff admin before the business-owner demo of 26/11. Before this change
`npm audit` in `frontend/` reported 1 high (zod, GHSA-mrhx-c7xc-m3w5) and 6 moderate (next 16.4.2: seven Next.js
advisories fixed in 16.4.5; glob 13.0.7, GHSA-3g3m-2fjr-6pw4; minimatch 10.2.6, GHSA-g3hv-75wg-m249). After it:
0 vulnerabilities. Supersedes Dependabot CIS #1586.

## Decisions

- `next` 16.4.2 -> 16.4.6, pinned exactly: the version the Booking admin and the CRM web already run.
- `@sentry/nextjs` `^10.78.0` -> `11.11.0`, pinned exactly: the version the Booking admin and the CRM web run.
  The vulnerable glob / minimatch come only through `@sentry/bundler-plugins` <= 11.10; Sentry 11.11 brings
  glob 14.0.6 + minimatch 11.0.9. An npm `overrides` entry forcing glob 14 under Sentry 10 was the other option; it
  was not taken because it would run Sentry 10's build plugin on a glob major it was never released with.
- Sentry 11 serves `withSentryConfig` from `@sentry/nextjs/config` (the package root no longer exports it). That is the
  `withSentryConfig is not a function` / TS2305 failure of Dependabot #1586, which bumped Sentry to 11.14 without
  changing the import. `next.config.ts` now imports from `@sentry/nextjs/config`.
- `disableLogger` was removed in Sentry 11: replaced by `bundleSizeOptimizations.excludeDebugStatements: true`
  (same intent; it now also applies to the Turbopack build).
- `buildTimeInstrumentation: false`: new in Sentry 11 and on by default for Turbopack on Next 16 (build-time
  instrumentation of server dependencies, for tracing). The admin sends errors only (`lib/sentryConfig.ts`,
  `tracesSampleRate: 0`), and the Booking admin and the CRM web run Sentry 11 with no wrapper at all, so the runtime
  stays as close to those two proven set-ups as possible.
- `zod` (dev only, through `eslint-config-next` -> `eslint-plugin-react-hooks`) 4.3.6 -> 4.6.7 with `npm update zod`,
  inside the range its parent asks for; no package.json entry.
- The npm audit gate of the AA-Partner CRM web is ported as-is: `.github/scripts/npm_audit_gate.py` (+ its unittest),
  `.github/npm-audit-allow.json` (empty: nothing needs accepting today) and a step "npm audit (admin frontend,
  failing on high)" in the Security Audit job, which runs on every pull request.

## Changed

- The brief asked for "glob >= 14.0.2 and minimatch >= 11.0.1"; the minimatch 3.1.5 (eslint) and 11.0.9
  (typescript-eslint) copies were not in the advisory range (7.0.0 - 11.0.0) and are left as they were.
- The lockfile is not minimal in line count: replacing the Sentry 10 tree with the 11 tree removes webpack and its
  plugins (Sentry 10's bundler plugin pulled them) and adds `oxc-parser` and the `sentry` CLI package (the same as the
  Booking admin's lockfile). Outside the Sentry / OpenTelemetry / Next trees only `@next/env`, `glob`, `minimatch`,
  `brace-expansion`, `path-scurry`, `zod` and `web-vitals` (added) move.

## Tradeoffs

- The audit step runs on every pull request, also backend-only ones: a newly published high advisory in the admin's
  tree turns every CIS pull request red until it is fixed or accepted in the allowlist (at most 90 days). That is the
  point of the gate (the same trade-off the CRM web made).
- The gate's 90-day ceiling is a constant in a CI script, not an admin setting: it is a CI policy, not an app
  threshold, and it stays identical to the AA-Partner copy.

## Should know

- Verified locally: `npm ci`, `tsc --noEmit`, `eslint --prune-suppressions` (suppressions file unchanged), the node
  unit tests (`npm run test:unit`, kit tests), `next build` (and the standalone build the image uses), `npm audit`
  (0 vulnerabilities), the gate script on the real report, and on the standalone server: the public smoke
  (`smoke-public`, 66/66: login light/dark, security headers, BFF cross-site guard, staff pages without a session
  sent to /login, forged cookie refused) and the visual run (kit gallery 1440/390 light/dark). The repo has no
  mocked-BFF path for signed-in staff pages (since AA-911); those are checked after the deploy by the signed-in smoke.
- Local quirk: run the smoke with `BASE_URL=http://localhost:<port>`, not `127.0.0.1`: Next reports
  `nextUrl.origin` as `localhost`, so with `127.0.0.1` the BFF guard refuses the same-origin write probes (8 tests).
- Sentry probe (local, not in the diff): a standalone build with a DSN pointing at a local fake ingest delivered a
  browser error event from `/login` (SDK `sentry.javascript.nextjs/11.11.0`, `environment=dev`, the release tag; the
  query value the page was opened with did not reach the ingest). The standalone server prints one startup warning
  from Sentry 11 ("Failed to register diagnostics-channel injection hooks ... channel-based integrations will not
  record spans"): it is about tracing spans only, which the admin does not record (`tracesSampleRate: 0`); the
  server SDK still initialises.
- `tests/unit/test_aa1111_frontend_deps.py` pins the floor: next >= 16.4.6, an exact Sentry 11 pin, the
  `@sentry/nextjs/config` import, no vulnerable zod / glob / minimatch in the lockfile, the CI gate step.
- No change to `proxy.ts`, middleware, auth or session code.
