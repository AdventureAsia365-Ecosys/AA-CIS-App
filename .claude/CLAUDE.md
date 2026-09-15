# AA-CIS-App — Claude Code Context

## ⚠️ RESTRUCTURE 15/09/2026 — ĐỌC TRƯỚC
- **Workspace gom lại:** repo này giờ nằm ở `~/projects/AA-Ecosys/apps/AA-CIS-App` (KHÔNG còn
  `~/projects/aa-cis/AA-CIS-App`). Multi-repo: mỗi repo giữ `.git` riêng, deploy độc lập.
- **Org GitHub đổi tên:** `AdventureAsia365-CIS` → **`AdventureAsia365-Ecosys`** (redirect tự động
  còn hoạt động). Tên repo GIỮ NGUYÊN (`AA-CIS-App`). Remote origin đã trỏ org mới.
- **Hệ sinh thái 3 repo** dưới cùng org: `AA-CIS-App` (repo này), `AA-TripPlanner-Web` (B2C trip
  planner, dùng chung RDS acc2 — schema `tripplanner.*` + `shared.destinations`), `AA-CIS-Infra`.
  Sắp có `AA-Booking` (AAA). Sơ đồ tổng: `~/projects/AA-Ecosys/docs/ecosystem-architecture.md`.
- **CI/CD:** action `aws-actions/configure-aws-credentials` đã nâng `@v4`→`@v6` (Node 24) trong
  `deploy-dev.yml` + `eval-regression.yml`. OIDC role/tên tài nguyên KHÔNG đổi (độc lập tên org).
- Nội dung LIVE STATE bên dưới (nghiệp vụ) vẫn giữ nguyên, chưa re-verify lại sau restructure.

# Updated: 25/08/2026 (AA-458, PR #219+#220 merged + deployed + real HTTP-verified) | main d45d130
# latest migration: 117 (shared.tenant_integrations, AA-457 — per-tenant WordPress credential
# pointer; AA-458 itself needed none, only read/wrote the existing publish_log/content_piece
# tables from migrations 116/115)
# NOTE: ECS task def below is LIVE-VERIFIED as of 25/08/2026 (`aws ecs describe-services`, now
# :139, post-AA-458 deploy). Deploy Prod # / Vercel Prod hash were NOT re-checked this session —
# still treat those two specifically as unverified until a fresh check. The full T7→T8→T9→T10→T11
# tenant content pipeline is now built, deployed, and live-verified end-to-end — see AA-458's LIVE
# STATE entry below, full trace in docs/implementation-notes/AA-458.md "Live Verify".

## LIVE STATE
- API: https://api-cis.lumiguides.it.com ✅ (via API Gateway 4ylo382khg — corrected 22/08/2026,
  AA-432; `owq9as3wjl` was stale/no longer exists, confirmed via `aws apigateway get-rest-apis`)
- **T7→T8→T9→T10→T11 tenant content pipeline: COMPLETE (25/08/2026)** — AA-456/457/459/460/458
  closed the full chain. T11 = real, tenant-facing WordPress publish, blog-channel only (7 other
  channels deferred, no code). `shared.tenant_integrations` (migration 117, AA-457) holds a
  per-tenant WordPress site pointer; real credentials (URL + Application Password) live in
  Secrets Manager (`acp/cms/{tenant_id}`), never plaintext in Postgres — `POST /v1/integrations/
  wordpress` (save) + `/test` (test-connection, `GET {wp_url}/wp-json/wp/v2/users/me`). AA-460
  fixed a real false-positive found live during AA-457's own verify: `test_wordpress()` used to
  accept ANY `200` as success, including anti-bot/WAF HTML challenge pages — now requires
  `content-type: application/json` AND a real WordPress-shaped body (`id` field) before reporting
  success. AA-458 applied the identical lesson to the actual publish call itself
  (`WordPressAdapter.create_post()`, `services/acp_s4_blog/cms/wordpress.py`) — requires `id` AND
  `link` in a real JSON response before writing `publish_log status='published'`; anything else
  (including a 200 HTML page) writes `status='failed'` with a clear `last_error`, never a
  fabricated success. `GET /v1/publish-log/pending` + `POST /v1/publish-log/{piece_id}/publish`
  (api/routers/v1_publish.py) are the tenant-facing list/publish endpoints — ownership + `status=
  'approved'` + `channel='blog'` all checked, 422 "Connect WordPress to publish" when no
  integration exists. `/portal/t11-publish` (frontend) is the standalone route (Sidebar NAV1,
  "Publish") — connect form when not connected, pending-content list + per-piece Publish button
  otherwise. A4's force-unpublish (`admin_a4.py`, from AA-455) is the safety net — no admin
  pre-approval gates T11's own auto-publish (ADR-2026-038 §0.2), by design.
  **Known, real, non-blocking gap**: neither AA-460's nor AA-458's live-verify could get a
  genuine successful publish against the real test site `aa-wordpress.rf.gd` specifically — that
  free host's (InfinityFree) anti-bot layer challenges every non-browser HTTP client
  unconditionally regardless of credentials, confirmed structural and persistent across both
  sessions (a real Playwright browser sails through it fine — used successfully for Application
  Password creation/revocation both times). The negative-path guarantee (never report success on
  a page that isn't really WordPress) is thoroughly live-proven either way; the positive path
  (real WordPress JSON → real published row) is proven by deterministic unit test only
  (`test_create_post_success_real_wp_response`) — needs either a non-anti-bot-gated test site or
  time for this one's escalation to lapse to close live. Full detail (this task's own trace):
  `docs/implementation-notes/AA-458.md`. AA-456/457/459/460's own session notes live only in
  `docs/claude_audit/` (gitignored, not committed) — not re-derivable from this repo checkout,
  see their respective Linear issues for the full record instead.
