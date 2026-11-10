# AA-1039 follow-up — tour retirement: notice only by default, one-click "Move to draft", opt-in automatic drafts (S276)

Tech lead decision S276 (10/11/2026), root ADR 0019 d20 amended: when Adventure Asia retires a tour, the system no
longer changes an agency's WordPress site by default. Before this change every live WordPress page about the retired
key got a `tour_retired` flag with `draft_after_notice` and was moved to draft by the `retired_page_draft` job after
the notice period (default 7 days).

## Decisions

- **Default = `notice_only` for WordPress too.** The fan-out writes `draft_after_notice` (with `draft_due_at`) only
  when the agency opted in; otherwise `notice_only`, no `draft_due_at`, tenant message "This page stays online: you can
  rewrite it …, move it to draft with one click …, or keep it."
- **Opt-in column `acp_shared.tour_retirement_policy.auto_draft`** (CIS 397, boolean NOT NULL DEFAULT false), with a
  CHECK that the platform row (`tenant_id IS NULL`) can never be opted in: a platform switch would turn automatic
  drafts on for every agency, which is exactly what the decision forbids. The fan-out reads `auto_draft` only from the
  agency's own row (`tenant_id IS NOT NULL AND auto_draft`).
- **Migration class: contract** (ALTER TABLE ADD COLUMN + ADD CONSTRAINT on an existing table). MIG-4: migration-only
  PR, applied by Infra Migration Apply, then this code PR raises readiness (`REQUIRED_CIS_ABOVE_GAP` gets 397).
- **Number 397** as assigned by the orchestrator (395 and 396 held by other S276 lanes). If they do not ship first,
  renumber per the header of the migration file.
- **Settings route `GET` / `PATCH /v1/account/tour-retirement`.** Under `/v1/account`, so the existing tenant role
  middleware makes the PATCH owner-only (editor, viewer, "view as tenant" get 403). Body `{auto_draft, notice_days?}`,
  days 1–30 (pydantic 422 outside), `extra="forbid"`. Upsert on the existing unique expression index
  (`COALESCE(tenant_id, zero uuid)`). Audit row `tenant.tour_retirement_settings_changed` (before/after) only when
  something changed. 503 before CIS 397 (UndefinedColumn).
- **One-click route `POST /v1/publish-log/{publish_id}/move-to-draft`** reuses `fanout.move_to_draft` with the tenant
  as actor (`membership:<mid>` or `tenant:<id>`): same re-checks as the job (tour still not served / Segment Subject
  still dead, post still live, WordPress connected, same site), same WordPress call (`set_draft` path), same record
  (`publish_log` unpublished by `system:tour_retirement`, flag resolved `drafted`). Allowed on any open
  `tour_retired` WordPress flag of that tenant (`notice_only` or `draft_after_notice`, before its due date too).
  404 for another tenant's page or a page with no open flag (no existence leak); 409 for a Facebook post, a tour back
  on sale or a page no longer live; 422 WordPress not connected / other site; 502 on a WordPress error (page still
  online).
- **Idempotency.** The flag is resolved first in the final transaction (`state = 'open'` in the UPDATE, RETURNING):
  of two concurrent runs only one marks the row unpublished and audits; the other answers `already_drafted`. A
  repeat click after the flag is resolved finds no open flag and answers from `DRAFTED_PAGE_SQL` (`replayed: true`,
  no WordPress call).
- **List route `GET /v1/publish-log/retired-pages`** — every live post with an open `tour_retired` flag (Facebook too,
  `can_move_to_draft: false`), `draft_due_at` only for `draft_after_notice` flags.
- **Bell row `tour.pages_to_review`** (new tenant-facing event type), written once with the retirement notice (a
  fan-out replay inserts no notice, so no second row), payload `{tour_name, event_key, pages: [{publish_id, url,
  draft_on}], count}`. `/v1/notifications` returns the pages so the bell can offer "Move to draft" per page.
- **The draft job re-reads the opt-in.** When the agency turned automatic drafts off after the flag was written
  (`updated_at > flag.created_at` and `NOT auto_draft`), the job turns the flag `notice_only` (no `draft_due_at`,
  notice-only message) and leaves the page online. A flag of an agency with no row, or whose row is older than the
  flag, keeps its date: this is what leaves the open pre-S276 `draft_after_notice` flags on Dev as they are.

## Changed (vs the original R6 behaviour)

- `services/tour_lifecycle/fanout.py`: `policy()` (days + opt-in; falls back to the notice-days read before 397 and to
  defaults before 360), notice-only branch, `page_notice_message`, `tour.pages_to_review` emit, `move_to_draft` gains
  `actor` / `tenant_id`, the opt-out re-read, resolve-first ordering.
- `services/tour_lifecycle/notice_mail.py`: the e-mail lists pages that stay online with the three choices and says
  Adventure Asia does not change the site; a dated line only for opted-in pages ("as set in your Content studio").
- `api/routers/v1_notifications.py`: title/message for `tour.pages_to_review`; the "tour no longer served" message no
  longer says "remove or edit".
- `api/core/readiness.py`: 397 in `REQUIRED_CIS_ABOVE_GAP`.
- Tests that relied on the old automatic draft now insert an opted-in policy row (gate/fan-out, put-back,
  segment liveness).

## Tradeoffs

- **E-mail stays English only.** No language preference is stored per member (the Content studio itself switches
  EN/VI client-side); the bell row carries data (`tour_name`, pages) so the agency app can render its own EN/VI copy.
- **Existing open `draft_after_notice` flags are not converted** (tech lead decision S276): converting would need a
  data migration and a guess about consent. The orchestrator reports their count after deploy.
- **Opt-out applies to already scheduled drafts** (the job re-reads it), but opt-in does not back-date: flags written
  as `notice_only` stay so. Turning it on is about the next retirements; the agency can click "Move to draft" for the
  pages already listed.

## Should know

- The click and the job both mark `publish_log.unpublished_by = 'system:tour_retirement'` on purpose: the restore
  fan-out and Put back online select drafted pages by that value. Who asked is on the flag (`resolved_by`,
  `detail.drafted_by` = `tenant` | `auto_draft`) and in the audit row.
- Before CIS 397 the code treats everyone as not opted in (UndefinedColumn caught), and the settings routes answer
  503; the readiness gate keeps the rollout behind the migration anyway.
