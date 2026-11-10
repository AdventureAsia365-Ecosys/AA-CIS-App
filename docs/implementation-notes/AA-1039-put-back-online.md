# AA-1039 follow-up — "Put back online" after a restore (option A, S275)

The S274 rehearsal on the WordPress test site found that a restore resolved only the open retirement flags: a page the
retirement had already moved to draft stayed in draft while the tenant was mailed "back on sale". An automatic
republish was built on a side branch and failed a third independent review (it writes to a site the tenant also edits,
and every lost network answer leaves the remote state unknown). The tech lead chose option A: no automatic republish;
the restore notice lists each drafted page and the tenant puts it back with one click.

## Decisions

- **One click, one WordPress publish call, on the page that exists.** `POST /v1/publish-log/{publish_id}/put-back`
  sets the drafted post back to `publish` (`WordPressAdapter.set_published`); it never creates a new post (the
  publish route's `create_post` would leave the draft behind and put a second copy online).
- **The publish route's checks, in its order**: ownership (404 for another tenant's page or one the retirement never
  drafted, no existence leak), the tour served (409 `tour_discontinued` / `tour_paused` via `ensure_served`), the
  latest piece of its request (409, AA-802 rule), no other live / in-flight post of the same request (409), the
  AA-subject gate (409 `content_needs_change`, 503 when it cannot answer; always run, the row is not a live revision),
  the WordPress connection (422), a tombstoned secret (422), another site (422).
- **WordPress is read before acting** (`post_status`, edit context): `draft` → published; `publish` → recorded as
  `already_online` (the tenant published it on WordPress; no call); anything else (`private`, `pending`, `future`,
  `trash`, gone) → `changed_on_wordpress`, left as is. A lost answer to the publish call is checked with a second read.
- **Idempotency on the page's own `publish_log` row** (the AA-726 state machine): `unpublished` by
  `system:tour_retirement` → `requested` (the claim; a concurrent click gets 409, a claim older than 120 s is taken
  over) → `published` with `unpublished_at` / `unpublished_by` cleared, or back to `unpublished` when left as is or on
  a WordPress error (`last_error` keeps the reason). The partial unique key index (215) refuses the claim when the same
  revision is live as another row (409). A repeat click answers from the row (`published`) or from the flag's note
  (`changed_on_wordpress`) with `replayed: true` and no WordPress call.
- **The outcome is noted on the retirement flag** (`detail.put_back = {outcome, wordpress_status, by, at}`) so the
  page leaves the list; no new column or migration (the flag's jsonb `detail` holds it, as the draft job's notes do).
- **Own tenant-pool flag** `aa1039:put_back_pool` (publish_log blocks under RLS; one flag per route, S241). The
  retirement flags (`content_compliance_flag`) have no tenant-pool grant: read and noted on MAIN with an explicit
  `tenant_id` condition in every statement, like the subject gate's reads.
- **Not counted against the daily publish limit** (AA-819): the page was already published once; this is not a new
  post. `published_at` keeps the first publish date.
- **Audit**: `content_piece.put_back_online` (tenant activity feed: "Put a page back online", detail "WordPress ·
  online again / already online / left as is").
- **The notice**: the restore fan-out writes `summary.pages_drafted` (`[{publish_id, url}]`, the tours' pages whose
  latest flag is `drafted`, still the retirement's draft, no click noted) on the "back on sale" notice of each tenant
  it already notifies (never a new recipient), makes that notice `immediate`, and, when the notice is new, one bell
  row `tour.pages_to_put_back` (target `tenant`) with the same pages. A fan-out replay inserts no notice, so no second
  bell row. The mail names each page and says how to put it back.
- **Only pages the click can put back are offered** (S275 review). The restore notice, the list and the click share
  one rule (`fanout.SUPERSEDED_SQL` / `fanout.OTHER_LIVE_SQL` over the page's `publish_log` row): a page whose piece
  is no longer its request's latest version, or whose request has another live / in-flight post, is left out of
  `summary.pages_drafted` and of GET `/put-back` (the click would always answer 409, and with no `put_back` note the
  page would sit on the list for good).
- **No second copy from Ready to publish** (S275 review). While a piece has a page to put back
  (`put_back.pages_of_pieces`, MAIN), GET `/pending` leaves it out when its tour is served (the page is listed under
  "Pages to put back online" on the same Publish page; while the tour is off sale the piece stays with its served
  badge) and `publish()` refuses it with 409 "put that page back online" before the claim; that read fails closed
  with 503 like the AA-subject gate (in `/pending` a read error hides nothing). A page put back, left as is, or no
  longer offered no longer blocks: e.g. after "changed_on_wordpress" (trashed on WordPress) the piece is back in
  Ready to publish and publishes as a new post.
- **GET `/v1/publish-log/put-back`** lists the tenant's pages still to put back, about a tour served now (the same
  Subject rule as the publish gate, `request_served_states`). Not on the view-as read list (a staff view-as session
  gets 403 there and the portal hides the section).

## Changed (vs. the side branch `fix/aa-1039-restore-republish`)

- Kept: the WordPress adapter calls `post_status` / `set_published` (+ `_set_status` shared with `set_draft`), and the
  idea of naming the pages in the restore mail.
- Dropped: the `restored_page_republish` job kind, the claim / resume / race machinery, `draft_post` and the
  `modified_gmt` bookkeeping, the deferred mail, the strict `served()` change and the M4 revert. `move_to_draft` is
  unchanged on main.

## Tradeoffs

- A page whose draft the tenant edited on WordPress is published as it is now when they click: the click is theirs.
- A page drafted in an older retire / restore cycle and never put back is listed again on a later restore of the key
  (only for tenants that restore notifies): the tenant still decides.
- The bell row carries the pages at fan-out time; a page put back since answers "already online" (replayed) if clicked
  again from an old bell entry. The Content studio list is read live.
- A piece the AA-subject gate holds still shows its page on the put-back list (the click answers 409 with what to
  change); an edit writes a newer piece, which takes the old page off the list and publishes as a new post.
- Ready to publish and publish() read the retirement flags on MAIN once per call (one indexed query per page load /
  click): cheaper than a column on `publish_log`, which would need a contract migration.
- `changed_on_wordpress` removes the page from the list for good (the tenant changed it on purpose); they can still
  publish it on WordPress.

## Should know

- No migration. CIS 360's tables and the 215 key index are all it needs.
- Tests: `tests/unit/test_aa1039_put_back_online.py` (adapter shapes, the remote decision, the route: authz, every
  refusal, the claim race, replays, revert, note and audit; the list filter; the mail; the bell item; the activity
  label) and `tests/integration/test_aa1039_put_back_online.py` (retire → draft → restore on Postgres: notice + one
  bell row, the list per tenant, another tenant's 404 on the tenant pool, the owner's click and its replay, a
  re-retirement flags the page again, a private page left as is, another live post keeps the page in draft and off
  the list, Ready to publish leaves the piece out and publish() refuses it until the page is settled, a rewrite
  before the restore keeps the old page out of the notice and the list). Unit tests stub the MAIN read of Ready to
  publish / publish() in conftest (`_put_back_guard_off`); opt in with the marker `real_put_back_guard`.
- Root flag tooling (`docs/audits/2026-10-16-S240-aa859/aa859_flag.py`, ALLOWED) gets `aa1039:put_back_pool` in a root
  PR so the flag can be set false on Dev.
- The Content studio (AA-Partner web) shows the list on Publish and the pages with a "Put back online" button in the
  bell.
