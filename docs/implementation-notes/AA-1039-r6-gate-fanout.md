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
  admin quality label), so the gate cannot reuse `held`. `acp_shared.content_compliance_flag` (CIS 360) holds both
  kinds: `subject_gate` (one open row per piece, updated in place) and `tour_retired` (one row per event and post).
- **Gate stages.** "Created" = when T9 finishes the piece (`_finalize_piece`, approved / held), because the text and
  its links exist only then; the placeholder insert has no text. The publish stage runs again right before the
  channel call, so a tenant edit is always checked. Both on the CIS main pool: the flag table and the Segment tables
  have no tenant-pool grant.
- **What counts as an AA subject:** the request's tour (its trip, else its atom's tour) is `served`, or its atom is in
  `acp_contract.v_active_tour_atoms`, or its Slate Segment has a live member. Brand content (no sales link, goal not
  promotion / lead_generation / conversion) passes without one.
- **What a sales link is** (fix pass 2): the request CTA; a link whose text or URL path has a booking word (book, buy,
  reserve, enquire, quote, price, deal, checkout, cart); or a product word (tour, trip, package, holiday, itinerary,
  offer, excursion, cruise) together with a commercial tour host (`DEFAULT_COMMERCIAL_HOSTS`, env
  `COMMERCIAL_TOUR_HOSTS`) or a product listing path (`/tours/<page>`). A product word alone is not one, so a museum's
  `/visit/tours` page or the tenant's `/blog/best-trips` article passes. The "text" of a bare URL is the words just
  before it on its line (up to 80 characters, from the last sentence end): "Book your cruise here: https://…".
- **Allowed targets:** an Adventure Asia host (`adventure.asia`, env `AA_SALES_LINK_HOSTS`), a trip page
  `/trip/<served tour id>` on a trip page host (admin host, CRM client host, `TRIP_PAGE_BASE_URL`, env
  `TRIP_PAGE_HOSTS`), the tenant's own page of a served AA tour (`acp_deliver.tenant_tour_pages`, exact host + path),
  or the TripPlanner site (the Dev custom domain `tripplanner.lumiguides.it.com` and `aa-tripplanner.vercel.app`, the
  host of `TRIPPLANNER_REVALIDATE_URL`, env `TRIPPLANNER_SITE_HOSTS`). The per-tenant white-label TripPlanner host of R5 plugs into `tripplanner_hosts()`.
- **Release.** Staff (admin, `QUALITY_WRITE`) can release an open gate flag with a reason and the flag's
  `content_sha256` (the hash of the text the gate read, whose links the page shows). The route locks the flag and the
  piece and answers 409 `content_changed_since_check` unless that hash, the flag's and the current text's are all
  equal: a tenant edit between the check and the click is never released unreviewed (fix pass 2). The gate then lets
  exactly that text through; any edit is checked again. The gate does not re-run on an edit (the next publish does).
- **WordPress draft is scheduled by a sweep, not a job queued 7 days ahead.** The deploy gate and the migration idle
  check count queued rows; a flag carries `draft_due_at` and the sampler enqueues `retired_page_draft` due-now once it
  passes. The job re-checks that the tour is still not served and the post is still live, refuses another site
  (`other_account`), calls `POST /wp/v2/posts/{id} {"status":"draft"}` (new `WordPressAdapter.set_draft`, validated
  like the other calls) and marks the `publish_log` row `unpublished` by `system:tour_retirement` with a tenant
  audit row.
- **Notice period policy data:** `acp_shared.tour_retirement_policy` (platform row `tenant_id IS NULL`, tenant rows
  override, 1–90 days); no row = 7 days (b7). Created empty: no seed, so 360 stays expand.
- **Pending publishes.** There is no scheduled publish in T11: a publish of a retired tour's piece is already refused
  (409 `tour_discontinued`, L3). The fan-out closes `requested` claims older than 15 min (abandoned) as `failed`
  ("cancelled: …") and reports the count in the mail.
