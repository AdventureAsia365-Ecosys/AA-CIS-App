# Changelog

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
