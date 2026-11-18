# AA-1111 — security headers slice (S284, lane l5-aa1111-headers)

Source: the S283 ZAP baseline triage (root `docs/audits/2026-11-17-S283-access-review/zap-baseline.md`).

## Decisions

- **Nonce CSP on the admin.** `frontend/proxy.ts` makes a 128-bit nonce per request and sends the full policy
  (`frontend/lib/securityHeaders.ts`) on the response and on the request (Next reads the nonce from the request's
  CSP and puts it on its own scripts). `app/layout.tsx` reads `x-nonce` for the three restore scripts.
  `script-src 'self' 'nonce-…' 'strict-dynamic'`: no `'unsafe-inline'` for scripts.
- **style-src keeps `'unsafe-inline'`.** The kit and the pages use React `style={{…}}` everywhere; a nonce cannot cover
  style attributes. Dropping it is a kit-wide change (follow-up).
- **img-src `'self' data: blob: https:`.** Tour photos, partner logos and source images come from several hosts; a
  fixed list would break pages for no real gain (images do not run code). Every other fetch directive is explicit.
- **connect-src = self + the Sentry DSN origin**, derived from `NEXT_PUBLIC_SENTRY_DSN` at build time: the browser
  only calls the same-origin BFF.
- **Proxy matcher** widened to every path but `/_next/static`, `/_next/image` and `/api/*`, so 404s, `/robots.txt`
  and `/sitemap.xml` get the nonce CSP. The staff gate still runs only on `/admin/*` and `/login` (`isGatedPath`, the
  old matcher). `/api/*` stays out: a running proxy clones the request body and cuts it at 10 MiB (uploads); those
  paths and `/_next/*` get the same policy without a nonce from `next.config.ts`.
- **HSTS stays `max-age=63072000; includeSubDomains`** (two years, no preload), the value every app and the smoke
  specs already use; the decision asked for at least one year without preload.
- **API**: `api/middleware/security_headers.py` adds `nosniff` + HSTS, registered just inside `RequestIdMiddleware`
  (the AA-871 test keeps that one outermost); the unhandled 500 handler adds them itself. uvicorn runs with
  `--no-server-header` (API Gateway was passing `x-amzn-remapped-server: uvicorn`).

## Changed

- Root layout became `async` and reads `headers()`: every page renders per request (they were already dynamic but
  the login and 404 shells; nothing is cached by a CDN on the admin host).
- `Permissions-Policy` adds `payment=()` and `usb=()`; `Cross-Origin-Opener-Policy: same-origin` is new.
- No CORP / COEP: COEP would block cross-origin images without CORP; the API must stay readable by the TripPlanner and
  agency domains' `<img>` loads.

## Tradeoffs

- No settings row for the CSP (the decision said not to invent one): the source lists are code, reviewed in PRs.
- `next.config.ts` redirects (`/upload`, `/portal/*`, the IA table) are answered before any header rule and carry
  no header at all (unchanged from before). Moving them into the proxy is a separate change.

## Should know

- Local check (next start, Chromium): every script tag on `/login` carries the nonce, the page hydrates, an injected
  inline event handler is refused by the policy; `/robots.txt`, `/sitemap.xml`, a 404 and `/api/healthz` carry the set.
- `tests/e2e/smoke/aa876-security-headers.spec.ts` (smoke-public) checks a page, `/robots.txt`, `/sitemap.xml`, a 404
  and the BFF on the deployed host; `tests/unit/test_aa876_frontend_edge.py` checks the wiring in CI;
  `frontend/lib/securityHeaders.test.mjs` the policy builder (`npm run test:unit`).
- The 403s ZAP saw from `awselb` on the admin host are WAF block responses, not the app: AA-CIS-Infra adds the headers
  to the WAF custom responses in the same wave.