- **No lost immediate mail** (review fix): `fan_out` returns every `immediate` notice of the event with `mailed_at
  IS NULL`, not only the ones this run inserted, so a fan-out job retried after its commit (worker stop, deploy,
  enqueue error) enqueues the mail again (key `tour_notice_email:<notice_id>`, so a replay while the job exists adds
  nothing). The digest also takes an `immediate` notice still unmailed an hour after it was written (its mail job
  died after `MAIL_ATTEMPTS`, or the fan-out could not rerun); `tour_notice_email` has concurrency 1 and an immediate
  job re-reads `mailed_at` before sending, so the two never mail the same notice twice.
- **E-mail.** Immediate when the tenant still has live posts of the tour, else the daily digest (08:00 UTC, one mail
  per tenant, notices of the last 14 days). `restored` → "back on sale", immediate when flags were cleared, and only
  to tenants whose latest notice for the key's tours (in lifecycle event order) is `retired` (fix pass 2: a restore
  after a pause, a second restore or a re-import + republish mails nobody; d20 "a paused tour gets no mail"). `paused`
  and `superseded` → nothing.
- **One retirement per key** (fix pass 2): a `retired` → `retired` change of reason (Master trashed, then the source
  deleted) writes a second lifecycle event; its fan-out skips posts that already have an open `tour_retired` flag of
  another event (the first draft date stands) and tenants whose latest notice is `retired`, so no second flag, notice
  or mail. Fan-outs of one key are serialised with a transaction advisory lock on the key.
- **Mail claim** (fix pass 2): a mail job claims its notices (`mail_job_id`, unmailed, not held by another queued /
  running mail job) before it sends and sends only those; a deferral clears the claim. The 08:00 digest therefore
  skips an immediate notice whose mail job is in retry backoff. Left: SES accepting a mail and the `mailed_at` write
  then failing still lets that job's retry send again (no SES idempotency key). Recipients: active `owner` and `editor` members (a `viewer` is not a content member).
- **Addresses from the partner pool.** CIS stores no tenant e-mail (AA-978: identity only). The mail job reads the
  verified e-mail by `ListUsers` with `sub = "<sub>"` on the partner pool of `COGNITO_PARTNER_ISSUER`. This needs a
  read-only IAM grant for the CIS task role (AA-CIS-Infra PR of this lane). Until it is applied the notices wait
  (`skipped_reason`, delivery turns into digest) and the next digest retries.
- **Unit tests stub the gate hooks.** Publish and T9 unit tests script their pool calls in order; `tests/unit/
  conftest.py` stubs `_subject_gate`, `_pending_needs_change` and `_subject_gate_after_write` unless a test has the
  marker `real_subject_gate` (same pattern as `real_atom_seed`).

## Changed

- New: CIS 360 (expand), `services/acp_shared/subject_gate.py`, `services/tour_lifecycle/fanout.py`,
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

- **Publish gate fails closed** (review fix): any error while the gate checks a piece answers 503
  `content_check_unavailable` ("We couldn't check this piece just now. Please try publishing it again in a minute.",
  `Retry-After: 60`), never a pass. The first version failed open, and a tenant's own text could trigger it: a link
  `/trip/` + 36 dashes passed the old `[0-9a-f-]{36}` match, `$1::uuid[]` raised, the publish went ahead with no
  flag. Now a trip page id must parse as a uuid (else the link is not a trip page and, as a sales link, is refused),
  and a DB hiccup costs the tenant one retry. Before CIS 360 the gate still answers (flag reads / writes skip
  `UndefinedTable` inside `record` / `released`), so the order of the two PRs does not matter. The write stage stays
  best-effort (logged): the publish stage checks again.
- **Link extraction from the published HTML** (fix pass 2, replaces the regex reader): the piece is rendered with the
  publisher's own markdown call (`extra` + `nl2br`, as `export.render_content_text_to_fragment`) and every `<a href>`
  is read with `html.parser` (entities decoded, the first `href` wins, an unclosed `<a>` still counts). The href is
  resolved like a browser (`resolve_href`): tabs / newlines dropped, `\` as `/`, scheme case-blind, `//host` and
  `https:host` get `https://`, scheme-less `www.host` / `host.tld/path` read as that host; relative, `#`, `mailto:`,
  `tel:`, `javascript:` hrefs cannot leave the tenant's page and are not links. Titles, angle brackets and reference
  links therefore come out as plain anchors. Bare and scheme-less URLs are read from the HTML text and from the raw
  piece too (Facebook posts the raw text and links bare URLs, also inside an HTML comment). Unit cases pin each
  bypass form of the review. A URL nested in another URL's query is not read separately.
