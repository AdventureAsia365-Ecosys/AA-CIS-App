# AA-1039 — R6 slice 2: AA-subject gate (d21) and tour retirement fan-out (d20) (lane l11-r6-gate-fanout, S266)

Plan: root ADR 0019 d20 / d21, clarifications TL-20 / TL-21, README §6.5, ADR 0015 (tour liveness). Slice 1 (admin
IA) is `AA-1039-r6-admin-ia.md`.

## Decisions

- **The event already exists.** "`catalog.tour_liveness_changed`" of d20 is the ADR 0015 change record
  `gold_aa_internal.tour_lifecycle_event` (CIS 272, append-only, written only by `services/tour_lifecycle`) and its
  contract view `catalog.v_tour_lifecycle_outbox` (CIS 274). No new event table; the CIS-internal consumers follow
  the AA-987 pattern: one job per event (`tour_retirement_fanout:<event key>`), enqueued after commit and by a 2 h
  sweep in the api ops sampler.
- **Flags in a new table, not in `content_piece.status`.** Since AA-770 a `held` piece still publishes (it is an
  admin quality label), so the gate cannot reuse `held`. `acp_shared.content_compliance_flag` (CIS 362) holds both
  kinds: `subject_gate` (one open row per piece, updated in place) and `tour_retired` (one row per event and post).
- **Gate stages.** "Created" = when T9 finishes the piece (`_finalize_piece`, approved / held), because the text and
  its links exist only then; the placeholder insert has no text. The publish stage runs again right before the
  channel call, so a tenant edit is always checked. Both on the CIS main pool: the flag table and the Segment tables
  have no tenant-pool grant.
- **What counts as an AA subject:** the request's tour (its trip, else its atom's tour) is `served`, or its atom is in
  `acp_contract.v_active_tour_atoms`, or its Slate Segment has a live member. Brand content (no sales link, goal not
  promotion / lead_generation / conversion) passes without one.
- **What a sales link is:** the request CTA, or a link whose anchor text or URL path has a booking word (book, tour,
  trip, price, deal, package, enquire, …). Other links (a museum, a map, a social profile) are not limited by d21.
- **Allowed targets:** an Adventure Asia host (`adventure.asia`, env `AA_SALES_LINK_HOSTS`), a trip page
  `/trip/<served tour id>` on a trip page host (admin host, CRM client host, `TRIP_PAGE_BASE_URL`, env
  `TRIP_PAGE_HOSTS`), the tenant's own page of a served AA tour (`acp_deliver.tenant_tour_pages`, exact host + path),
  or the TripPlanner site (`aa-tripplanner.vercel.app`, the host of `TRIPPLANNER_REVALIDATE_URL`, env
  `TRIPPLANNER_SITE_HOSTS`). The per-tenant white-label TripPlanner host of R5 plugs into `tripplanner_hosts()`.
- **Release.** Staff (admin, `QUALITY_WRITE`) can release an open gate flag with a reason; the release stores the
  sha256 of the current text and the gate lets exactly that text through. Any edit is checked again.
- **WordPress draft is scheduled by a sweep, not a job queued 7 days ahead.** The deploy gate and the migration idle
  check count queued rows; a flag carries `draft_due_at` and the sampler enqueues `retired_page_draft` due-now once it
  passes. The job re-checks that the tour is still not served and the post is still live, refuses another site
  (`other_account`), calls `POST /wp/v2/posts/{id} {"status":"draft"}` (new `WordPressAdapter.set_draft`, validated
  like the other calls) and marks the `publish_log` row `unpublished` by `system:tour_retirement` with a tenant
  audit row.
- **Notice period policy data:** `acp_shared.tour_retirement_policy` (platform row `tenant_id IS NULL`, tenant rows
  override, 1–90 days); no row = 7 days (b7). Created empty: no seed, so 362 stays expand.
- **Pending publishes.** There is no scheduled publish in T11: a publish of a retired tour's piece is already refused
  (409 `tour_discontinued`, L3). The fan-out closes `requested` claims older than 15 min (abandoned) as `failed`
  ("cancelled: …") and reports the count in the mail.