- AA-452 (24/08/2026) — T10 extended from 6 gates to the full F1-F9 set (F3 structural variance/
  F5 atom density/F7 FAQ dedup added), scoped to `channel == 'blog'` only — the other 7 channels
  keep the exact 6-gate stack AA-450 shipped, unchanged. STEP0 correction (this task's own
  premise was off by one): AA-450 already had 6 gates, not 5 (`F4_extreme_length` already
  existed) — real gap was 3 missing gates, not 4. `services/acp_content_writing/prompts.py`
  now instructs the `blog`-channel writer to produce real markdown `## ` H2 sections, a
  `## FAQ` section (`**Q: .../A: ...` pairs) when the piece has one, and `[R:{atom_id}]`
  citation tags after fact-bearing sentences — verified reliable via 3 real Bedrock runs before
  any gate code was written. Citation tags are internal-only, never tenant-visible, for any
  channel or outcome: `quality_gates.strip_citation_tags()`/`deep_strip_citation_tags()` run
  once at the end of `service.write_and_check()`, on `content_text` + every `gate_ledger`/
  `repair_log` violation string + `held_reason`, for both `approved` and `held` pieces — real
  live-verify (real Bedrock, real RDS, real HTTP through the actual domain) confirmed zero
  `[R:`/`[F:` leakage on a real held blog piece whose gate violations DID internally quote
  tagged text before the scrub. No new DB column for the tagged intermediate — no real consumer
  yet (T10 admin review-queue UI still out of scope), avoided speculative schema. 79 new/updated
  tests, full suite 1585 passed (post-rebase onto AA-451/#208, which merged mid-session and
  redeployed the task from under this session's own live-verify — rebased cleanly, no conflicts,
  re-verified). Real post-deploy HTTP verify (real tenant `test-n1-flow`, actual domain): full
  create→goal→choose→write lifecycle for `channel='blog'` ran all 9 gates (held on real content
  after 2 attempts — gates evaluating critically, not rubber-stamping), zero tag leak in the
  `/write` response, the persisted DB row, AND the independent `GET /pieces/{id}` re-fetch;
  `channel='facebook'` regression-checked at exactly the pre-existing 6 gates, approved, no leak
  (confirmed no-op). **`GET /health` stayed 200 across 8 checks over ~65s while `/write` ran the
  full 9-gate blog path** — the one check AA-450 could only defer to post-deploy, now confirmed:
  the non-blocking `asyncio.to_thread()` guarantee holds under real traffic with the larger gate
  stack, not just synthetic unit-test timing. **PR #210 merged** (`d1f8ea0`, squash), deployed
  live (task def `:133`). Real, pre-existing, NOT-AA-452 finding noted in passing: T8's own
  `/goal` and T9's `/write` endpoints both hit a ~29s API-Gateway 504 on real long LLM calls
  while the backend keeps working and completes anyway (confirmed via DB re-check both times) —
  a real gap worth its own ticket, not something this task's scope covered or fixed. Full detail:
  `docs/implementation-notes/AA-452-t10-nine-gates.md`, gate-map/decisions in
  `docs/claude_audit/AA-452-t10-nine-gates.md`.
