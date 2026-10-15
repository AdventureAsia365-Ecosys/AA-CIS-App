# AA-819 follow-up — warn on CTAs that claim reviews or ratings

Nghiệp's decision (S237, 15/10/2026): a tenant CTA that claims reviews, ratings or testimonials is
saved and published as typed, the portal shows a warning, and AA admins get a bell notification so
the case can feed the Terms (AA-820) and a talk with the business owner. F19's CTA exemption stays.

## Decisions
- **One rule.** `authenticity.cta_review_claims(cta)` reuses F19's `_TESTIMONIAL` and `_SCORES`
  patterns (with the trek-grade guard) and adds a short CTA-only list: plural `reviews`,
  `reviewer(s)`, `reviewed by <guests/travellers>`, `rating(s)` (not in a trek-grade sentence),
  `rated` only as a claim (`rated us/excellent/#1/by`, `guests rated`; not "Rated trips for
  families" or "rated PG"), `NN% of our guests recommend`, `5-star / five stars` (not followed by
  hotel, resort, lodge, luxury, stay...), `what our guests say/think`, `loved/recommended/praised/
  trusted by ... travellers`, and TripAdvisor's `Travellers' Choice`. Overlapping hits collapse to the widest phrase. Only the testimonial family is used:
  first-person and independence claims were not part of the request.
- **Live check uses the backend rule.** `POST /v1/content-writing/cta-check` (tenant JWT, no DB, no
  notification) is called by the portal 450 ms after typing stops, through react-query
  (`useQuery` keyed on the text, 10 min `staleTime`, no retry), per the kit rule. This keeps a single source of
  truth instead of a second regex list in TypeScript (Python inline flags `(?i:...)` do not port).
- **Entry points.** Only two tenant paths save a CTA: `POST /admin/brand-identity` (tenant JWT,
  T0 `default_cta`) and `POST /v1/content-writing/requests/{id}/write` (`cta` body, used only when
  the request has none). `angle_gate_request.cta` is never set by a tenant (stays NULL since AA-522).
- **Write route checks the typed CTA only when it is the one used** (`context["cta"] == body.cta`).
  A brand default CTA is checked when it is saved in T0, not again on every write.
- **Notification.** Existing writer `NotificationService.emit` with a new
  `EventType.TENANT_CTA_REVIEW_CLAIM = "tenant.cta.review_claim"`, `target_roles ['admin']`,
  `entity_type 'tenant_cta'`, `entity_id` = tenant slug (so the Overview alerts row reads
  `tenant_cta · <slug>`), `tenant_id` = the tenant. Payload: message, tenant_name, tenant_slug,
  where, cta, phrases, cta_key.
- **Idempotent per tenant + CTA text** with no migration: `cta_key` = sha256 of the lower-cased,
  whitespace-collapsed CTA (24 hex chars) in the payload; the writer checks
  `payload->>'cta_key'` for the tenant before inserting, under `pg_advisory_xact_lock` so two
  concurrent saves cannot both insert. The check has no time window and ignores `is_read`: one row
  per tenant + text, ever.
- **Storm guard.** On top of that, at most `DAILY_CAP` (5) rows per tenant per rolling day: an API
  client that templates a review CTA per piece makes a new text on every write. Past the cap the
  case is logged (`cta_review_claim_notify_capped`) and the tenant still gets the warning. The
  payload keeps at most 500 characters of the CTA (200 in the message).
- **aa_internal** (AA's own brand) gets the warning but no notification, on both paths: the
  skip lives in `check_saved_cta` itself (`AA_INTERNAL_TENANT_ID`), so the write route is covered
  too, not only the brand endpoint's staff path.
- **Fail open.** `check_saved_cta` runs after the save committed; a rule error or a notification
  error is logged (`cta_review_claim_check_failed` / `cta_review_claim_notify_failed`) and the
  route returns as before.

## Changed
- `services/acp_content_writing/authenticity.py`: `cta_review_claims()` + `_CTA_REVIEW_EXTRA`.
  `flagged()` / F19 untouched (a test asserts the CTA line is still exempt).
- `services/acp_shared/cta_review_notice.py` (new): `cta_warning`, `cta_key`, `notify_admins`,
  `check_saved_cta`.
- `services/notifications.py`: new event type + default roles.
- `api/routers/admin_pipeline.py::update_brand_identity`: returns `cta_warning` when present.
- `api/routers/v1_content_writing.py`: `write` returns `cta_warning` when present; new `cta-check`.
- `api/routers/admin.py::list_notifications`: label "Tenant CTA mentions reviews or ratings".
- Portal: `CtaReviewNotice.tsx` (notice + `useCtaReviewCheck` + `ctaWarningFrom`), wired into
  `BrandTab.tsx` (Default Call to Action) and `AngleGateWizard.tsx` (inline CTA input + write card).
- `CONTEXT.md`: one paragraph under T0.

## Tradeoffs
- A response key that appears only when there is a warning (not `cta_warning: null` always): keeps
  the existing 202 body byte-identical for every normal write (AA-613 tenant-safe shape tests).
- `5-star` hotel-class exclusion is a lookahead list; "Stay in 5-star comfort" would still warn.
  A false positive costs one non-blocking line and one admin row, so recall was preferred.
- Singular `review` is not matched ("Review the full itinerary" is a normal CTA); "Leave us a
  review" is therefore not warned (it asks for reviews, it does not claim them).
- The live check is a network call per pause in typing; it is cheap (pure regex) and debounced.
- Existing CTAs saved before this change are not scanned; they are caught the next time the tenant
  saves Brand Identity. A one-off scan of `shared.tenant_brand_rules.default_cta` was out of scope
  (no Dev DB access in this task).

## Should know
- No migration. `shared.notifications.payload` is jsonb; the dedupe reads `payload->>'cta_key'`
  (table is tiny; `idx_notif_event_type` narrows by event type).
- The admin bell (`Topbar.tsx`) renders `title` / `message` from the backend, so no admin
  frontend change was needed. The tenant bell (`NotificationsBell.tsx`) is a view of billing
  activity and never reads `shared.notifications`, so the admin row cannot reach the tenant.
  Any future tenant reader of `shared.notifications` must filter on `target_roles`.
- `admin.py PUT /admin/tenants/{id}/brand-identity` (AA staff editing a tenant's brand) is not
  a tenant endpoint and was left unchanged.
- Tenant-facing copy: "Only mention reviews or ratings if they are real and you can show them
  publicly. AI-written copy never adds reviews for you." plus the matched phrases as chips. No
  mention of checks, gates, F19 or flags.

## Adversarial review (S237)
- Rule narrowed: bare `rated` and `reviewed` were false positives on ordinary CTAs ("Rated trips for
  families", "Get your itinerary reviewed by a local expert"); now claim-shaped only. Added
  `NN% of our guests recommend` and `Travellers' Choice` (false negatives).
- Notification storm: daily cap per tenant + CTA length cap in the payload (see Decisions).
- aa_internal skip moved into `check_saved_cta` so the T9 write route does not notify for AA's own
  brand either.
- Portal live check moved to react-query; phrase chips wrap (`overflowWrap: anywhere`,
  `minWidth: 0`) so a long phrase does not overflow at 390 px.
- Checked and unchanged: no other tenant path sets a CTA (`angle_gate_request.cta` is only copied
  by the admin regenerate path; `TenantConfigService.update_brand_rules` never touches
  `default_cta`; the slate pick inserts no CTA). Table/column names match
  `docs/architecture/db-schema-reference.md` (`shared.notifications`, `shared.tenants`).
  F19 `flagged()` is byte-identical to main.
- Known soft false positive: "Check availability and ratings for each hotel" warns on `ratings`.
  Accepted (non-blocking, recall preferred).
