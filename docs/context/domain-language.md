# Content Pipeline Domain

> Detail page of [CONTEXT.md](../../CONTEXT.md): the glossary of master, platform-derived and tenant content, model operations and Jev decisions. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.


The Master Content → Tenant Content pipeline: how one tour becomes the platform's shared master
content (Admin, A-series), and how a tenant turns a picked tour into channel-specific published
content (Tenant, T-series). Built AA-539 (06/09/2026), after AA-527 shipped a page that mixed up
this exact boundary from having no single written glossary. Other subsystems (Excel ingestion,
LLM/model routing, marketplace/billing, legacy N0-N8) are out of scope for this file — split into
their own `CONTEXT.md` under a `CONTEXT-MAP.md` if/when they need one.

## ✅ Segment/Score/Route/Hub are now platform-wide (AA-545, shipped 06/09/2026)

**Update (AA-551, 07/09/2026): this used to be an open tech-debt warning here — it is DONE, not
pending.** AA-542 (same day, commit `43c18c0`) flagged Segment/Score/Route/Hub's then-per-tenant
schema as tech debt requiring a redesign; AA-545 (commit `15743a9`, merged later THE SAME DAY)
shipped that redesign — migration `146_segment_score_route_hub_platform_wide.sql` drops
`tenant_id` from `acp_contract.atom_segment`/`atom_ranking`/`route`/`hub` outright. A reader
landing on the older wording (still present verbatim in this file's git history, and in the
Ownership table row text further down) would wrongly re-block new work behind a freeze that no
longer applies — confirmed directly from `api/routers/admin_dashboard.py`'s own already-updated
SQL/docstrings (no `tenant_id` anywhere in any of the 4 queries) while building AA-551's
platform-wide `/admin/atom-curation` page against this exact data.

The layering principle that motivated the fix, decided by Nghiệp: **any step that does NOT read a
tenant's own brand voice or a tenant-specific DFS/keyword signal should NOT be per-tenant.**
Segment/Score/Route/Hub read neither — they operate purely on Atom (platform-wide, A3) and Search
Demand (platform-wide cache) inputs. They are now computed once for the whole Master Content pool,
still scoped by `tour_id` (a Route is inherently a day-span within ONE tour's itinerary — dropping
`tenant_id` did not make these tour-agnostic, just tenant-agnostic). The Ownership table further
down was corrected to match on 25/09/2026.

**Still true and unchanged by AA-545**: Slate (`acp_shared.subject`) is a genuine, deliberate
per-tenant exception — a Subject is one specific tenant's proposal, never shared.

## Language

### Master content (Admin side)

**Atom**:
One concrete, verbatim-derived place-and-activity pair from a tour's itinerary text (`place` +
`action`) — never a summary, paraphrase, or invented detail. Generated once, platform-wide, at
A3 (see Pipeline Stages) — not per tenant.
_Avoid_: fact, moment, detail.

**Facts Entry (platform scope)**:
One hand-written, sourced claim not present in any tour's itinerary text (price, season, visa
requirement, typical transfer time) that AA-admin writes ONCE for every tenant to cite from.
Distinct from Facts Entry (tenant scope) below — same table (`acp_shared.facts`), `scope` column
tells them apart, never merged.
_Avoid_: Fact (bare), reference data.

