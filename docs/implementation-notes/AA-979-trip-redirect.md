# AA-979 P2b §4.3 — the admin host forwards `/trip/*` to the CRM client host

Session S254, agent Claude Code. Branch `feat/aa-979-trip-redirect`. Runbook: root
`docs/audits/2026-10-26-S252-aa979-p2b/RUNBOOK.md` §4.3 step 2 (design §5 301 map; critic S5). The page itself moved to
AA-Partner `web/app/trip/[tourId]` (branch `feat/aa-979-trip-slice`).

## Decisions

- **307, not 301** (`permanent: false` in `frontend/next.config.ts`). Browsers cache a 301 with no expiry, so anyone
  who followed it could never be sent back (critic S5). The switch to 301 is a P2b clean-up step (runbook §4.5).
- **Destination is a code constant** `TRIP_PAGE_BASE_URL` in `frontend/lib/apps.ts` (`https://go-dev.lumiguides.it.com`),
  the same pattern as `PORTAL_BASE_URL`: no Vercel env to set, one place to change for prod. Path and query carry over.
- **The CIS page stays** (`frontend/app/trip/[tourId]`): redirects run before pages, so it is not served, but reverting
  the redirect brings it back at once. Deleted at the clean-up.
- **`real_url` from env `TRIP_PAGE_BASE_URL`** (`api/routers/v1_trip_page.py` `trip_page_base_url()`), default the
  admin host. With the default, nothing in Terraform must change before this ships: a URL written on the admin host
  still reaches the page through the 307 (S250 lesson: code defaults first, env after). A value that is not a bare
  https origin is ignored (warning log) rather than written into stored page URLs.

## Changed

- `frontend/next.config.ts`, `frontend/lib/apps.ts`: the 307 and its destination.
- `api/routers/v1_trip_page.py`: `trip_page_base_url()`, `trip_page_url()`; `publish_trip_page` uses them.
- `frontend/app/trip/[tourId]/page.tsx`: header comment only (rollback target).
- Tests: `tests/unit/test_aa979_trip_redirect.py` (new), `tests/unit/test_aa482_trip_page.py` (base URL cases).
- `CONTEXT.md`, `README.md` (env table).

## Tradeoffs

- Env read per call (not at import): a test or a redeploy with a new value needs no restart logic; the cost is one
  `os.environ` lookup per publish (admin-only, rare).
- The stored `tenant_tour_pages.url` rows keep the admin host until republished or the env is set; they still resolve
  (307), and the T10 gate F6 reads `url_alive`, not the host.

## Should know

- **Merge only after** the AA-Partner page is live: `curl -sI https://go-dev.lumiguides.it.com/trip/<id>` -> 200
  (needs the AA-Partner web deploy and AA-CIS-Infra `CIS_API_URL` on the web task).
- Live after the Vercel production deploy: `curl -sI https://aa-cis.lumiguides.it.com/trip/<id>` -> 307,
  `location: https://go-dev.lumiguides.it.com/trip/<id>`.
- Setting `TRIP_PAGE_BASE_URL=https://go-dev.lumiguides.it.com` on the CIS api task is a later Infra change (module.ecs
  task definition; mind the S250 lesson on template revisions and runtime flags such as `S1_WRITER_V2`).
