# Changelog

## [0.19.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.18.0...v0.19.0) (2026-11-07)


### ⚠ BREAKING CHANGES

* retire the CIS to Booking sign-in bridge (booking-token and its JWKS are gone) ([#1339](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1339))

### Features

* a3_place_dedupe acc2 rung (339) and central LLM budget scopes in spend_budget (340) ([#1249](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1249)) ([70b3cfd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/70b3cfdbf1c0985968f37c7eb158bddd751228ad))
* AA-subject gate on tenant pieces and tour retirement fan-out (flags, WordPress draft after notice, tenant e-mail) ([#1283](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1283)) ([c20912e](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c20912e5e28cd9672997ca9d7f6cd2a2f4f4fc3c))
* acp_shared.fact_sheet and fs_id on generated_content and tenant_tour_versions (CIS 386) ([#1362](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1362)) ([8fbfe66](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8fbfe66840d74209ff66a39a51b9961bac623a1a))
* add-on requests (agency asks, staff approve or decline in Tenant 360, staff bell) ([#1312](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1312)) ([44345fd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/44345fd95f09652478f1c20f1029f3856ebfb87b))
* admin Overview hides the TripPlanner customers and Booking funnel cards (B2B-only) ([#1271](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1271)) ([66e1c5c](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/66e1c5cb5a5cda8cb6d11092785117d8a760af80))
* admin route control center (order mode in the DB, provider order, presets, preview, versions with revert, Route control tab) ([#1302](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1302)) ([8bf8492](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8bf8492f6bb64c172c61dc15912785e6f69f9a6a))
* Agencies (CRM) withdraws a first-owner invitation and deletes a test agency (crm.ops.v1 1.8) ([#1240](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1240)) ([a5109c8](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a5109c8eb8bd4309682e18ebb1df10f018f25a48))
* agency first-owner invite can also invite the owner to the Content studio ([#1257](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1257)) ([01054cf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/01054cf0fca8ac57bf97f662993b374582b5e6b7))
* AI and data usage explorer over an hourly rollup of the paid-call ledgers (AI & spend &gt; Usage explorer) ([#1306](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1306)) ([c6a3139](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c6a313937c450c9eaee22dd89efa91bbb0b1ec1d))
* AI gateway Guide tab, the gateway page from the docs catalog plus the live route tables ([#1329](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1329)) ([bbf345c](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/bbf345c4ee1f16337afd1acbd348abf6047ed5b0))
* AI gateway lane L2 - Batch kill switch, provider switches for Jev/DataForSEO/Geoapify, balance and reconcile e-mails, embedding + Geoapify probe ([#1218](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1218)) ([28e21fb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/28e21fb552aa6be9d108a1f1bb47ce4e7f4656be))
* AI gateway registry v2 (families and capabilities as data, stage x model params, deployment api_style, provider kinds and settings from the connection rows) ([#1204](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1204)) ([d649cf0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d649cf05814859a485f40c92a5786aed144b9702))
* AI gateway reliability part 1 (connection circuit, alerts, job re-queue, failure-domain report, probe off) ([#1198](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1198)) ([723733d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/723733db5665678579fdf9b42a93594e976a34cc))
* AI gateway reliability part 2 (route deadline, legacy paths in the breaker, truncated class, access_state, tenant AI budget wait, prompt register) ([#1210](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1210)) ([be75f49](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/be75f497622dee0531ac9be50939a32e3d7304cb))
* AI gateway route trace, route alerts and fallback quality guard (Trace tab) ([#1301](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1301)) ([67a3ec8](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/67a3ec8916f4e5ef36f003b5f57bb5c7c3820471))
* AI gateway v2 step 1 zero-cost fixes (acc2 attribution, Anthropic spend-cap 429, Jev daily cap, DataForSEO cap on every path) ([#1197](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1197)) ([3cfba24](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/3cfba249730cfa7dd64049508a35c086d960ab1d))
* AI providers admin page (Providers tab + job Trace drawer) ([#1195](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1195)) ([2acf2b6](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2acf2b62f756b647bc2de2e90be0877f4423cd6e))
* AI providers C4 follow-ups (tenant AI budget, Experiments, access/probe/failure domain, warm paths, re-queue refused jobs) and live findings F1-F10 ([#1208](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1208)) ([cc03473](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/cc034731d3e1a3f6444e20323c679d58f138d7fc))
* AI providers Calls tab, tour trace and the AI & data spend tab ([#1205](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1205)) ([959ab59](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/959ab596e309bab565fcdc845b215b6aa76593b2))
* AI providers page shows errors and latency per row, the Jev / DataForSEO / Geoapify switch, CRM AI, Guardrails and Mapbox rows ([#1242](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1242)) ([ac015ff](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ac015fffb05410dc950f79cb0899f1e507722422))
* AI providers Routing, Models & trials and Budgets tabs (budgets read, old Settings/Cost Ops tabs redirected) ([#1199](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1199)) ([7f6e3c2](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/7f6e3c2a043812fd191e191fe457d2e767137c63))
* app profile AI switch per app and the federated ledger read (Geoapify status, latency, app, stage; Jev role decide) ([#1263](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1263)) ([a08d720](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a08d7209b0e18a1a9dfc16f739fc727c35c16634))
* app profile table (migration 345) and geocode_call ledger columns (migration 346) ([#1262](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1262)) ([378a145](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/378a14526609f88d1dfd6e3124903651ababe864))
* capacity limits table (migration 348) and rerun wave pause columns (migration 349) ([#1267](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1267)) ([02bbb26](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/02bbb26ddcbe8c367c06e32c34d8d05f460dbbe6))
* capacity S12: request lines, TPM limiter per connection and model, claim backpressure and wave pause ([#1268](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1268)) ([f260ff0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f260ff04874f6642a047cbd299fa6a2a62f39d99))
* catalog view of the TripPlanner app profile (migration 347) ([#1269](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1269)) ([e944b2a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e944b2a67cf57c34a56e0a7c285602930244bb61))
* catalog.http.v1 1.3 serves Tour.ai, the machine-readable AI mark of the Master text ([#1281](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1281)) ([666c339](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/666c339273682bc8b610b14b66361b7fae8cc7e2))
* central LLM budgets checked by the gateway before any attempt, admin editor and coverage per call site ([#1252](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1252)) ([55852c6](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/55852c65fd0169d8ac7d37d3e4e530104799791b))
* CIS 358 supply content AI mark columns and CIS 359 catalog.v_tour text_updated_at (migration-only) ([#1280](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1280)) ([c7ff205](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c7ff2058afdb1b7b8df36f75576ab5c485992351))
* CIS 360 content compliance flags, tour notices and retirement policy tables ([#1282](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1282)) ([884dc00](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/884dc00784471d9b0c7f899c9388e70a8ab44785))
* CIS 361 trial grace until the account tick and platform keys from the content plan ([#1284](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1284)) ([aba88eb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/aba88eb1eb4004d50e38eff84e0d554a9050ed96))
* CIS 365 catalog.v_tour_price_reference_v1 for the Booking reviewed reference price ([#1291](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1291)) ([1a48074](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/1a48074e584889a45bca17e69fe4bd8701bba81f))
* CIS 366 family fallback chains (model first, AWS preferred not only), Converse rows of Sonnet 4.6 / Haiku 4.5, acc3 and Anthropic caps ([#1294](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1294)) ([9ca2c61](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/9ca2c6120b4e4eea06762f90368391026ef609be))
* CIS 367 acc3 candidate models (disabled) and the per-deployment probe state ([#1295](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1295)) ([dc025b9](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/dc025b9cbb39fedc2b0228f902f2f1a8c1205333))
* CIS 368 sets shared.tenants.api_key_hash NULL (contract) ([#1297](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1297)) ([227cb94](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/227cb948d90f9b6eb4663833fa92c8425400c05b))
* CIS 369 drops shared.tenants.api_key_hash (destructive) and the fixtures stop naming it ([#1298](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1298)) ([943b2f7](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/943b2f7559041479753934a3df46c2b2008045d2))
* CIS 370 route control settings, route config versions, ledger route version, deployment priority grant, gpt-4.1 deployment off (contract) ([#1300](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1300)) ([f2c279d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f2c279d12657ac7fc92d0450b334ef1c8bf1a61d))
* CIS 372 hourly AI usage rollup and the usage explorer saved views (expand) ([#1304](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1304)) ([58e74dd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/58e74dd7918dfb7ea30bf66056fdec49003c98cc))
* CIS 373 shared.llm_role_config.pii_class (content | pii), contract ([#1309](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1309)) ([f25a3f6](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f25a3f620e923e54db32ffbf91fbbc917cc93756))
* CIS 374 shared.tenant_addon_request (agency add-on requests), contract ([#1310](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1310)) ([ad9081f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ad9081ff3c6e96af4c45699eef0370d486217dca))
* CIS 375 removes the reviewer alias row and forbids reviewer (contract) ([#1315](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1315)) ([6a846cf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6a846cfdf518226d1c2352ff5bcfa7eee3395105))
* CIS 376 every Claude model of the Anthropic API key in the gateway catalog ([#1319](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1319)) ([711c224](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/711c22452441044dcf2bced80f4d256e24d1b56b))
* CIS 377 catalog.v_tenant_billing_v1, Booking's read of subscriptions, plan prices and bill-to (contract) ([#1320](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1320)) ([b7b7e57](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b7b7e571ee96e9d3b2fa4b7d95d7a6adb602c3e1))
* CIS 378, admin_users_email_chk accepts the Dev-only E2E customer_success address (A10d) ([#1323](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1323)) ([202fea7](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/202fea7788689f2f306c394243643fe927a4ef29))
* CIS 379 catalog.v_tenant_tripplanner_v1, TripPlanner's tenant read, and the demo tenant (contract) ([#1336](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1336)) ([d310e49](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d310e49134b1085eb5103ce202101fa2ed9f0eab))
* CIS admin image and Deploy Admin Dev to its own ECS service; redirects and origin check work behind the ALB ([#1338](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1338)) ([1d93c36](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/1d93c36c905de7829df4d2a5ef65d71072c470c7))
* CIS admin nav in seven groups (Home, Tenants, AA Catalogue, Tenant content QA, AI & spend, Writing Lab, Platform) with redirects ([#1277](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1277)) ([de0db1c](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/de0db1c599f5fb088e814069a6410d179326b35f))
* claim-level ground truth tooling (panel format, label store, detector, mutation and R1 harnesses) ([#1373](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1373)) ([5c17667](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5c176670fa86fdeeee437a79e1a25d82bc08d953))
* collapsible sidebar nav groups in the UI kit core, used by the CIS admin sidebar ([#1254](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1254)) ([53c4a13](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/53c4a131537680082bd58e625422adcabf5c11f4))
* contract smoke job for the acc2 judge / validate fallbacks, and an acc2 brand audit pass is held for a human ([#1232](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1232)) ([6c82c47](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6c82c4737660246047dfdd471fe3426328929efe))
* cost parity and Converse prompt caching (gateway S6) ([#1181](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1181)) ([6d721cf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6d721cfccb47f0417c31a8c4df772071c9167383))
* data scopes per stage replace the S235 / S3 egress constants (gateway S5) ([#1180](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1180)) ([459d4cd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/459d4cda7f843b7c6fcbc1ba0419d24822cc4362))
* Dev-only read-only E2E customer_success identity, no write allow-list (A10d) ([#1324](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1324)) ([8137655](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8137655580cf85126b12eb783de0dae84d31c38d))
* embedding calls write ledger v2 rows (attempt facts on success, refused and error rows on failure) ([#1221](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1221)) ([69b34c9](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/69b34c91f6ccdfb300d26aef8d3cbbb5caa3a286))
* embedding space registry, Titan V2 fallback embedding model (off) and the re-embed job ([#1225](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1225)) ([5ca2f07](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5ca2f07d8c139444f641d845333e0802a422f857))
* FactSheet module, persistence and fs_id stamps, W1 input measurements ([#1363](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1363)) ([7c48053](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/7c48053b415389b63f10082734dea06ee6a05df1))
* gateway cassette recorder stores raw replies with request and prompt shas (record-only) ([#1354](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1354)) ([f323247](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f323247ee8f9411a72466ef1135b44a59afb9b2d))
* gateway contract view carries route deadline, deployment routing facts and circuit state (migration 341) ([#1251](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1251)) ([586ade3](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/586ade34ddb1cef6b1c4265fcf60ab599915aed9))
* gateway owns retries, routes filter connections by served role with a typed refusal, embedding rows carry attempt facts and a real price, Llama 4 structured output in the prompt ([#1245](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1245)) ([4f1244a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/4f1244a9df92468b85c2ce317241ce020d6ae251))
* gateway owns the output-token budget (reasoning floor + one truncation retry per route model, route_cut alert) ([#1344](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1344)) ([f7d310d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f7d310db53ff4eb792c2131beae2d517f4fa007e))
* gateway owns the truncation retry and marks degraded instruments; effort-floor lint over every routed stage ([#1350](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1350)) ([0af34d6](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0af34d6d67a19f6a62406b021ae2c77d5e0a7ef8))
* gateway routing by pii_class, Writing Lab Releases and Metrics, Tenant 360 route, Catalogue settings and Place cards (AA-1039 R6) ([#1311](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1311)) ([25142da](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/25142dae7709f03f17457fb364e4cb53b64f8d10))
* gateway sends the stage x model thinking mode (Anthropic thinking per family, OpenAI reasoning_effort) ([#1346](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1346)) ([0b86afa](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0b86afadd2c993fb6f3e873b35b87a10929a8bf0))
* gateway view carries the stage x model params (350); tripplanner table reads revoked (351) ([#1264](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1264)) ([55d2c78](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/55d2c78fe7c8f70b452ae493693e249be870db78))
* guard the wave start and the deploy gate, gitleaks before push, workflow lint, PR text rule ([#1371](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1371)) ([8a10a75](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8a10a75bbfeeb9ffa2629ef06bb2f5b5d74963b7))
* hide_flagged pass for relocate-flagged pins with audited, lasting unhide ([#1178](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1178)) ([b1dbfca](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b1dbfca0d3b167577d7a5a49278be0e4200132d1))
* human labels on T2 versions and A1 rewrites, offboarding proof, retire the lessons loop and /v1/rules (WM-32..WM-34 code) ([#1188](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1188)) ([0928e75](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0928e7599c0f37933864e8bfa4058daf18cdb5ae))
* in-app ops checks replace the CIS CloudWatch alarms and custom metrics (S263 alert diet) ([#1227](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1227)) ([0d5a63d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0d5a63d7138578a4bd646c06832963b1c7fa9a25))
* judge / validate routes ending on the OpenAI API get an acc2 pick before that hop (migration 333) ([#1239](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1239)) ([0bf0991](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0bf0991621eebd7a1f3e12d2af9e37314083198e))
* kit AiNotice (staff, publisher, traveller) and the notice on admin reviewer screens ([#1235](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1235)) ([8a2def2](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8a2def27cf47b42acd36923ccdbee3ba2c76065d))
* kit AiNotice narration variant and AI-assisted badge form ([#1273](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1273)) ([8d14143](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8d14143a709873454fa92378a871ff24996666f6))
* kit core button variants (link, icon, segmented, chip, switch, group, list row, surface, underline tabs) ([#1177](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1177)) ([539fc58](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/539fc5872446747248ea57d0bef5c36e1d477640))
* kit ShellFrame, ARIA track tabs and the visual job for CIS admin ([#1317](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1317)) ([6d04188](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6d041888626c4dc3c3eab27479a94f72788f6484))
* ledger v2 writers and readers (one row per attempt with its outcome; Jev, DataForSEO, Geoapify outcomes; S1 Bedrock Batch rows) ([#1201](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1201)) ([18d0941](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/18d0941cea74afd02f9afcb4a7c5cab223510d8c))
* LLM availability sweep of disabled candidates and the stage coverage alarm ([#1296](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1296)) ([76cd648](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/76cd648a75ea3f15533afbf0c858c337af9a67c0))
* machine-readable AI mark on Content studio pieces, tour versions and Master tour text ([#1274](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1274)) ([ce1e70b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ce1e70b74f53bc0fd5df311afd3fee4583aa4cf3))
* migration 318 seeds the Jev daily cap and the S1 rewrite DataForSEO per-run cap ([#1196](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1196)) ([49f0d7b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/49f0d7b136cde4ee7c6daddcad894ab4fb7780ee))
* migration 319 ledger v2 (attempt outcome columns, CHECKs and indexes on the LLM, DataForSEO and Geoapify ledgers) ([#1200](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1200)) ([b0f4b33](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b0f4b331820c70216c956534554ed86d1bd16873))
* migration 322 provider connection v2 (decide, data and guard kinds, per-kind secret and host guard, limits; secret name and endpoint migration-only) ([#1203](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1203)) ([520def9](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/520def976e5b14fece820d16b6f3b433c2df8057))
* migration 323 TripPlanner gateway contract views (AA-642 S8) ([#1207](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1207)) ([a3d4941](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a3d4941203d31dda00c08e781bd0cc2be141e031))
* migration 324 catalogues five acc2 candidate chat models with an acc2 deployment each and no route ([#1209](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1209)) ([f47637a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f47637a7084eb7e431f283e7c2b2d0757a3b74c0))
* migrations 320 (registry v2 families, stage x model params, deployment api_style) and 321 (tenant AI budget, prompt register) ([#1202](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1202)) ([d954bf1](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d954bf18618d41f274bcdbfa3aa8021efa9db127))
* model lifecycle as data (nightly Bedrock lifecycle sync, migration 352, EOL bell and digest) ([#1266](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1266)) ([209554b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/209554ba44567c095bae348e572c33549a6e06df))
* model-first route order, family chain rules, $0 route check and model-scoped refusals off the connection circuit ([#1293](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1293)) ([e73d251](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e73d251f837af1b2a3e5898c0c0a81a3d00961a2))
* monthly budget per LLM connection and a nightly cost reconciliation (gateway S10) ([#1190](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1190)) ([0be32ff](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0be32ff8008663ce0421422962bdc488c66c58a0))
* one writing policy registry (writing_policy.yaml v1, compiler, lint, contradiction suite), gates fail-closed as on main ([#1368](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1368)) ([c7e9129](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c7e9129441decb31db4f8981400bc2cdc3282126))
* OpenAI and Azure OpenAI clients per connection, the OpenAI key from a runtime secret (gateway S9) ([#1191](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1191)) ([041ba9a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/041ba9a29a66b07d8d64a46982ec27a17e1a5c7c))
* pinned writing exemplars, tenant preference summaries and the Exemplars tab (WM-27..WM-29) ([#1186](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1186)) ([a70bb7a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a70bb7a31be5a935076ef15334ef2dfd5cc84bf0))
* plans and add-ons catalogue, Tenant 360 subscription and overrides, trial tick, plan-driven limits, fail-closed tenant AI budget ([#1285](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1285)) ([68a5b9d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/68a5b9d01ccc35e4da5b43b834df5f77d9bc7490))
* platform judge / validate stages that reach only acc3 get an acc2 Bedrock fallback last, gated on the contract smoke (migration 331) ([#1233](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1233)) ([9ba60fa](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/9ba60fa3d4d28ff4785f0a7916440fcb9d42088f))
* provider circuit table and credit ledger (migration 342), vendor API balance from the ledger (migration 343) ([#1253](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1253)) ([c29ee9d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c29ee9db49b8d11f034fe81eea9e520f9a44b55d))
* quality funnel snapshot (CIS-08), nightly watchdog alerts and GET /admin/quality/funnel ([#1358](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1358)) ([b3213c9](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b3213c978b0c640d16efed3f188cdb8eb4397f24))
* quarantine shared.pipeline_lessons and acp_shared.acp_output_rules in schema retired (WM-34, CIS 316) ([#1189](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1189)) ([ed422a2](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ed422a2f0bd074ef5519ffe8c8ff71b681257914))
* R2 close-out (CRM staff-scope read, Tenant QA own book, reviewer alias removed, send again to the kept address) ([#1316](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1316)) ([e426ed3](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e426ed3c6b21fa77765b084c06f0575e12f7db24))
* R2 slice 2: read-only view as partner, Assignments page, logged Lab read for ai_ops, staff bell ([#1303](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1303)) ([e86bd3c](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e86bd3c041215ee0d3fc647ebc538c8caab8f72a))
* R2 staff roles, deny-by-default role map and require_tenant_scope on every tenant route ([#1290](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1290)) ([408416f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/408416fbc0d42ef28b1a64df6433bd0d817d5163))
* R4 part 2 reports (TP-01, SEC-03, SEC-08, DEV-01, DEV-03, OPS-15) and a stream-targeted nightly probe read ([#1182](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1182)) ([55828c5](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/55828c5b636066029cbb0ea5bab4ab6838a17407))
* register the 32 manifest prompt versions (migration 325) and fail CI when a bump has no migration ([#1219](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1219)) ([f883889](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f88388966a0a5cd0b2601f3cc2e86c933dbbfdac))
* remove tenant API keys (tenant-login 410, no key issue, rotate or verify) ([#1292](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1292)) ([ee253a6](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ee253a6e8beb07396734ef3533dfae59b7e27e40))
* REPORT-mode final contract on every S1, T2 and T9 exit + W0 review follow-ups ([#1369](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1369)) ([24dc5ce](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/24dc5cec2a67c8cce5cea3514981f6d0d7fc5cfe))
* route deadline per role, segment research stages, dead routes off, Llama 4 prompt mode, ledger class role_not_served (migrations 334-338) ([#1244](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1244)) ([85693ef](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/85693ef4cd54030f43c4c8190eb969180a579f1e))
* route governance - family judge rule, route change evidence, invariant warnings, no per-request model_tier ([#1243](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1243)) ([9fcd1b8](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/9fcd1b8f4e5cc65f6c44ba384a31223d82507722))
* routes off the legacy haiku / sonnet keys, A-series writers fall back to the OpenAI API after every AWS path, OpenAI budget (migration 327) ([#1224](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1224)) ([f33d4e9](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f33d4e96864c526215c0892dfd5428f2ebafe708))
* serve tenant.entitlements.v1 at /tenant/v1 (account snapshot and account event feed) ([#1276](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1276)) ([76a80b4](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/76a80b465075e09231eb148ac243cdc3e249f369))
* shared AI gateway core aa_llm and its sync tooling (AA-642 S8) ([#1206](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1206)) ([ba6afe1](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ba6afe134863de9970e2d29982a542e0ecb94722))
* shared circuit for Jev, DataForSEO and Geoapify; credit ledger and low-credit alert for the vendor APIs ([#1255](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1255)) ([6d22f1b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6d22f1bf9a7a36dba2776eaf984ba894abd92158))
* Staff page grants CRM roles (multi-select) and a Platform owner preset (migration 330) ([#1230](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1230)) ([af9fd10](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/af9fd10915b3365f3c766f38a781daf47703335f))
* staff roles R2 migrations 362-364 (admin_role values, reviewer merge, tenant assignment tables) ([#1289](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1289)) ([22b1d8a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/22b1d8a02f17e1edf58678064c83f33a73348dee))
* stage kill switch and declared degrade mode columns, ledger class disabled (migration 326) ([#1220](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1220)) ([bca3956](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/bca39569f91063a8a2760f1aa4f0fb08079a9b64))
* stage kill switch refuses calls before any provider attempt; declared degrade modes pass the failure-domain rule ([#1223](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1223)) ([e8f0fa8](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e8f0fa8ae9b25546bcae5d6cf748eaec0b9c2999))
* stop naming the Booking B2C tables before their drop (data coverage, KPIs, erasure checklist) ([#1337](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1337)) ([32c32b0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/32c32b00a0a4d4a91f380a1e855e74864c9493b3))
* T10 per-check modes to promote F12_scope and F9_brand_style, unused seed facts for the F14 floor rewrite ([#1184](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1184)) ([484e092](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/484e0924e4db1f4c348e680173f7b54bb2fcd88d))
* T2 DOCX export carries the X-AI-* headers like the piece exports ([#1366](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1366)) ([257a36b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/257a36b6dd9a90272f37a7270797f7ca02bf207d))
* Tenant 360 Booking invoices (read-only) and the Trade nav group ([#1326](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1326)) ([8a7a718](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8a7a718486de2420ee4bf1026240e8bd47814749))
* Tenant 360 Members tab to invite and manage Content studio members ([#1256](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1256)) ([c722134](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c722134eea44f65954f4b096163dba895d2cdd83))
* tenant AI budget fails closed for T-stage calls when the spend counter cannot be read; new budgets hold jobs by default ([#1222](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1222)) ([45804c1](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/45804c1823c1f1476c3bdb2726193820b0880619))
* tenant control plane storage (CIS 353-355): plans, subscriptions, entitlements, account events ([#1275](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1275)) ([a633603](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a6336039eb0a2b104e292055a8d747ae4e294d92))
* tenant entitlement resolver fixes (CIS 356) and catalog.v_tenant_account_v1 for Booking (CIS 357) ([#1278](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1278)) ([014fd60](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/014fd609bbd039f341e5c91416cb0904d2834b22))
* tenant judge / validate stages that reach only acc3 get the acc2 fallback last, gated on the contract smoke (migration 332, held) ([#1234](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1234)) ([6d19c7a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6d19c7a0c69d6bd284ab55d7b51e063061d2628b))
* tenant writer stages fall back to the OpenAI API last (migration 344) ([#1259](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1259)) ([b86d66a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b86d66aeb3136ccc2d9d4dc2e2347eaa8d65f517))
* tenant_billing_tick moves the tenant account from the Booking billing status ([#1321](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1321)) ([4aed550](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/4aed550d64b056c7d359131ed2754ff851092e8c))
* tenant_provision saga on shared.job; Platform Agencies shows CRM features read-only; contract 1.1 copy ([#1279](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1279)) ([020f01f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/020f01f7817ad42587e07de98f27d2b3552b5584))
* tenant-row retention and the E2E identity skip the pilot tenant ([#1314](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1314)) ([03530bd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/03530bdc22f779c067851aa2defa8d0a4952b13f))
* tenant.addons.v1 (the CRM relays its owner's add-on request, M2M cis.tenant/addons) ([#1313](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1313)) ([9d11a67](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/9d11a67088276e1becc5e764e820e24f10665331))
* test and CI guards from repeated lessons (pre-push integration stamp, replay errors, order lints, renumber refs, nightly random order) ([#1360](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1360)) ([8e148fe](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8e148fe4a0e35535a089676e4641c4fffe6b8c43))
* the tour pool answer carries the price.from.v1 from price for the Content studio Marketplace ([#1299](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1299)) ([3568851](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/3568851cc4bc272d3af0c9765200f8ba052f2311))
* TripPlanner spend alert on the CIS bell and the CRM app profile relay ([#1270](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1270)) ([c0e19f2](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c0e19f2457223c4dd3326bd1aa31cdfb2f959678))
* widen the writer release config fingerprint, freeze mining and activation until R1, L0 lexicon warn-only ([#1357](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1357)) ([862fb15](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/862fb158292b4895e0261af0e6314bb800bee199))
* Writing Lab stage pages, one Spend page and the AI gateway in the admin IA ([#1287](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1287)) ([6dd79a8](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6dd79a8bf8d1383275e7c6012e6025f3de9c7f09))
* Writing Lab stage read over the existing learning tables ([#1286](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1286)) ([7f5a9f7](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/7f5a9f72f0ba34a6efd643e7c4bc6cdf25becc37))
* writing-memory weekly KPIs, soft-stop rollback, keeps and the disagreement queue (WM-30, WM-31) ([#1187](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1187)) ([5e3e2b3](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5e3e2b3d9b5f151359caa653ad590720158b16ac))


### Bug Fixes

* admin UI follow-ups from the S263 production check (overview alerts, table inset, Content Trace paging, Master Content range) ([#1229](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1229)) ([610c1d1](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/610c1d1d2fb151ddcaf69541cfeb27addb5da5bb))
* AI providers polish N1-N4 (cost tree wraps, Calls table uncapped, kit tabs on Tenants, plain words for codes and actors) ([#1211](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1211)) ([67668f8](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/67668f85767a4acc458ab43b3567b3b8da14ffc3))
* Blog Slate proposes Segments that walk their Route, written about the Subject's own moment ([#1342](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1342)) ([d3330cb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d3330cb1b960477dc31cad07290b317436db4f5a))
* bound the T9 blog write with a word band, a hard floor and a one-pass rule (t9_write v2) ([#1341](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1341)) ([8de568b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8de568bf1abc65307ba8d71504d1ee6b7f86df99))
* cassette records billed unusable replies; link rule and embedding scope documented ([#1364](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1364)) ([69ea471](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/69ea471dcfaac8a909473643f122b973fdd54128))
* catalog destination route hides suspect points and refuses merged ids ([#1185](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1185)) ([2b5b371](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2b5b3712af08261287fbda9763177064130b4a55))
* CIS 372 ships as a contract file (tenant_id rollup table with RLS off by design) ([#1305](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1305)) ([8265a20](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8265a20a3951ebd67b0648d7e12745efdc2c5283))
* contract smoke counts a refused provider call as a failed stage and skips a model that refuses the stage's forced tool choice ([#1241](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1241)) ([7f6e620](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/7f6e620b8211123d304994d1205507d62b6762ec))
* Create agency hides tenants that have an agency and explains a deleted agency's id ([#1258](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1258)) ([e651bc8](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e651bc89acecd3af26eaae7f27b1758d3167cd6d))
* daily LLM probe cap 50 ahead of CIS 376's Anthropic API rows ([#1318](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1318)) ([76fbaf6](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/76fbaf6ed5457d976800a0a5688ed4208f23008f))
* Deploy Admin Dev result step parses with jq ([#1343](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1343)) ([69737c4](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/69737c4246612434157bf28a68a6c82a38396a1b))
* gateway defers rungs behind an open refusal circuit instead of logging a refused row per request ([#1332](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1332)) ([2d9374c](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2d9374c48b441777469c8ef6958bfcca86f50487))
* guard review fixes (non-UTF-8 migration refs, replay error match, conftest smoke stamp, nightly-red issue) ([#1361](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1361)) ([0fc608e](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0fc608eaf938620142e74bca2ec98036400760b8))
* hide AI providers write controls from read-only sessions, fit tables at 1440 ([#1248](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1248)) ([06ce2ac](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/06ce2ac1f2747aa3dc7967c61cbf98f8cc848983))
* hide_flagged / unhide restore only what the hide changed, audit relocate reveals, re-check the stamp on confirm ([#1213](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1213)) ([0982cbd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0982cbd9d47fb7fcd77a32b438e3fcc2ec7a8ebe))
* migration 329 keeps only the shadow reset trigger function; the re-embed owner steps move to the Infra run-script ([#1231](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1231)) ([3c5ee82](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/3c5ee82fed648bb1576ce731098b33c46fec61b2))
* pin @playwright/test to 1.64.1 while 1.65.0 is incomplete on npm ([#1333](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1333)) ([122a15b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/122a15b6d332ce8ab762c0ec3b9f0b0955e193c7))
* R2 eval interim pairs controls on sample and input sha, refuses at $0 and pins the judge ([#1352](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1352)) ([e6e844f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e6e844f675d2074060a5b4fbf7c0acca7ef2970f))
* raise tenant writing token ceilings for 5.x models, one budget retry on a cut reply ([#1330](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1330)) ([40979fd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/40979fd44ee3f0d439d01202eb007b912dd9b079))
* record place-card groups left for a paid draft as needs_draft, carry over a replaced atom's card only when unambiguous ([#1194](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1194)) ([547040c](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/547040c4cce61c9e1e5023addb547920b87301f6))
* refusal noise: known no-access models re-probed less, logged at info and shown as expected refusals ([#1348](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1348)) ([3fef158](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/3fef1587146f5f24c741f7199fdc3f89752ba0b9))
* retire TP-03 and mark the B2C inquiry metrics historical after the B2B-only switch-off ([#1272](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1272)) ([4d4cbdb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/4d4cbdbd138b13ec33c5a46c4b5534a510395487))
* route preview names the deployment that would serve, skipping refused ones ([#1307](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1307)) ([3dd1182](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/3dd1182e6e3db83a209ab5dd357c58f9ace028a3))
* S1/T2 W0 safety interim (elephant policy, hitl exit through checks, approve gate, T2 interim profile, S1 judge off live path) ([#1355](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1355)) ([275ccbb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/275ccbbc53fcdc310d8132a673933f1a64d3b8fc))
* stage parsers read the first complete JSON value; contract smoke cases for the migration 333 stages ([#1238](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1238)) ([2aaa275](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2aaa275cc520a4f4fb6303071e3c67310b68b151))
* strict accept rule aa997.4 keeps model points the use day backs ([#1193](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1193)) ([1b6e944](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/1b6e9448f0c805552850bb0d40c47ce1aced3551))
* T9 attempt 3 stops and labels at $0, every failing gate stored, interim F9 policy ([#1356](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1356)) ([f5c2a32](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f5c2a32e620826935b25dde80b3598e8c15c2da9))
* Tenant 360 Members tab and agency studio box show writes only when the API allows them ([#1260](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1260)) ([d621ae7](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d621ae7ecef6ddaeaf423705cc07edb3dde2fbe9))
* tenants directory hides write controls the role cannot use ([#1331](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1331)) ([9f60884](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/9f60884d15bc34069191d728a20a9ece00feb2a6))
* the shell asks the Review Queue count only for roles that may open Review ([#1334](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1334)) ([590796a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/590796a3c3e173022ca5d950fdc6bbc5eda08782))
* upgrade the API image's Debian packages at build time ([#1335](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1335)) ([2ef1f69](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2ef1f690309cbc11bc400d99eb9de4d7eaaef795))
* view-as minors (verify-tenant, owner check on end, offboard and sign-out close sessions, E2E admin own only) ([#1308](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1308)) ([b59c0ae](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b59c0aeaf7ad28c1637f2b04eed282ccb3590729))
* W1 input measure closes its read-only transaction before the master_drift census ([#1370](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1370)) ([ff46807](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ff4680764bb72bb1d167a99bb98d2890a12b910a))
* wrap the Upload stepper so /admin/upload fits a 390 px phone ([#1250](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1250)) ([6693e48](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6693e48a8a05ac566246daa5790d05d708d268e3))
* Writing Lab gate tiles fit long values and titles ([#1288](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1288)) ([7b76b29](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/7b76b29e83b1d0f42be806c6f07223e573bf57fd))


### Code Refactoring

* retire the CIS to Booking sign-in bridge (booking-token and its JWKS are gone) ([#1339](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1339)) ([51b6b15](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/51b6b150d4b04c06b8933af7c71e33406df18a6e))

## [0.18.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.17.0...v0.18.0) (2026-10-30)


### Features

* circuit breaker, limiter, provider health and the Connections tab (gateway S4) ([#1176](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1176)) ([1878349](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/1878349b9fe2921da3ba7aaec20774c82f0af3c9))
* R4 quality, reliability and data reports (CIS-01, CIS-06, CIS-07, OPS-13, OPS-14, DATA-02, DATA-03, DATA-04) ([#1174](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1174)) ([02b1f4f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/02b1f4f3678ead2d6c6938f5d4b051815d2f8372))
* route LLM calls over deployments, alias legacy keys, accounts from the registry (gateway S3) ([#1168](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1168)) ([b7cd4cf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b7cd4cfa36b3b9fca42cfd2e3d01ccdba3251c21))
* T10 tier-2 LLM adjudicator for the recall forms seed alignment cannot reach (off by default) ([#1172](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1172)) ([a38b3dc](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a38b3dc59d81b6cbb194b9fee9a0ec574e74adfb))
* weekly and monthly money reports (EX-01, FI-01..FI-06, OPS-08) on the report engine ([#1169](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1169)) ([6372fbe](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6372fbeda5d8977bb63003d84ddcfdd80bc1d6e9))


### Bug Fixes

* an OpenAI 429 'no credits remaining' reads as quota_exhausted ([#1173](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1173)) ([b327896](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b32789677c63096a7126e6dc8824c39d35c5949a))

## [0.17.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.16.0...v0.17.0) (2026-10-30)


### Features

* Anthropic API deployments behind Bedrock and the connection switch grant (migration 306) ([#1163](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1163)) ([c96fc58](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c96fc58afa5734a8fc7a8f6096e6dd2bd9b8af9b))
* Anthropic Messages API adapter behind the connection registry (gateway slice S2) ([#1164](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1164)) ([ec0dd46](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ec0dd46bfbdd8ec1a91b08fbc38ea6ddfd4d87ee))
* daily safety net (OPS-12 digest, TP-03 handoff check, DATA-01 probe KPIs) ([#1162](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1162)) ([f7462bf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f7462bf055733603bf68ff544ee13bbd65549875))
* freeze chosen ids for a calibration labelling round and pick the fixed blog sample ([#1154](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1154)) ([2d959a1](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2d959a140d5d993b5c6f0de3f12cad4f38356ee2))
* kit form controls and staged lint rule no-raw-form-control ([#1155](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1155)) ([16d8428](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/16d8428106743add51dcc8f069fc678b23746eb0))
* LLM availability card and fail fast on account-level LLM refusals ([#1148](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1148)) ([907662a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/907662a22869264618653f6962ad1f2aecc4b921))
* LLM connection registry and model deployment tables (migration 304) ([#1158](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1158)) ([8128086](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8128086da66412c751c3d42d804cc637dea8eb3d))
* photo matcher v2 and a nearby/country cover chain for pool tours ([#1153](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1153)) ([557edcd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/557edcd04dfc77c82ca9da95c4a831bdf0090b64))
* read side of the LLM connection registry and an admin listing ([#1159](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1159)) ([2cc3cc5](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2cc3cc52b46a8ca38a6b936eebc8414c3ac2ae13))
* report core R1 (KPI snapshots, report runs, /admin/reports) ([#1161](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1161)) ([dd8eea0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/dd8eea05f6b4b72f6ea0ea6bce7b5fb02d6bbeff))
* seed alignment checks for the T10 recall gap, default off ([#1152](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1152)) ([5840b0a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5840b0a4bf60857f9c3becb1da0a56497d2b3f8f))
* strict geocode rule aa997.3 - route evidence only from the day's own stop ([#1147](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1147)) ([24fda20](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/24fda2089817dc576d1c74ba4291b3d14ecd41d0))
* T2 reads Facts Entries behind T2_FACTS, and the Facts Source label in the leak checks ([#1149](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1149)) ([fb68375](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/fb683750bb96093de9f32a3253a81ec7e162bd2c))
* writer input without the naming-note prose or a thin-moment shape ([#1151](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1151)) ([02e5e4c](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/02e5e4c806a5bffd3aa8522fe5c5fe63a074015e))


### Bug Fixes

* allow-list the TP-03 inquiry key contract views in the RLS catalogue rules ([#1166](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1166)) ([4f14735](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/4f14735aafab069de7eaac178ce6e64173eee4e9))
* Cost Ops shows the acc2 budget Terraform sets (300 USD) ([#1157](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1157)) ([e2d5914](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e2d59144239413801f33c67ee429633517a597ff))
* schema gate accepts a newer green standalone isolation probe after a probe-only red run ([#1167](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1167)) ([3980b77](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/3980b775cad2df51bae7c900727e70989941e7ae))
* the LLM availability card says unknown when the last answer is older than 6 hours ([#1160](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1160)) ([d4950cf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d4950cfe262dba59980ddb80dfe876647be8c61a))

## [0.16.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.15.0...v0.16.0) (2026-10-29)


### Features

* acp_shared.facts kind, valid_until, updated_by and tenant delete (migration 302) ([#1138](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1138)) ([bd8df50](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/bd8df505f62b16fffc49f019f09d4361bc97e208))
* Deploy Dev schema-gate and CI Migration preflight ([#1145](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1145)) ([b1d209e](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b1d209e63c019e8fd850358bd63db0532955603e))
* Facts Entry API, writer precedence and admin Facts page ([#1141](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1141)) ([d8fce64](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d8fce643f676bc6a6eb0686196b71d7dcbd54667))
* geocode accuracy v2 - stricter acceptance (switch), query variants, relocate recheck, hide policy ([#1139](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1139)) ([070dd7b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/070dd7ba9febe6889ccc6e06f45dc597798e241d))
* judge kappa per stratum, kappa diag fix, judge-model comparison harness ([#1132](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1132)) ([eba2ab7](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/eba2ab7faef82002d9112eab525ce5a1bed22d12))
* kit sync bot, Kit Consumers check and kit freshness report ([#1133](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1133)) ([715582f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/715582fddae24f0473cfef78208f9f035962b231))
* LLM prompt register, eval runbook and adhoc_aa900 label for R2 change evals ([#1136](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1136)) ([22d1b70](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/22d1b7046deeff37913ec3e980365657bb47754f))
* match tour photos to the destination of their own tour ([#1144](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1144)) ([2e8e10a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2e8e10a3eed22b9b75ca92a7313deca97189e230))
* pool cover falls back to a photo of the tour's primary place ([#1131](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1131)) ([978ea46](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/978ea46cad8f332c5ce527086521e63852a0efea))
* record who set a photo's destination (migration 303) ([#1143](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1143)) ([b683b24](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b683b241ef5271ac20c78cc9d5bea863e0ab203e))
* T10 gate recall tuning from the S234 panel defect classes ([#1140](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1140)) ([0c4dd6a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0c4dd6a6640cb6d7b87724480288dc1e38b283e2))
* T9 reader checklist built from the seed, behind a default-off flag ([#1135](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1135)) ([d085620](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d085620efc30e5fd17e741d5d69048d107a94ef8))
* upstream Booking layout, filter, menu, confirm and format parts into the kit core ([#1137](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1137)) ([fc224ff](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/fc224ff6e80caea0b6b331788479a0746b265d00))


### Bug Fixes

* membership feed sorts by the xid8 column, not its text output ([#1130](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1130)) ([e886d86](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e886d865eb2e0bf8d1317a5a9a2a0521af2702e2))

## [0.15.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.14.0...v0.15.0) (2026-10-29)


### Features

* activities, intensity and cover photo on the tenant pool API, with activity / intensity / recently-added filters ([#1111](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1111)) ([e85c37f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e85c37f4a82912ddad5cb73ccef8ca6fd856f580))
* add place_photo.s3_key_medium for a 960 w photo rendition (migration 301) ([#1124](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1124)) ([fe88f16](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/fe88f168016f7c636106cb31ea031ed2c6ecda45))
* add tenant_tour_versions.generation_meta (migration 297) ([#1112](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1112)) ([c5b6db6](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c5b6db685b4fc908769b7ee62e07f21ded3242d1))
* add tenant_tour_versions.judge_breakdown (migration 300) ([#1119](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1119)) ([0a80543](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0a8054381e7d58beebf84d1351037b535697d437))
* admin UI in English or Vietnamese (one switch, kit i18n) ([#1129](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1129)) ([3994a1a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/3994a1a804b3b5c8bb4abf83a675fbac8a1c961f))
* catalog.v_tour_place_name for the Booking trip-plans list (migration 298) ([#1118](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1118)) ([61b479b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/61b479b38d5a1a17bee8e56815729faae0399bff))
* docs reader shows the bundle's checked SVG diagrams ([#1110](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1110)) ([e1a5f82](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e1a5f820cfcfff909944e6f4dc397e1f3cdd27e6))
* ground the T8 angle brief in the content seed before the T9 writer reads it (flag, default off) ([#1108](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1108)) ([d3ae346](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d3ae3468a7ae398b2a6a3eb2f8c2aca9e65a984b))
* name every CIS database connection built from get_database_url ([#1109](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1109)) ([5b6a317](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5b6a317425d533f8832d9373d942683c56317cbc))
* recorded cost incidents on Cost Operations, with the 25/09 DataForSEO sweep ([#1106](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1106)) ([1b88edc](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/1b88edce3acf8500b3e35d39dc07cd909df98715))
* registered_keys and REQUIRED_CIS_KEYS on /health/ready ([#1121](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1121)) ([6a1f06d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6a1f06db66ed6a674820eb675f1678aabf3e32c9))
* serve a 960 w photo rendition (size=medium) and backfill it for stored photos ([#1125](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1125)) ([5923c46](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5923c46bd7a29e370d3383931de424c4487188f1))
* store the T2 judge breakdown per version and check brand claims against the source ([#1123](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1123)) ([b6b6ea8](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b6b6ea820d2009c972d40d88a405a414a8836557))
* T2 writing-memory stamp in generation_meta (migration 297), hidden from tenants, plus Check now ([#1114](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1114)) ([717c58f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/717c58f1802c9993bc6519bc530c504f71dfa711))


### Bug Fixes

* draw Mermaid diagrams after the brand font has loaded ([#1115](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1115)) ([4b356eb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/4b356ebeb0337ddfc465c712ef3280ba859875cc))
* let Content studio viewers call the read-only CTA check ([#1127](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1127)) ([6e68f48](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6e68f48ec5344bd81f9585c84d8ac0209cf41a89))
* Mermaid labels in the docs reader never break mid-word ([#1128](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1128)) ([5b13ccf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5b13ccf958cf1b0601b992f3f1da303b176be8cd))
* R2 eval judge reads the same writer input the label panel read ([#1120](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1120)) ([39909fc](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/39909fc29327eed8c7300d77930d00f30b842c57))
* settle the label rubric on one-phrase claims and add a calibration relabel path ([#1107](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1107)) ([051434e](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/051434ebf78ec3efda811211b843087148e6af2e))

## [0.14.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.13.0...v0.14.0) (2026-10-28)


### Features

* /admin/docs docs reader (API + page) ([#1100](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1100)) ([b874e30](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b874e3007d375f1b3cac0c8626e741fb23968283))
* add shared.dfs_call_log.job_id (migration 293) ([#1094](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1094)) ([0350c79](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0350c79547944e66fa80b690cce2b89405880d62))
* Cost Operations page (AWS bill, Jev, DataForSEO runs by job, month-end forecast) ([#1097](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1097)) ([d24fdd3](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d24fdd3526003f4e46451a0c1dd6b4817ab39387))
* docs reader EN/VI switch, Mermaid diagrams, callouts and Dev owner-page read ([#1104](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1104)) ([a427e40](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a427e40cb808c48aeadd09b191b0ddcd550dbb25))
* geocoder-only coordinates, route guard and non-place filter for places ([#1092](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1092)) ([2076d58](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2076d58352645f2893a40393a34371c8de68d959))
* inject approved writing rules into the four writers with a post-activation check ([#1099](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1099)) ([172e463](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/172e4632d809aa98cb49b6bebf892e676ca07f48))
* run LLM on acc3 only and drop acc1 from the gateway, cost reads and admin UI ([#1102](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1102)) ([9d919f0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/9d919f0f9cf3f2a451fdb1f3d065088637af5ade))
* T8 angle brief contract behind T8_ANGLE_BRIEF_MODE (AA-790 round 8) ([#1095](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1095)) ([646c2bd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/646c2bdd8f2f90c8fc16f7b929f93109422c21c6))


### Bug Fixes

* R2 judge uses the golden-label rubric; calibration needs both label classes ([#1096](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1096)) ([8d2a6bf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8d2a6bff56aa25756b57f84015ea3cfd1d0b9d4e))
* route transit tours on their legs (tour-level transit rule) ([#1091](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1091)) ([9c6dcc7](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/9c6dcc703d57c70a27eefa6cd412a99cdfb1e0ce))

## [0.13.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.12.0...v0.13.0) (2026-10-28)


### Features

* admin host forwards /trip/* to the CRM client host (307) ([#1082](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1082)) ([0ef0d14](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/0ef0d1475d9598ede50e6502c88f566370b31fde))
* deploy guard blocks only on an open wave or a running deploy-blocking kind (AA-993 G5) ([#1081](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1081)) ([69dc633](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/69dc6332ba83ed04763a1d3ab8594f080b1b6761))
* drain-safe s1_rewrite persist stages and a3_atomize tour lock (AA-993 G2, G3) ([#1077](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1077)) ([74c545d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/74c545dcc0c3cf589ab7f0a13ef2f76db9ca8e39))
* kit lint rules in the core at error with a shrinking suppressions baseline ([#1078](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1078)) ([9980781](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/99807813134cb8c7c1e0b2a4d1e5d7a0fc78f842))
* T9 writer round 7, brand focus from the style guide and the angle brief as a brief ([#1085](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1085)) ([2b7d776](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2b7d776cc68f1bdd85c656b52ee5432bee42e653))
* writing memory Releases tab (switch, train close, release actions) ([#1076](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1076)) ([39dc01f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/39dc01f06280536d98959eed9944b4e3dff326b2))
* writing-memory R2 runner and targeted shadow (WM-19, WM-21) ([#1084](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1084)) ([32d7473](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/32d7473fea32705faf7cfb6bdbd2907777eebee1))


### Bug Fixes

* clean cross-border place card locality/region when the rebuild refuses a country change ([#1074](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1074)) ([ecef8db](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ecef8db1cd1fc64fa4996573d0101a94d75017ac))
* dedupe catalog photos by path and use canonical destination covers ([#1073](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1073)) ([96eb393](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/96eb39329ea7ce9d079f4144735a35854bedfe94))
* keep S1 llm_call_log rows when a job is enqueued with the aa_internal tenant slug ([#1079](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1079)) ([e22e5fb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e22e5fb6cb4ffc3cc51e3e17c140dc53af39f158))
* keep Writing memory tables inside the page column (Releases, Signals) ([#1080](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1080)) ([1976bf9](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/1976bf94e8b313a890cf407dc47c6fecee229fb1))
* mark kit core useMediaQuery as a client module ([#1083](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1083)) ([a38efd4](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a38efd49b9bb7ad125cf89fd4c354aff4cc945db))
* pick the release tenant by name and show tenant names on the Releases tab ([#1086](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1086)) ([4ea505f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/4ea505f1908d38cc9ec7d20025ade2113158b5bf))
* pin self-propelled transit journeys in the place build ([#1089](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1089)) ([8aabaf0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8aabaf0a9b5accc7ba4ea3033f96bfad3c9c1112))


### Reverts

* T9 writer round 7 (blind panel did not pass the ship bars) ([#1087](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1087)) ([f43141b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f43141ba106c9177954ca58022f4c55be12e6de0))

## [0.12.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.11.0...v0.12.0) (2026-10-27)


### Features

* canonical UI kit core in frontend/app/_kit/core with manifest and copy check ([#1067](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1067)) ([de63cb4](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/de63cb470beff874afdcaeed5d4d8555da46acb4))
* Guide tab on Writing memory, built on a reusable kit Guide pattern ([#1054](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1054)) ([1b1b25b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/1b1b25b81eaf4f0a3682967c404a07d9ac1e3220))
* one kit token set in core/tokens.css with contrast and CSS guards ([#1071](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1071)) ([51d2a65](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/51d2a65a318e84168be764d7f74388bbaf12e16d))
* photo read views give copies of one file a single canonical path (migration 290) ([#1068](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1068)) ([857f94b](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/857f94b2d932a461565a5ac08091bbff243fb7b8))
* staff reconcile lists known E2E accounts apart; CRM agency list shows lock source and feed health ([#1063](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1063)) ([7b66218](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/7b66218842827324cba2cd880ccf1ef9f837a173))
* worker drain window from env, deploy releases not counted, released endpoint ([#1072](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1072)) ([960dafd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/960dafdad1440bd107254724c686dd4afc947a1f))


### Bug Fixes

* Browse Pool duration filter no longer overflows on hour-based tours ([#1048](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1048)) ([c9b0973](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c9b097374240fd928453e087f5b874131feed15c))
* every admin page uses the kit AdminMain frame ([#1056](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1056)) ([3e8b972](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/3e8b9722199c223ba6d5ab9dce7d5acb1ea48bcf))
* Guide fills the page width; Writing memory uses the standard admin frame ([#1055](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1055)) ([6e6a252](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6e6a252a453522a698fc52e9c12c8c5a55a9dd2b))
* one ranged day heading rule for every day reader ([#1061](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1061)) ([63216eb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/63216eb37bb33b6c69a5008881b15176cdef9f12))
* Pakistan and the Philippines in the place country lists (one source, drift test) ([#1047](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1047)) ([e4d4a6a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e4d4a6a4cca34dc9bab5568ee4083d7d4d2d9c66))
* place cards keep city localities and first-level city regions, clear one-sided border places ([#1065](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1065)) ([43a9213](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/43a92134ee684f467f3b8821c166c98ab4c16be6))
* place cards keep only administrative regions, empty remote localities, drop unverifiable claims ([#1059](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1059)) ([83dfd11](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/83dfd11484ffcd2596799d806b1d4da55724d7b7))
* place geocoding resumes after the daily credit limit and flags stalled names ([#1050](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1050)) ([6dc5d8d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6dc5d8d3ee868665e2e3647ce2f4e2aa8afd8cd7))
* Places Merges tab no longer crashes when a dedupe run has conflicts ([#1058](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1058)) ([d1409c6](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d1409c6d1e8f7737ca15bed705df653b2d490643))
* restore the place-card and segment-matching changes that [#1061](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1061) reverted ([#1064](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1064)) ([f8545b3](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f8545b38f4400cbad0b34e1710dcbc402f87d2a3))
* S1 day parser reads quoted day headings; last-step day guard ([#1057](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1057)) ([81bd11d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/81bd11dab0ca881ace33f4f76e97e99bcb8849e6))
* segment matching never drops an atom on an id collision ([#1060](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1060)) ([9a5d0ae](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/9a5d0aeb7a1295047e7ef0528f3640e319fc1094))
* tour countries come from shared.countries (ISO), not a hand list ([#1052](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1052)) ([69695ba](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/69695ba62d6e32be1a327b552634b0d144b871ea))

## [0.11.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.10.0...v0.11.0) (2026-10-26)


### Features

* admin Data coverage page, one live numbers table from raw uploads to TripPlanner, Booking and CRM ([#1032](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1032)) ([f983327](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f9833274a7faaab5a85ddac81d47825b4d6e8b38))
* CIS admin page for Booking supply content (list and /admin/supply-content/&lt;id&gt;) ([#1006](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1006)) ([7e3cdfa](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/7e3cdfa61b340947247ce73d2213ded5aa16573e))
* Dev-only E2E CIS admin session, read-only except the content-quality sentence-apply routes ([#1019](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1019)) ([f9654a5](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f9654a50057d1a00f62ea43bc700f334717325e5))
* golden set tables, importer and the no-tenant-text guard (WM-17) ([#1021](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1021)) ([2fb51e6](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2fb51e669210bc14b533ae464cd7989224980bb7))
* migration 283 allows the Dev-only E2E CIS admin address in admin_users_email_chk ([#1018](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1018)) ([2bb404f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2bb404f4ec357124ff405f0230af521cf14c28f5))
* My Content list carries each request's live post (published_at, published_url, published_piece_id) ([#1007](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1007)) ([2825323](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2825323f79437efcf500f76fc72b2aa08a2d78eb))
* one-click reason chips and example toggles (writing memory WM-07) ([#1005](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1005)) ([2989bfd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2989bfd4b126617e64dc27ec8a462f53c9090f8a))
* partner-pool Content studio sign-in, studio invitations and role checks ([#1029](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1029)) ([95cd283](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/95cd283b6c89b444694ea3f0e21d82301e880627))
* per-violation severity badges in the Content Quality drawers ([#1004](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1004)) ([5d385a0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5d385a0e805122d4c2f316bc5a602366cd686ac4))
* place-card draft on GPT-6 Luna (migration 286), 4000-token draft limit ([#1030](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1030)) ([59d006e](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/59d006e30862744430bdfc3738b82dafb213fa85))
* saved pins and angle requests survive place rebuilds (component redirects, mig 285) ([#1025](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1025)) ([7e02ff5](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/7e02ff501f5a34d3546c27c787db1ed7c5a8d4d9))
* segments, card links and places follow a Master publish ([#1020](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1020)) ([303ae6e](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/303ae6e9920d3627b6cf23255df7893077a92a35))
* T9 writer round 2 — no invented visit details, more AI-tell forms, no internal labels ([#1017](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1017)) ([a7d4bcb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a7d4bcb653008d55932da979fe972eebe396fbcd))
* T9 writer round 3 — fact-preservation invariant, panel-quoted tells, LinkedIn fidelity ([#1024](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1024)) ([fd7e946](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/fd7e946e03aa46e34155e2ad008b33e110db5e81))
* T9 writer round 4 — prompt-side AI-tell pairs, self-check, family presence from the seed ([#1027](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1027)) ([b0f191a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b0f191af3aa20db92dea6dbbb06b61a9993c2198))
* T9 writer round 5, family brands read warm and concrete again (children in the seed's moments) ([#1033](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1033)) ([33c09fc](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/33c09fc0fc442f684458e43d65c61158fe6b6003))
* T9 writer round 6, brand texture for energetic, luxury and nature brands ([#1035](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1035)) ([9c0fe93](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/9c0fe930a930e23c1b31b089e32be238741e19a9))
* T9 writer-side AI-tell pass, writer brief rules and LinkedIn lead-fact rotation ([#1015](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1015)) ([e5c82e9](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e5c82e9da44eb6814af09726755e2c7770a7ee02))
* tenant account endpoints for the portal (activity trail, CSV export, API key facts) ([#1002](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1002)) ([f580057](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f5800578b9c6cf2dbfc71941dfe28927ed8f12fc))
* tenant Members read over the CIS membership directory (GET /v1/account/members) ([#1008](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1008)) ([038a1ca](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/038a1cab1bcfbdccc4b05eb499667db27cf728d7))
* tenant.membership.v1 1.2, getTenantMembers serves is_synthetic ([#1037](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1037)) ([13429ef](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/13429efdf51123126c21c5fda9b99e6c0cf4a131))
* the E2E CIS admin may send Content studio invitations on synthetic tenants only ([#1036](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1036)) ([b7da3d1](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b7da3d1a16d6459bd3d37afc557d084b0b7415de))
* writing-memory releases with activate-requires-eval and release 0 replay (WM-18, WM-20) ([#1039](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1039)) ([dddb7cf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/dddb7cffe92432613564b198e7d8ae24a067d026))


### Bug Fixes

* admin smoke covers Supply content and Review Queue reason modals; fix full-page shots, sidebar label, /login theme ([#1014](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1014)) ([414e323](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/414e3236c4e92cb975f566e52d61f01b7e7cdf64))
* admin-evidence spec waits for the page heading ([#1022](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1022)) ([df811ef](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/df811ef6ec6df6f4232ec64b5767cd1f010a13a2))
* AI spend test uses days outside every rollup window ([#1023](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1023)) ([f700df4](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f700df4b9b6b91b1cf904f3a9cdb29e8d4f8488c))
* data coverage reads v_tour_place in its own tp_places source (per-tour form) ([#1034](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1034)) ([00c6ced](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/00c6cedbdc0ec9155da99af9908022f40f61b4cf))
* kit DataTable sets aria-sort on sorted headers and gains a compact density ([#1012](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1012)) ([888a32c](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/888a32c23dc6cc048a032d9a01087bd89615747a))
* linear-time sentence end scan in hygiene.sentence_spans ([#1001](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1001)) ([f31ccbb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f31ccbbeea4ad4ecc6dede022784bc8263b5dcf5))
* migration 281 codifies the CIS schema state Dev has but no migration makes ([#1003](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1003)) ([ce833c9](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ce833c952d80af410e5c185907e9f757d1e2f9b5))
* page header actions wrap inside the screen on phones ([#1040](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1040)) ([c53ab11](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c53ab1197df7ccd34b86f2dedeceb48de819aaac))
* retired gates labelled in Content Quality; lock source and feed health in the CRM console ([#1026](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1026)) ([900f246](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/900f2463b42edcf63072408f9e2f5499cfa80756))
* structured output on legacy LLM keys and the supply writer route ([#1016](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1016)) ([55e2402](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/55e240267b053834290d6c208361a20134f07edd))
* typed 409 atom_replaced when a T9 write or angle regenerate hits a replaced atom ([#1013](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1013)) ([3d03b42](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/3d03b425a8b6df5e87c7064d7bc68918abf79fb9))
* write gitleaks sample Google API keys without a literal key ([#1010](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/1010)) ([520199c](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/520199cd2c07c5598d0959f11df0edaf84635420))

## [0.10.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.9.0...v0.10.0) (2026-10-25)


### Features

* writing memory P1 (rule mining, backfill, R0 replay, L0 lexicon fast path) ([#998](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/998)) ([4ce6a3a](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/4ce6a3aa3e807dbac14c1e1f61b6052402b0d2ef))

## [0.9.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.8.0...v0.9.0) (2026-10-25)


### Features

* AA-authored content over catalog.http.v1 and the tenant.membership.v1 feed for the partner CRM ([#987](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/987)) ([ba9e786](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/ba9e78673931f862878d96ca557d0fbcacd1d7c3))
* admin editor page for AA-authored content (CRM prep packs, templates, campaign blocks) ([#993](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/993)) ([6453b74](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6453b7465b508b3b203399f4a17d86a7237e6cc4))
* build Redis connections from env with TLS and AUTH token support ([#983](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/983)) ([c15b592](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c15b592a4e3bab95118ddd26eda4d74364e15823))
* CIS consumer of the Booking supply feed, LLM draft job, photo purge and supply content review API ([#991](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/991)) ([8107bdb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8107bdb07a29f9e3e8053cfd34b5e804935b938e))
* CIS migration 275, AA-authored content tables (items, versions, published view) ([#980](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/980)) ([d1e0eb4](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d1e0eb43bfe6e030a7501c2802c16b4a13cf0161))
* CIS migration 277, shared.writing_signal store for writing-memory signals ([#988](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/988)) ([d798f44](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d798f4475beccf2bdae0a165aaf30aa6ae2f0548))
* CIS migration 278, supply content tables and the catalog.v_supply_content read-back view ([#989](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/989)) ([a996cbd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a996cbdb7995e599564e3d31070c0188626a38f5))
* CIS migration 279, shared.countries (ISO 3166-1), country_iso2 and catalog.v_country ([#995](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/995)) ([32b9ed0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/32b9ed065aff146a2fbe4d8679c5c5296b07e141))
* CIS migration 280, shared.writing_rule store for learned writing rules ([#996](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/996)) ([1021790](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/1021790f722a040cddf83f5da86af1e8cb3827ee))
* portal /terms AI acceptable-use summary (draft v0.2) ([#981](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/981)) ([32ca42d](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/32ca42d79b522653fec22ccc7779581f68a89083))
* quality registry, error taxonomy, single thresholds file and lint (Q-1) ([#982](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/982)) ([a8ec0fe](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a8ec0fe4c51ab7d3ca0bbc4d92dbe5949fe6be4a))
* seed script and starter content for AA-authored CRM content (10 prep packs, templates, campaign blocks) ([#994](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/994)) ([8952afa](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/8952afa336f6b5f925023a42bdd8980bcecdc820))


### Bug Fixes

* retry a failed a3 atomize read and let the job retry failed days ([#985](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/985)) ([58c3ca0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/58c3ca05156e416b13ee03f8b5a09439602756c7))

## [0.8.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.7.0...v0.8.0) (2026-10-24)


### Features

* A1 brand-fit rubric v2 (voice within the source) + re-measure and shadow harnesses ([#969](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/969)) ([bd75b14](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/bd75b14e364efd5745640472fece0572ae370ec9))
* CIS admin S-01 console for the partner CRM agencies (crm.ops.v1 L1) ([#978](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/978)) ([053488f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/053488f4a9031a21afac131f4b147856ae71f076))
* OpenAPI export and breaking-change check ([#974](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/974)) ([e7b3813](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e7b38130cf4d1438ced6f83a3179fa4c0ff6aaa5))
* Platform Apps and the cost page show the partner CRM (AppInfo crm, jobs and LLM spend from crm.ops.v1) ([#975](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/975)) ([50fd625](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/50fd625800d63a5574398dbb62bea9212a8f12ab))
* portal sidebar links to the Partner CRM ([#972](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/972)) ([d070acf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/d070acfecc29696fb201b94275032b6ca3a1c0dd))
* serve catalog.http.v1 tours and destinations to the partner CRM over M2M ([#973](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/973)) ([07f625e](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/07f625eb7eacc675ad69de557028654a3103cbfc))
* TripPlanner cache purge job on every tour lifecycle event ([#971](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/971)) ([853a6ac](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/853a6ac020e994ecf1e3cb46d3fdec7e0defcbfd))


### Bug Fixes

* drop Sentry frame locals and redact tokens and e-mails in events ([#979](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/979)) ([aeca5cd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/aeca5cd8ffc934efa4b961f707c266ec6fa00566))
* stop flag_fix dropping itinerary days on writer-v2 drafts ([#976](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/976)) ([969d221](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/969d221b6c6ea8d66f47e6544538991df1484139))

## [0.7.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.6.0...v0.7.0) (2026-10-23)


### Features

* AA-645 step 0 — publish dates, promote rollback, atomize cleanup, writer v2 flag, wave report ([#965](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/965)) ([89b31fb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/89b31fbbb0e4b37ba750a701b5d793b6117755e8))
* admin page for data erasure (DRY RUN, approved apply, history) ([#960](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/960)) ([4d432be](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/4d432be59d990e7295677be42306f98eaaa242e0))
* CIS 272 tour lifecycle record, Master status reason and v_tour_liveness ([#962](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/962)) ([841e310](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/841e3109615dfc5bb32615e6096d7bffa4812bbe))
* CIS 274 tenant reads its tour notifications, catalog tombstone and lifecycle outbox views ([#966](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/966)) ([5950ee8](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5950ee819e1f2ed68b2f3b93b78a6cbd06ea7fbd))
* migration 270 revokes TripPlanner's remaining direct reads of CIS ([#952](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/952)) ([6a13dd8](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/6a13dd853341a53688387be6b91ad4a78f9afc01))
* migration 273 — published_tours.updated_at; v_tour.updated_at follows the Master row ([#963](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/963)) ([38f13ea](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/38f13ea8dfa4ab6c6abe064f4b6a151efbe17bb5))
* one served-tour rule for atoms and publish gates (live source required) ([#959](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/959)) ([563a6da](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/563a6da5b3b8a341db59b936a6c0f6c5d75613f9))
* one tour lifecycle service for every writer of a tour's served state ([#964](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/964)) ([89bbbd9](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/89bbbd961c5382657f26570a02b9cfd2602690f2))
* tenants see and are stopped on tours AA no longer serves; trip page 410 / 301 ([#967](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/967)) ([5543d1f](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5543d1fc83705bec0499fba5dc9b36a6e0c8220b))


### Bug Fixes

* AA-982 check reads the stored place_paa count and the job chain ([#949](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/949)) ([cb8d357](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/cb8d357b8f546a32291f8fb35e5eb6f6aef09f68))
* blog F9 heading claims are repairable; over-long blogs are trimmed into the band ([#951](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/951)) ([7f0b2fd](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/7f0b2fd845807031d1cd2511e3bc686f15d36ad1))
* erasure fingerprint binds the subject (tenant id or salted traveller hash) ([#961](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/961)) ([a54cdda](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/a54cddaf58113740d16d29bd62aeb7eb58254ca8))
* F17 ended-role check binds the claim to the entity whose role ended ([#956](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/956)) ([40bada6](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/40bada6acf73e7ea49e81acbdebac61cb47c6f85))
* F17 ended-role check matches role identity, not place identity ([#955](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/955)) ([3473038](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/347303879f8b7026feb53f94f8ba6906c367f2e2))
* question headings repair as headings; trim keeps cited paragraphs inside the band ([#953](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/953)) ([1a30cc0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/1a30cc0c12dae8092dcba4ee18c1ef9e8a1b178d))

## [0.6.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.5.0...v0.6.0) (2026-10-23)


### Features

* data_erasure job (DRY RUN by default) for a traveller and an offboarded tenant ([#946](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/946)) ([b4ef027](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b4ef0278d4ba8250f1a1760688a30cd60e2a5fcc))
* migration 269 lets crm_owner register crm- keys ([#944](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/944)) ([46665f0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/46665f0fb0e6327f30d30d7a5fb44ca1d35e197b))
* S1 fact table and offline code checks (S1-first step 1) ([#945](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/945)) ([5fa9f02](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5fa9f0276b476ba6b614b6bdb9b2271681de13bf))
* S1 writer v2, A/B arms and the 3-model screen harness (S1-first step 2) ([#948](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/948)) ([4c9ccbf](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/4c9ccbf4aa34cdb4b7906e2fe5e894819441a2cf))
* T9 blog chain drops place PAA already rejected by A3 Jev ([#941](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/941)) ([2aff012](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2aff0122fbe4d9466c0211c26c4cf5130d6ea950))


### Bug Fixes

* AA-983 backfill keeps other-version pointers and prints an extractable JSON block ([#947](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/947)) ([64e2f28](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/64e2f287bd0db2a0dc8e17d7f252ae8f8393671b))
* muted text contrast in admin and portal, review filter row at 400 px ([#939](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/939)) ([2863c52](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2863c52948531ab36b4f894a8aa15c88eacdfbc7))
* revalidation keeps published_tours.quality_score_id on the new score row ([#943](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/943)) ([dde86c9](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/dde86c91e41e83a39352bc71f81a8629595b45f0))
* verify-admin and verify-tenant answer 401/403 instead of 500 on bad input ([#942](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/942)) ([9ef03eb](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/9ef03eb9336bffec9585672856d5a4c5dc00716a))

## [0.5.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.4.0...v0.5.0) (2026-10-22)


### Features

* AA-859 D5b detail reads and wave 3 (brand identity, publish) on the tenant pool, migration 262 ([#927](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/927)) ([b57ec67](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b57ec67aa22f1976a4d9d04cfb5c4fb896832595))
* AA-859 wave 4 — tenant planning / slate / angle-gate / marketplace / reviews on the tenant pool, migrations 265 + 266, route hub retired ([#935](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/935)) ([5d4497e](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/5d4497ebbf5a53d79333797e92644c46bbb246d2))
* main-pool acquire-wait signal and default acquire timeout ([#938](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/938)) ([c0b6c37](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/c0b6c374bc27f1c33f07b88b76cb806e8df7edcb))
* migration 254 revokes the TripPlanner reads of raw_tours and tour_atoms ([#933](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/933)) ([eea0c08](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/eea0c087bc1955534e69b6e498e6be48b06a54b2))
* migration 268 restores the published_tours.quality_score_id foreign key ([#937](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/937)) ([b2ef525](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b2ef525953418f5bf8c2a65c637d192d9510da9f))
* optional LLM filter for T9 blog FAQ questions behind a flag ([#931](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/931)) ([b4388df](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/b4388df015c789413847bbc5966ef949dbcefa90))
* T9 FAQ answer-fit check (drops FAQ pairs that do not answer their question) ([#934](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/934)) ([e042a98](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/e042a98c843dc5d903a9bf3262f0c280e8c80cc9))


### Bug Fixes

* portal empty states with a next step, portal sessions not metered, readable muted2 ([#936](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/936)) ([51415e2](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/51415e21c46a80c70cfa1330b9ef3e8efd14d04a))
* trip page answers 404 for a tour id that is not a UUID ([#929](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/929)) ([2a2c021](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2a2c021480ac178c0652f378bf6051d30a577316))

## [0.4.0](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/compare/v0.3.0...v0.4.0) (2026-10-22)


### Features

* release-please and the release version on /health ([#928](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/928)) ([f7c4e87](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/f7c4e877e0f70e187ff9fd11a72f0b483c0b363c))


### Bug Fixes

* portal page audit — tenant-only copy, load states, contrast, full paging ([#925](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/925)) ([78114a5](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/78114a5159b910da1f1c1aa1654ccb2ef4d7b75f))
* the first logging configuration of a process wins (worker said cis-api) ([#926](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/issues/926)) ([2285259](https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/2285259b014889a7dbebe580b4618433998b2a73))
