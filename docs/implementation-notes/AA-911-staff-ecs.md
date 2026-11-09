# AA-911 — CIS admin on ECS (lane l7-aa911-staff-ecs, S271)

Root ADR 0019 d23 (+ S271 amendment): the CIS admin and the Booking admin leave Vercel and run on Dev as one Fargate
Spot service each (AA-CIS-Infra `modules/environment/staff_web.tf`): `aa-cis-dev-admin` (container `cis-admin` :3000)
and `aa-booking-dev-admin` (`booking-admin` :3001). Infra side:
AA-CIS-Infra `docs/implementation-notes/AA-911-staff-ecs.md`.

## Decisions

- `frontend/Dockerfile` copies the AA-Partner web image (Next standalone, node 22 alpine, non-root, npm removed from the
  runtime, `KEEP_ALIVE_TIMEOUT` above the ALB idle timeout, `HOSTNAME=0.0.0.0` forced in CMD: lesson S249).
- `output: "standalone"` only when `NEXT_OUTPUT=standalone` (set by the Dockerfile), so the Vercel builds do not change
  until the Vercel project is retired.
- `/api/healthz` (no auth, no upstream, returns the commit) for the container check, the target group and the deploy
  verify. Under `/api/`, outside `proxy.ts`.
- `deploy-admin-dev.yml` is separate from `deploy-dev.yml` (API/worker): different image, no job guard (the admin runs
  no job), its own concurrency group. It copies the latest revision of its own family `aa-cis-dev-admin` and swaps the
  image (S271 fix pass: the first draft shared one family with the Booking admin; per-app families keep the deploy
  roles apart and remove the "other container still on bootstrap" stop). When the service runs no task (desired 0,
  before the scale-up) the verify job is skipped and the result says so.
- Same public host as today (`aa-cis.lumiguides.it.com`), so `lib/cognito.ts` needs no change: outside Vercel the
  production client and the production host are already the allowlist.

## Changed

- **Found while testing the image:** behind the ALB, Next standalone builds `request.url` / `request.nextUrl` from its
  bind address (`https://0.0.0.0:3000`), not from the `Host` header. Two consequences, both fixed here:
  - every BFF write would be refused as cross-site (`isSameOrigin` compared `Origin` with `nextUrl.origin`): the check
    now takes the public origin from `Host` when the request came over HTTPS (`requestOrigin`), same result on Vercel;
  - redirects built with `new URL(path, request.url)` in `lib/cognitoRoutes.ts` pointed at `0.0.0.0`: they now send a
    relative Location (`lib/redirect.ts redirectToPath`). `proxy.ts` redirects were already relative (Next makes a
    same-origin middleware redirect relative).

## Tradeoffs

- `Host` is trusted only for the comparison with the browser-set `Origin`; a cross-site page controls neither. A
  non-browser client can forge both but carries no victim cookie.
- No source-map upload from the image build (no `SENTRY_AUTH_TOKEN` in the build); browser errors still report.

## Should know

- Local check (S271): `docker build` of `frontend/` passed; the container answered `/api/healthz` with the build
  commit, `/login` 200, the Docker health check went healthy with a foreign `HOSTNAME`, a same-origin POST passed and a
  sibling-host POST got 403, sign-in errors redirect to `/login?...` (relative).
- GitHub `dev` environment needs `vars.STAFF_WEB_ENABLED=true` (after the Infra apply) and `vars.STAFF_WEB_ALB_DNS`
  (the ALB DNS name, for the pre-cut-over verify); optional `vars.ADMIN_PUBLIC_SENTRY_DSN`.
