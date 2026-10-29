# AA-978 follow-up: feed served out of order (flaky `test_feed_cursor_pages_one_by_one`)

## Decisions

- The S256 note blamed "two rows with the same timestamp, no id tie-breaker". The CI log (release PR #1113,
  Integration Tests attempt 1) says otherwise: the 500-row walk returned the same event ids twice
  (`[a, b, c, a, b]`) and the 1-row walk skipped one (`[a, b]`). Event ids are UNIQUE and the feed keyset is already
  `(txid, seq)` with `seq` the primary key, so the tie-breaker existed. A repeat within one keyset walk means the
  page order is not the cursor order.
- Root cause: `FEED_SQL` selected `txid::text AS txid` and ordered by `txid, seq`. Postgres resolves a bare ORDER BY
  name to an output column before an input column, so the sort was on the text value ('10' < '9') while the WHERE
  compared the xid8 column. When the feed holds txids of different digit lengths (CI: the test DB's xid counter
  crossing a power of ten, or rows of earlier tests), a page ends on a cursor that is not its numeric maximum: the
  next page serves rows again, and `limit=1` jumps past rows. Whether a given CI run hit it depended on the xid
  counter, hence "random".
- Fix in the service, not the test: the statement qualifies every column (`FROM ... e`, `ORDER BY e.txid, e.seq`)
  and the text output gets its own name, `txid_text`, so a later unqualified edit cannot bring the clash back.

## Changed

- `services/tenant_membership/feed.py`: `FEED_SQL` as above; `event_body` reads `row["txid_text"]`. Cursor format,
  contract and response shape unchanged.
- `tests/unit/test_aa978_tenant_membership_contract.py`: fake rows use `txid_text`; new statement guard (ORDER BY
  terms are exactly `e.txid, e.seq`, the only alias is `txid_text`); new scenario with txids 9 / 10 / 99 / 100 and
  three rows of one txid walked with `limit=1` and `limit=500`.
- `tests/integration/test_aa978_membership_service.py`: new test inserting rows with hand-picked txids across
  digit-length boundaries (7 ... 1000), three rows per txid 10, one identical `occurred_at`, insertion order unlike
  txid order; walks with limit 1, 2 and 500 must all equal the `(txid, seq)` order. Fails on the old statement
  (`limit=1`), passes on the new one (local Postgres 16 run).

## Tradeoffs

- The integration test writes rows directly with `session_replication_role = replica` (the trigger-only guard
  skipped), like the existing fixture teardown; only possible on the superuser test DB. That is the only way to get
  chosen txids; waiting for the real counter to cross a power of ten is not testable.
- The unit fake sorts numerically, so it cannot see a text sort; the guard checks the SQL text instead. Crude, but
  the integration test covers behaviour.

## Should know

- Dev impact: a text sort and a numeric sort only differ when txids of different lengths sit in the feed. Dev's xid
  counter is far past CI's, so all Dev rows most likely share one digit length and the CRM consumer saw the right
  order. Read-only check (cis_app is enough): `SELECT length(min(txid)::text), length(max(txid)::text) FROM
  shared.tenant_membership_event` - equal lengths mean nothing was served out of order. If they differ, the CRM
  mirror should be re-synced from the snapshot endpoint.
- No migration, no contract change, no index change (the `(txid, seq)` index now actually serves the sort).
