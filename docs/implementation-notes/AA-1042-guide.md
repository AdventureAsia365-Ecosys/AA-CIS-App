# AA-1042 — Guide tab (lane aa1042-guide, S270)

Scope addition of S267: the AI gateway Guide tab renders the one prose source of the gateway (root
`docs/architecture/ai-gateway.md` and its `.vi.md`) plus live tables, with a CI drift guard in the root repo.

## Decisions

- **Endpoint:** `GET /admin/ai-gateway/guide` in `api/routers/admin_ai_gateway.py`, logic in
  `api/services/ai_gateway_guide.py`. POLICY `spend.read` (admin, finance, ai_ops), next to the route-control GET. The
  BFF rule `API_READ_RULES` prefix `ai-gateway` already covers `/api/admin/ai-gateway/guide`; `lib/rbac.ts` unchanged.
- **Response:** `{generated_at, page_id, doc, doc_reason, live}`.
  - `doc` is the English page plus its translations (`[{lang, path, title, markdown}]`); the tab picks the app language.
  - Owner tier (or any access but `staff`, or two English entries with the id): `doc = null`, `doc_reason = "owner_tier"`,
    and no page body is read (`docs_reader.DocsRestricted`).
  - Not in the bundle yet: `doc = {missing: true, reason: "not_published"}`; storage or format failure:
    `reason: "unavailable"`. The tab shows a note; the live tables still render.
- **Doc read:** new `docs_reader.load_staff_page_by_id(page_id, repo)`. It finds the English entry by catalog id in the
  manifest and reads bodies through `load_page(..., owner_read=False)`, i.e. by the manifest key, never a caller path. The
  id is a constant (`ARCH-01-ai-gateway`); the route takes no parameter. A translation that is missing, owner tier or
  unreadable is skipped and the English page still serves.
- **Links in the page:** `doc.links = {reader, published}`.
  - `reader` = `denial_reason(principal, "GET", "/admin/docs/page") is None` (ops.read today, admin only).
  - `published` = the staff pages of the bundle, sent only when `reader` is true.
  - The tab opens `/admin/docs?path=...` only when the backend says reader AND `canSeePage(role, "/admin/docs")`; every
    other link to a repo file is plain text (`lib/aiGatewayGuide.guideLink`, on top of `docsReader.resolveLink`, the
    same resolver as the docs page). https / mailto open a new tab; `javascript:` and the like are text.
- **Live tables reuse the existing readers (no new SQL):**
  - routes: `llm_route_control.state_of(world)` (the route-control GET payload, editor options and presets dropped)
    plus `llm_route_control.serving(world)`. `serving` runs `_resolve_all` with the live priorities, order mode and
    the refusal evidence, the resolver of the #1307 preview, so `first` is the deployment that would SERVE and a refused
    one is listed in `skipped`. Never `chain[0]`.
  - matrix: the deployment rows the refusal evidence already read (`stage_coverage.DEPLOYMENTS_V2_SQL`, with
    `access_state` and `last_probe_at`); falls back to the route-control deployment rows before CIS 367.
  - register: `connections.fetch_connections(conn, llm_only=False)`, every kind (LLM, Jev, DataForSEO, Geoapify,
    guardrails), reduced to names and states (`REGISTER_FIELDS` + served roles, credential source, deployment counts).
  - caps: `ai_gateway._read_connection_budgets` (the Budgets tab's connection tier) without `account_id`.
  - coverage: `stage_coverage.read` reduced to the stage lines and the `disabled` list.
  - Each source runs under `ai_gateway._guard`: one failure leaves its table empty and adds `{source, error}` with the
    generic public text (no exception text reaches the client).
- **Frontend:** `GuideTab.tsx` (kit `Section`, `DataTable`, `Badge`, CIS-local kit `MarkdownView`). The short page
  guide (`guide.ts` / `guide.vi.ts`) stays as a "How to read this page" panel; the kit `GuidePanel` is still rendered
  by `AiGateway.tsx` and passed in, so `lib/guides.test.mjs` keeps checking the page the kit way. Test ids:
  `ai-gateway-guide`, `ai-gateway-guide-doc`, `ai-gateway-guide-live-ready`.
- **No issue IDs in what the tab shows:** the page body keeps its issue references for engineers in git;
  `stripIssueIdsMarkdown` removes them at display time, line by line, keeping indentation, tables, fences and inline
  code. `lib/issueIds.stripIssueIds` on the whole body collapsed indentation (nested lists, mermaid), so it is not used
  directly. The root page was reworded in three places so the text reads well without the reference.
- **No migration.**

## Changed (vs the lane goal)

- Register lists every connection kind, not only the LLM kinds (the doc names the Jev / DataForSEO / Geoapify rows and
  their secret prefix too). Still names only.
- The coverage table shows the stage lines without their per-deployment detail (`models`); the Routing tab's Stage
  coverage card keeps the detail.

## Tradeoffs

- **The tab gives finance and ai_ops one staff-tier docs page they cannot open in `/admin/docs` (ops.read).** This is
  deliberate (S267: the guide must be readable where the gateway is operated) and limited to the one fixed id, staff
  tier only. They get no manifest and no other page body; their links render as plain text.
- The doc and live parts are read in parallel in one request (`asyncio.gather`), so the tab needs one round trip; the
  doc comes from the docs reader's 5-minute cache, the tables are read live on every request (staleTime 60 s in the
  tab). Five small reads per request; the page is rarely opened.
- `state_of` still computes the editor options per stage (the route-control GET code path) and drops them. A second,
  options-free path would duplicate the stage mapping; the cost is small.

## Should know

- **Order:** the root PR (vi twin, generator schema 2, recaptured snapshot, drift guard) merges first. Its Docs sync
  run uploads `ARCH-01-ai-gateway` and the `.vi.md`. Until then the tab shows the "not published yet" note.
- **The root branch is not pushable until the snapshot is recaptured (review fix pass).** The lane cannot run ECS exec,
  so the committed snapshot stays the schema-1 capture of 2026-11-04 (labelled 1, as on master). The generator now
  keeps the label equal to the payload: schema 2 needs both `connections` and `deployments`, schema 1 neither, and a
  relabelled schema-1 file is refused by `gen_llm_routes.py check`. The new docs-lint step `check_ai_gateway_names.py`
  stays strict and fails on a schema-1 snapshot, so the orchestrator runs `dump` in the CIS api container, `import`,
  commits, and re-runs both checks before pushing. A local simulation (current models + the CIS 376 keys + the
  connection keys the CIS migrations seed) passes the guard with 60 names checked, so no page edit is expected; if the
  live capture lacks a key the page names (for example CIS 376 not applied yet), correct both language pages.
- Integration tests: `tests/integration/test_aa1042_guide_db.py` (a saved route change shows in the guide with its
  version; a probe refusal moves `first` and is listed in `skipped`; the register / caps carry no account ID). Unit:
  `tests/unit/test_aa1042_guide.py` (owner tier never read, missing / unavailable notes, reader flag per role, names
  only with a planted env value, a refused first deployment not shown as first, `serving` passes the evidence).
- Regenerated with the pinned FastAPI 0.161.1 / pydantic 2.14.1: `tests/unit/snapshots/aa882_staff_route_policy.json`,
  `aa1035_role_route_matrix.json`, `docs/api/openapi.json`. `view_as.READ_ROUTES` is unchanged.
- The admin-evidence spec lists `/admin/ai-gateway?tab=guide` with the ready marker `ai-gateway-guide-live-ready`.
