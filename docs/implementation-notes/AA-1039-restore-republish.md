# AA-1039 — a restore republishes the WordPress pages the retirement drafted (S274)

Found in the S274 AA-1060 rehearsal on the WordPress test site: after a `retired` event the `retired_page_draft` job
moved the page to draft (flag resolved `drafted`, `publish_log` unpublished); the later `restored` event resolved only
the still-open flags and mailed "back on sale", so the drafted page stayed in draft and had to be republished by hand.

## Decisions

- **No migration.** Everything fits the CIS 360 columns and CHECKs: the flag keeps `state = 'resolved'`,
  `resolution = 'drafted'` (the resolution CHECK has no "republished" value, and the flag did draft the page) and
  records the restore in its jsonb `detail` under `restore_republish`
  (`{event_key, state: queued|republished|skipped|kept, queued_at, done_at, reason, republished_at, wordpress}`).
  `publish_log` goes back to `status = 'published'`, `unpublished_at` / `unpublished_by` NULL (the reverse of
  `MARK_UNPUBLISHED_SQL`); `published_at` keeps the first publish date (WordPress keeps the post date too). The
  notice keeps its page list in `summary` jsonb (`pages_returning` / `pages_back` / `pages_kept`).
- **Which drafted pages a restore puts back:** the `tour_retired` flags of the key's tours that are resolved
  `drafted`, not yet marked `restore_republish`, and belong to a `retired` event of the same key between the previous
  `restored` event of that key and this one ("the retirement this restore undoes"; all its retired events, since a
  reason change writes a second one). A page drafted by an older retire / restore cycle is never touched, also when
  that older restore ran before this code. The mark is written in the fan-out transaction under the key's advisory
  lock, so a replay finds nothing new to mark.
- **The job is enqueued in the same transaction** (`INSERT INTO shared.job ... SELECT` from the marked flags, key
  `restored_page_republish:<flag_id>`, `ON CONFLICT DO NOTHING`), like the sweep's `DUE_DRAFTS_SQL`: no enqueue can be
  lost after the commit, and a replay creates no second job. Due now; 4 attempts like `retired_page_draft`.
- **Republish only when all of these hold** (re-read when the job runs):
  - the restore mark is there and has no outcome yet; the flag is still resolved `drafted`;
  - the tour key of the flag is `served` (strict: a missing state is not taken as served here, unlike the draft
    path, because this one makes a page public);
  - the piece still exists, is `approved` / `held` (the publish route's status rule, AA-770), its request's tour is
    served (the AA-986 L3 `tour_discontinued` rule, `shared.tour_liveness.ensure_served`), and it has no open
    `subject_gate` flag (the gate holds it);
  - our `publish_log` row is still the one this system unpublished (`status = 'unpublished'`,
    `unpublished_by = 'system:tour_retirement'`), and no other live / in-flight `publish_log` row of the same piece and
    channel exists (the tenant published it again from the Content studio: republishing the old post would put two
    copies online);
  - WordPress is still connected, the post is on the connected site (`other_account`, same as the draft path);
  - WordPress says the post still exists and is `draft`. Deleted / trashed (`removed_on_site`), already published
    (`already_published`), scheduled (`scheduled_on_site`) or any other status (`changed_on_site`) → nothing is
    changed and the reason is recorded.
- **Remote calls:** two new `WordPressAdapter` methods next to `set_draft`: `post_status(id)` (GET
  `/wp/v2/posts/{id}?context=edit`, `missing` on WordPress's own 404 / 410 error codes) and `set_published(id)` (POST
  `{"status": "publish"}`, must answer a real post object now `publish`). Same AA-460 rule as `set_draft`: a non-JSON
  2xx or a host error page raises, never counts as done.
- **The mail tells the truth about the page.** The restored notice of a tenant with pages to put back is `immediate`
  but is **not** mailed by the fan-out (`UNMAILED_IMMEDIATE_SQL` skips a notice whose `summary.pages_returning` is not
  empty). Each republish job moves its page from `pages_returning` to `pages_back` (put back) or `pages_kept` (we
  could not: not connected, other site, content held, studio republish, the last attempt failed); a page the tenant
  removed, published or scheduled themselves, or whose tour / piece is no longer servable, is just dropped from the
  list. When the list is empty the job enqueues the notice's mail (`tour_notice_email:<notice_id>`, idempotent). The
  digest backstop (an immediate notice unmailed for an hour) still covers a job that never finishes; the mail then
  says the page "is being put back online".
- **Facebook:** unchanged (notice only).

## Changed

- `services/tour_lifecycle/fanout.py`: `_restored` marks + enqueues; `republish_page`, `give_up_republish`;
  `UNMAILED_IMMEDIATE_SQL` waits for the pages.
- `services/jobs/tour_retirement_jobs.py`: kind `restored_page_republish` (`REPUBLISH_KIND`), handler `run_republish`
  (audit row `CONTENT_PUBLISHED` with `reason = tour_restored` on success; mail enqueue when the notice is ready).
- `services/acp_s4_blog/cms/wordpress.py`: `post_status`, `set_published`.
- `services/tour_lifecycle/notice_mail.py`: the restored section names the pages put back / still in draft.
- `shared/llm_client/capacity.py`: `restored_page_republish: None` (no LLM call).
- `CONTEXT.md`: the restore paragraph.

## Tradeoffs

- A restore of a page the tenant had rewritten in WordPress while it was in draft republishes their edited text: we
  only check the status, not the content. A draft is the state we put it in, so publishing it again restores what
  they had; the notice names the page so they can take it down.
- A superseded piece (a newer version exists in the studio) is still put back: the page was live with that text
  before the retirement and the restore returns the status quo. A newer version published from the studio is caught
  by the "other live publish_log row" check.
- The mail can wait for the republish jobs (seconds, or until the last retry). A failing WordPress site delays the
  mail until the job gives up or the 1 h digest backstop takes it.

## Should know

- A permanent refusal (not connected, other site) ends the job `succeeded` with `status = kept` (the page stays in
  draft, the tenant is told). Transient WordPress errors retry; on the last attempt the page goes to `pages_kept` and
  the job fails NonRetryable, so it shows in the job runner.
- Tests: `tests/unit/test_aa1039_restore_republish.py` (decision logic, job, WordPress calls, mail wording, with
  fakes) and three integration tests in `tests/integration/test_aa1039_gate_and_fanout.py` (retire → draft → restore
  → republish end to end with one job and an idempotent replay; a page the tenant published themselves is left
  alone; a page drafted by an older retire / restore cycle is not resurrected). Both ran locally (integration on a
  throwaway PostgreSQL 16).
- The WordPress adapter has no live test against a real site here; the first live check is the WordPress test site
  rehearsal (retire → wait for draft → restore → the page is public again, `publish_log` back to `published`).
