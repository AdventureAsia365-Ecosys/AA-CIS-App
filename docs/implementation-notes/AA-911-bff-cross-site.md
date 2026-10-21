# AA-911 — BFF write routes refuse cross-site requests

Pre-requisite for the fixed Preview hosts of AA-911 (root ADR 0006a A11). Found by the AA-911 security review
(blocking finding B-1). It deploys to CIS production **before** the Infra apply that creates the
`booking-preview` / `aa-cis-preview` CNAMEs.

## Why

`it.com` is on the Public Suffix List, so the registrable site is `lumiguides.it.com`. Every
`*.lumiguides.it.com` host is the *same site* as `aa-cis.lumiguides.it.com` and `portal.lumiguides.it.com`,
and its requests carry their `SameSite=Lax` session cookies. Until now previews ran on `*.vercel.app` (another
site). The fixed Preview hosts serve code from PRs that are not merged, on a same-site origin. That code could
send a `text/plain` POST (no CORS preflight) to `/api/admin/...`. The proxy forwarded the body as JSON with the
signed-in person's Cognito Bearer, so the result was an admin write. Booking was not affected: its proxy already
checks `isSameOrigin`.

## Decisions

- One rule, `lib/sameOrigin.ts` `isCrossSiteWrite(req)`: a request is refused when its method is not
  GET/HEAD/OPTIONS and `Origin` (or, without it, `Sec-Fetch-Site`) is not this origin. It is non-strict: a
  request with neither header is allowed, because every current browser sends one of them on such a request.
  A request with neither is a non-browser client, which holds no victim's cookies. The existing Playwright
  API smoke tests send neither header, and keep their 401 answers.
- `lib/crossSiteGuard.ts` `refuseCrossSiteWrite(req)` returns a 403 `{"detail":"Cross-site request refused"}`
  (`Cache-Control: no-store`) or null. It is the first statement of every write handler, before
  `requireAdmin`/`requireTenant` reads the cookie and before the body is read.
- Covered routes. Admin: `/api/admin/*`, `/api/pipeline/*`, `/api/tenant/*` and
  `/api/tenant/pipeline/brand-identity` POST. Portal: `/api/tenant/*`, `/api/playground/proxy` and
  `/api/auth/tenant-logout` (logout CSRF, the same as the admin sign-out).
- The portal gets byte copies (`portal/lib/sameOrigin.ts`, `portal/lib/crossSiteGuard.ts`), registered in the
  `VENDORED` map of `tests/unit/test_aa885_portal_split.py` and in CONTEXT.md.

## Changed vs. the review's suggestion

- **No 415 on non-JSON bodies (yet).** The review also suggested refusing any content type other than JSON or
  multipart, instead of forwarding it as JSON. Admin UI callers that send `JSON.stringify` without a
  `Content-Type` header (which the browser sends as `text/plain`) could not all be ruled out statically, so a
  415 could break real admin writes. The Origin check alone closes the CSRF: a cross-site request is refused
  before the body is read. The JSON coercion now only applies to same-origin requests. This is a follow-up
  hardening item.
- **Sign-in routes are not guarded** (`/api/auth/login`, portal `/api/auth/tenant-login`). They read no session
  cookie, so a cross-site POST cannot act as a signed-in user. Login CSRF is accepted for the dev phase. They
  are listed as exempt, with the reason, in `tests/unit/test_aa911_bff_cross_site.py`. The bridge-retirement
  branch also edits the admin login route, and this avoids a conflict.

## Tradeoffs

- Non-strict rather than strict: strict would also refuse header-less clients. That would change the existing
  smoke answers from 401 to 403 and gain nothing against a browser attacker.
- A static test (every write handler calls the guard before auth or body reads) plus live Playwright smoke
  specs, because the repo has no JS unit runner. The pure function is tested in the smoke spec, as AA-816 does.

## Should know

- Tests: `tests/unit/test_aa911_bff_cross_site.py` (static, CI unit job). Live specs:
  `tests/e2e/smoke/aa911-bff-cross-site.spec.ts` (admin) and
  `tests/e2e/smoke-portal/aa911-portal-cross-site.spec.ts` (portal). They need no session: a refused request is
  the guard's 403, and an allowed one stops at the auth check (401/403).
- Verified locally with `next dev` on both apps. Every cross-site write got 403 from the guard, every
  same-origin one reached auth, and a cross-site GET was not refused. Negative control: without the guard, a
  cross-site `text/plain` POST to `/api/admin/waves/check` reached the auth check (401). With a session, that
  would have been a forwarded write.
- Live verify after deploy (production host, no session):
  `curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Origin: https://booking-preview.lumiguides.it.com" -H "Content-Type: text/plain" --data '{}' https://aa-cis.lumiguides.it.com/api/admin/waves/check`
  must print 403. The same request with `-H "Origin: https://aa-cis.lumiguides.it.com"` prints 401.
- Before prod (review note 7): use `__Host-`-prefixed session cookies, so that same-site preview code cannot
  set a `Domain=lumiguides.it.com` cookie that shadows them.
