# Changelog

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
