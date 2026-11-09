# AA-1039 — a restore republishes the WordPress pages the retirement drafted (S274)

Found in the S274 AA-1060 rehearsal on the WordPress test site: after a `retired` event the `retired_page_draft` job
moved the page to draft (flag resolved `drafted`, `publish_log` unpublished); the later `restored` event resolved only
the still-open flags and mailed "back on sale", so the drafted page stayed in draft and had to be republished by hand.
Second commit: fixes from an independent review (B1, M1–M4, m1–m4 below).

## Decisions

- **No migration.** Everything fits the CIS 360 columns and CHECKs: the flag keeps `state = 'resolved'`,
  `resolution = 'drafted'` (the resolution CHECK has no "republished" value) and records the restore in its jsonb
  `detail` under `restore_republish` (`{event_key, state: queued|republished|skipped|kept, queued_at, done_at, reason,
  claimed_at, error, republished_at, wordpress}`). `publish_log` goes back to `status = 'published'`,
  `unpublished_at` / `unpublished_by` NULL (the reverse of `MARK_UNPUBLISHED_SQL`); `published_at` keeps the first
  publish date. The notice keeps its page lists in `summary` jsonb (`pages_returning` / `pages_back` / `pages_kept`).
- **Only a page that was public before is ever published again (B1).** `move_to_draft` now reads the post first
  (`post_status`) and drafts only a post WordPress says is `publish`. It records `wordpress = 'drafted'`,
  `wordpress_before = 'publish'` and the `modified_gmt` WordPress returns from the draft call. A post that is private,
  pending, scheduled, already a draft, trashed or gone is not touched: the flag is resolved `unpublished` with
  `wordpress = 'not_public'` and `wordpress_before = <status>`, and `publish_log` is marked unpublished (the post is
  not live). `unpublished` (not `drafted`) because we did not draft it; a restore never looks at it.
- **Which drafted pages a restore looks at:** `tour_retired` flags of the key's tours resolved `drafted`, not yet
  marked, of a `retired` event of the same key between the key's previous `restored` event and this one. Each is
  marked once, in the fan-out transaction under the key's advisory lock:
  - `queued` when `wordpress = 'drafted'`, `wordpress_before = 'publish'` and `wordpress_modified_gmt` is recorded:
    one `restored_page_republish` job, inserted in the same transaction (key `restored_page_republish:<flag_id>`,
    `ON CONFLICT DO NOTHING`), so no enqueue is lost and a replay creates no second job;
  - `kept` (`drafted_before_check`) for a page drafted before these were recorded: we cannot tell it was public, so
    it stays in draft and the tenant's notice names it (`pages_kept`);
  - `skipped` (`not_drafted_by_us`) otherwise.
  A page drafted by an older retire / restore cycle is never touched.