- AA-451 (24/08/2026) — T8's `create_request()` (`services/acp_angle_gate/service.py`) gains
  optional `year`/`month`: when given and no slot is already persisted for
  `(tenant, channel, atom)`, computes that tenant's month slot-grid with the SAME tenant-scoped
  fetchers T7's `GET /v1/planning/slot-grid` uses (never `allocate_month()`'s platform-wide,
  `owner_scope`-buggy fetchers — AA-445-02), persists it, and re-reads the real `cta_target` —
  closes the gap AA-450/migration 114 documented (`angle_gate_request.cta` realistically always
  NULL for real tenant self-service). T7's `GET /v1/planning/slot-grid` itself is UNCHANGED
  (still pure read-only, deliberately — STEP0 confirmed this was AA-448's intentional design,
  not an oversight); persistence is triggered only by real T8 usage, never by browsing. Nghiep
  confirmed this design (Option B of 3 presented) before build. Backward compatible — `year`/
  `month` omitted keeps prior behavior, T9's ask-the-tenant CTA fallback (422) untouched.
  6 new tests, full suite 1910 collected/1844 passed, 0 new failures (28 pre-existing
  live-DB-dependent failures confirmed identical on `main` before this change). Real post-deploy
  HTTP-verified (24/08/2026, real tenant `test-n1-flow`, via ECS-internal localhost:8000): full
  T7-finalize→T7-preview→T8-create(with year/month) lifecycle, `cta` came back as the tenant's
  real trip URL (not NULL); DB-confirmed the `acp_v2_slots`/`acp_v2_runs` rows were actually
  written; idempotency confirmed (repeat call, slot row count stayed 1); cross-tenant isolation
  confirmed (`test-agency` cannot see `test-n1-flow`'s persisted slot). **PR #208 merged**
  (`cc5fcf5`, squash), deployed live (task def `:132`). Full detail:
  `docs/implementation-notes/AA-451.md`.
- AA-449 (23/08/2026) — T8 Angle Gate, written fresh per ADR-2026-038 §0.5 (NO reuse of
  `acp_s4_social`). New `services/acp_angle_gate/` package + `api/routers/v1_angle_gate.py`
  (`/v1/angle-gate/*`, tenant-JWT-only) + `acp_shared.angle_gate_request`/`angle_gate_option`
  (migration 113, **applied live**). Terminology fixed vs. the round-1 STEP0 confusion: "Goal" =
  the 8-value Bang-1 list, "Angle" = the 3 LLM-generated options per (atom, channel) request
  (name/why_it_works/formula_fit/best_final_style). Bonus fix bundled in (build task's own §2,
  a real pre-existing gap STEP0 found): T7's `Slot.channel`/`compute_slot_grid()` extended from
  4 to 8 channel values (was silently unable to support 4 of Bang 2's 7 real channels — a tenant
  configuring e.g. `linkedin` would have hit a real Pydantic `ValidationError`) — also updated
  `admin.py::_VALID_CHANNELS` + `frontend/app/admin/tenants/page.tsx::ALL_CHANNELS`, the 2 other
  call sites of the same 4-value list. 53 new tests (channel-extension regression suite +
  generate/service/router units), full suite 1499 passed. Live-verified pre-merge (function-level,
  real Bedrock + real RDS, real tenant `test-n1-flow`): full create→goal→3-angles→choose lifecycle,
  including confirming the AA-448-class "stale response after write" bug does NOT recur here. Real
  live finding, not this task's bug: native acc2 Bedrock Sonnet 4.5 currently rejects with
  `...not available for channel program accounts...` — `LLMClient` correctly fell through to the
  acc3 satellite, exactly as designed. Full detail: `docs/implementation-notes/
  AA-449-t8-angle-gate.md`. **PR merged** (`8093645`), deployed live (task def `:130`). T9 now
  built — see AA-450 below.
- AA-450 (24/08/2026) — T9 Content Writing + T10-inline quality gates, written fresh (ADR §0.5).
  New `services/acp_content_writing/` package (`prompts.py`/`generate.py`/`framework_rubrics.py`/
  `quality_gates.py`/`service.py`) + `api/routers/v1_content_writing.py` (`/v1/content-writing/*`,
  tenant-JWT-only) + `acp_shared.content_piece` (migration 115, **applied live**). CTA gap fix
  bundled in (STEP0-flagged, build task §3): `angle_gate_request.cta` column (migration 114,
  **applied live**) + `services/acp_angle_gate/service.py::_fetch_slot_cta()` wired into
  `create_request()`. **Real finding confirmed live, not just predicted**: this CTA lookup will
  realistically stay NULL for essentially every real tenant self-service request — T7's own
  tenant-facing endpoint (`v1_planning.py::get_slot_grid()`) never calls `persist_slot_grid()`,
  only admin-triggered paths do — the live-verify run's `angle_gate_request.cta` was `None` both
  right after creation and after `choose_angle()`, exactly as this finding predicts. T9's write
  endpoint asks for a CTA (optional body field) rather than fabricating one, per SKILL_v2.md's
  own step 4. Architecture (Nghiep, post-Phase-1): ONE endpoint, write→check→up to 1 retry with
  specific feedback→persist, all inline in one request — not N7's separate async retry loop
  (Phase 1 traced N7's real production ALB-timeout incidents + low judge-gate convergence rate to
  that architecture, `docs/claude_audit/AA-450-01-t9-t10-retry-loop-investigation.md`); every
  blocking LLM call wrapped in `asyncio.to_thread()` from the start, not patched in after an
  incident. T10 = 5 gates (not N7's 9) — full gate-by-gate mapping:
  `docs/claude_audit/AA-450-02-t10-gate-map.md`. Frontend: NO separate `/portal/t9-write` route —
  mid-build decision, `AngleGateTab.tsx` (`/portal/t8-angle-gate`) extended into one continuous
  wizard, T9 fires automatically the instant an angle is chosen. 53 new tests, full suite 1552
  passed. Live-verified pre-merge (function-level, real Bedrock + real RDS, real tenant
  `test-n1-flow`, real atom `atom_0e9a4a62ed`): full create→goal→choose→write→check lifecycle,
  all 6 T10 gates passed on attempt 1, response verified to match an independent DB re-fetch (the
  AA-448-class stale-response bug, confirmed NOT repeated). **PR #207 merged (`9512d8a`), Deploy
  Dev green, task def `:131`.** Real post-deploy HTTP verify (24/08/2026, real tenant JWT, actual
  domain): full create→goal→choose→422-no-cta→write-with-cta→get lifecycle, ~15.8s for the real
  write+T10-inline call, all 6 gates passed, `/health` stayed 200 throughout (non-blocking
  guarantee held under real traffic, not just the unit test's synthetic timing). Full detail:
  `docs/implementation-notes/AA-450-t9-content-writing.md` ("Post-merge / post-deploy record").
  T10's standalone admin review-queue UI (for `held` pieces) and T11 (publish) remain out of
  scope, no issue created yet.
- AA-445-02 (23/08/2026) — B4 `CompetitorIndex`/`score_distinctiveness()`, DFS→T2, competitor UI.
  PR #199 merged (df19ec9), Deploy Dev run 32632513361 green, migration 111 applied live. Full
  live E2E verified: real tenant JWT (test-n1-flow) → real `POST /v1/competitors` add → real T2
  rewrite (real Bedrock call, `quality_score=10.00`) → T5 atomize produced 7 real atoms, **all
  `distinctiveness=HIGH`** (live-fetched competitor corpus, 120 phrases, not the old flat
  LOW/MED default) — confirms the actual new mechanism works end-to-end. Test data cleaned up
  after (0 leftover rows).
  **Real pre-existing gap found, NOT part of AA-445-02, NOT yet fixed**: `services/acp_planning/
  quarter.py::fetch_atoms_by_trip()` (the one real atom-loader both N5 `compute_quarter_plan`
  AND N6 `allocate_month`/`allocate_and_persist_week` call) joins `WHERE rt.tenant_id = $1` —
  i.e. it scopes by the TOUR's original owning tenant (`raw_tours.tenant_id`), not by
  `tour_atoms.owner_scope` (where T5 correctly stores the REWRITING tenant's id per
  ADR-2026-038 Hướng B). Since `/v1/tours/pool` only ever offers tours where
  `raw_tours.tenant_id = aa_internal`, **every T5 atom from a tenant rewriting a shared-pool
  tour is invisible to N5/N6 today** — live-confirmed twice this session (0 trips/0 atoms
  returned for test-n1-flow, both before and after the fresh 7-atom test run, across 15 real
  T5 atoms total). `score_distinctiveness()` itself is correct and live-proven; this is a
  separate, real blocker on the "N5/N6 actually use the new value" outcome — needs its own
  decision/ticket, not silently patched here.
- AA-432 (this session, part 2 — auth architecture) — **LIVE as of 22/08/2026, verified**:
  `/v1/*`'s API Gateway resource (`/v1/{v1_proxy+}`) changed from `authorizationType: CUSTOM`
  (`tenant-key-authorizer`, required `X-API-Key`) to `NONE`. Root cause (STEP0,
  `docs/claude_audit/AA-432-api-gateway-401-step0.md`): the tenant-portal BFF proxy only ever
  sent `Authorization: Bearer <JWT>`, never `X-API-Key`, so every `/v1/*` call 401'd at the
  gateway edge before FastAPI ever ran — confirmed this was NOT limited to 2 routes, it broke
  the whole `/v1/*` surface for both tenant and staff (`(internal)/*`) traffic. Confirmed safe to
  remove: `get_tenant()` (`api/routers/v1_tours.py`, `v1_exports.py`) decodes the JWT itself and
  never reads any gateway-authorizer context; the `X-API-Key` gate was a redundant coarse edge
  check, not the real auth boundary. **Do NOT re-add an `X-API-Key` requirement to the gateway
  for `/v1/*` without re-reading STEP0 first** — this was a deliberate, reviewed removal, not an
  oversight. JWT (verified in FastAPI) is now the only auth boundary for `/v1/*`, matching NONE
  at the gateway for `/admin`, `/auth`, `/content` already. Per-tenant rate-limit + revoke moved
  to the app layer: `api/middleware/rate_limit.py`'s `rate_limit_middleware` (already wired
  globally on `/v1/*`) now reads the tenant's real `shared.tenants.rate_limit_rpm` + `is_active`
  (Redis-cached 30s, `tenant_meta:{tenant_id}` key) instead of a hardcoded plan-tier RPM bucket
  and a login-time-only `is_active` check — `is_active=false` now blocks within ~30s instead of
  waiting up to 24h for the tenant's JWT to expire. **3 routers
  (`v1_acp.py`, `v1_s1.py`, `v1_s1_from_atom.py`) use a DIFFERENT, unrelated auth dependency**
  (`verify_tenant_api_key`, AA-181 — a real `X-API-Key`/`X-Admin-Secret` header FastAPI itself
  checks, independent of the gateway) — confirmed unaffected by this change either way, don't
  confuse the two auth mechanisms if touching either again.
  **Terraform applied 22/08/2026** via `Terraform Apply Prod` workflow (AA-CIS-Infra, PR #30 —
  merge triggered `Deploy Dev` here too, PR #190): `Apply complete! Resources: 1 added, 2
  changed, 1 destroyed` (new `aws_api_gateway_deployment`, `v1_any` method, `dev` stage
  repointed, old deployment destroyed). **Important fix bundled into the same PR**: the
  deployment's `triggers` hash originally only covered integration URIs + the authorizer
  resource's own id — never any method's `authorization`/`authorizer_id`. The first plan run
  showed a clean 1-attribute method change with NO deployment/stage change, which would have
  applied "successfully" while leaving the `dev` stage pinned to the OLD deployment (API Gateway
  stages serve a frozen snapshot; editing a method doesn't retarget a stage by itself) — the 401
  bug would not actually have gone away. Fixed by adding `v1_any`/`admin_any`/`auth_any`/
  `content_any`'s `authorization` (+ `v1_any`'s `authorizer_id`, coalesced since `NONE` leaves it
  null) to the trigger hash — if touching any of these methods' authorization again, keep them
  in that hash or a future change can silently no-op the same way.
  **Live-verified** (real minted tenant JWT, `test-n1-flow`, no `X-API-Key`): `GET
  /v1/tours/pool` and `/v1/tours/my-versions` → 200 with real data (previously 401 at the
  gateway edge, `x-amzn-errortype: UnauthorizedException`, before FastAPI ever ran — now the
  error body when the JWT itself is bad is FastAPI's own `{"detail":"Invalid or expired
  token"}`/`{"detail":"Not authenticated"}`, confirming requests now reach the app).
  `X-RateLimit-Limit`/`X-RateLimit-Remaining`/`X-RateLimit-Plan` headers present and
  decrementing per call. Revoke verified live: flipped `test-n1-flow.is_active` to `false`, next
  request → `403 {"detail":"Tenant is deactivated"}`; flipped back to `true` afterward (tenant
  restored to its original state). `/docs`, `/openapi.json` (unaffected NONE-auth routes)
  still 200.
- Frontend: https://aa-cis.lumiguides.it.com ✅ (Vercel — AA-103 production)
- ECS task def: **aa-cis-dev-api:133** (live-verified 24/08/2026 via `aws ecs describe-services`,
  post-AA-452 Deploy Dev, rolloutState COMPLETED) | main d1f8ea0 (PR #210 merged) | Vercel Prod
  hash unverified
- AA-384 (this session): product-direction correction on AA-309/AA-330's posts_per_week/Mirror
  (posts_per_week is now a free tenant choice, migration 099 — see DB SCHEMA below; Mirror is
  purely informational, no upsell language — see api/routers/admin.py get_tenant_mirror()). Real
  Marketplace UI now lives in THIS repo
  (frontend/app/admin/marketplace) — the AA-ACP-App copy (src/app/(admin)/workspace/marketplace)
  is abandoned, 0 traffic since 09/07/2026, not touched. Repo policy: AA-ACP-App is abandoned
  outright — any new ACP-facing UI work goes into AA-CIS-App, never AA-ACP-App.
- AA-330 (Marketplace catalog/portfolio) + AA-309 (N1 tenant onboarding: seed/angle/Mirror/Gate A)
  SHIPPED (merged main, PR #119) — then AA-384 corrected their posts_per_week/Mirror direction
  (see below). AA-330/AA-309 themselves are NOT reopened by AA-384.
- N7 (content production pipeline: DataForSEO keyword/SERP → Brief → Outline/Draft → Adapt(FB/
  TikTok)/FAQ → F2-F9 gates+repair → Slot scheduling, services/acp_produce/*, migration 096) —
  DONE (AA-368 through AA-380).
- N8 (weekly flywheel delivery: assemble_packet, ready/delivered lifecycle, usage_log,
  services/acp_deliver/*, migration 094) — DONE (AA-367, AA-372).
- AA-241 [AA-234 Phần C] SHIPPED Prod (S84): Review Queue UI full 11-field edit + fail markers + revalidate gate + reviewer audit. AA-234 epic DONE. AA-242 (Regenerate) tach doc lap Backlog.
  /admin/review-queue now returns full editable gc fields + audit columns (human_edited/reviewed_by/
  edited_at/revalidate_passed) + a `failures` array re-derived on CURRENT content via
  _derive_field_failures (shared _VALIDATE_FORBIDDEN/_CODE_FIELD_MAP consts from graph.py + seo_meta_utils
  thresholds — no logic copy). A code whose field a reviewer has since fixed is NOT re-surfaced. No new
  migration (still 072). 12/12 unit tests. Carryover AA-241 (Phần C) — edit UI maps 1:1 to PATCH fields.
- AA-234 Phần A SHIPPED Prod (S82): re-validate human-edited review content before approve. Reviewer
  edits a version in place (full fields, no new version) → async re-validate (build_revalidation_graph:
  validate→judge→brand_audit→human_edit_gate, NO flag_fix) → approve gated on revalidate_passed. Hard-block
  codes (META_TOO_SHORT/FORBIDDEN_WORD/etc) fail the gate even at high score. Migration 072.
- AA-233 SHIPPED Prod (S82): _execute_run_tour return dict surfaces fallback_used (was None; DB correct since AA-224)
- S82 backlog cleanup: AA-221 canceled (dup AA-223), AA-236 canceled (dead Lambda path), AA-160 deferred
- AA-238 + AA-239 SHIPPED Prod (S81): seo_meta band-guard — forbidden-word pad no longer accepted as
  in-band (D1: forbidden-free is a HARD band criterion; unified _seo_meta_forbidden ∪ tenant list) +
  sentence salvage picks longest complete-sentence prefix ≥140 instead of last-period-only/downward
  (D3); un-fixable cases escalate to _rerepair_meta → manual_check/HITL rather than reaching gold
- AA-235 SHIPPED Prod (S79): keyword_ideas shape guard — _as_list guarantees a list (dedup 4 inline
  copies → 1 module-level helper), FE Array.isArray guard in DfsCompareSection, writer persists [] on
  empty DFS + custom_keywords read-guard. Backfilled 21 legacy {seed:null} object rows → []. Fixes the
  28-version-tour Version Compare crash ("o is not iterable") + export-docx 500 (keyword_ideas[:25] on
  dict). Follow-up AA-236 = route effective_seed through build_seed() (seed quality, doubled "tours")
- AA-223 SHIPPED Prod (S79): async run-tour 202+job poll, pipeline_jobs table
- AA-205 SHIPPED (S71): post-repair seo_meta band guard — extract seo_meta_utils (single source of
  truth, breaks graph↔flag_fix circular import) + best_meta_candidate deterministic salvage +
  bounded _rerepair_meta (1 LLM call). Under-140 repair output can no longer clear the 7.0 gate into gold
- AA-215 SHIPPED (S70): revalidate node (flag_fix → revalidate → END) — re-validate+re-judge repaired content
- AA-213 SHIPPED (S70): persist fallback_used + score_overall + batch_id + revalidate_* to generated_content.metadata
- AA-214 SHIPPED (S70): .flake8 aligned to CI (max-line-length 120 + extend-ignore + exclude)
- AA-211/212 SHIPPED (S69): export gate + HITL review_queue re-wire
- AA-198 [F1] SHIPPED: brand_identity_id resolver + /admin/brand-rules + s1 brand-picker
- AA-197 [F2] SHIPPED: DataForSEO rebuild — buyer-market location, seed builder, real keyword_ideas
- Pre-ADR-2026-023 note: "Deploy Prod" workflow used to be a STUB/placeholder (no-op), real ECS deploy ran
  via "Deploy Dev" on develop merge (last run #128) — this Dev/Prod split and the develop branch no longer
  exist post-ADR-2026-023 (see CI/CD section below)
- AWS: STOPPED after S84 (cis-stop done — ECS desired=0, RDS stop, NAT stop). cis-start can o dau S85.
- Lambda aa-cis-dev-acp-s4-evaluate: DEPLOYED ✅ (AA-49 H-1)
- Lambda aa-cis-dev-acp-s4-trigger: DEPLOYED ✅ | ALB_INTERNAL_URL: FIXED ✅
- Lambda aa-cis-dev-acp-s3-campaign-planner: DEPLOYED ✅ (AA-45)
- API Gateway: 4ylo382khg (aa-cis-dev-api, stage `dev`; corrected 22/08/2026, AA-432 — see note
  in LIVE STATE above) | Lambda Authorizer: aa-cis-dev-authorizer (TOKEN type, identitySource
  `X-API-Key` ONLY — no Bearer/JWT branch, confirmed via source read, AA-432)
- DB: PostgreSQL 15, aa_cis_dev, secret: aa-cis/dev/rds (plain DSN)
- Models: Bedrock Haiku 4.5 (primary) → Sonnet 4.5 (quality fallback)

## STACK
- Backend: FastAPI (api/main.py → api.main:app), asyncpg, Redis
- Frontend: Next.js React 19, Vercel deploy
- AI: AWS Bedrock (us-west-1), LangGraph orchestration
- Infra: ECS Fargate, RDS PostgreSQL 15, S3, Lambda, Step Functions

## FRONTEND ROUTES (frontend/app/, real as of AA-384)
Admin (frontend/app/admin/*, gated by requireAdmin() in frontend/app/api/admin/[...path]/route.ts):
  /admin/dashboard, /admin/upload (S0), /admin/s1-rewrite, /admin/pipeline/{s1,s2,s3,s4-blog,
  s4-social}, /admin/master-content, /admin/curation (+/preview), /admin/review, /admin/brand,
  /admin/tenants, /admin/marketplace (AA-384 — catalog/portfolio/finalize UI, real repo), /admin/
  run-health, /admin/settings.
  Shared components: frontend/app/admin/_components/{AdminSidebar,adminUi}.tsx — adminUi.tsx is
  the design-token source of truth (A colors, serif/mono/sans, Card/Btn/Badge/TH/TD/LoadingScreen).
  New admin pages should reuse these, not invent new styling.
Content-team-facing (frontend/app/(internal)/*): /catalog, /upload, /brand, /review — older
  route-group split from /admin/*, still live, not part of AA-384's scope.
Tenant portal (frontend/app/(tenant)/portal/*): tenant-facing pipeline view, separate auth
  (/tenant-login). T-series routes: /portal/t0-brand, /portal/t1-rewrite, /portal/t4-pool,
  /portal/t6-atoms (AA-431), /portal/t7-planning (AA-448), /portal/t8-angle-gate (AA-449/AA-450 —
  ONE continuous wizard covering both T8 goal/angle-choice AND T9 write/T10-inline check, no
  separate T9 route — see AA-450's LIVE STATE entry). No dedicated /portal/t3-* route — T3 is a
  badge on t4-pool, not its own page (ADR-2026-038 §0.1).
API proxy convention: every /admin/* page calls same-origin /api/admin/[...path] (never the ECS
  API URL directly from the client) — that route attaches X-Admin-Secret + x-admin-user-id server-
  side after requireAdmin() verification. New admin pages MUST follow this, not fetch API_URL
  client-side.

## DB SCHEMA (Medallion)
shared.*              → tenants, pipeline_runs, membership_plans, tenant_brand_rules
silver_aa_internal.*  → raw_tours, generated_content, seo_context
gold_aa_internal.*    → published_tours
acp_shared.*          → marketplace_portfolios (097, DEPRECATED as of AA-444/23-08-2026 for
                        "tenant's current Marketplace" purposes — kept live ONLY as the seed
                        source for N1 pre-tenant onboarding, tenant_onboarding.portfolio_id
                        is a real FK into it; the tenant's live Marketplace view is now
                        GET /v1/marketplace, api/routers/v1_marketplace.py, see
                        docs/implementation-notes/AA-444-marketplace-view.md), tenant_atom_state
                        + tenant_onboarding (098, N1 Gate A), acp_quota_ledger, audit_log,
                        year_plan + quarter_plan/quarter_plan_version (092/112, T7),
                        angle_gate_request/angle_gate_option (113+114 cta col, T8, AA-449/450),
                        content_piece (115, T9+T10-inline, AA-450)
acp_contract.*        → tour_atoms (079; owner_scope was platform-only at 079, ADR-2026-038
                        Hướng B (21/08/2026) changed this to per-tenant — owner_scope=tenant_id
                        for tenant-rewritten-tour atoms (T5), owner_scope='platform' remains for
                        admin/A1 atoms — free-text column, no migration needed), v_trip_registry
acp_produce.* / acp_deliver.* → N7 production pipeline / N8 weekly flywheel delivery state

silver_{tenant_slug}.* → per B2B tenant (same structure)
gold_{tenant_slug}.*

### Key columns (verified 11/05/2026, tenants.posts_per_week added AA-384 08/08/2026):
tenants:             tenant_id, name, slug, plan_tier(enum), posts_per_week(int, 1-14, migration
                    099 — AA-384: free tenant choice, NOT derived from plan_tier anymore),
                    rate_limit_rpm, is_active, country
raw_tours:          tour_id, tenant_id, src_name, country, duration, price_raw,
                    src_itineraries, src_highlights, src_summary, pipeline_status(enum)
generated_content:  tour_id, aa_name, aa_subtitle, aa_summary, aa_description,
                    aa_highlights(jsonb), aa_itineraries, seo_title, seo_meta,
                    model_editorial, model_schema, status(enum), created_at
published_tours:    tour_id, generated_content_id, aa_name, aa_subtitle,
                    aa_itineraries, seo_title, seo_meta, quality_score,
                    s3_gold_path, published_at
seo_context:        tour_id, keyword_search, top_keywords(jsonb), keyword_ideas(jsonb),
                    provider(enum), fetched_at
pipeline_runs:      id, tenant_id, batch_id, status, cost_usd, llm_model,
                    tours_total, tours_passed, started_at, completed_at

## MIGRATIONS + APPROVAL GATES (stable conventions)
- Migrations are plain numbered .sql files in api/migrations/ (115 latest as of AA-450), applied
  against RDS via the global S3-mediated ECS exec pattern (see ~/.claude/CLAUDE.md) — no ORM
  auto-migrate, no psql direct connect (ECS container has neither). Each file self-registers into
  shared.schema_versions (version, applied_at, description) with ON CONFLICT DO NOTHING, so re-runs
  are safe no-ops.
- Two named approval gates in this codebase, same UI pattern (row-locked approve, reject re-
  approval), different subject:
  - **Gate A** (AA-309, acp_shared.tenant_onboarding) — a NEW TENANT's onboarding, approved once,
    flips shared.tenants.is_active true. POST /admin/tenants/{id}/gate-a/approve.
  - **Gate B** (AA-320, services/acp_planning/quarter.py) — a QUARTER PLAN VERSION, REQUIRED before
    it becomes allocatable, never auto-approved. Gate A's approval code explicitly mirrors Gate B's
    pattern (same row-lock/reject-re-approval shape) — they are two instances of one convention,
    not unrelated code.
- PR auto-merge: repo has allow_auto_merge=true, branch protection requires 5 status checks (Lint,
  Security Audit, Unit Tests, Integration Tests, Docker Build Check) and 0 required reviews
  (verified live 08/08/2026) — see "CI/CD — solo-operator mode" below. A PR still needs
  `gh pr merge --auto --squash` run explicitly; it is not automatic on open. A PR carrying a
  migration should NOT be auto-merged regardless of CI outcome — schema changes get a manual look.

## CRITICAL RULES
- raw_tours PK = tour_id (NOT id)
- published_tours has NO country column → always JOIN raw_tours ON tour_id
- seo_context has NO country column → JOIN raw_tours
- All UUIDs: default=str in json.dumps()
- Schema-qualify all queries: silver_aa_internal.raw_tours (not just raw_tours)
- max_tokens = 4096 (NOT 2000 — JSON truncation bug fixed)
- generate() is SYNCHRONOUS (asyncio deadlock fix, Python 3.12)
- Log group: /ecs/aa-cis-dev (CORRECT) | /ecs/aa-cis-dev-api (WRONG — always empty)

## CONTENT QUALITY RULES (aa_internal tenant)
- aa_name MUST be rewritten (not src_name passthrough)
- seo_meta forbidden: hostel, budget, public transport, cheap, backpacker, dorm
- Subtitle must differ clearly between V1/V2/V3 configs
- Brand: "Discreet Executive Adventure" | Target: 40-60 senior professional $250k+
- Forbidden words: deals, cheap, book now, instant booking, epic
- CTA: "Design This Journey"

## FASTAPI ROUTE ORDER — CRITICAL
/{id}/full MUST come BEFORE /{id} — FastAPI greedy matching.
NEVER reorder these routes.

## safe() PATTERN
Always use safe() for UUID and Decimal in JSON responses:
from api.utils import safe
return {"id": safe(tour.tour_id), "cost": safe(tour.cost_usd)}

## EXCEL PARSER RULES
File: api/services/excel_parser.py
- COLUMN_MAP: source Excel column name → DB field name
- Column "name" → src_name | "price" → price_raw | "itineraries" → src_itineraries
- Multi-header Excel: row 1 = group labels (skip), row 2 = actual column names
- Provider: title-case normalization ("horizon voyages" → "Horizon Voyages")
- Dedup by src_name + provider when no tour_id_external

## BEDROCK CONFIG
Primary: us.anthropic.claude-haiku-4-5-20251001-v1:0 (~$0.002/tour)
Fallback: us.anthropic.claude-sonnet-4-5-20251001-v1:0 (~$0.02/tour)
Region: us-west-1 | IAM: ECS task role has bedrock:InvokeModel

## SEO SEED RULE (BUG-3 fix)
DataForSEO seed keyword must be country-based, NOT tour name:
seed = f"{tour.country} tours" if tour.country else tour.src_name

## PIPELINE ARCHITECTURE
S3 Bronze upload → Ingestion Lambda → shared.pipeline_runs (status=ingesting)
→ Step Functions (bypassed — tech debt AA-22) → /v1/pipeline/run-tour (ECS)
→ LangGraph: validate → generate → evaluate → seo_context
→ Export: gold layer write + pipeline_runs status=completed

## AWS PATTERNS (WSL2)
- Single-line commands only (multi-line backslash hangs in WSL2)
- ECS has no AWS CLI → use boto3 for S3 upload from container
- S3-mediated ECS exec: write script → upload S3 → presign → ECS execute-command
- DBeaver tunnel: cis-tunnel alias → localhost:15432
- SSM only, no SSH port 22

## SESSION ALIASES (~/.zshrc)
```bash
cis-start  # start NAT Instance + RDS + ECS
cis-stop   # stop ECS + RDS + NAT Instance
cis-status # check NAT instance state
```

## KNOWN TECH DEBT — DO NOT BREAK
- api_task_def_arn hardcoded :21 in main.tf (AA-CIS-Infra) — do not change (AA-22)
- Step Functions deployed but bypassed — direct API flow only
- webhook_deliveries = 0 — deferred P2
- content_exports table does not exist in shared schema
- Lambda DATABASE_URL plaintext → Secrets Manager (P4-S6)
- mobile_card_text no prompt → always NULL
- AA-36: No char limits on rewrite fields — Backlog

## CI/CD
- ADR-2026-023 (trunk-based, effective 09/07/2026): `develop` branch removed. Feature/fix branch → PR →
  CI required → merge to main via PR — auto-merge enabled once CI passes (see "CI/CD — solo-operator
  mode" section below); human-only merge was the original design, now relaxed for solo-operator use.
  Push to main auto-triggers deploy (paths-filter, single pipeline — no more separate Dev/Prod workflows).
- Image tag: always :latest (never commit hash)
- Lint: flake8 (120 char limit, 2 spaces before inline comment)
- No static AWS keys — GitHub Actions OIDC only
- Vercel: auto-deploys on main push (CIS Admin)

## CI/CD — solo-operator mode (since 31/07/2026, S131)
- Auto-merge is enabled on PRs once all 5 required CI checks pass (Lint, Security Audit,
  Unit Tests, Integration Tests, Docker Build Check). No manual merge click required.
- This is a deliberate simplification for a single-operator team. When a second engineer
  joins, RE-ENABLE human-only merge review: go to repo Settings → General → Pull Requests,
  disable "Allow auto-merge", and require at least 1 approving review in branch protection
  (currently NOT required — only status checks gate merge).
- deploy-prod.yml was removed same session — see .github/workflows/DELETED-deploy-prod.md
  for rollback instructions when a real prod environment exists.

## TESTING
pytest tests/ -v
104 integration tests + 23 E2E Playwright tests baseline

## ACTIVE WORK — 08/08/2026 (AA-384)
AA-103 (all CIS admin pages live on Vercel prod) has been done since Session 31 — see FRONTEND
ROUTES above for the real, current route list instead of a page-by-page table here (this section
was stale from 23/05/2026 until AA-384's CLAUDE.md sync; don't let it go stale again — update it
per-session, not the LIVE STATE bullets which are meant to stay append-only history).

Open thread as of AA-384: the AA-384 Linear issue itself is deliberately left NOT Done by that
session — a product-direction change (posts_per_week/Mirror wording) needs Nghiep's explicit
confirmation, not an agent auto-closing it. Check AA-384's Linear status before assuming it's
settled.

## Implementation Notes Pattern
For every Linear issue involving code changes, maintain a parallel notes file
while implementing (not after).

Path: docs/implementation-notes/<ISSUE-ID>.md

Required sections:
- Decisions — choices made not specified in Linear issue
- Changed — what was modified vs. original requirement
- Tradeoffs — what was weighed and why
- Should know — anything reviewer needs before reading the diff

Trigger: create when starting any task — "implement AA-XX", "fix AA-XX", "build AA-XX"
Update incrementally as decisions are made — not in one batch at the end.

## Domain Model / Architecture Process (AA-539, 06/09/2026)
After AA-527 shipped a page that mixed up the Admin/Tenant boundary from having no single
written glossary, `mattpocock/skills` (`domain-modeling`/`grill-with-docs`/
`improve-codebase-architecture`/`codebase-design`) is installed (`.claude/skills/`) and this is
now standing process, not optional:

- Before starting any build/architecture task that touches multiple modules (especially UI
  Admin/Tenant work, or the T5-T11 social pipeline), read `CONTEXT.md` (repo root) first.
- If the task involves an architecture decision that is hard to reverse + surprising without
  context + the result of a real trade-off, write a new ADR in `docs/adr/` in the existing format
  (see `docs/adr/0001-*.md` for the first one, or the 26-ADR convention in
  `docs/AI-gent-for automation works/aa-soscial-media-main` for reference) — do not skip this
  because the decision "felt obvious in the moment."
- When a build prompt from Claude Chat has large scope (an epic, a multi-section redesign), run
  `/grill-with-docs` on that spec BEFORE writing code — even when the spec reads as complete.
  This is exactly the step AA-527 skipped.