**FactSheet (AA-1047, root ADR 0020)**:
The facts one write rests on, built by code (`services/quality/factsheet.py`): typed, numbered lines, each with
its provenance (`source_raw`, `human_verified`, `tenant_asserted`, `llm_derived`, `external`) and, for a tour,
a pointer into the supplier source; plus what the builder left out and why. Two builders: the **tour builder**
(S1 / T2: the raw source tour, rendered as the S1 writer v2 FACT SHEET) and the **route builder** (T9: seed +
Facts Entries + brand / audience / trip / destination lines, rendered as the gates' SOURCE). `render()` is
byte-equal to what each workflow read before W1. Stored once per distinct input in `acp_shared.fact_sheet`
(CIS 386, `services/quality/factsheet_store.py`, best-effort) and named by `generated_content.fs_id`,
`tenant_tour_versions.fs_id` and `content_piece.writer_input.fs_id`. The brief is never a FactSheet line.
_Avoid_: fact pack, source bundle, grounding text (the old names of its parts).

**Lineage (AA-1052, root ADR 0020 layer 3)**:
The versions one writer output was made with, from stored fields only (`services/quality/lineage.py`): `policy_sha`,
`fs_id` (the FactSheet), `prompt_version` (the writer's own system-prompt sha, plus the registered version of every
stage in the object's ledger rows) and `route_config_version` (stamped at the exit, plus the versions the ledger
trigger stamped on its calls). The writer's store step writes `lineage: {v, policy_sha, route_config_version}` ONCE,
next to the contract record (`generated_content.metadata.lineage`, `tenant_tour_versions.judge_breakdown.lineage`,
`content_piece.writer_input.lineage`; no I/O: the route version is the one the process's gateway last read, just
refreshed by the writer's own calls); a later exit (approve, publish, human edit, re-validation, admin sentence apply)
replaces only `final_contract`, so the object's lineage stays the writer's. Each exit record also carries its own
`final_contract.lineage` (kept in the S1 `final_contract_log` entries): the policy in force at that exit, with a route
version only at a writer exit. An eval result reads its run's fingerprint (`config_fp.parts.policy`,
`route_config_version`) and its `judge_version`; it has no FactSheet. `GET /admin/quality/lineage/coverage?days=`
(the share of recent objects carrying each field) and `GET /admin/quality/lineage/{s1|t2|t9|eval}/{id}` (one object,
with `missing` / `complete`); ops.read or lab.read; ids and shas, never text. Writing Lab › Eval runs
(`/admin/lab/eval-runs`, AA-1169) shows them, and reads an evaluation run by hand through
`GET /admin/writing-memory/eval-runs[/{run_id}[/items/{item_id}]]` (`services/writing_memory/eval_review.py`): the
lists carry ids, codes and counts (Lab R); one item returns the stored writer input and output (a logged Lab text read).
_Avoid_: provenance (that is a FactSheet line's source class), audit trail.

**Source-change fan-out (AA-1052 W6, CIS 392)**:
When a source a writer read changes — an admin raw-tour edit (`raw_tour`), a Master publish (`master`), an admin atom
edit or delete (`atom`), an admin place card facts / status edit or merge (`place_card`) — the write path enqueues the
`$0` job `source_fanout` (`services/quality/source_fanout.py`; one job per source and minute). It finds the writer
objects written from that source BEFORE the change (S1: the live and the newest Master version; T2: each tenant's
newest version of the tour; T9: approved / held pieces citing an atom of the tour, or the atom, or an atom linked to
the card, by the angle request or a route moment), re-checks them (an S1 / T2 object of a raw-tour change whose stored
FactSheet text equals the sheet rebuilt now is left alone) and marks the rest in `shared.source_change_mark` with a
typed reason (`raw_tour_edited`, `master_published`, `atom_edited`, `atom_deleted`, `place_card_facts_edited`,
`place_card_status_changed`, `place_cards_merged`, `manual_recheck`); one open mark per object and source. It never
regenerates, never calls a model and never changes an object's status. `GET /admin/quality/lineage/fanout/{tour_id}`
shows a tour's marks (ids and reasons); `POST .../fanout/{tour_id}/recheck` queues a manual re-check (dry run by
default; quality.write).
_Avoid_: stale (a mark says the source moved, not that the text is wrong), invalidation (nothing is deleted).

**Claim shadow (AA-1050, root ADR 0020 W4, D6 / D7)**:
The pinned claim instrument calibrated in W2 (`services/ground_truth/r1.py`, extraction mode, R1 prompts byte for byte,
3 votes, 2 agreeing to decide) run beside a live decision without acting on it (`services/quality/claim_shadow.py`):
after a T9 piece is finalized (baseline: its final `F9_cta_fact` entry) and after a T2 version is saved (baseline: the
brand-claim check in `judge_breakdown.brand_claims`), and on the replay corpus (`python -m
services.quality.claim_shadow_replay`, DRY RUN default). One row per object in `shared.claim_shadow_verdict` (CIS 391,
no text: spans, claim hashes, classes, votes) with a typed reason, the agreement with the baseline and what the
per-(writer, class) enforce map would do (`shadow_action`; every class warns until a class is in both
`ENFORCE_OVERRIDES` and the instrument's passing record `CALIBRATED`, both empty). A miss, refusal, unusable reply,
split vote, answer from another model / connection, caller error or the spend guard is `unverified`, never a pass;
the tenant's CTA / brand name spans never count. Cache first (key = instrument + votes + prompt sha + each field's
prompt), then a hard spend guard over the `claim_shadow` stage's ledger window (`CLAIM_SHADOW_CAP_USD`, default 0.30,
max 0.50; `CLAIM_SHADOW_WINDOW_DAYS`, default 30; an unreadable ledger closes it). Switch `CLAIM_SHADOW=on` (default
off: no read, no call). Calls go through the gateway on stage `claim_shadow` (platform stage, role judge,
`{"shadow": true}`), never change a status and never raise into the job. Read side: `GET
/admin/writing-memory/claim-shadow/summary` (Writing Lab read: counts, cells, reasons, spend vs cap, a seeded sample of
disagreement ids). The enforce flip (holds for measured classes, truth-cut clause first) is a later, measured change.
_Avoid_: shadow judge (the gateway's model shadow, `llm_shadow_log`, is a different thing).

**Pooled claim class (AA-1048, tech lead S278, root ADR 0020 amendment)**:
A declared union of closely related claim classes that the R1 record measures as one class, because no single class
has enough held-out violations yet ("measure the pool first, split later"). Declared only in code
(`services/ground_truth/pools.py` `POOLS`, a reviewed PR; today `pool.fact_unsupported` = fact.embellish +
fact.inclusion + fact.misbinding + fact.number on t2, t9_blog, t9_social). `r1.measure` applies the unchanged bar to
the pool per writer (writer precision over every fire, pool n_pos >= 20, >= 1 caught, held-out, instrument
conditions) and lists `passing_pools`; a passing pool calibrates only its members on that writer (`calibrated` =
`pools.calibrated_keys`, what `claim_shadow.CALIBRATED` may hold). Each member keeps its own counts; `split_ready`
names a member that reaches the bar alone, which is then measured as its own class.
_Avoid_: class group, family (a set family is a label set id family, `claims.SET_FAMILIES`).

**Instrument prompt version (AA-1048, S279)**:
The texts an R1 claim instrument sends (`services/ground_truth/prompts.py`): `v1` = the S272-S278 prompt (the default;
`claim_shadow` and every R1 record use it), `v2` = the S279 panel's changes B1-B5 (whole sentences, paraphrase and
one-step inference allowed, place background allowed until it becomes a trip promise, the seed's "Place background"
lines as support, headings / FAQ questions judged with their answer, the confirmed promise patterns kept), `v3` = the
S280 restatement test for paraphrase (set aside wording that adds no checkable fact and fire only on a fact the
instrument can name; trip-wide facts support one day; a label made of source words and an inferred day start are
paraphrase; an unstated order and a number bound to the wrong thing stay unsupported). A version
other than v1 is part of the instrument id (`...:entailment#v2`), and every call records `aa1048-r1-<id>@<sha>` in
`shared.llm_call_log.prompt_version`. A new version is measured first by the gated evaluation
(`python -m services.ground_truth.instrument_eval`, DRY RUN default; a stratified subset of the claims R1 fired on;
cap, claim limits and go / no-go bars in the settings registry, `cis.spend.instrument_eval_*`,
`cis.quality.instrument_eval_*`, CIS 417); it never writes a label. Claims a version's examples were drawn from (the
five S279 relabels for v2) are in-sample: reported apart, never in the TP-kept bar. Claims of an earlier evaluation
whose misses shaped the next version are in-sample for it too: `--exclude <report>` keeps them out of its subset.
`score` replays a finished run from its stored votes at $0; `--measured <report>` prices the plan from a measured run
(the live gate still charges every call's worst case).
**Two-step instrument (AA-1048, S281)**: after v2-v4 were no_go, `services/ground_truth/two_step.py` splits the question
in two registered step versions: `v5c` (step `classify`: restatement of the source or new claim, listing the new
specifics) and `v5j` (step `judge`: is each listed specific supported). A claim is cleared when the step-1 majority is
a restatement; otherwise the step-2 majority decides (an unverified step 1 never clears). A step version is
entailment-only and refused by `r1.instruments`. The evaluation is pre-registered
(`services/ground_truth/preregistration/aa1048_two_step_v5.json`, `docs/calibration/aa1048-two-step-preregistration.md`):
a live run refuses unless the settings bars (CIS 417 overall + CIS 423 step bars), prompt tags, candidate and votes
equal the file, and unless the pool is out of sample for both steps: a fresh pool (`services/ground_truth/
fresh_pool.py`: R1-fired claims on pieces no earlier round touched, the agent panel's sheet, a manifest with the pool
digest and the excluded runs and pieces) or `--exclude` of the shaping runs. The R1 firing run that feeds the fresh
pool (`services/ground_truth/r1_fire.py`) fires the R1 extraction instrument on a salted, writer-proportional sample of
the eligible claims whatever their label (size: CIS 426 `cis.quality.claim_fire_sample_max`; cap: CIS 417), refuses an
eligible file that is in-sample for the steps, and writes a text-free fire report that `fresh_pool sample` reads.
A live fire appends a text-free checkpoint line per billed call; `--resume` continues a cut run (same identity, its
ledger rows reconciled by call id, the spend carried over) into one run's report, and `fresh_pool sample` refuses a
stopped one.
Step sets (S282, `services/ground_truth/two_step_sets.py`): each registered pair of step versions pins its
pre-registration file and that file's sha256; a set closes in code after its one gated run (v5 and v6 are closed, v7 is
the default: `v7c` writes the closest source wording for each trip specific before it sorts, `v7j` writes a finding per
listed specific and clears a sentence the source supports). A shaping pool that is itself a train-split pool
(`prompts.TRAIN_SPLIT_POOLS`) must be named in a fresh manifest's `excluded_pools`; a train-split manifest does not keep
it out.
_Avoid_: prompt registry version (`shared.prompt_version`, AA-900, is the writers' registry).

**Served state (tour liveness, AA-986, root ADR 0015)**:
Whether AA serves a tour, per catalog key (`raw_tours.source_key` = `COALESCE(source_group_id, tour_id)`):
`served` (live source version + live Master), `paused` (Master inactive, or a supersede waiting for the new
version's Master), `retired` (source deleted at S0/A0, Master trashed, or every version superseded). Derived, never
stored: read it from `gold_aa_internal.v_tour_liveness` (CIS 272). Every change goes through ONE write service,
`services/tour_lifecycle` (retire / restore source, supersede, set-active, Master pause / activate / trash /
restore, the publish insert, the elephant-riding `policy_hold` of AA-1049): in one transaction it changes the status
columns with the reason (`published_tours.master_status_reason`: staff_deactivated / staff_trashed / source_deleted /
superseded, `policy_hold` since CIS 398), switches off any Master over a dead source (never two active Masters per
key), dismisses pending reviews, cancels queued T2 / T8 / T9 jobs, writes `gold_aa_internal.tour_lifecycle_event`
(append-only) and notifies every tenant with a derivative (`tour.discontinued` / `tour.reinstated`). A unit guard
(`tests/unit/test_aa986_l2_lifecycle_guard.py`) fails any other code that writes `source_status` / `master_status` /
`deleted_at` or inserts into `raw_tours` / `published_tours`. Retiring a published tour needs `master.admin` (Q8).
Tenants (L3): every tenant `/v1` row about a tour carries `served_status` / `served_reason` / `discontinued_at` /
`served_notice` (`shared/tour_liveness.py` `ServedState`; the portal badge reads `served_notice`: discontinued,
paused, or nothing for a supersede, Q2); new work on a non-served tour (rewrite, retry, T8 goal, T9 write, publish)
answers 409 `tour_discontinued` / `tour_paused`; the tenant bell reads its own rows through `GET /v1/notifications`.
Other apps (L4): `catalog.v_tour_retired` (tombstone) and `catalog.v_tour_lifecycle_outbox` (CIS 274, contract
`catalog.tour.v1.3`); `/v1/trip/{id}` answers 410 for a retired tour and 301 for an older version of a served one.
TripPlanner purge (AA-987): every lifecycle event gets one `tripplanner_purge` job (key
`tripplanner_purge:<event idempotency_key>`, enqueued after commit by `run_after_commit` and by the publish of
`process_export`, plus a sweep in the api ops sampler for any event without a job, last 2 h). The job re-reads the
event from `gold_aa_internal.tour_lifecycle_event` and POSTs it, HMAC-signed (`X-AA-Signature: t=..,v1=..`, secret
`aa-cis/<env>/tripplanner-revalidate-hmac`, same value as TripPlanner's env `CATALOG_REVALIDATE_SECRET`), to
`TRIPPLANNER_REVALIDATE_URL` (default `https://tripplanner.lumiguides.it.com/api/revalidate/catalog`, the public host
served by the TripPlanner web ECS service behind CloudFront since AA-1038 retired its Vercel project; Infra sets the
same value), which purges the TripPlanner ISR / CDN caches. 4 attempts with backoff; not configured = succeeded `skipped: not_configured`.
Tenant fan-out (AA-1039 R6, root ADR 0019 d20; CIS 360): every `retired` / `restored` event also gets one
`tour_retirement_fanout` job (`services/tour_lifecycle/fanout.py`, `services/jobs/tour_retirement_jobs.py`; same
after-commit enqueue + 2 h sweep). `retired`: each live post of the key's tours gets a `tour_retired` row in
`acp_shared.content_compliance_flag`, `notice_only` by default (tech lead decision S276, ADR 0019 d20 amended:
nothing changes on the agency's site unless it asked; WordPress and Facebook alike). Only an agency that turned on
**Automatically move posts about a discontinued tour to draft** (`acp_shared.tour_retirement_policy.auto_draft`, CIS
395, agency row only, off by default; `GET` / `PATCH /v1/account/tour-retirement`, owner only, 1-30 days, default 7,
audited `tenant.tour_retirement_settings_changed`) gets WordPress `draft_after_notice`: the page goes to draft at
`draft_due_at` = event + its days through a `retired_page_draft` job the sampler enqueues due-now; the job re-reads
the opt-in and turns the flag `notice_only` when the agency switched it off after the flag was written. The tenant's
bell gets one `tour.pages_to_review` row listing its live WordPress pages, and the Content studio lists every flagged
live post (`GET /v1/publish-log/retired-pages`) with a one-click **Move to draft** for a WordPress page (`POST
/v1/publish-log/{publish_id}/move-to-draft`: own page with an open `tour_retired` flag only, 404 otherwise; the same
`move_to_draft` path and re-checks as the job, the tenant as `resolved_by`, audited `content_piece.moved_to_draft`;
a repeat click replays "already a draft"). Either way the `publish_log` row is unpublished by
`system:tour_retirement`, so the restore notice and Put back online treat both the same. Open `draft_after_notice`
flags written before S276 are left as they are. Stale `requested` publish claims are closed; every derivative
tenant gets an `acp_shared.tenant_tour_notice` row, mailed at once (`tour_notice_email`, SES) when it still has live
posts, else in the daily digest (08:00 UTC). A fan-out replay re-enqueues every unmailed `immediate` notice of the
event (mail job key `tour_notice_email:<notice_id>`), and the digest also takes an `immediate` notice left unmailed
for an hour (a dead mail job); a mail job claims its notices before sending, so the two never both send one. A second
`retired` event of a retired key (a reason change) adds no flag, notice or mail. `restored` resolves the open flags
and writes the "back on sale" notice only for tenants whose latest notice is `retired` (never after a pause). A page
the retirement already moved to draft is never republished on its own (AA-1039 option A, S275): the notice lists it
(`summary.pages_drafted`, mailed at once) and the tenant's bell gets one `tour.pages_to_put_back` row; the tenant
clicks **Put back online** in the Content studio (`GET /v1/publish-log/put-back`, `POST
/v1/publish-log/{publish_id}/put-back`, flag `aa1039:put_back_pool`): the publish route's checks (served, latest
piece, no other live post of the request, the AA-subject gate, connection, site), then the page's `publish_log` row
is claimed and WordPress read: a draft is published (one call), a live page recorded, anything else left as is
(`detail.put_back` on the flag; a repeat click replays). A page the click would refuse (its piece superseded, or
another post of the request live) is never offered in the notice or the list. While a page is still to put back, its
piece is left out of Ready to publish (tour served) and a new publish of it is refused (409), so no second copy goes
online. Before
CIS 360 is applied the fan-out fails retryable and the sampler re-queues those jobs once the tables exist.
Recipients: active `owner` / `editor` members, address read at send time from the partner pool (`ListUsers` by sub,
verified e-mail only); CIS stores no address. A `paused` tour gets no mail.
**Subject rule for Segment Subjects (AA-1057 GAP 1)**: a request on a Segment Subject is planned on one
representative member (its seed atom and that atom's tour as `trip_id`), but the Subject lives while its Segment has
any served member (the Slate's `segment_dead` rule). Every liveness gate on a request reads that rule through
`shared/tour_liveness.py` (`segment_served_member_sql`, `requests_about_tours_sql`; a merged Segment id is read on its
live id): the write / goal / publish 409s and served badges (`REQUEST_TOURS_SQL`: a retired representative is
replaced by a served member's tour; a paused one is not, since the retarget stage never re-seeds it, so the request
answers 409 `tour_paused` and its pause still cancels its queued T8 / T9 jobs), A4's live-post state, the derivatives
(bell, job cancels, notices), the
fan-out's flags / claims / mails (a request is about an event when the event takes the Segment's last served member,
whichever member it was planned on) and the move to draft (resolved `restored` with `subject_alive` when a member is
served again). A request with no Segment Subject follows its tour as before.
**AA-subject gate** (AA-1039 R6, d21; `services/acp_shared/subject_gate.py`, no LLM): when T9 finishes a piece
(`_finalize_piece`) and again right before `POST /v1/publish-log/{id}/publish`, a piece must be about a served AA
tour, a live AA atom or a Slate Segment with a live member (brand content with no sales link and a non-sales goal is
exempt), and every sales link (the CTA, a link whose text / path has a booking word, or a product word with a
commercial tour host or a `/tours/<page>` path) must go to an AA host, a trip
page of a served tour, the tenant's own page of a served AA tour (`acp_deliver.tenant_tour_pages`) or the TripPlanner
site. A failing piece has one open `subject_gate` flag; the publish answers 409 `content_needs_change` with the flag's
content-style `tenant_message`, and `/pending` items carry it as `needs_change`; when the gate cannot answer the
publish fails closed (503 `content_check_unavailable`, "try again"); a repeat publish of a revision already live or
in flight is not checked again. Links are read from the HTML the publisher sends (the same markdown render, every
`<a href>` through an HTML parser, entities decoded, `//host` and scheme-less hrefs resolved) plus bare and
scheme-less URLs of the text and the raw piece; a trip page link counts only with a real uuid; the TripPlanner hosts
include the Dev domain `tripplanner.lumiguides.it.com` and (AA-1114) the tenant's OWN verified custom hosts (the
`active` rows of `shared.tenant_tripplanner_domain` of that tenant, exact host: never a sub-domain of it or another
tenant's host). Admin Tenant content QA › Compliance
(`/admin/tenant-qa/compliance`, API `GET /admin/tenant-qa/compliance`, `POST …/{flag_id}/release`) lists both kinds;
a release sends the `content_sha256` the gate read and is refused (409) when the text changed since; it lets exactly
that text through. Flags are admin data; tenants never see them.
_Avoid_: "delete" for a published tour (it is retired and can be restored); `raw_tours.lifecycle_stage` as a
deletion signal (it is the sales phase).

### Platform-derived content (computed once from the Master Content pool, shared by all tenants)

**Segment**:
A group of Atoms (usually from different tours) that describe the same real-world moment,
matched deterministically on place-token-similarity + verb-match on action — never an LLM/
embedding call, because `segment_id` must stay stable across re-runs; pure CPU, no external API
cost either way. **Platform-wide since AA-545 (migration 146)**: `acp_contract.atom_segment` has
no `tenant_id`; new `segment_id`s are `sha256(place|verb)` (rows minted before 146 keep their old
opaque id). A Segment can span many tours and every tenant reads the same set. Carries two
cached columns for Score: `questions_count` (AA-610, migration 163) and `contested`
(AA-631, migration 165) — `NULL` means "recompute next time". History of the per-tenant period:
`docs/adr/0001-*.md`, `docs/adr/0003-*.md`. **A Segment never spans two countries** (tour country,
`raw_tours.country`): AA-695 vetoes a cross-country pair, and AA-857 holds the rule for the whole
connected group (an unknown-country atom or pseudo-Segment can no longer bridge two countries).
A `segment_id` is the mint of the moment that FIRST created it, not a promise about its current
label: an A3 run relabels only ids it claimed (its atoms' prior Segments + its pool) or ids with no
member left, re-mints a fresh id that collides with another country's Segment or an alias old id
(`sha256(id|country|aa857|n)`), and rolls back if a written id would gain a second country among
its live atoms (AA-857). Subjects stored on an id that later merged resolve through
`atom_segment_alias` to the live id (an id that still has members is live, never followed). A Segment with no
reference left (AA-988: no live member, ranking, alias, Subject, brand-fit or route reference) is deleted by the
`segment_gc` recompute stage; it is re-minted with the same id if its place / verb comes back.
_Avoid_: cluster, group, topic.

**Search Demand**:
The cached DataForSEO signal (search volume + People Also Ask) behind a Segment's place, bought
by an LLM research loop. Cached and looked up by `(keyword, market)` — or `(place, market)` for
the loop-skip freshness check — with NO tenant scoping at all. Since AA-631 it also stores the
organic result domains of the same SERP call (`search_demand.serp_domains`, no extra DFS cost).
When PAA or SERP data for a keyword is re-fetched, the `questions_count` / `contested` caches of
every Segment claiming that keyword are reset to `NULL` (AA-630/631) — never recomputed inline.
AA-1055 (CIS 393): segment research is the one buyer. Its scope is the Segments of active platform
Masters (`v_active_tour_atoms`) minus the ranking exclusions; a place's keyword map is proposed once
and kept in `acp_contract.place_keyword` (with Jev's verdict and the prompt version); volume is bought
per market and logged per place in `segment_research_log` tier `volume`; each place gets ONE SERP
(US / English, tier `serp`), whose PAA, related searches and domains sit on the keyword's US row
(`paa_retrieved_on`, `serp_location`). Both tiers are fresh for 365 days (AA-1120: the settings
`cis.seo.research_volume_fresh_days` / `cis.seo.research_serp_fresh_days`). AA-1120: every read and write of the store
goes through ONE research cache (`services/seo_intelligence/research_cache.py`), the Standard queue included (it takes
the cache at construction, deduplicates its asks, answers from the store first, collects free a task paid for and never
collected within `cis.seo.dfs_queue_recover_days` — the post row's meta `posted` lists each task id with its keywords —
and writes every answer back); one SERP per distinct keyword; hits are 0 USD `cache_hit` rows under the endpoint label
the run buys through; a guard test fails on any paid call site not listed in `research_cache.DFS_CALL_SITES`; the
preview shows `estimate.deduped` (`research_estimate.py`) beside the upper bound. A run is bounded by a USD
cap (not a place count, at most 50 USD) that is also its hard DFS spend limit, checked against the
DFS balance; the per-place ReAct loop strategy is retired. A run enqueues one debounced platform
recompute for the places it changed (more than 300 Segments outside the night window wait for the
sweep); the worker's night landing sweep catches Segments whose newest research never landed.
S274 (`keyword_qualify.py`): before Jev, the proposed map is qualified — a namesake place name (a building or
lodging type with only dedication, kind or qualifier words: "<saint> Cathedral", "Hotel <range>", "City Palace"; never
a natural feature or a stock word on a landmark) must carry its locality (place card, else tour country), a plant /
animal / food (head noun of the card's `what_it_is`; a market or food court stays a place) buys nothing, a garbled
phrase is dropped; dropped keywords are stored as `jev_zone 'skipped'`, `jev_mode 'rule:<reason>'`. A fresh place
whose older map the rules would change is re-proposed on the next run; any other older map is reused. Ranking stops
claiming a namesake place's bare-name keyword once a locality-qualified keyword of it is stored.
Stale guard (AA-1055, `research_coverage.py`, $0, read only): per market an active tenant targets (resolved like the
Slate, never recording an unmapped market), the share of the research scope with no research or research older than
the freshness window (the run's own `stale_tiers`; the SERP counts against the US only), and the places atomized since
their last research that are stale ("waiting after an atomize"). The api ops sampler measures it once a day (ops check
`research_stale_share` above 20 % in any market: bell + one e-mail a day, the issue equivalent) and the data coverage
page shows the same numbers (`research.stale_share.<mk>`, `research.waiting_after_reatomize`). A re-atomize queues its
new places as research scope `reatomized` (preview priced, bought only when an admin runs it); there is no queue table.
AA-1122 (CIS 440-442, "always ready"): every Google Ads volume answer (live, Standard queue, keyword ideas, the S1
fetch) also keeps its 12 monthly volumes in `acp_contract.search_volume_monthly` (one row per keyword, market, month,
written once, never overwritten; `search_history.py` reads a place's season: peak month, year-over-year). Keywords of a
place with identical volume in every market are one demand group (Google's close variants): the one SERP goes to the
strongest group, and inside it to a variant whose SERP is already stored, else the most natural phrasing, never by
volume (`research_select.py`). The windows default to 180 days, maker-checker. Selection order (`research_plan.py`):
never researched (most tours first), stale (oldest first), refresh-ahead on a refresh run (its share capped, bought
through narrowed windows), requalify last (capped). The worker's hourly research tick (`research_auto.py`, after the
landing sweep) enqueues at most one `auto_new` / `auto_refresh` run per kind per UTC day inside its settings window,
for the markets of active agencies only (plus the platform default; demo and internal tenants never count; a market
mostly unresearched waits for an admin run). Every research job (`segment_research`, `tour_paa_backfill`) runs under
one hard monthly cap over DataForSEO + LLM + Jev, measured from the ledger by job (`shared/research_budget.py`, fail
closed), and an automatic run under its own run cap across retries. Paying research is one lane across both kinds (the
runner's concurrency 1 is per kind): the run endpoints refuse with 409 while another paying research job is queued or
running, and a paying attempt that starts beside one goes back to the queue before it reads the month (a store-only
PAA backfill is $0 and runs beside a research run); alerts `research_auto_failed`, `_capped`,
`research_new_waiting`, `research_month_spend`. A same-plain-keyword rename or merge in segment matching carries the
place's log and map (`research_carry.py`). Tour PAA backfill (`tour_paa.py`, job `tour_paa_backfill`): store first, then
one capped SERP per tour keyword; provenance in `seo_context.paa_source` / `paa_filled_at`. The backfill owns a row
only while no S1 write came after its fill (`fetched_at <= paa_filled_at`): a later S1 list is never overwritten. Admin: SEO Intelligence ›
Research tab (GET `/admin/segment-research/status`). Every threshold, schedule and cap is a `cis.seo.*` / `cis.spend.*`
setting.
_Avoid_: DFS, keyword research (both used loosely elsewhere for the same underlying calls).

**Score** (Atom Ranking):
The rank-sum of a Segment's **four** signals (AA-610): Demand (search volume), Recurrence (how
many tours contain it), Questions (PAA questions that land on it by embedding match — atom and
question embeddings are fetched in one batched pre-pass, up to 96 texts per Cohere call, and
cached in `atom_embedding` / `question_embedding`, AA-688) and Said
(how much the itinerary already says about it). Deterministic, no LLM. Persisted on
`acp_contract.atom_ranking`, **platform-wide since AA-545**: primary key
`(market, tour_id, segment_id)`, one full pass per buyer market (US/UK/AU/DE/FR/NL). Every
downstream consumer (Route, Slate) reads this value, never recomputes it. A change to one tour
only recomputes the Questions signal for that tour's Segments; the rest is read from the cache
(AA-610 scope fix: cold start ~6 min, warm ~19 s, down from >6 h).
_Avoid_: rank, weight, priority.

**Route**:
A consecutive-day span (2-5 days) of one tour's ranked, non-excluded Segments — a Blog-only
concept: since AA-741 S271 the walk a Blog Segment Subject carries (the origin's `walk`); from AA-511 to S271
Blog was proposed at Route grain, which left each agency one or two Blog Subjects ever. Rebuilt whole on
every re-run (old rows are superseded, never deleted). Platform-wide since AA-545: identity is
`(tour_id, first_day, last_day)`, no `tenant_id`.
A blog request keeps a frozen copy of the Route's Segment ids (`angle_gate_request.route_segment_ids`).
Since AA-914 every reader of that copy (Slate pick, T8 angles, T9 write, admin regenerate) goes through
`services/acp_shared/route_guard.py`: a merged id is followed to its live Segment, and a Segment counts
only when the request's trip has a live member atom in it; every dropped id is logged
(`route_segment_dropped`). A pick or regenerate reads only the current Route version
(`superseded_at IS NULL`); a Subject still on a superseded Route is not pickable. A picked / used one is moved to the
current version by the `retarget` recompute stage (AA-1057).
_Avoid_: itinerary segment, leg, journey (that's Hub).

**Hub**:
The marketer's unit of choice: a persistent, human-named journey ("Nakasendo Way: The Kiso
Valley from Kyoto") that a family of Routes belongs to. Persists across Route rebuilds — reused
by `route_detection.py` when the same tour-id family regroups, never deleted, even when
orphaned (no Route currently maps to it), because a Subject that already snapshotted the name
still needs it to mean something. Platform-wide since AA-545.
_Avoid_: journey, family, group (Route Family, a distinct upstream concept, has no column here).

### Tenant content (per tenant, from the Slate onward)

**Subject**:
One proposable (Segment or Route) × Channel pair (every Channel proposes Segments since AA-741 S271; Route
Subjects are the Blog ones picked before then and stay valid), with a `state` (proposed/picked/used/cut), a
`score` (copied from Atom Ranking or Route.score, never recomputed), and `cleared_bar_reason`
(why it passed/failed that Channel's numeric Bar). One row = one thing a tenant can pick.
_Avoid_: proposal, candidate, slot (Slot is the older, deprecated N7 term — do not reuse).

**Slate**:
The tenant-facing mechanism/screen (portal "Social Content", `GET /v1/slate`) that proposes
Bar-cleared Subjects for a Channel. `services/acp_shared/slate.py::propose_slate()` runs, in order:

1. Fetch candidates (Segments/Routes) for the tenant's markets. A declared market outside the
   6 supported ones is no longer silently replaced by US: it is returned as
   `unmatched_markets`, shown as a banner to the tenant, and recorded in
   `shared.unmapped_market_requests` for admin (AA-629). Since AA-1080 every unsupported declared market
   counts, also when another declared one is supported (one row per country, also written at the Brand
   Identity save), and the T2 keyword market is the agency's FIRST supported declared market
   (`seed_builder.first_supported_market`; before, the lowest MARKET_RANK, so US won for every list holding it).
2. **Debate** (AA-631, `services/acp_shared/debate.py`) — two advisory signals before choosing. Since AA-1079
   (S277) Debate RANKS ONLY by default (setting `cis.quality_modes.t7_debate` = `rank`): no candidate is dropped; a flagged one
   carries `debate_flags` and is ordered after every unflagged candidate of its Channel queue, and the `t7_slate`
   job result records the flags (`debate`, admin only). The setting at `cut` brings back the cut below, for when the
   brand-fit judge is calibrated against the golden-set brand labels (`services/quality/brand_labels.py`,
   `scripts/quality/measure_aa1079_brand_fit.py`). The two signals:
   - _contested_: share of a keyword's SERP taken by "everywhere" domains (wikipedia,
     tripadvisor…). Deterministic and cached; cut threshold 0.5, measured on real SERP data.
   - _brand-fit_: the same brand-fit scorer (judge route) the T2 judge uses
     (`services/content_generation/brand_fit.py`), cached per
     `(tenant, candidate, brand_version)` in `acp_shared.debate_brand_fit_cache`. Cache misses are judged
     `T7_DEBATE_CONCURRENCY` at a time (env, default 8, 1 = one at a time; AA-1081 S278) in worker threads with the
     job's context, inside the gateway's own connection / TPM / tenant-budget limits; the signals keep the input
     order, so the rank equals a one-at-a-time run.
   - Safety rules (cut mode): never cuts more than 50% of a list; an error on a candidate lets it pass; if
     Debate fails entirely the Slate behaves as before.
3. Bar check per Channel, only for the tenant's enabled Channels (`acp_shared.tenant_config.channels`,
   set in admin Tenant 360 > Planning config; no row = every Channel), then the place rule
   (AA-840, replaced AA-632's one-place-per-Slate set): Channels take turns, strongest candidate
   first; a place appears at most once per Channel and on at most `MAX_CHANNELS_PER_PLACE` (3)
   Channels of one Slate. A Channel the tenant switches off loses its `proposed` rows on the next
   proposal and they are hidden from `GET /v1/slate` at once (picked/used rows stay).
   **Blog** (AA-741 S271): Segment grain with the Bar demand >= 1,000 and questions >= 3, read from the Segment's
   best market (lowest total_rank, its numbers together); a place the tenant already picked / used / cut on Blog
   is never proposed there again (`one_piece_per_place`). The Blog pick fills `angle_gate_request.route_segment_ids`
   with the walk: the current Route on the seed atom's tour that holds the Segment (merged ids followed, read through
   `route_guard`, at least two Segments; when the seed's tour has none, a member of the same match grade on a tour
   that has one). No Route = a single-place blog. A Blog piece is about its Subject's own moment (the **walk focus**,
   `services/acp_shared/walk_focus.py`, S271 review): T8's Route digest names it FOCUS and the other moments CONTEXT
   (an angle leading with another moment is re-ranked, three of them retried), T9 marks it FOCUS in the seed with the
   WALK FOCUS rule (t9_write v3), so sibling Subjects of one Route get different articles. When that moment is off
   the walk or thin, T8 and T9 both write a single-place piece; the Slate does not propose a Blog Subject whose own
   seed is thin. A Route-grain Subject (picked before S271) keeps the whole-walk digest with no focus.
   **Every adopted tour** (AA-1081, S277, Channels with `cover_each_tour` = Blog): before the round robin the Blog
   queue seats, in rank order, the first candidate on each tour that has no picked / used Blog Subject yet (same
   rules as every seat), so a tour with a free candidate always gets a Blog topic. A tour that still has none gets a
   typed reason (`slate.NO_TOPIC_REASONS`: `not_ready`, `low_search_interest`, `no_place_match`, `seed_too_thin`,
   `no_fitting_topic`, `places_covered`) in the `t7_slate` job result (`blog_coverage`, with admin counts);
   `GET /v1/slate` returns the tenant view (`blog_topics`: tour id, catalog name, topic count, code) and the portal
   words it. **Blog minimum seed** (AA-1077 memo option C): env `T7_BLOG_MIN_SEED_WORDS` (unset/0 = off, the
   default); when set, a Blog candidate whose seed (`blog_seed.blog_seed_words`: the seed atom's seed + its walk's
   moment seeds) is shorter is not proposed on Blog and stays on the shorter Channels.

A Segment Subject is offered and written only from its **seed atom** (AA-857,
`services/acp_shared/subject_seed.py`): a live member on the tenant's own tours whose place IS the
Subject's place (normalised, alias / place-card aware; graded so an exact name beats a member
that only names the place in its parenthetical; a word-set subset/superset — the relation A3 groups
by, since `canonical_place` is the shortest wording — is the last-resort tier, non-generic names in
one known country only), in one country — never the first member by `atom_id`. A Segment with no such member is not offered (log `slate_subject_skipped`) and cannot
be picked.
_Avoid_: Weekly Slots (the pre-AA-511 name — fully replaced, never use again).

**Goal**:
One of a fixed 8-value list (name/description/logic/marketing_term, e.g. "Promotion" →
AIDA) a tenant picks before writing — verbatim from Nghiệp's own "Bang 1" table, not invented.
_Avoid_: objective, intent, campaign type.

**Angle**:
One of 3 LLM-generated framing options (name/why_it_works/formula_fit/best_final_style) offered
per (Subject, Channel, Goal) — the tenant picks one before Write runs.
_Avoid_: approach, take, framing (used loosely elsewhere; Angle is the specific picked object).

**Gate**:
An automated check run inline right after Write (T9/T10). Since AA-924 (S250) a blog gets 23 gate
results, LinkedIn 22 and the other channels 21 (AA-787 had 25 / 24 / 23; count them in `run_quality_gates`).
AA-924 — **per-finding severity and two merged entries.** The gates still run as their legacy source checks;
the stored ledger folds F2_banned_patterns + F2_brand_forbidden + F15_voice's stock findings (STOCK_PATTERNS,
template lines) into one **F2_lexicon** entry (at F2_banned_patterns' place; one finding per word and sentence,
so a word on two lists is judged once; a longer phrase around a shorter word is a second finding), and F15_voice +
F15_voice_warn into one **F15_voice** entry. A merged entry carries `findings: [{text, severity: block|warn, source}]` (source = the legacy gate, a retired name),
`severity` (the worst), and `parts` (one legacy record per source); `violations` and `blocking` (worst is block)
stay for older readers. `quality_gates.expand_ledger` rebuilds the legacy entries and every routing step reads
that view (first failure, hold, sentence-repair plan, waive, regression guard keyed by source), so routing is
the legacy one; `held_reason` and `repair_log[].gate_targeted` name the stored entry (`F2_lexicon: …`). The
AA-800 layers stay their own gates and skip the tenant's own forbidden words. Ledgers stored before AA-924 keep
the old names and read as before. Since AA-613 each failure has a **severity**: `block` (product truth —
facts, grounding, CTA), `warn` (brand/style, e.g. generic AI wording, brand-fit) or `note`. Only
`block` triggers a rewrite (max 2 retries). Never assume "failed a gate" means "held" — check
the severity first. S233: blog shape gates F3/F5/F7 are `warn` (F7 auto-drops FAQ pairs that
restate the body). AA-787 (S245) removed `promises_an_option` (F17_trip_fact owns offered moments, ADR
0009), F8_framework (its LLM call), F3 and F5; F7 folded into F16_faq (the restate fixer runs before
the gates, logged as `F7_faq_autofix`). Old ledgers keep those names. S233 also added the blocking
gates F0_complete (cut-off text), F2_brand_forbidden (the tenant's own forbidden words; inside F2_lexicon since AA-924) and F11_brand_name (another brand / the platform speaking). An F9
judge outage is never a blocking gate (no rewrite on it), but since AA-925 (Q-2, reversing S233's "warn, not a
hold") it **holds** the piece: the F9 result carries a `degraded` marker and, when the FINAL attempt has it,
the last step after `_final_integrity` labels an approved piece `held` (`held_reason` "degraded:
f9_judge_unavailable"), adds a non-blocking `degraded` flag and a `repair_log` entry — see **Degraded
detectors** below. Count gate failures from `gate_ledger`, never `held_reason`
(it names only the first blocking gate). `services/acp_shared/grounding.py` is shared by T3 and T9 and reads numbers with units ("4,460m") correctly (AA-639).
S235 (AA-785): T9/T10 F1 (`quality_gates.gate_grounding`) calls
`find_novel_numeric_claims(..., year_forms=True)`. In that mode a year range ("1637–38") or a decade
("the late 1630s") is read as years, and it passes when the seed has the year: for a range, the full
end year, or the start year when the end is start + 1; for a decade, any seed year inside it.
Otherwise F1 reports the full year or decade ('1652', '1880s'). T3, A1 and `source_facts` keep the
default strict digit-run reading. F1 stays blocking. (AA-776: an F4_seo_surface-only ledger is fixed
by the SEO title/meta fit (`repair_mode: seo_fields`), never by a full rewrite, unless the F4
violation is about the slug.)
S234 (AA-768): `services/acp_content_writing/hygiene.py` owns the deterministic text rules (scope
lexicon + out-of-scope gazetteer from the TRIP name and "other trips" lines, `LEAK_RE`,
`SOCIAL_PROOF_RE`, repeats, hashtag spelling). It fixes what is safe in place before the gates
(logged as the `F13_autofix` flag) and two gates report the rest: **F12_scope** (a place outside
the SCOPE in title/meta/headings/body; warn by default — AA-788: setting `cis.quality_modes.t10_f12_scope` = block makes a body finding
block for one sentence repair and hold when it is left, a title/meta-only finding stays a warning; S287: no code
switch — the setting is read at gate time, and its `block` value is refused unless the evidence is a passing pilot
measurement record reference `aa788-pilot:<run id>:<12 hex>` printed by `scripts/aa788/pilot.py score`, root ADR 0020
precision bar for a deterministic detector, `quality_settings.evidence_problem`) and
**F13_hygiene** (warn). Blogs also drop FAQ pairs the seed cannot answer (age, fitness, price…),
logged under `F7_faq_autofix`.
S234 (AA-771): LinkedIn has a **channel spec** by brand register (`services/acp_shared/channel_spec.py`:
consumer_social / editorial / warm_practical / observational, from the voice adjectives then brand_type) —
word band 120–250 (editorial 300), ≤3 hashtags, first line ≤140 chars; hygiene.autofix applies the hashtag cap
(each tag once — AA-780 S246) and first-line split. **F14_social_shape** (LinkedIn shape; warn, blocks above 1.3× the band → full
rewrite; AA-780: under the band's floor blocks on attempt 1 only → one full rewrite to the target (AA-788: the rewrite
is also given the source's fact sentences the post does not tell yet; kill switch: setting `cis.quality_modes.t10_f14_floor` = warn;
S287: an F14 word-count violation reads as `length_short` / `length_long` like a blog's F4 one,
`length_contract.ledger_length_codes`, with no floor of its own), and never
holds a piece — `quality_gates.never_holds`; the AA-528 regression guard keeps a repairable rewrite of a floor-only attempt 1 for the attempt-3 repair, and on a revert waives the floor block), **F15_voice** (stock-phrase hard list; blocks on attempt 1 only, for one sentence repair, and
never holds a piece — `hold_failure` skips it; AA-780 S246: a STOCK_PATTERNS sentence still in the final attempt's text is cut in code after the loop, `service._cut_stock_lines` — never the hook or the CTA, never below the word band; shown as the non-blocking `F15_stock_cut` flag) and **F15_voice_warn** (trade register, audience-number leak,
generic or repeated opener, AA-780 broetry — ≥2 consecutive prose sentences of ≤5 words, bullets and emoji-only lines excluded; warn only, never repaired; stored as warn findings of F15_voice since AA-924, whose stock-list findings sit in F2_lexicon). All pass for every other channel. The F9 judge
gets the brand's voice adjectives and no longer favours a calm house voice. **F9_brand_style** (off-brand voice /
generic AI wording, from the same judge call) warns by default; AA-788: setting `cis.quality_modes.t10_f9_brand_style` = repair makes an
attempt-1 failure whose flagged phrase sits in a body sentence block for one `t9_repair` sentence repair, never a hold
(`STYLE_ONLY_GATES`). Each promoted check logs its decision (`t10_promoted_check`) when its mode is not the default;
`scripts/aa788/promotion_replay.py` measures the expected hold rate on stored pieces ($0).
S234 round 2 (AA-768): **F13_leak** (blocking, sentence-repairable) — writer-instruction wording ("A Sentence
on…", "This guide covers…", the SCOPE's day span), the brand pack's audience ranges ("25–40", "seven-to-sixteen")
and a piece opening on a pronoun with no referent; hygiene.autofix trims headings and deletes such a sentence
only when no pronoun is orphaned. A part-of-trip piece's writer gets the **scoped trip name** (no duration, no
out-of-scope places); the whole trip's length and "the journey concludes" are F12_scope hits. **F15_voice**
also counts contrast frames (>1 per 150 words) and lines shared with up to 5 earlier pieces of the same request,
on every channel; a style-only repair runs on `t9_repair`. Contract gates last in the ledger, owned by one-call
fix steps (`t9_repair`) and never sent to a rewrite: **F16_faq** (blog ends with "## FAQ" and ≥3 pairs after
the droppers; one FAQ regeneration per writer text) and **F14_first_line** (LinkedIn hook still over the
spec after the split and one first-sentence rewrite). Every sentence repair, FAQ regeneration and first-line
rewrite is reverted when it leaves a new repeat or an orphaned pronoun (`sentence_repair.splice_checked`). An
approved piece's leftover style-only failure is stored `blocking: false, waived: true` in `gate_ledger`.
S234 round 3 (AA-768): `services/acp_content_writing/trip_facts.py` owns the trip-fact rules. **F13_repeat**
(blocking, sentence-repairable) — a figure told twice, an echoed phrase, an orphan line, a moment repeated under
another moment's heading (autofix deletes the safe ones first; every repair is integrity-checked for the same and
deletes the new later duplicate before it reverts). **F17_trip_fact** — an optional activity written as fixed, a
time / ordinal / position the seed lacks, a family brand (audience with children) pitching a moment without the
seed's access caveat, a role the seed dates as ended written in the present or a wrong span; **F18_rarity** —
rarity / uniqueness claims and quantifiers stronger than the seed's. Both blocking and sentence-repairable; a
title/meta-only finding is a warning (the SEO fit refuses such a meta). The full trip's places outside the scoped
days (`v_active_tour_atoms`) join the out-of-scope gazetteer. F15_voice adds cross-round stock lines and brand-pack /
angle-brief phrases on every channel. The configured CTA line is restored word for word after autofix. When the
only blocking failures are F15/F13_repeat/F17/F18 the repair runs on `t9_repair`; an attempt-3 repair call that
fails cuts the flagged sentence in code instead of holding.
S234 round 4 (AA-768): `services/acp_content_writing/text_integrity.py` owns the **final pass** — the last step
before the gates read an attempt AND before the piece is stored (after every repair, revert and the tag strip):
whitespace normalised, exactly one CTA (a reworded copy next to the literal line is cut), later duplicates cut
(paraphrased ones too), lower-case fragments cut; what it cannot fix is `hygiene.find_issues()["repeat"]` —
the same list F13_repeat reads — and holds the piece (fail closed). A far repeat blocks only when it is a
real one (`hygiene.strict_repeat`: a copy, Jaccard ≥ 0.5, or nothing new); two shared trigrams alone (a place
phrase, the keyword) stay an F13_hygiene warning. The CTA paraphrase search covers only the closing sentences
next to the literal line (never the hook), and the literal line is matched across sentence breaks like
`has_cta_literal`. The admin sentence apply path returns what an edit newly breaks as an F13_repeat warning. Before it, `trip_facts.fix_trip_facts`
folds a "Thin moment"'s own heading into the section before and cuts an interpretation appended to its closing
mention, deletes a family-brand reassurance that plays down a seed hazard, and removes a background-only place
made the reader's gateway / base (FAQ included); the rest is F17. A trip place the seed's place background names
is not out of scope. The blog SEO title may not repeat a title of the tenant's last 50 blog pieces (F4); only the
blog gets an SEO title/meta (the fit no longer runs for other channels).
S258 (AA-789 tier 1) — **seed alignment** (`services/acp_content_writing/seed_align/`): deterministic checks that
compare a sentence with the typed facts of its seed (`seed_graph.SeedGraph`: offered items, planned moments, 'near'
relations, world figures, the seed's own superlatives, brief lines). A finding needs a spaCy dependency structure
(`en_core_web_sm`, pinned, loaded lazily), a seed relation and a trigger word; any missing key abstains. Nine classes
(optional told as planned, planned told as optional, near told as at, difficulty promise, world share, scopeless
superlative, brief recipe, naming note, orphan opener). Each class has a mode (settings `cis.quality_modes.t10_seed_align[_<class>]`:
off / shadow / repair / hold, default off): shadow records candidates in `writer_input.seed_align` only; repair adds
`sa:<class>: …` findings to the existing F17_trip_fact / F18_rarity / F13_leak / F13_repeat results (no new gate);
`quality_gates.never_holds` keeps an `sa:`-only result from holding unless its class is in
`seed_align.modes.HOLD_PROMOTED` (empty: a promotion PR with shadow evidence, only the four fact classes). With every
class off the ledger is byte for byte what it was.
AA-1000 (tier 2) — **adjudicator** (`seed_align/adjudicate.py`): one LLM call per attempt (stage `t10_adjudicate`,
role judge, GPT-6 Luna on acc3, one `llm_call_log` row per reply) over the sentences tier 1 triggered on but left
alone (deterministic spaCy-free triggers; tier 1 must have run cleanly this attempt; ≤ 6 sentences), each with a seed
excerpt, for four forms tier 1 cannot reach: paraphrased naming note (F13_leak), implicit-subject repeat (F13_repeat),
open WHAT / WHY / HOW FAQ answer that does not answer (F13_repeat; answers of 2+ sentences only), hype such as "hard to
overstate" (F18_rarity). A VIOLATION counts only when its piece quote is in the sentence and its seed quote in the
excerpt and the seed. Modes (settings `cis.quality_modes.t10_adjudicate[_<class>]`): off (default; no call) / shadow (recorded in
`writer_input.seed_align[*].tier2`) / repair (at most ONE `sa:t2.<class>: …` finding per attempt → that gate's
sentence repair or cut); any other value is off. It never holds (`t2.` classes are never hold-capable), and a
result failed only by a tier-2 finding is no AA-528 regression (`service._regression_failures`); any model
error or the 60 s deadline changes nothing; a $0 replay (`f9_results`) calls nothing. Offline calibration:
`scripts/aa1000_adjudicate_calibrate.py` (hard cap $5).
S245 (AA-779) — **FAQ from People Also Ask, every answer names its subject.** The request's
`dfs_paa_snapshot.people_also_ask` (per tour) rides in the T9 context; the job keeps the questions of the
piece's own places (`quality_gates.paa_for_scope`: no out-of-scope place, no topic the source cannot answer,
names an own place or shares 2 seed words; ≤6) and gives them to the blog writer, the rewrite and `write_faq`
as SEARCH QUESTIONS (used only when a seed fact answers one). After the other FAQ droppers,
`drop_subjectless_faq` drops a pair whose answer does not name the question's subject (pronoun or
demonstrative opening, wrong subject) or only says what is not the case, while more than 3 pairs remain;
pairs it had to keep make the writer text's one FAQ regeneration rewrite them. Logged in `F7_faq_autofix`;
F16_faq stays the only FAQ hold.
S245 (AA-779 2A + 3) — **real-question source chain for blogs** (`services/acp_shared/real_questions.py`,
$0 DB reads, never a DataForSEO call): a blog's questions come first from its own Segments
(`atom_matches` kind `question` on the Segments' atoms — per Segment only the `questions_count` newest rows,
the table is upsert-only), then from `search_demand.people_also_ask` of keywords naming one of its places
(Segment and place questions English only — those can come from DE/FR/NL markets), then the tour-level PAA
of the request snapshot (a US/English SERP: not language-checked). T8 angles of a blog request read
the merged list (snapshot keeps the tour list as `tour_people_also_ask`); T9 `start_write` re-reads it live
with the piece's kept moments as scope. Off-meaning PAA (the place name used as a word — 'Is moron a bad
word?', or as a common noun — 'What is a moron?' — matched against the piece's own place words) is dropped in
the chain (every tier, so it never reaches the T8 prompt, ranking or the tenant-visible snapshot) and in
`paa_for_scope`. Each piece's `writer_input` (admin-only)
records `search_questions` (with source), `question_sources` (per-source counts) and `faq_questions` (each
final FAQ question's real source, or `writer`); the admin Content Quality drawer shows them. AA-779 (B) SERP
top-up and (C) Autocomplete are admin jobs that will add sources to `DB_SOURCES`.
S245 r7 follow-up (AA-779) — **real questions first.** The off-meaning test also drops a place name used as
a dictionary word ('Is moron a British word?', 'What is moron in German?', spelling / translation cues, or
the name lowercase with no travel word). When the writer's FAQ skips a given real question, the blog's one FAQ
regeneration (`write_faq` `required_questions`) answers it first; the FAQ then lists real-question pairs first
(a reorder only — a writer pair goes only above 5 pairs; deterministic, `real_first_faq`; `writer_input`
records `faq_pairs_before`/`after`). The lowercase-name test reads only whole single-word place names that are
not common English words, with no other place / country / travel word in the question, never on autocomplete
text. (B) is built: `services/acp_shared/question_topup.py` + `scripts/aa779_serp_topup.py`
(admin-run only, dry run by default, hard cap $3 across all runs, summed from `shared.dfs_call_log` endpoint
`serp_advanced_paa_deep`) buys one SERP with `people_also_ask_click_depth=2` per blog place with < 3 real
questions, query '<place> <country>', stored in `search_demand` (keyword `topup_keyword`, its own market
`US-PAA2` apart from the Segment research cache, no volume; an empty row marks a place bought with no
question) and read back as the `serp_topup` tier.
S248 (AA-982) — **T9 drops place PAA that A3 Jev already rejected.** On a blog write, `start_write` runs the
chain with `a3_filter=True`: the `place_paa` tier's questions are looked up in `shared.decision_log` under the
subject keys A3 wrote (`atom_ranking.scope_subject_key` / `landing_subject_key`) through
`decide.platform_verdicts()` (read-only: current wording hash, `NOT cached`, ≤ 180 days, platform tenant,
Zone recomputed with today's floors, acts only on enforce-mode confident verdicts; no Jev call, no new gate,
$0). A question goes when A3's own scope rule (`scope_dropped`) rejects it for the piece's country set or for a
Segment set that contains every piece country, or when `a3_landing_belongs` rejects it for every moment of
the piece (unless about_here accepts it for the piece). No verdict → kept. The dropped text cannot come back
through a later tier; `segment_match` is never filtered (it passed the same gates). Counts go to the
`t9_place_paa_a3_filter` log line (api, the write request) and `writer_input.a3_question_filter`. Kill switch: api env
`T9_A3_PAA_FILTER=off` (default on). T8 angles do not use it yet.
S235 (AA-786) — **F9 v1.1** (`F9_PROMPT_VERSION = "f9_v1.1"`): the judge reads the piece without
citation tags (F1/F5 keep them) and, for blogs, the search title and meta description. New
product-truth code **TEXT_INTEGRITY** quotes each broken sentence and goes to sentence repair, not a
full rewrite. A non-pass F9 verdict with no checkable reason is a warn, never a hold. A claim that is
not in the body at all but is in the title/meta is a `title …`/`meta …` warn owned by the SEO fit. On
LinkedIn, at the last attempt, the sentences behind an F9 product-truth failure are cut (never the
hook or the CTA); F9 is waived only when every flagged sentence was cut and the post stays in its
length band, otherwise it stays blocking and the piece is held for the admin (the tenant still
receives it, AA-770). A CTA failure still holds.
S239 (AA-777) — **F9 sticky pass**: one set per write job (`f9_passed_sentences`, passed to
`run_quality_gates`) keeps the body sentences an F9 verdict of that piece passed; a later verdict that
flags one of them while it is unchanged has that claim dropped (`F9_sticky_pass` log, `sticky_pass` on the
F9 entry). Only a verdict with a checkable reason adds to the set; an outage adds nothing. **Blog truth
cut**: on a blog's attempt 3, when attempt 2 was a sentence repair of a failing F1/F9 gate, the F1/F9
items still failing are cut in code (`sentence_repair.delete_flagged` + `tidy_after_cut`) instead of a
second repair call; other items still go to the repair, and the gates re-judge the cut text. A cut that
would take the blog under `_BLOG_MIN_CHARS` (or break its FAQ / ending) is refused: the old repair path
runs and the piece stays held if it fails. The S234 attempt-3 fallback cut obeys the same floor on blogs.
S239 r6 follow-up — a sentence-repair replacement that ADDS a number, time / ordinal / position or (blog)
pace word the sentence and the source lack is not spliced (`sentence_repair.new_specifics`, the F1/F17
functions themselves); `plan()` merges two findings on one sentence (truth gate first) instead of dropping
the second; the sticky key carries the previous sentence only for a referent-led sentence ('It…', 'These…',
'There…', 'Then…', 'But it…', 'The <noun>…') and a multi-sentence judge quote is sticky when each sentence is;
the blog attempt-3 cut also takes F17 'cut it' items once F17 was repaired before. A sentence whose finding
says to keep it (played-down hazard 'keep the hazard', family caveat 'add ONE') is never cut, merged or not,
and an empty repair of a hazard sentence is not spliced; a multi-sentence cut checks every sentence in it
(hook, CTA, FAQ question) and the sentence after it (orphaned pronoun).
S271 W0 (AA-900, root ADR 0020 D6) — **stop and label**: attempt 3 no longer pays. A piece still failing after
attempt 2 keeps the better of attempts 1 and 2 (the AA-528 guard) and is labelled at $0: no second repair, no third
F9 judge call and no code truth cut (the blog cut, the LinkedIn F9 cut and the attempt-3 fallback cut above run only
with `T9_LAST_ATTEMPT_MODE=legacy`, the rollback switch; a cut needs a passing measurement record for its class).
A held piece always ends its `repair_log` with a label entry (`gate_targeted`/`repair_mode` `stop_and_label`, or
`repair_mode` `label` when no paid attempt was skipped): `reason_codes` (one typed code per hold-capable failing
gate, `gate_eval.HOLD_REASON_CODES`, plus `detector_degraded`), `kept_attempt`, `cost_usd` 0 and `failing`.
Every attempt's `repair_log` entry lists **every failing gate** (`failing`, `gate_eval.failing_gates`: gate,
source, blocking, may_hold, n, F9 `classes`), not only the first one `held_reason` names. **Interim F9 policy**
(`services/acp_content_writing/f9_policy.py`, declared diff, setting `cis.quality_modes.t10_f9_policy` = legacy rolls back):
F9_cta_fact holds only on a quoted body claim with a why_not_in_source that names a concrete trip activity,
service, time or view (`trip_promise_kinds`, D1/D4/D15); its violations keep those claims only. Every other F9 class
warns (`blocking: false`, in the flags): place background and other unsupported claims, CTA_NOT_REFLECTED,
TEXT_INTEGRITY, title / meta claims, a fact flag with no named claim. The F9 classes (`f9_policy.entry_classes`)
keep `judge_unavailable` and `text_integrity` apart from the fidelity classes in every count.
AA-1077 (S277) — **blog length contract** (`services/acp_content_writing/length_contract.py`): one owner for the F4
blog band (4,700–13,000 tag-stripped chars; `quality_gates._BLOG_MIN_CHARS/_MAX_CHARS` read it), the writer's word
target (read from the hashed `prompts._BLOG_WORDS` / `_BLOG_WORDS_MAX`, pinned above the floor by a test) and the
extend target. A blog F4 violation names its side (`length_short: content is only N chars (about W words), under the
blog floor …` / `length_long: … far beyond …`) and the hold reason code is `length_short` / `length_long`
(`gate_eval`; old labels keep `length`, group with `gate_eval.reason_family`). The normalizer ends with an F4 re-check
of the stored blog text (`length_recheck.recheck_length`, service's `_recheck_length`: an approved piece under the floor is held) and
`writer_input.length_trace` (chars after each step); its FAQ answer fit never drops a pair that would take the blog under the
floor. **Extend repair** (`length_extend.py`; switch = the setting `cis.t9.blog_length_extend`, off | on, CIS 418,
platform + tenant scope, resolved once per piece for its tenant (S280); a job `trial_flags` value first, the
setting next (no env override since CIS 424); default off = the full rewrite): attempt 2 of a blog short of the floor adds ONE paragraph to each of up to 3 thinnest body sections
from the seed's untold fact sentences (one `t9_write` call, `generate.extend_sections`, mode `length_extend`), each
checked in code (plain prose, size, `repair_contract.new_specifics`, seam integrity) before every gate re-runs; a gate
needing the full rewrite still gets it. Scripts: `scripts/aa1077/remeasure_length.py` ($0) and `extend_trial.py`
(paid, capped, never writes a piece).
AA-1077 S278 — **blog length target**: every length AIM is one number above the floor, never the floor itself:
`length_contract.target_chars()` = F4 floor × (1 + `t9.blog_length_target_margin`; S279 L2: the setting
cis.quality.t9_blog_length_target_margin, 0.17 → 5,499 chars) and `target_words()` (950). The writer prompt's word floor
(`prompts._blog_words()[0]`, t9_write v8, CIS 413) renders `target_words()` at call time (the F4 floor
cis.t9.blog_chars_min and the margin are settings); a value other than the seed puts `.s<8 hex>` on the ledger's
prompt version; the extend repair
aims at the target; a blog's F4-short rewrite feedback carries "lengthen the piece to at least 5,499 characters …"
(`service._f4_target`). The floor (4,700) is unchanged. The normalizer's optional steps
(`length_contract.NORMALIZER_STEPS`: the FAQ answer fit; the stock-line cut guards itself) never make an edit that
leaves a blog under the floor and shorter (`floor_guard`: an in-band blog is not taken under it, one already under it
is not cut further); mandatory steps (usage-policy cut, final integrity pass) still cut and the re-check holds.
`writer_input.length_trace` records `target` and `floor_kept`. `scripts/aa1077/margin_replay.py` ($0) replays stored
length traces under the new rule.
AA-1077 S278 round 2 — **repair round net length** (the paid trial held 2 of 3: a repair with 6 cut fixes shrank a short
blog 3,957 → 3,546 chars; a lengthen line naming "CONTENT SEED … FACTS lines" was copied as "The source notes name …").
t9_write v7 (CIS 405) adds a section plan to the blog block (`prompts._blog_length_plan`: opening, 5 sections with the
richest 1.5×, FAQ at its low end; budgets add up to ≥ the word floor, from hashed symbols only) and a never-mention
rule (block and every rewrite footer). No instruction names where facts come from without that rule
(`length_contract.NO_SOURCE_MENTION`); F13_leak (`hygiene.LEAK_RE`) also catches "the itinerary says / names …" (provenance verbs only;
"the itinerary gives you …" is reader copy), "the source(s) / notes say …", "source facts", "FACTS lines". A blog rewrite under the floor is told the
cut-type fixes first (each with `REPLACE_NOT_DELETE`) and the length line LAST ("after every fix above"). With
the extend switch on: every attempt-2 blog repair round (sentence repair or full rewrite) that leaves the text under the
floor runs the expand step on THAT text (`length_extend.plan_after_repair`: deficit to the target incl. the cuts, the
sections the repair shortened first, marked to the model), cut-type sentence-repair problems (never a repeat) carry
`KEEP_NOT_CUT` when the cuts would leave the blog under the floor (attempts 2 and 3), attempt 3 (no expand after it)
puts back any delete that would take the blog under the floor (`service._floor_kept_cuts`, `length_floor_kept`), an
expand paragraph that names a source is refused, and `repair_log[-1].length_net` records before / after_repair /
after_expand / expand reason (`attempt_3_no_expand` on attempt 3) / `shrank_under_floor`. Switch off = main's loop (the feedback order and
wording above are on for everyone).
AA-1077 S280 — **post-normalizer lengthen pass** (`length_recheck.lengthen_after_normalizer`, after
`_normalize_final_text`, before the result is unpacked): a blog the loop APPROVED that a mandatory normalizer step
(usage-policy cut, final integrity pass) took under the floor, so the re-check held it, gets ONE lengthen pass when the
piece's pinned extend switch is on and it has a PAID attempt left (`paid_attempts_for`: `cis.t9.max_attempts`, and in
stop-and-label mode never past attempt 2, root ADR 0020 D6: so there only an attempt-1 approval is eligible): the expand plan
on the stored text (`plan_after_repair`, the cut sections first), one `extend_sections` call (quality signal
`step: post_normalizer`; a tenant-budget stop is re-raised), then `text_integrity.final_pass` and EVERY gate (the loop's
`run_quality_gates` partial, attempt > 1) on that text; accepted (status approved, the re-run ledger) only when all pass,
else the normalizer's result byte for byte. Recorded in `writer_input.length_trace.post_normalizer`. Switch off = main.
AA-1077 S281 — two settings of the expand (CIS 422, platform + tenant, pinned with the switch once per piece, read only
while it is on): `cis.t9.blog_extend_material` (source_facts default | with_background: a paragraph may also give
third-person general background about what the post or its source names, `_EXTEND_BACKGROUND_SYSTEM`; each background
paragraph is refused in code on a reader / group address, traveller / trip / service wording (`_TRIP_RE`), a name the post and source lack, a new number / time, or a
NEW F17 / F18 finding on the spliced text; a plan no longer needs an untold fact) and
`cis.t9.blog_extend_shrink_sections` (default 3; 0 = deficit only): after a repair round or a mandatory normalizer cut
that shrank the blog under the floor, the plan asks that many sections, the same words shared out.
Quality registry (qtax-v1, AA-926): every gate, flag and gate log name is a constant in
`services/quality/registry.py`, with its label, class(es), kind (`gate` / `flag` / `telemetry`), channels and
`retired` flag in `services/quality/taxonomy.yaml`. The same file maps every Jev question key, admin chip and A1
validate / brand-audit / day-oracle / S1-first code-check code to an error class (17 classes + `other.*` at warn;
Writing Memory hard stop 1 = every `fact.*` class with `severity: hard`). `code_checks` names the detector modules
(`repair_contract`, `degraded`, `day_oracle`) and `aliases` maps each Q0 corpus class id (`other.length`,
`other.cta`, …) to its canonical class, so the Q0 cases are never relabelled to fit the taxonomy.
`python -m scripts.quality.redundancy_census` lists enforcing detectors whose classes another code check already
enforces (input for gate removals, AA-924). **Quality settings (S279 lane L2, CIS 412):** the 7.0 pass line and the
other score cut-offs, the detector modes, the claim verdicts, the T10 / seed-align / adjudicator / T9 brief / T7
debate / T2 judge gate / T3 background modes, the T7 debate cut and Channel bars, the SEO bands and the blog length
band are settings of the platform settings registry (Platform › Settings: evidence-gated, maker-checker,
`services/quality/settings_keys.py` names every key). `services/quality/thresholds.yaml` keeps one key per consumer
(A1 retry, judge, T2 status, T3, T7, admin views, golden-set eval), each naming its `setting` (`registry.threshold`
reads it at call time); taxonomy.yaml keeps the detectors without their mode; writing_policy.yaml keeps the structure
(`writing_policy.effective` lays the values in; the compiled file and `policy_sha` stay the seeded policy). Rules
between keys (a cap under the pass line, a band's minimum under its maximum, the blog length band inside the word
band the t9_write prompt states, the Channel bar fields) are `quality_settings.cross_problem`, checked when a value is
proposed and again when it is approved (against the approved rows read in the decision transaction, now and once
future-dated approvals take effect: 409 `cross_check`); the SEO bands and meta aims are listed but not editable yet (the A1 / T2 writer
prompts read them at process start). The change-control `gates` lock pins the governance and seed of every gate
setting and requires evidence_required on each. Jev floors stay per question in `shared.decision_question`. Names
are persisted (ledger, `repair_log`, `held_reason`, flags, `llm_call_log.quality_signal`): never rename one; a removed
gate stays with `retired: true`. The admin UI reads gate labels from `frontend/lib/quality-registry.generated.ts`
(`python -m services.quality.gen_frontend`) and the pass line, score bands and SEO bands at runtime
(`useQualityThresholds`, GET /admin/quality/thresholds).
_Avoid_: check, validator, rule (Gate is the specific automated-check object; "rule" is generic).

**Piece**:
One row of generated content for one (Subject, Goal, Angle) — carries `attempt_number` (max 3;
attempt 3 is a sentence-level repair only, on the same row — the AA-485 buffer retry row is gone; since S271 W0
attempt 3 is the $0 stop-and-label, so a stored piece has at most 2 unless the legacy mode is on),
`gate_ledger` (every Gate's result on the persisted attempt), `status`
(`approved`/`held`/`failed`), and (blog only) a `publish_log` row once T11 ships it.
Each failed attempt's `repair_log` entry also keeps that attempt's own trimmed ledger
(`repair_log[].gate_ledger`: gate, passed, blocking, at most 5 violations; AA-791,
`gate_eval.ledger_snapshot`; AA-924: a merged entry also keeps `severity` and at most 5 `findings`
{text, severity, source}), so gate precision and recall can be measured across attempts; pieces
written before AA-791 have only the persisted attempt's ledger. A **Label** is an admin's human
verdict on one Piece (`acp_shared.content_piece_label`, migration 231, one row per Piece and
labeller: `as_is`/`light`/`heavy`/`reject`, per-class defects, minutes to fix, reason, and the
sha256 of the text labelled, so a later admin edit shows as changed). It is the ground truth gates
are re-scored against (AA-783 criteria); admin-only, set from the Content Quality drawer, no tenant
column and no `aa_app_user` grant.
The same Label exists for a T2 tour version and an A1 (S1) rewrite (AA-923 WM-32,
`acp_shared.content_label`, migration 315: `subject_type` `t2_version` / `a1_content`, `subject_id`,
the subject's `tenant_id` as the erasure key, the same CHECKs; one row per subject and labeller),
set from the T2 version drawer and the tour panel's Rewrite tab (`PieceLabel.tsx` `SubjectLabelSection`);
`content_piece_label` and its T9 readers are unchanged.
`held` is an **admin-only quality label** (AA-770, S233): the tenant always receives the content
and can read, edit and export a held Piece like an approved one — including a piece held because a detector
was down (AA-925 `degraded`). Its **publish** is the account owner's explicit choice (AA-1088, M3 of the S278
audit): `POST /v1/publish-log/{id}/publish` answers a typed 409 `content_needs_final_check` (body `detail` =
a content-style sentence with no score, gate or reason, `code`, `can_publish_anyway`; header `X-Error-Code`)
until the owner (studio role `owner`, or a token without a role) sends `{"publish_anyway": true}`; that choice is
written to `acp_shared.audit_log` (`content_piece.published_anyway`: channel, membership, role, revision key)
before the post goes out and fails closed (503) without the row; an editor's `publish_anyway` is refused.
Put back online (a page already live once) is not re-checked. **Tenants never see internal gate
data**: the tenant-safe projection (`_tenant_safe_piece`) drops `status`, `held_reason`,
`gate_ledger`, `repair_log` and `flags`, exposing only `ready_state` (`ready` / `not_ready`, where
`not_ready` means there is no content yet). The same rule covers T2 tour versions (see **Tenant
projection** below). `writer_input` (migration 220) stores what the writer was given. Admins
see everything on **Content quality** (`/admin/tenant-qa/quality`, AA-769; was `/admin/content-quality`), plus
Content Trace and the review log (Tenant content QA). An **admin** (staff role `admin` only) can cut or rewrite one flagged sentence of
an approved/held Piece from that drawer (S234): *suggest* is one cheap LLM call (stage
`t9_repair`, logged with `content_piece_id`, nothing saved); *apply* is deterministic (span-text
match under `FOR UPDATE`, the replacement re-checked by F1/F2/brand-forbidden/F11 and — with the
stored `writer_input` — F12_scope/F13_hygiene as warnings only, a near-verbatim neighbour blocks;
splice + tidy, using the
same sentence primitives as the T9 hygiene in `hygiene.py`)
and writes `content_text` plus an `acp_shared.audit_log` row
(`content_piece.admin_sentence_cut|_rewritten|_edited`, before/after) — the audit log is the
Piece's edit history. `gate_ledger`/`flags` stay as the writer produced them, and a published
post is not re-synced. The replacement is cleaned like pipeline output (no citation tag, no
markdown on plain-text channels) before it is checked and saved. An admin can also regenerate a
request's 3 T8 angles (`POST /admin/content-quality/requests/{id}/regenerate-angles`): it clones
the request (same tenant, atom, subject, route, channel, CTA, goal) and runs the `t8_angles` job on
the clone, so the old request, its option rows and its pieces' angle labels never change.
_Avoid_: post, draft, content (all used loosely elsewhere; Piece is the specific DB row).

**Tenant projection (AA-770, AA-796)**:
Every tenant-JWT read of tours and tour versions goes through `api/routers/_tenant_projection.py`,
the single tenant rule:
- Stored status: T2 still stores `status='needs_review'` when the score is < 7.0 (or the T3 QA gate
  escalated). That stored value is the admin signal (admins read it in `/admin/content-quality` and
  in the tenant rewrite activity). Tenant reads map it to `ai_generated` ("Ready to review");
  Marketplace shows it as `ready`.
- Hidden fields: no tenant API returns `quality_score`, `quality_score_id`, `qa_*`, judge or writer
  output (`generated_content`, `quality_scores`), the A1 review state of `raw_tours`, or LLM/Jev cost
  (`/v1/billing` dropped `llm_cost_usd`, reversing AA-636).
- Commercial data (AA-1188, S295 data-boundary audit DB-01): no tenant API returns AA's supplier or commercial
  data: the supplier named in a source sheet (`raw_tours.provider`, `supplier_id`), the source price text
  (`raw_tours.price_raw`, root ADR 0005 d7: reference only) or any net / cost / margin / markup figure.
  `TENANT_COMMERCIAL_KEY_RE` refuses those keys in every projection and in the key-walk, next to the AA-770
  rule `TENANT_FORBIDDEN_KEY_RE`. A `raw_tours` row reaches a tenant only through the allowlist
  `TENANT_RAW_FIELDS` (tour id and trip facts: country, country code, duration, group size, best time to go,
  activities, inclusions, exclusions); a new `raw_tours` column stays AA-side until it is added there. The
  staff / pipeline columns of `published_tours` (approver, lock, deletion, gold path, embedding, writer row)
  are hidden too. The price a tenant sees is AA-Booking's `from_price` (`/v1/tours/pool`, `/v1/marketplace`).
- Pool: newest-first only, no quality sort or filter.
- Admin path: `/v1/tours/{id}/full` keeps the full before/after payload only for the X-Admin-Secret
  path.
- Test: `tests/unit/test_aa796_tenant_projection.py` key-walks the tenant routes.
- Silent correction (AA-802): an **admin** can cut / rewrite / edit one sentence of a T2 version
  from the Content Quality tour-versions drawer (`/admin/content-quality/tour-versions/{id}/sentences/suggest|apply`).
  This works only for finished AI text the tenant has not decided on (`ai_generated` / `needs_review`),
  only on their latest version, and never on a `tenant_edit` version. The row is updated **in place**
  (`rewritten_content` only, by `id AND tenant_id`; 0 rows → 409). There is no new version, no quota use
  and no tenant-visible mark. The history is the `audit_log` row
  (`tenant_tour_version.admin_sentence_*`, sentence and whole section before/after). Both drawers
  (T9 piece, T2 version) warn the admin when the tenant took a copy away (published, or exported;
  the T2 DOCX export writes `tenant_tour_version.exported`) before a later admin edit. Nothing
  re-syncs that copy.

**Channel**:
One of 8 values: `blog`, `linkedin`, `facebook`, `instagram`, `tiktok`, `email`,
`landing_page`, `ads`. Each has its own Slate Bar and Gate count (blog ≠ the other 7).
_Avoid_: platform, network (Channel is this project's own term for the publishing surface).

**Facts Entry (tenant scope)**:
One hand-written, sourced claim a tenant writes for themselves only (their own pricing,
cancel/rebook terms, deals) — visible only to that tenant, never to others. Same table as the
platform-scope entry above; `tenant_id` is required here, NULL there (enforced by a DB CHECK).
Writers read both scopes for the trip's country, a tenant entry replacing the platform entries on
its subject (`facts.pick_facts`): T9 always (the FACTS seed, each entry with a `Source:` line the
leak checks know); T2 only with the worker env `T2_FACTS=on` (default off: a FACTS block in the
T2 user prompt, also read as T3 grounding source; `services/content_generation/t2_facts.py`).
AA-1144 (S285 decision D7): an agency writes only `booking_terms` facts (`facts_store.AGENCY_FACT_KINDS`); prices,
seasons, visas, transfer times and other tour facts are Adventure Asia's (the agency's markup is a CRM price rule).
`/v1/facts` refuses any other kind (422); an older own fact of another kind stays listed (`read_only`) and deletable,
is never edited, never reaches a writer (the read SQL and `pick_facts` leave it out) and never replaces a platform fact.
_Avoid_: Fact (bare) — always say which scope.

**Fact lock (agency T2 copy)**:
AA-1144 (S285 decision D8): an agency may reword its T2 copy (`PATCH /v1/tours/versions/{id}`, action `edit`) but not
change what the tour is. `services/content_generation/t2_fact_lock.py` checks the edit against the version it edits,
the Master (`published_tours.aa_*`) and the raw source (never `price_raw`, AA-1188) with the FactSheet extractors (`services/quality/s1_facts.py`):
a non-wording key, another day structure, a figure the base never states (a spelled-out duration such as "a two-day
walk" counts), a new place in the highlights or itinerary (checked against the parent's highlights / itinerary and the
Master / raw texts only, never the parent's free-wording fields), a day / night / week figure other than the Master's
length or one AA wrote (the version, the Master; never the raw texts), a dropped / added "not included" item group, or
a new "included" claim the Master does not state (the version / Master text says it is included or the raw inclusions
list names it, with the same scope: "international flights" is not cleared by domestic ones) or the source excludes
is a 422 `fact_locked` (`{code, message, locked:
[{field, fact, value}]}`). The edit is laid over the version (fields it does not send are kept); `edited_by_user` is the member from
the token. Every edit and refusal writes `tenant_tour_version.edited` / `.edit_refused` (field + old / new sha256, no
text) on MAIN after the transaction, best effort; the agency sees both in Account › Activity.
_Avoid_: "T2 facts" for this (that is the `T2_FACTS` writer input above).

### Model operations (platform)

**Stage**:
One named place in the code that calls a model for one purpose (`t9_write`, `s1_judge`,
`t5_atomize`...). Admin picks the model per Stage; every call is logged under its Stage.
_Avoid_: step, node, role (Role is a separate property: writer / judge / validate).

**Model Key**:
The short, stable name the platform uses for one model (`haiku`, `sonnet-5`, `gpt-5.6-luna`),
independent of vendor ids, inference-profile prefixes or which AWS account serves it.
_Avoid_: model id, tier (the old name for the same thing).

**Model Catalog**:
The single list of models the platform can call, with each Model Key's vendor, how it is called,
which accounts serve it, its price, and whether it is enabled. The admin dropdowns and every cost
figure come from it. See ADR 0005.
_Avoid_: model list, pricing table.

**Stage Route**:
The ordered list of Model Keys a Stage tries (primary first, then fallbacks), plus an optional
shadow model that runs alongside for comparison and never changes the result. See ADR 0006.
_Avoid_: fallback chain (the old hardcoded Haiku/Sonnet chain), routing.

**Usage rollup** (AA-1045, CIS 372):
`shared.ai_usage_hourly`, one row per UTC hour and dimension set (source llm / jev / dfs / geocoder, app, provider,
account, model, stage, role, tenant, status, error class, traffic) with the summed requests, tokens, cost, latency sum
and a latency histogram, built from the paid-call ledgers by job kind `ai_usage_rollup` (`api/services/usage_rollup.py`,
`services/jobs/ai_usage_rollup_job.py`: queued by the api's ops sampler every quarter hour, re-rolling the last 6 hours
for late rows, DELETE + INSERT per hour range, idempotent; a one-time backfill of the raw window; never an hour older
than 175 days). Its totals equal the ledgers' for the same hours (`GET /admin/usage-explorer/rollup/check`,
`scripts/aa1045/rollup_live_check.py`). One definition of each dimension per ledger, shared with the Calls tab's
filters: `api/services/usage_dims.py`. The **Usage explorer** page (`/admin/usage`, AI & spend; spend.read: admin,
finance, ai_ops) reads it (`api/services/usage_explorer.py`) plus the CRM's daily totals through crm.ops.v1 GET
/llm/usage; its saved views are `shared.ai_usage_saved_view` (per staff member); a cell opens the Calls tab with the
same range and dimensions (`since` / `until`, `tenant`, `model`, `account`, `role`, `error_class`, `product`).
_Avoid_: usage report, analytics table.

### Jev decisions (platform)

**Jev Question**:
One fixed, typed question a Stage asks Jev (TypeSafe) about a subject: yes/no (noul), pick one
label (choice), or a level on a scale (score). Its wording is data, versioned, and it carries its
own Mode and Floors. See ADR 0007.
_Avoid_: rule, check, gate (a Gate is a T10 check), judge (a Judge writes feedback; Jev never writes).

**Verdict**:
Jev's answer to one Jev Question about one subject: a probability (or a pick and its confidence)
and the Zone it falls in. Every Verdict is kept, whatever its Zone.
_Avoid_: answer, judgment, decision.

**Zone**:
Where a Verdict falls: accept (confident yes), reject (confident no), grey (not confident),
error (Jev could not answer), skipped (not asked). Only accept and reject can change what a Stage
does; grey and error keep the Stage's existing rule.
_Avoid_: result, outcome.

**Floor**:
The two probability limits of a Jev Question — accept floor (≥ means confident yes) and reject
ceiling (≤ means confident no). Set from a Calibration Record, placed on the side of the costlier
mistake.
_Avoid_: threshold, cut-off.

**Mode**:
Whether a Jev Question is asked and used: off (not asked), shadow (asked and kept, never acted on),
enforce (acted on when the Verdict is in accept or reject).
_Avoid_: status, enabled.

**Calibration Record**:
The written evidence for a Jev Question's Floors: a labelled sample of real subjects, reviewed by
a person, with the precision at each Floor. A Jev Question cannot be set to enforce without one.
_Avoid_: tuning, eval.

**Act (on a Verdict)**:
What an enforced, confident Verdict may change: exclude something from a purchase (DataForSEO) or
from a count (Score, questions landed), or block or send content back for repair before it is
published. It never deletes data and never unpublishes published content.
_Avoid_: block (alone), delete, filter.