- **E-mail.** Immediate when the tenant still has live posts of the tour, else the daily digest (08:00 UTC, one mail
  per tenant, notices of the last 14 days). `restored` → "back on sale", immediate when flags were cleared. `paused`
  and `superseded` → nothing. Recipients: active `owner` and `editor` members (a `viewer` is not a content member).
- **Addresses from the partner pool.** CIS stores no tenant e-mail (AA-978: identity only). The mail job reads the
  verified e-mail by `ListUsers` with `sub = "<sub>"` on the partner pool of `COGNITO_PARTNER_ISSUER`. This needs a
  read-only IAM grant for the CIS task role (AA-CIS-Infra PR of this lane). Until it is applied the notices wait
  (`skipped_reason`, delivery turns into digest) and the next digest retries.
- **Unit tests stub the gate hooks.** Publish and T9 unit tests script their pool calls in order; `tests/unit/
  conftest.py` stubs `_subject_gate`, `_pending_needs_change` and `_subject_gate_after_write` unless a test has the
  marker `real_subject_gate` (same pattern as `real_atom_seed`).

## Changed

- New: CIS 362 (expand), `services/acp_shared/subject_gate.py`, `services/tour_lifecycle/fanout.py`,
  `services/tour_lifecycle/notice_mail.py`, `services/jobs/tour_retirement_jobs.py` (3 kinds, none calls an LLM),
  `api/routers/admin_tenant_compliance.py`, `frontend/app/admin/tenant-qa/compliance/`.
- Hooks: `_finalize_piece` (write stage), `POST /v1/publish-log/{id}/publish` (publish stage, 409),
  `GET /v1/publish-log/pending` (`needs_change`), `run_after_commit` and `process_export` (fan-out enqueue), the ops
  sampler (`_tour_retirement_part`: sweep, due drafts, digest).
- Admin nav: Tenant content QA › Compliance (lib/adminIa.ts, sub-nav, EN/VI labels); BFF write rule
  `tenant-qa/compliance/` admin only; staff POLICY entries + route snapshot; `docs/api/openapi.json` regenerated with
  the pinned fastapi 0.161.1 / pydantic 2.14.1.
- Erasure inventory and the X1 conftest RLS lists know the three new tables.

## Tradeoffs

- **Publish gate fails open on a read error** (logged `subject_gate_publish_error`). It is a contract rule, not an
  auth boundary; a DB hiccup should not block a tenant. The write stage records the verdict first, so a failure at
  publish is rare and visible in logs.
- **Keyword detection of sales links** can miss a sales link without a booking word, or count a "tour of the old
  town" museum link. Kept simple and $0; an LLM classifier is out of scope (TL-21: only if the rule lets too much
  through, separate cost yes).
- **The CRM agent task is not built.** The CRM already marks lines "no longer offered" from its hourly catalog sync
  (`app/catalog/sync.py`, 410 → `retired`), but there is no existing hook that creates an agent task; listed as a
  follow-up rather than adding a new CRM job in this lane.
- **Tenant-visible reason** reaches the tenant through the 409 detail and `/pending` `needs_change`; showing
  `needs_change` in the Content studio Publish list is an AA-Partner portal change (follow-up).

## Should know

- **Order on Dev:** the migration-only PR (362, expand) is auto-applied by Infra Migration Apply; the code PR can merge
  before or after (every new read catches `UndefinedTable`: the gate still answers, flag writes are skipped, the admin
  route answers 503). `REQUIRED_CIS_SCHEMA` is not raised (358-361 are other lanes).
- **The Infra IAM PR** (partner pool `ListUsers` for the CIS task role) must be applied before a mail can name its
  recipients; without it the mail job defers the notices (no failure, no lost notice).
- **Verified already, no code here:** TripPlanner hides a retired tour (purge notice of AA-987 + `catalog.v_tour`;
  tour page 410, `backend/assembly/availability.py`); Booking refuses new quotes and holds (`app/b2b/served.py`,
  409 `tour_not_served`); the CRM hides it and marks lines (hourly sync).
- **Consumers of the 409:** the portal Publish list (AA-Partner `portal/app/portal/_components/PublishPendingList.tsx`)
  already shows a 409 `detail` as its plain sentence (the L3 path); the header is
  `X-Error-Code: content_needs_change`.
- `services/tour_lifecycle/fanout.py` treats `publish_log.channel` `blog` and `wordpress` (old rows) as WordPress.