- **Republish only when all of these hold**, re-read when the job runs and again under the key lock at the claim:
  - the mark is `queued`; the flag is still resolved `drafted`; our `publish_log` row is still the one this system
    took down (`unpublished`, `unpublished_by = 'system:tour_retirement'`);
  - the tour key is `served`;
  - the piece exists (a deleted piece: `skipped`, `piece_deleted`), is `approved` / `held` (else `kept`,
    `piece_not_publishable`), is the **latest** piece of its request (else `kept`, `newer_version`, the publish
    route's AA-802 rule), its request's tour is served (AA-986 `ensure_served`), and it has no open `subject_gate`
    flag (`kept`, `content_needs_change`);
  - no other `published` / `requested` WordPress `publish_log` row of **any piece of the same request** (one piece
    row per write attempt, M2): else `skipped`, `published_again_from_studio`;
  - WordPress is connected and the post is on the connected site (else `kept`);
  - WordPress says the post is `draft` **with the same `modified_gmt` recorded at the draft (M1)**: an edited draft is
    `kept` (`changed_on_site`); deleted / trashed / published / scheduled / other → `skipped` with the reason.
- **Claim first (M3).** Under the key's fan-out lock the job re-checks the above and sets `publish_log` back to
  `published` (so a retire fan-out that runs meanwhile flags the page), commits, then calls `set_published`. If
  WordPress fails, the job asks WordPress for the status: `publish` counts as done (a lost answer), else the claim is
  reverted (`publish_log` unpublished again) and the job retries. A unique-key clash at the claim (a studio publish of
  the same revision) means the claim is never made: `kept`, the post stays in draft, no untracked live post.
- **The draft job's race with a restore (M4).** `move_to_draft`'s final write takes the key's fan-out lock and checks
  that the flag was still open. If a restore resolved it while WordPress was moving the page, the page is published
  back at once (it was `publish` a moment before) and `detail.draft_revert` records it (`pending` → `done`); a
  failed revert stays `pending` and the draft job's retry does it.
- **Strict served state everywhere (m4).** `fanout.served(state)` is `state == 'served'`. A key without a liveness
  row (no Master ever published, or the raw tour gone) is not served: its restore fan-out does nothing, no page is put
  back, and a due open flag of it is drafted (before, `move_to_draft` and the restore fan-out treated a missing state
  as served). The fan-out now reads the state under the key lock.
- **Mail.** A tenant with pages to put back gets an `immediate` notice that the fan-out does not mail
  (`UNMAILED_IMMEDIATE_SQL` skips one whose `pages_returning` is not empty); the job whose page empties the list
  enqueues it. When the 1 h digest backstop takes a notice still waiting, the waiting pages move to `pages_kept`
  first, and the mail never names a waiting page, so it never says a page "is being put back". A "back on sale"
  notice whose key is not served at send time (m2) is closed without a mail: `mailed_at` set, `message_id` NULL,
  `skipped_reason = 'not sent: the tour is off sale again'` (nothing reads `mailed_at` as "sent" outside the mail job).
- **Job failures (m1).** Any exception on the last attempt (or a permanent refusal) gives the page up: a claim left
  behind is reverted, the page goes to `pages_kept`, the notice's mail is enqueued, the job ends NonRetryable.
- **Remote calls.** `WordPressAdapter.post_status(id)` returns `PostState(status, modified_gmt)` (GET with
  `context=edit`; `missing` on WordPress's own 404 / 410 codes). `draft_post(id)` returns `PostChange(outcome,
  modified_gmt)`; `set_draft` keeps returning the outcome string. `set_published(id)` must get back a post object now
  `publish`. Same AA-460 rule: a non-JSON 2xx or a host error page raises.
- **Facebook:** unchanged (notice only).

## Changed

- `services/tour_lifecycle/fanout.py`: `served()`; the fan-out reads the state under the lock; `_restored` marks
  (queued / kept / skipped) and enqueues; `move_to_draft` reads the post first, records before / modified, locks the
  final write and reverts a lost race; `republish_page` (claim first), `give_up_republish`;
  `UNMAILED_IMMEDIATE_SQL` waits for the pages.
- `services/jobs/tour_retirement_jobs.py`: kind `restored_page_republish` (`run_republish`: audit row
  `CONTENT_PUBLISHED`, reason `tour_restored`; any last-attempt failure gives up); the mail job closes stale
  "back on sale" notices and moves waiting pages to kept.
- `services/acp_s4_blog/cms/wordpress.py`: `post_status`, `draft_post`, `set_published`, `PostState`, `PostChange`.
- `services/tour_lifecycle/notice_mail.py`: the restored section names the pages back online / still in draft.
- `shared/llm_client/capacity.py`: `restored_page_republish: None` (no LLM call).
- `CONTEXT.md`: the restore paragraph.

## Tradeoffs

- A page drafted before this change (no recorded public status) is never published again automatically, even if it
  was public: the tenant is told it is still in draft and publishes it in WordPress. Safer than guessing.
- An edited draft (`modified_gmt` changed) is kept even when the edit was trivial: the tenant decides.
- A superseded piece is now kept (`newer_version`), unlike the first commit: the publish route only publishes a
  request's latest piece, and the restore follows the same rule.
- The claim marks `publish_log` live a moment before WordPress publishes; a retire fan-out in that window flags the
  page, which then follows the normal notice period. Reverted on a WordPress refusal.
- The mail waits for the republish jobs (seconds, or until the last retry); the digest backstop sends it after 1 h
  with the waiting pages listed as still in draft, even if a late retry later succeeds.
- A revert after a lost race (M4) that keeps failing past the draft job's attempts stays `pending` on the flag (shown
  in admin data); nothing re-queues it automatically.

## Should know

- A permanent refusal at republish time (not connected, other site) ends the job `succeeded` with `status = kept`.
- Tests: `tests/unit/test_aa1039_restore_republish.py` (69 tests: B1 private / pending / draft / scheduled / trashed /
  gone and old flags, M1, M2, M3 claim order / revert / lost answer / unique clash / retire at the claim, M4 race and
  retried revert, m1–m4, the WordPress calls, the mail) and eight integration tests in
  `tests/integration/test_aa1039_gate_and_fanout.py` (end to end with one job and an idempotent replay; the tenant
  published it themselves; older cycle not resurrected; private at draft time; old flag kept and mailed at once;
  edited draft kept; newer version / other live post of the request; claim reverted then retried; restore wins the
  race with the draft job). Integration ran locally on a throwaway PostgreSQL 16.
- The WordPress adapter has no live test against a real site here; the first live check is the WordPress test site
  rehearsal (retire → draft → restore → the page is public again, `publish_log` back to `published`), plus one with
  the post set to private before the notice period ends (it must stay private).
