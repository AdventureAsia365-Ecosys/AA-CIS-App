-- Migration 158: AA-626 — add 'dismissed' to review_status_enum
--
-- Context: A tour can have multiple generated_content versions. Once a tour already has an
-- approved version in the Master Content pool, its older HITL versions still sitting in the
-- review_queue are noise — the reviewer does not want to edit/fix them, just clear them out.
-- 'dismissed' lets the admin drop such a stale failed version from the queue WITHOUT editing
-- content and WITHOUT publishing, kept distinct from:
--   - 'rejected'   (reviewer judged the content bad)
--   - 'superseded' (auto-replaced by a newer regenerated version, AA-242)
--   - 'skipped'    (dead value since 002/003)
--
-- Behaviour (AA-626, "option A"): dismiss only hides the CURRENT row from the queue. The
-- _enqueue_review NOT EXISTS guard is scoped to review_status='pending', so a future pipeline
-- rerun that fails the same tour again can still enqueue a fresh pending row — a real new
-- failure is a real signal and is intentionally NOT permanently suppressed.
--
-- Postgres requires ALTER TYPE ... ADD VALUE outside a txn block on old versions; on 12+ it is
-- transactional-safe as a single statement. Apply once against the shared Dev+Prod DB.

ALTER TYPE review_status_enum ADD VALUE IF NOT EXISTS 'dismissed';
