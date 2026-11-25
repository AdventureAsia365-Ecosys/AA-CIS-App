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
- **connect-src = self + the S3 upload hosts + the Sentry DSN origin.** The Sentry origin is derived from
  `NEXT_PUBLIC_SENTRY_DSN` at build time. The browser calls the same-origin BFF for everything except one call: the
  S0 upload page (`app/admin/upload`) PUTs the Excel file straight to a presigned `put_object` URL on the bronze
  bucket (`api/routers/admin_pipeline/uploads.py`), so `connect-src` names `https://*.s3.amazonaws.com` and
  `https://*.s3.us-west-1.amazonaws.com` (`S3_UPLOAD_ORIGINS`). A host pattern, never the bucket name (it carries the
  account ID and this repo is public). The first version of this slice missed it (review finding, fixed before
  merge); `tests/unit/test_aa876_frontend_edge.py::test_admin_csp_lets_the_s0_upload_reach_s3` keeps it in CI.
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

- Live check after deploy: upload one small .xlsx on `/admin/upload` and look for a CSP violation in the console;
  the header smoke alone does not run an upload.

- Local check (next start, Chromium): every script tag on `/login` carries the nonce, the page hydrates, an injected
  inline event handler is refused by the policy; `/robots.txt`, `/sitemap.xml`, a 404 and `/api/healthz` carry the set.
- `tests/e2e/smoke/aa876-security-headers.spec.ts` (smoke-public) checks a page, `/robots.txt`, `/sitemap.xml`, a 404
  and the BFF on the deployed host; `tests/unit/test_aa876_frontend_edge.py` checks the wiring in CI;
  `frontend/lib/securityHeaders.test.mjs` the policy builder (`npm run test:unit`).
- The 403s ZAP saw from `awselb` on the admin host are WAF block responses, not the app: AA-CIS-Infra adds the headers
  to the WAF custom responses in the same wave.

## S294 (lane l16-sec-headers, ZAP re-run of 25/11)

- **Decisions:** no header change in the app. Live `curl` on `/`, `/login`, `/robots.txt`, `/sitemap.xml` and a 404
  shows the full set already (nonce CSP, HSTS two years with sub-domains, Permissions-Policy, COOP, no `X-Powered-By`,
  no `Server`). HSTS stays at two years (the slice asked for at least one; lowering it would be a step back).
- **Root cause of the S294 ZAP alerts** ("CSP / HSTS / Permissions-Policy not set" and `Server: awselb/2.0` on `/`,
  `/robots.txt`, `/sitemap.xml`): ZAP's first requests carry no `User-Agent`, the WAF managed rule
  `NoUserAgent_HEADER` blocks them, and that block is the ALB's plain 403 with no header (WAF sampled requests of
  the scan window). The app never answered those requests. Fix belongs to AA-CIS-Infra: a custom block response with
  the security headers on the managed rule group (follow-up), or a `User-Agent` on the scan.
- **Changed:** the header smoke also checks the host root `/` (the scan's first request; a redirect, still the set).
- **Should know:** the `next.config.ts` redirects (`/upload`, `/review`, the IA table, `/portal/*`, `/trip/*`) still
  leave with no header at all: Next answers them before `headers()` and before `proxy.ts`. Moving them into the proxy
  touches the auth gate, frozen until after the 26/11 meeting: follow-up.