- **Keyword detection of sales links** is narrower since fix pass 2: a competitor link with a product word only in
  its slug (`/hanoi-food-tour`, text "Hanoi food tour", no booking word, not a listing path, not a commercial host)
  now passes, in exchange for not refusing informational links (museums, the tenant's own articles). Kept simple and
  $0; an LLM classifier is out of scope (TL-21: only if the rule lets too much through, separate cost yes).
- **Publish-stage gate on a new publish only** (fix pass 2): `_subject_gate` gets the revision key and returns at once
  when a `publish_log` row of that key is `published` or `requested` (claim() answers the replay / in progress), so a
  piece that went live before the deploy is not held on a repeat call and gets no Compliance hold.
- **The CRM agent task is not built.** The CRM already marks lines "no longer offered" from its hourly catalog sync
  (`app/catalog/sync.py`, 410 → `retired`), but there is no existing hook that creates an agent task; listed as a
  follow-up rather than adding a new CRM job in this lane.
- **Tenant-visible reason** reaches the tenant through the 409 detail and `/pending` `needs_change`; showing
  `needs_change` in the Content studio Publish list is an AA-Partner portal change (follow-up).

## Should know

- **Order on Dev** (fix pass 2): the migration-only PR (360, expand) should go first, but the code PR may deploy
  before it without losing an event. Before 360, `fan_out` raises `SchemaMissing` (retryable, error
  `fanout_schema_missing: CIS 360 ...`): the job retries, then fails; once the three tables exist the sampler's tick
  re-queues every failed fan-out job with that error (`REQUEUE_SCHEMA_MISSING_SQL`, any age, a fresh set of
  attempts), and the fan-out re-checks the key's current state. The notice-period read runs in a savepoint, so a
  missing policy table never aborts the fan-out transaction. The gate still answers (flag writes skipped) and the
  admin route answers 503 naming the migration from `fanout.MIGRATION`. `REQUIRED_CIS_SCHEMA` is not raised (it
  would block the deploy until every lower number is applied; the retry path does not).
- **Renumber** (fix pass 2): the migration is 360 (main max 359 after AA-1040's 358 / 359). The number lives in the
  file name, its header and registration, `fanout.MIGRATION` (runtime strings), the code comments, the tests and the
  X1 lists, CONTEXT.md, these notes, the root DM-7.1 rows and the Infra notes.
- **The Infra IAM PR** (partner pool `ListUsers` for the CIS task role; the explicit Deny is NotAction ListUsers on
  the partner pool, so pool / app-client / IdP configuration writes are denied too) must be applied before (or with)
  the CIS rollout: without it the mail job defers the notices, and the digest reads only the last 14 days, so a
  notice deferred longer is never mailed.
- **Left as follow-ups** (review minors not fixed here): a restore does not undo a WordPress draft (a drafted page
  stays drafted, the restored notice does not list it, and republishing the same text makes a second post); the
  fan-out closes `requested` publish rows older than 15 min as failed without a remote lookup, so a post created
  behind a lost response is not flagged; `recipients()` does not check the Cognito `Enabled` / `UserStatus` and asks
  ListUsers for every attribute.
- **Verified already, no code here:** TripPlanner hides a retired tour (purge notice of AA-987 + `catalog.v_tour`;
  tour page 410, `backend/assembly/availability.py`); Booking refuses new quotes and holds (`app/b2b/served.py`,
  409 `tour_not_served`); the CRM hides it and marks lines (hourly sync).
- **Consumers of the 409:** the portal Publish list (AA-Partner `portal/app/portal/_components/PublishPendingList.tsx`)
  already shows a 409 `detail` as its plain sentence (the L3 path); the header is
  `X-Error-Code: content_needs_change`.
- `services/tour_lifecycle/fanout.py` treats `publish_log.channel` `blog` and `wordpress` (old rows) as WordPress.
