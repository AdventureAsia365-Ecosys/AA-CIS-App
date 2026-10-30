# Changelog

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
