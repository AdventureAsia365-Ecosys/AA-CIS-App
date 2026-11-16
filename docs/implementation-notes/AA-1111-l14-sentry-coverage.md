# AA-1111 — lane l14 Sentry coverage: CIS admin (frontend/)

S282 observability hygiene. The Sentry project of the admin app had no event in 90 days. Two causes: the `dev`
environment variable `ADMIN_PUBLIC_SENTRY_DSN` was never set (the image was built with an empty DSN), and the app had
only the legacy `sentry.client/server/edge.config.ts`, which a Turbopack build of Next 16 never loads with
`@sentry/nextjs` 10. This PR fixes the code half; the orchestrator sets the variable and redeploys.

## Decisions

- `instrumentation.ts`: `register()` imports `sentry.server.config` (nodejs runtime) or `sentry.edge.config` (edge
  runtime); `export const onRequestError = Sentry.captureRequestError` catches errors of server components, route
  handlers and the proxy. Same shape as the AA-Partner web.
- `instrumentation-client.ts` replaces `sentry.client.config.ts` (deleted: on a webpack build both would initialise).
  It also exports `onRouterTransitionStart` (the hook the SDK asks for; a no-op without tracing).
- One options builder, `lib/sentryConfig.ts` (`sentryInitOptions`, `scrubEvent`), used by all three runtimes. No
  DSN (empty or blank) returns `null` and the caller skips `Sentry.init`, so an image without the variable sends
  nothing.
- Same privacy rules as the AA-Partner web: `dataCollection` off for user, cookies, headers, query strings, bodies
  and frame variables, plus `beforeSend: scrubEvent`. The CIS scrub also drops `X-Admin-Secret` and, found by the
  local probe below, the query string in `contexts.nextjs.request_path` (written by `captureRequestError`) and in
  breadcrumb URLs (`from`/`to`/`url`).
- The server DSN is `SENTRY_DSN` when a task maps one at run time, else the build-time `NEXT_PUBLIC_SENTRY_DSN`
  (Next inlines it into the server bundle too). Today only the build arg exists, so no Infra change is needed.

## Changed

- `tracesSampleRate` goes from 0.1 to 0: the old value never ran (the files were not loaded), and turning tracing
  on now would spend the Sentry span quota. Errors only, like the AA-Partner web.
- `next.config.ts` is unchanged (still `withSentryConfig`, no source-map upload without `SENTRY_AUTH_TOKEN`).

## Tradeoffs

- The guard that CI runs is a Python source check (`tests/unit/test_aa1111_admin_sentry_instrumentation.py`). This
  repo's CI does not run `npm run test:unit`, so the behaviour test (`lib/sentryConfig.test.mjs`) runs locally
  and in the pre-push hook only.
- `tracesSampleRate: 0` is a fixed value, not an admin setting: it is the off switch of a feature we do not use.
  It is not a tuning threshold.

## Should know

- Local end-to-end proof: a standalone build with a DSN that points at a local fake ingest received one server event
  (a route that throws, through `onRequestError`) and one browser event (an uncaught error on `/login`). They carried
  `environment=dev` and the build SHA as the release, and none of the query value, cookie or bearer token that the
  requests sent. The probe route was temporary and is not in the diff.
- The build prints two old deprecation warnings from `next.config.ts` (`withSentryConfig` from `@sentry/nextjs`,
  and `disableLogger`). They become errors in v11. Left for the SDK upgrade.
- After merge: set the `dev` variable `ADMIN_PUBLIC_SENTRY_DSN` (the DSN of aa-cis-frontend, public by design, never
  in the repo) and run Deploy Admin Dev. The build arg wiring in `deploy-admin-dev.yml` is unchanged.
