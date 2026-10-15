# Admin API docs page (/admin/api-docs) — Refs AA-577

## Decisions
- New backend route `GET /admin/openapi.json` in its own small router (`api/routers/admin_api_docs.py`),
  `include_in_schema=False`, protected by `verify_admin_secret` (always, also local/CI — unlike the AA-577
  `/openapi.json` gate which is open off-ECS). Returns `request.app.openapi()` (no import of `api.main`, so no cycle).
- The router is included right after the hand-registered AA-577 docs routes and BEFORE every other router,
  so a future `GET /admin/{x}` catch-all can never shadow it. A unit test pins that ordering.
- Backend also requires `x-admin-role: admin` (403 otherwise). Reviewer/content staff share the backend admin
  secret (CONTEXT.md "Staff roles and permissions"), so the secret alone would not keep them out; the BFF always
  forwards the verified JWT role, same pattern as AA-797 `_require_tenant_admin`.
- BFF: new `API_READ_RULES` in `lib/rbac.ts` (`openapi.json` → admin) + `canReadAdminPath()`, checked in
  `app/api/admin/[...path]/route.ts` for every method. Without it any staff role could GET `/api/admin/openapi.json`
  (API_RULES.admin = all staff for reads).
- Page `/admin/api-docs`: PAGE_RULES admin-only; nav entry "API Docs" in the **Operations** group (admin-only like the
  other Operations pages; Settings is open to all staff, so it is not the right home).
- Renderer: dependency-free, built on the kit (Badge, Button, PageHeader, EmptyState, ErrorState, TableSkeleton,
  Spinner, tokens). No swagger-ui-react, no CDN script. Parsing is a pure module `app/admin/api-docs/openapi.ts`.
- Groups by first tag (spec `tags` order if declared — FastAPI emits none here — then alphabetical; untagged last),
  all collapsed by default (36 groups / 252 operations today). Search filters on method + path + summary +
  operationId + tag (every word must match) and auto-opens every matching group.
- Details per operation: description, operationId, parameters (name, in, type label, required, description),
  request body (schema name, content types, required, plus the top-level fields of the referenced schema),
  responses (code badge coloured by class, description, schema name).
- Fetch: react-query `["admin","openapi"]`, `staleTime` 5 min, Reload button = `refetch()`.
- `<main>` uses inline style + `useIsPhone()` for the phone padding instead of `className="aa-admin-main"`, so the
  file is clean of all kit lint patterns and is added to `KIT_CLEAN_FILES` (eslint `error` level).
- Review pass (adversarial, same branch): every secret-bearing BFF proxy (`/api/admin`, `/api/pipeline`, `/api/tenant`,
  `/api/tour-full`) now builds its backend path with `lib/bffPath.ts` `proxyPath()`. Next.js 16 splits the catch-all on
  `/` and THEN decodes each segment (checked on a built Next 16.2.4 app: `/api/x/a%2F..%2F..%2Fopenapi.json` gives the
  single segment `a/../../openapi.json`), and `fetch()` resolves the dots, so a reviewer could reach backend
  `/openapi.json` (and any admin-only write path) with the admin secret attached after the rbac rules saw a harmless
  path. `proxyPath()` returns null (BFF 400) for an empty, `.`, `..`, `/` or `\` segment and percent-encodes the rest;
  the backend decodes once, so it routes exactly the string the rbac rules checked (no double encoding: `a b` →
  `a%20b`, `x%2F` → `x%252F`).
- Review pass: the staff branch of `/api/tenant/[...path]` attaches the admin secret to ANY backend path, so a reviewer
  could already GET `/api/tenant/openapi.json` (AA-577 route) and read the schema this page keeps admin-only. It now
  refuses `openapi.json`, `docs`, `docs/*`, `redoc` for every staff role but admin (`lib/rbac.ts` `isApiDocsPath`).
  The tenant branch is unchanged (Bearer token only, no secret: ECS answers 403).
- Review pass, page: while searching, a group header click used to do nothing visible (open was forced) and silently
  flipped the non-search open state; now it closes/opens that group for the current search (reset on every keystroke,
  so a new hit is never hidden). Search input is `type="text"` (no second, browser-drawn clear button) and Escape
  clears it. A deprecated operation gets a "deprecated" badge next to the strike-through path.

## Changed
- Nothing from the AA-577 decision: `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect` untouched (a test pins it).
- Beyond the brief: the backend role check and the BFF read rule (see Decisions) — the brief only asked for the
  admin secret, but without them reviewer/content could read the schema through the BFF.
- CONTEXT.md: admin page list, Operations nav and the RBAC table updated.

## Tradeoffs
- Kit renderer vs swagger-ui-react: swagger-ui-react is ~1 MB+ of JS and CSS that would need the dynamic-import
  dance and does not follow the brand tokens / dark mode; its main extra is "Try it out", which is explicitly
  out of scope. The kit renderer covers what is needed to read the API at no bundle cost. Not covered: nested
  schema trees beyond one level, examples, security schemes per operation.
- The new route is always secret-gated (also local/CI), unlike AA-577's local-open docs. Local devs keep using
  `/docs`; the admin page is for the deployed system.
- Schema JSON is ~275 KB per load; acceptable for an admin-only page with a 5-min cache.

## Should know
- Backend test: `tests/unit/test_admin_api_docs.py` (403 no/wrong secret, 403 non-admin role, 503 secret unset,
  200 + `openapi` key + same schema as `app.openapi()`, AA-577 routes unchanged, route order guard).
- The page needs the backend deployed before it shows data: on a Vercel preview against the current Dev API the
  BFF reaches `/admin/openapi.json`, which does not exist yet → 404 → the page shows the kit ErrorState with Retry.
- UI not yet verified in a browser (no API/AWS in this run): do the aa-ui-verify pass (desktop + 390 px, light +
  dark, admin vs reviewer login) on the Vercel preview after the backend deploys.
- No frontend unit-test runner exists; `proxyPath()`, `isApiDocsPath()` and `canReadAdminPath()` were checked by
  transpiling them and running the cases above in node, and the parser against the real `app.openapi()` (252 ops,
  36 groups, the one untagged op `GET /health` lands in "untagged", 0 params labelled "any").
- `npm run lint` exits 1 because of 54 pre-existing errors in legacy files (CI step is continue-on-error,
  AA-601); none in the files this branch touches.
