"""AA-997 — re-locate destinations whose point came from the model (S254 geocode audit, fix plan step 1 "then
re-locate the 3,770 located_by = llm rows"), the body of job kind `place_relocate`.

Selection: shared.destinations with `located_by` in the payload's list (default ['llm']; 'mapbox' is allowed too — the
legacy TripPlanner rows the audit found 8/9 right), not merged, not admin-fixed, not yet stamped by this RULE_VERSION
(`geocoder_meta.relocate.v`), optionally one country. Each row is checked against the route of the tours that use it
(store.route_anchors: the located overnight stops of days d-1..d+1, this row excluded).
AA-997 v2 (RULE_VERSION aa997.2, S257 interim audit): a row an earlier version already stamped is taken again only
when its outcome is in the payload's `recheck` list (RECHECK_ALLOWED): `llm_point` / `off_route` (the model's point is
still there: the row is re-located with the new rule), `confirmed` (an aa997.1 confirmation, located_by 'geoapify'
now: re-judged first from its stored geocoder_meta at no cost, re-searched only when the stricter rule refuses it).
Untouched rows come first, then rechecks.

dry_run (no paid call, no write): counts of what apply would do with no call (non-place names, off-route points,
Hong Kong / Macao points stored as China), the re-judge of `confirmed` rows, and the Geoapify request estimate (an
upper bound: every query the strategy may send — v1: geomatch.normalise, 1 or 2; v2: geomatch.query_plan, ≤ 3 —
Free plan: $0; a paid plan: GEOCODER_COST_PER_REQUEST_USD per request) and the days it takes at RELOCATE_DAILY_CREDITS.

apply (≤ MAX_ROWS_PER_RUN rows a run, under the Geoapify lock, each row in its own transaction under the destinations
lock), per row:
  - a non-place name (services/places/name_filter.py) → review_reason `non_place:<cause>`, no call;
  - the geocoder confirms the name against the route (or, with no route, within the bake-off radius of the stored point)
    → the confirmed point (located_by 'geoapify', precision by kind, review_reason cleared; China → Hong Kong / Macao
    when the point is there and the key is free) — `store.upsert`'s overwrite rule (guards.may_overwrite) applies; the
    stamp keeps the previous point (`prev`) so a wrong move can be undone;
  - else the stored point stays and is flagged: review_reason `off_route` (far off every route of its tours) or
    `llm_point` (no geocoder confirmed it); with `hide_unconfirmed` the row is also `suspect` (hidden from the catalog
    and from new links, like the outlier rule) — off by default, the owner's call;
  - a re-checked Geoapify row (an aa997.1 `confirmed` row; with recheck `geoapify` — DRY RUN ONLY until a labelled
    sample clears it — a place_geocode row never touched), only under accept_rule aa997.2: kept as is (`kept`, no
    call) when its stored answer passes the strict rule; `kept_unjudged` (no call) when the refusing check needs the
    model's point and it is unknown, or no answer is stored; else re-searched (route + the stored hint, or the current
    point) — a passing answer within the move cap (guards.route_radius) is written (`reconfirmed`), else review_reason
    `weak_confirm` (point kept).
Overnight towns are taken first (they geocode well and, once confirmed, give the later runs' sites their route). A
row an admin fixed, or place_dedupe merged, after the run read its selection is `skipped` (re-read under the lock).
Every processed row is stamped `geocoder_meta.relocate = {v, outcome, at, …}` so a rerun skips it. Nothing is deleted:
component links and destination ids stay as they are (TripPlanner pins them).

Acceptance rule (payload `accept_rule`): "v1" (default — the rule place_geocode uses) | "aa997.2" (v1 + the strict
checks, geomatch.strict_reason) | "aa997.3" (route evidence from anchored routes) | "aa997.4" (aa997.3 + the
day-evidence guard). The confirmation re-check needs a strict rule (it IS the stricter re-judge).
aa997.4 (S261): the routes also carry each use day's other located places (routes_by_destination `day_places`); when
the geocoder's only same-name candidates are ones the day contradicts (geomatch.day_disagree: refused `day_disagree`)
the model's point is kept — outcome `kept_model`, review_reason `same_name_elsewhere`, never hidden (a row a hide
policy hid is shown again), instead of `llm_point` / `off_route`.

hide / unhide (no paid call; AA-997 v2, the readers' "hide unconfirmed" policy, owner's call): `hide` lists — and with
`confirm` sets `suspect` on — rows still carrying one of HIDE_REASONS (the catalog, TripPlanner and new links already
skip `suspect` rows), stamped `geocoder_meta.hidden = {by, reason, at, log}`; `unhide` reverts the rows that stamp
marks whose review_reason is still the one hidden (a row another path flagged since stays as it is) and keeps the
record (`unhidden_at`; a later hide moves the old stamp into `log`).

hide_flagged (AA-997 S260, no paid call): the same hide, scoped by the relocate STAMP instead of the review reason —
only rows a relocate run already processed and left on the model's point (`geocoder_meta.relocate.outcome` in
HIDE_FLAGGED_OUTCOMES, default llm_point / off_route; optionally one stamp version / accept rule), still flagged
(review_reason set, not linear_feature), still on the point the stamp judged (located_by in the request's
`located_by`, default llm; weak_confirm: geoapify), geo_status 'ok' — the `_FLAG_SQL` hide of `hide_unconfirmed`,
applied after the fact. Rows the chain has not reached yet (no stamp) are never taken. Report first (counts per
country / outcome / reason / stamp version, how many are linked to a tour, a sample spread over countries, and the
sorted `ids` with a digest each of id | review_reason | outcome | located_by); `confirm` names that report
(`dry_run_job`, its row count `max_rows`; refused without them, or when the report is not a finished report of the same
selection younger than REPORT_MAX_AGE_S) and hides only the rows of the report still selected with the same digest
(`dropped_ids` = the others; rows new since the report are never hidden), in batches of HIDE_BATCH under the
destinations lock, stamped `hidden = {by: HIDE_FLAGGED_STAMP, reason, outcome, stamp_v, at, job, report, actor}`, one
`acp_shared.audit_log` row per batch (ids hidden). Reverted by `unhide` (`by` = the stamps to revert,
`destination_ids` = only those rows: the single-row restore of the review list, taken whatever the row's reason
now), which writes its own audit rows. A row a person restored by id (`unhidden_by` 'restore:<actor>') stays shown:
later hide / hide_flagged runs skip it (`restored_skipped`) until a relocate run judges it again (its stamp newer than
`unhidden_at`); a bulk unhide by stamp ('unhide:<actor>') is not sticky. A hidden row the geocoder later confirms
(relocate `confirmed` / `reconfirmed`) is shown again: its point is now the geocoder's (`unhidden_by`
'relocate:<outcome>'); one a relocate recheck flags with another reason (llm_point → off_route / non_place:*) stays
hidden and its hide stamp takes the new reason, so unhide still reverts it.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Optional

import structlog

from services.places import geomatch, guards, locate, name_filter, store

logger = structlog.get_logger()

RULE_VERSION = "aa997.2"
LOCATED_BY_ALLOWED = ("llm", "mapbox")
# outcomes of an earlier rule version that a v2 run may take again (payload `recheck`)
# (`geoapify` = rows place_geocode located with Geoapify, never touched by relocate: re-judged from their stored answer)
RECHECK_ALLOWED = ("llm_point", "off_route", "confirmed", "geoapify")
# rechecks that re-judge a stored Geoapify answer: only meaningful under the strict rule
RECHECK_STRICT_ONLY = ("confirmed", "geoapify")
# held back from apply until a labelled dry-run sample shows the re-judge refuses no correct row (review fix: ~4,000
# native rows at ~90–93 % correct, judged without the model's point)
RECHECK_DRY_RUN_ONLY = ("geoapify",)
MAX_ROWS_PER_RUN = 400
# Geoapify Free plan: 3,000 credits/day; place_geocode stops at 2,500 (GEOAPIFY_DAILY_CREDIT_LIMIT). The relocate job
# stops RESERVE_CREDITS earlier so new names of the day are still geocoded.
RESERVE_CREDITS = 1000
SAMPLE = 25
REVIEW_WEAK_CONFIRM = "weak_confirm"      # an aa997.1 confirmation the strict rule refuses and no new search confirms
# AA-997 S261 (rule aa997.4): the geocoder found the name elsewhere, the use day's evidence sides with the model's point
# (geomatch.day_disagree) — the point is kept (outcome `kept_model`) and listed for review, never hidden: the reason is
# not in HIDE_REASONS, the outcome not in HIDE_FLAGGED_OUTCOMES
REVIEW_SAME_NAME = "same_name_elsewhere"
KEPT_MODEL = "kept_model"
HIDE_REASONS = (locate.REVIEW_LLM_POINT, locate.REVIEW_OFF_ROUTE, locate.REVIEW_UNCONFIRMED_HARD, REVIEW_WEAK_CONFIRM)
# opt-in only (never in the default set): pins of names the pre-filter judged not a place (`non_place:<cause>`) — 25/28
# verified not-a-place names, but also 3/439 verified-correct names (S254 fixture), so it is asked for explicitly
HIDE_NON_PLACE = name_filter.NON_PLACE_PREFIX
HIDE_STAMP = "aa997.2-policy"
# hide_flagged (S260): rows a relocate run stamped and left on the model's point
HIDE_FLAGGED_STAMP = "aa997.3-flagged"
HIDE_FLAGGED_OUTCOMES = (locate.REVIEW_LLM_POINT, locate.REVIEW_OFF_ROUTE, REVIEW_WEAK_CONFIRM)
HIDE_FLAGGED_DEFAULT = (locate.REVIEW_LLM_POINT, locate.REVIEW_OFF_ROUTE)
# every stamp a hide policy writes (unhide reverts these; a confirmed relocate shows these rows again)
POLICY_STAMPS = (HIDE_STAMP, HIDE_FLAGGED_STAMP)
HIDE_BATCH = 100
AUDIT_RESOURCE = "places_relocate"
AUDIT_HIDE = "places.relocate.hide_flagged"
AUDIT_HIDE_POLICY = "places.relocate.hide"
# a hide_flagged confirm needs a finished report of its selection, younger than this (the chain keeps stamping rows)
REPORT_MAX_AGE_S = 6 * 3600
MAX_REPORT_IDS = 10_000          # a report above this lists no ids: narrow by country to confirm
MAX_DROPPED_IDS = 500            # ids a confirm result lists of the reported rows it did not hide
AUDIT_UNHIDE = "places.relocate.unhide"

OUTCOMES = ("confirmed", "non_place", "off_route", "llm_point", "country_fixed", "skipped", "kept", "kept_unjudged",
            "reconfirmed", "weak_confirm", "kept_model")

_ROWS_SQL = """
    SELECT d.id::text AS id, d.name, d.country, d.lat, d.lng, d.located_by, d.geo_status, d.geo_precision,
           d.review_reason, d.name_norm, d.locality_norm, d.geocoder_meta
      FROM shared.destinations d
     WHERE d.duplicate_of IS NULL AND d.geo_status <> 'manual'
       AND ($2::text IS NULL OR lower(btrim(d.country)) = lower(btrim($2)))
       AND (d.geocoder_meta -> 'relocate' ->> 'v') IS DISTINCT FROM $3
       AND ((d.located_by = ANY($1::text[])
             AND ((d.geocoder_meta -> 'relocate') IS NULL
                  OR (d.geocoder_meta -> 'relocate' ->> 'outcome') = ANY($5::text[])))
            OR ('confirmed' = ANY($5::text[]) AND d.located_by = 'geoapify'
                AND (d.geocoder_meta -> 'relocate' ->> 'outcome') = 'confirmed')
            OR ('geoapify' = ANY($5::text[]) AND d.located_by = 'geoapify'
                AND (d.geocoder_meta -> 'relocate') IS NULL))
     -- untouched rows first; then overnight towns: once confirmed they are route anchors (store.route_anchors) for the
     -- sites of later runs
     ORDER BY (d.geocoder_meta -> 'relocate') IS NOT NULL,
              EXISTS (SELECT 1 FROM geo.tour_day td WHERE td.overnight_destination_id = d.id) DESC,
              lower(btrim(d.country)), d.id
     LIMIT $4
"""
_COUNT_SQL = """
    SELECT count(*) FROM shared.destinations d
     WHERE d.duplicate_of IS NULL AND d.geo_status <> 'manual'
       AND ($2::text IS NULL OR lower(btrim(d.country)) = lower(btrim($2)))
       AND (d.geocoder_meta -> 'relocate' ->> 'v') IS DISTINCT FROM $3
       AND ((d.located_by = ANY($1::text[])
             AND ((d.geocoder_meta -> 'relocate') IS NULL
                  OR (d.geocoder_meta -> 'relocate' ->> 'outcome') = ANY($4::text[])))
            OR ('confirmed' = ANY($4::text[]) AND d.located_by = 'geoapify'
                AND (d.geocoder_meta -> 'relocate' ->> 'outcome') = 'confirmed')
            OR ('geoapify' = ANY($4::text[]) AND d.located_by = 'geoapify'
                AND (d.geocoder_meta -> 'relocate') IS NULL))
"""
_KEYS_SQL = """
    SELECT destination_id::text AS d, name_norm, country_norm, locality_norm FROM geo.place_name
     WHERE destination_id = ANY($1::uuid[])
"""
# strategy v2: the overnight towns of days d-1..d of the tours using a name (the context of a homonym query)
_TOWNS_SQL = """
    WITH k AS (SELECT * FROM unnest($1::text[], $2::text[], $3::text[]) AS k(n, c, l)),
         uses AS (
           SELECT k.n, k.c, k.l, tp.source_id, tp.day_index
             FROM geo.tour_place tp JOIN k ON (tp.name_norm, tp.country_norm, tp.locality_norm) = (k.n, k.c, k.l)
           UNION
           SELECT k.n, k.c, k.l, td.source_id, td.day_index
             FROM geo.tour_day td JOIN k ON (td.anchor_norm, td.anchor_country_norm, td.anchor_locality_norm)
                                            = (k.n, k.c, k.l))
    SELECT u.n, u.c, u.l, d.name, d.lat, d.lng, count(*) AS uses
      FROM uses u
      JOIN geo.tour_day td ON td.source_id = u.source_id AND td.day_index BETWEEN u.day_index - 1 AND u.day_index
      JOIN shared.destinations d ON d.id = td.overnight_destination_id
     WHERE d.located_by IN ('geoapify', 'manual', 'mapbox') AND d.geo_status <> 'suspect' AND d.duplicate_of IS NULL
       AND d.geo_precision IS DISTINCT FROM 'line' AND NOT (d.id = ANY($4::uuid[]))
     GROUP BY 1, 2, 3, 4, 5, 6
"""
_STAMP_SQL = """
    UPDATE shared.destinations
       SET geocoder_meta = coalesce(geocoder_meta, '{}'::jsonb) || jsonb_build_object('relocate', $2::jsonb)
     WHERE id = $1::uuid
"""
_FLAG_SQL = """
    UPDATE shared.destinations SET review_reason = $2,
           geo_status = CASE WHEN $3 AND geo_status = 'ok' THEN 'suspect' ELSE geo_status END
     WHERE id = $1::uuid AND geo_status <> 'manual'
"""
_HIDE_NAMES_SQL = """
    UPDATE geo.place_name SET status = 'suspect', updated_at = now()
     WHERE destination_id = $1::uuid AND status = 'resolved'
"""
_COUNTRY_SQL = "UPDATE shared.destinations SET country = $2 WHERE id = $1::uuid"
# re-read under the destinations lock: an admin may fix the row, or place_dedupe merge it, while the run (≤ 400 rows,
# minutes of geocoder calls) works from the selection it read at the start
_CURRENT_SQL = """
    SELECT located_by, geo_status, duplicate_of IS NOT NULL AS merged, geocoder_meta -> 'hidden' AS hidden
      FROM shared.destinations
     WHERE id = $1::uuid FOR UPDATE
"""
# hide / unhide policy (no call)
# a row a person restored — an explicit `destination_ids` unhide (`unhidden_by` 'restore:<actor>'; a bulk unhide by
# stamp writes 'unhide:<actor>', a relocate reveal 'relocate:<outcome>', neither counts) — that no relocate run has
# judged since (its stamp's `at` is not newer than `unhidden_at`): hide and hide_flagged leave it shown
# (`restored_skipped`); a new judgement may hide it again. `{t}` = the table alias prefix ('d.' or '').
_RESTORED_TPL = """({t}geocoder_meta -> 'hidden' ->> 'unhidden_at') IS NOT NULL
            AND coalesce({t}geocoder_meta -> 'hidden' ->> 'unhidden_by', '') LIKE 'restore:%'
            AND CASE WHEN ({t}geocoder_meta -> 'relocate' ->> 'at') IS NULL THEN true
                     ELSE ({t}geocoder_meta -> 'relocate' ->> 'at')::timestamptz
                          <= ({t}geocoder_meta -> 'hidden' ->> 'unhidden_at')::timestamptz END"""
_HIDE_CANDIDATES_SQL = """
    SELECT d.id::text AS id, d.name, d.country, d.review_reason, d.located_by,
           coalesce(""" + _RESTORED_TPL.format(t="d.") + """, false) AS restored
      FROM shared.destinations d
     WHERE d.duplicate_of IS NULL AND d.geo_status = 'ok'
       AND (d.review_reason = ANY($1::text[]) OR ($3::bool AND d.review_reason LIKE ($4::text || ':%')))
       AND ($2::text IS NULL OR lower(btrim(d.country)) = lower(btrim($2)))
     ORDER BY lower(btrim(d.country)), d.id
"""
# a row hidden and unhidden before keeps that earlier stamp in hidden.log (an append-only history); re-checked still
# flagged for the reason read ($3) and not restored by a person meanwhile
_HIDE_ROW_SQL = """
    UPDATE shared.destinations
       SET geo_status = 'suspect',
           geocoder_meta = coalesce(geocoder_meta, '{}'::jsonb) || jsonb_build_object('hidden', $2::jsonb
               || jsonb_build_object('log', coalesce(geocoder_meta -> 'hidden' -> 'log', '[]'::jsonb)
                  || CASE WHEN geocoder_meta ? 'hidden' THEN jsonb_build_array((geocoder_meta -> 'hidden') - 'log')
                          ELSE '[]'::jsonb END))
     WHERE id = $1::uuid AND geo_status = 'ok' AND duplicate_of IS NULL
       AND review_reason IS NOT DISTINCT FROM $3
       AND NOT coalesce(""" + _RESTORED_TPL.format(t="") + """, false)
"""
# hide_flagged: rows a relocate run stamped and left on the model's point, still flagged and still on that point
# ($1 outcomes, $2 stamp versions — empty = any, $3 stamp accept rule | NULL, $4 the request's located_by of a kept
# model point (default llm),
# $5 country | NULL); `linked` = used by a tour day (a pin a reader can show); `restored` = _RESTORED_TPL (counted,
# never hidden)
_HIDE_FLAGGED_SQL = """
    SELECT d.id::text AS id, d.name, d.country, d.lat, d.lng, d.review_reason, d.located_by,
           d.geocoder_meta -> 'relocate' ->> 'outcome' AS outcome, d.geocoder_meta -> 'relocate' ->> 'v' AS stamp_v,
           d.geocoder_meta -> 'relocate' ->> 'accept_rule' AS accept_rule,
           d.geocoder_meta -> 'relocate' ->> 'route_km' AS route_km,
           (EXISTS (SELECT 1 FROM geo.tour_place tp WHERE tp.destination_id = d.id)
            OR EXISTS (SELECT 1 FROM geo.tour_day td WHERE td.overnight_destination_id = d.id)) AS linked,
           coalesce(""" + _RESTORED_TPL.format(t="d.") + """, false) AS restored
      FROM shared.destinations d
     WHERE d.duplicate_of IS NULL AND d.geo_status = 'ok'
       AND d.review_reason IS NOT NULL AND d.review_reason <> 'linear_feature'
       AND (d.geocoder_meta -> 'relocate' ->> 'outcome') = ANY($1::text[])
       AND (cardinality($2::text[]) = 0 OR (d.geocoder_meta -> 'relocate' ->> 'v') = ANY($2::text[]))
       AND ($3::text IS NULL OR (d.geocoder_meta -> 'relocate' ->> 'accept_rule') = $3)
       AND (d.located_by = ANY($4::text[])
            OR ((d.geocoder_meta -> 'relocate' ->> 'outcome') = 'weak_confirm' AND d.located_by = 'geoapify'))
       AND ($5::text IS NULL OR lower(btrim(d.country)) = lower(btrim($5)))
     ORDER BY lower(btrim(d.country)), d.id
"""
# one row, re-checked unchanged since the report read it (reason, stamp outcome, located_by) and not restored by a
# person meanwhile; the hidden.log append of _HIDE_ROW_SQL
_HIDE_FLAGGED_ROW_SQL = """
    UPDATE shared.destinations
       SET geo_status = 'suspect',
           geocoder_meta = coalesce(geocoder_meta, '{}'::jsonb) || jsonb_build_object('hidden', $2::jsonb
               || jsonb_build_object('log', coalesce(geocoder_meta -> 'hidden' -> 'log', '[]'::jsonb)
                  || CASE WHEN geocoder_meta ? 'hidden' THEN jsonb_build_array((geocoder_meta -> 'hidden') - 'log')
                          ELSE '[]'::jsonb END))
     WHERE id = $1::uuid AND geo_status = 'ok' AND duplicate_of IS NULL
       AND review_reason IS NOT DISTINCT FROM $3
       AND (geocoder_meta -> 'relocate' ->> 'outcome') IS NOT DISTINCT FROM $4
       AND located_by IS NOT DISTINCT FROM $5
       AND NOT coalesce(""" + _RESTORED_TPL.format(t="") + """, false)
"""
# only rows still hidden by a policy stamp in $1, for the reason it hid them: a row another path flagged since
# (relocate with hide_unconfirmed, the outlier rule …) keeps its state; $3 = only these ids (NULL = all) — an explicit
# id list is the admin's single-row restore and is reverted whatever the reason now ($4 in _UNHIDE_ROW_SQL)
_UNHIDE_ROWS_SQL = """
    SELECT d.id::text AS id,
           d.review_reason IS NOT DISTINCT FROM (d.geocoder_meta -> 'hidden' ->> 'reason') AS same_reason
      FROM shared.destinations d
     WHERE d.geo_status = 'suspect' AND d.duplicate_of IS NULL
       AND (d.geocoder_meta -> 'hidden' ->> 'by') = ANY($1::text[])
       AND (d.geocoder_meta -> 'hidden' ->> 'unhidden_at') IS NULL
       AND ($2::text IS NULL OR lower(btrim(d.country)) = lower(btrim($2)))
       AND ($3::uuid[] IS NULL OR d.id = ANY($3::uuid[]))
     ORDER BY d.id
"""
# $3 unhidden_by: 'restore:<actor>' (explicit ids, sticky — _RESTORED_TPL) | 'unhide:<actor>' (bulk, not sticky);
# $5 the job
_UNHIDE_ROW_SQL = """
    UPDATE shared.destinations
       SET geo_status = 'ok',
           geocoder_meta = jsonb_set(geocoder_meta, '{hidden}', (geocoder_meta -> 'hidden')
               || jsonb_build_object('unhidden_at', $2::text, 'unhidden_by', $3::text, 'unhidden_job', $5::text))
     WHERE id = $1::uuid AND geo_status = 'suspect' AND duplicate_of IS NULL
       AND (geocoder_meta -> 'hidden' ->> 'unhidden_at') IS NULL
       AND ($4::bool OR review_reason IS NOT DISTINCT FROM (geocoder_meta -> 'hidden' ->> 'reason'))
"""
# a policy-hidden row the geocoder just confirmed: shown again, the hide record kept ($2 = the hidden stamp with
# unhidden_at / unhidden_by; _write_point replaced geocoder_meta, so the stamp is written back whole)
_REVEAL_CONFIRMED_SQL = """
    UPDATE shared.destinations
       SET geo_status = 'ok',
           geocoder_meta = coalesce(geocoder_meta, '{}'::jsonb) || jsonb_build_object('hidden', $2::jsonb)
     WHERE id = $1::uuid AND geo_status = 'suspect'
"""
# a policy-hidden row a relocate run flags again with another reason (an aa997.1 llm_point recheck now off_route /
# non_place:*): the hide stamp follows the new reason, so unhide (same-reason guard) still reverts it; `reason_was` /
# `reason_by` keep what changed it
_HIDDEN_REASON_SQL = """
    UPDATE shared.destinations
       SET geocoder_meta = jsonb_set(geocoder_meta, '{hidden}', (geocoder_meta -> 'hidden')
               || jsonb_build_object('reason', $2::text, 'reason_was', geocoder_meta -> 'hidden' -> 'reason',
                                     'reason_by', $3::text))
     WHERE id = $1::uuid AND geo_status = 'suspect' AND (geocoder_meta -> 'hidden' ->> 'unhidden_at') IS NULL
       AND (geocoder_meta -> 'hidden' ->> 'by') = ANY($4::text[])
       AND (geocoder_meta -> 'hidden' ->> 'reason') IS DISTINCT FROM $2
"""
# the report a hide_flagged confirm names (shared.job; age from finished_at)
_REPORT_JOB_SQL = """
    SELECT id::text AS id, status, result, extract(epoch FROM (now() - finished_at))::float8 AS age_s
      FROM shared.job WHERE id = $1::uuid AND kind = 'place_relocate'
"""
_UNHIDE_NAMES_SQL = """
    UPDATE geo.place_name SET status = 'resolved', updated_at = now()
     WHERE destination_id = $1::uuid AND status = 'suspect'
"""


async def _still_due(conn, row: dict) -> Optional[dict]:
    """The row's current located_by / geo_status when it may still be processed (not admin-fixed, not merged, still
    one of the selected sources), else None."""
    cur = await conn.fetchrow(_CURRENT_SQL, row["id"])
    if cur is None or cur["merged"] or cur["geo_status"] == "manual" or cur["located_by"] != row["located_by"]:
        return None
    return dict(cur)


def _policy_hidden(cur: dict) -> Optional[dict]:
    """The hide-policy stamp of a row a policy hid and nothing unhid since (still 'suspect'), else None."""
    h = cur.get("hidden")
    if isinstance(h, str):
        try:
            h = json.loads(h)
        except ValueError:
            h = None
    if cur.get("geo_status") != "suspect" or not isinstance(h, dict):
        return None
    return h if h.get("by") in POLICY_STAMPS and not h.get("unhidden_at") else None


async def _reveal_if_policy_hidden(conn, row_id: str, cur: dict, outcome: str) -> bool:
    """After the geocoder's point was written: a row a hide policy hid is shown again (and its names), the stamp kept
    with unhidden_at / unhidden_by. Any other suspect row (outlier rule, hide_unconfirmed) stays as it is."""
    h = _policy_hidden(cur)
    if h is None:
        return False
    stamp = {**h, "unhidden_at": datetime.now(timezone.utc).isoformat(), "unhidden_by": f"relocate:{outcome}"}
    status = await conn.execute(_REVEAL_CONFIRMED_SQL, row_id, json.dumps(stamp, default=str))
    if not str(status).endswith(" 1"):
        return False
    await conn.execute(_UNHIDE_NAMES_SQL, row_id)
    return True


async def _flag(conn, row_id: str, cur: Optional[dict], reason: str, hide_unconfirmed: bool, outcome: str) -> None:
    """_FLAG_SQL (+ the names with hide_unconfirmed); on a row a hide policy hid, the hide stamp's reason follows the
    new one (_HIDDEN_REASON_SQL) — the same policy still hides it, and unhide can still revert it."""
    await conn.execute(_FLAG_SQL, row_id, reason, hide_unconfirmed)
    if hide_unconfirmed:
        await conn.execute(_HIDE_NAMES_SQL, row_id)
    if cur is not None and _policy_hidden(cur) is not None:
        await conn.execute(_HIDDEN_REASON_SQL, row_id, reason, f"relocate:{outcome}", list(POLICY_STAMPS))


def located_by_list(value) -> list[str]:
    vals = [v for v in (value or ["llm"]) if v in LOCATED_BY_ALLOWED]
    return sorted(set(vals)) or ["llm"]


def recheck_list(value) -> list[str]:
    return sorted({v for v in (value or []) if v in RECHECK_ALLOWED})


def is_strict(accept_rule: Optional[str]) -> bool:
    """aa997.2 or aa997.3 (geomatch.STRICT_RULES)."""
    return accept_rule in geomatch.STRICT_RULES


def recheck_for(value, accept_rule: Optional[str]) -> list[str]:
    """The rechecks a run takes: RECHECK_STRICT_ONLY ones only under the strict rule (re-judging a stored answer
    with the rule that accepted it changes nothing)."""
    return [v for v in recheck_list(value) if is_strict(accept_rule) or v not in RECHECK_STRICT_ONLY]


async def candidates(conn, *, located_by: list[str], country: Optional[str], limit: int,
                     recheck: Optional[list[str]] = None) -> list[dict]:
    return [dict(r) for r in await conn.fetch(_ROWS_SQL, located_by, country, RULE_VERSION, limit,
                                              recheck_list(recheck))]


async def count_candidates(conn, *, located_by: list[str], country: Optional[str],
                           recheck: Optional[list[str]] = None) -> int:
    return int(await conn.fetchval(_COUNT_SQL, located_by, country, RULE_VERSION, recheck_list(recheck)) or 0)


async def _keys_by_destination(conn, ids: list[str]) -> dict[str, list[tuple]]:
    keys_by: dict[str, list[tuple]] = {}
    for k in await conn.fetch(_KEYS_SQL, ids):
        keys_by.setdefault(k["d"], []).append((k["name_norm"], k["country_norm"], k["locality_norm"]))
    return keys_by


async def routes_by_destination(conn, rows: list[dict], day_places: bool = False) -> dict[str, tuple]:
    """{destination id: routes} — the routes of every name key resolved to the row, the row itself excluded;
    `day_places` (rule aa997.4) also reads each use day's other located places (store.route_anchors)."""
    ids = [r["id"] for r in rows]
    if not ids:
        return {}
    keys_by = await _keys_by_destination(conn, ids)
    all_keys = [k for ks in keys_by.values() for k in ks]
    routes = await store.route_anchors(conn, all_keys, exclude=ids, day_places=day_places)
    return {d: tuple(r for k in ks for r in routes.get(k, ())) for d, ks in keys_by.items()}


async def towns_by_destination(conn, rows: list[dict]) -> dict[str, list[tuple]]:
    """{destination id: [(town name, lat, lng, uses), …]} — the overnight towns of days d-1..d of the tours using the
    row's names (strategy v2: the context of the second query)."""
    ids = [r["id"] for r in rows]
    if not ids:
        return {}
    keys_by = await _keys_by_destination(conn, ids)
    all_keys = list(dict.fromkeys(k for ks in keys_by.values() for k in ks))
    if not all_keys:
        return {}
    a, b, c = (list(x) for x in zip(*all_keys))
    by_key: dict[tuple, list[tuple]] = {}
    for r in await conn.fetch(_TOWNS_SQL, a, b, c, ids):
        by_key.setdefault((r["n"], r["c"], r["l"]), []).append(
            (r["name"], float(r["lat"]), float(r["lng"]), int(r["uses"])))
    return {d: [t for k in ks for t in by_key.get(k, [])] for d, ks in keys_by.items()}


def context_town(name: str, towns: list[tuple], lat: Optional[float], lng: Optional[float]) -> Optional[str]:
    """The town to add to a homonym query: the most used overnight town of the name's days, ties broken by the
    distance to the stored point; None when there is none or it is the place itself."""
    best = None
    for t_name, t_lat, t_lng, uses in towns or ():
        if not t_name or geomatch.name_sim(t_name, name) >= 0.999 or geomatch.name_sim(name, t_name) >= 0.999:
            continue
        dist = guards.km(lat, lng, t_lat, t_lng) if lat is not None and lng is not None else 0.0
        key = (-uses, dist)
        if best is None or key < best[0]:
            best = (key, t_name)
    return best[1] if best else None


def _requests_for(query: str, strategy: str, context: Optional[str] = None) -> int:
    """Searches the query plan sends for one row (relocate has no native-script name: no extra native query). 429 /
    5xx retries are not counted — the daily credit guard (geo.geocode_call) bounds the real spend."""
    from services.places.geocoder import STRATEGY_V2, V2_MAX_QUERIES
    if strategy == STRATEGY_V2:
        return len(geomatch.query_plan(query, context=context, max_queries=V2_MAX_QUERIES))
    return len(geomatch.normalise(query))


def classify_row(row: dict, routes: tuple, *, strategy: str = "v1", context: Optional[str] = None) -> dict:
    """What apply would do with no call: non-place, current point off the route, Hong Kong / Macao stored as China;
    and the Geoapify requests the row may cost (an upper bound; 0 for a non-place name)."""
    verdict = name_filter.classify(row["name"], row.get("locality_norm") or None)
    lat, lng = float(row["lat"]), float(row["lng"])
    on = guards.on_route(lat, lng, routes, row["name"])
    refined = guards.refine_country(lat, lng, row["country"])
    requests = _requests_for(verdict.query, strategy, context) if verdict.is_place else 0
    return {"non_place": verdict.cause, "query": verdict.query, "on_route": on,
            "route_km": None if on is None else round(guards.route_km(lat, lng, routes), 1),
            "hk_mo": refined if refined != row["country"] else None, "requests": requests}


def _meta(row: dict) -> dict:
    m = row.get("geocoder_meta")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = None
    return m if isinstance(m, dict) else {}


def _stamp_of(row: dict) -> dict:
    s = _meta(row).get("relocate")
    return s if isinstance(s, dict) else {}


def is_confirmed_recheck(row: dict) -> bool:
    """A Geoapify-located row taken by `recheck` (an aa997.1 `confirmed` row, or a place_geocode row never touched)."""
    return row.get("located_by") == "geoapify" and _stamp_of(row).get("outcome") in ("confirmed", None)


def _hint_of(row: dict) -> tuple[Optional[tuple], Optional[float]]:
    """(model's point, distance candidate→model's point) of a stored answer: `geocoder_meta.hint` (stored since AA-997
    v2), else only the distance — an aa997.1 relocate confirmation's stamp `moved_km` (the row moved from the model's
    point to the candidate) — else (None, None)."""
    h = _meta(row).get("hint")
    if isinstance(h, (list, tuple)) and len(h) >= 2 and h[0] is not None and h[1] is not None:
        return (float(h[0]), float(h[1])), None
    st = _stamp_of(row)
    if st.get("outcome") == "confirmed" and st.get("moved_km") is not None:
        try:
            return None, float(st["moved_km"])
        except (TypeError, ValueError):
            pass
    return None, None


def rejudge(row: dict, routes: tuple, accept_rule: str = geomatch.ACCEPT_RULE,
            params: Optional[geomatch.StrictParams] = None) -> dict:
    """The stored confirmation of a row against the strict rule (AA-997 v2), with no call: {'ok': bool, 'reason': why
    refused | None, 'unjudged': [checks that could not be judged], 'hint': the model's point | None}. The candidate is
    rebuilt from geocoder_meta (name, result_type, match_type) at the row's point and judged with the model's point
    when it is known (_hint_of). A check that needs the model's point and has no hint is NOT judged (`unjudged`, kept
    — review fix: it used to refuse rows accepted under the access-point exemption); no stored candidate name
    → ok with unjudged ['no_meta'] (no paid re-search for a row that cannot be judged).
    `accept_rule` picks the strict rule (aa997.2 | aa997.3); `params` overrides its knobs (the S258 P4 tune script
    sweeps them over the labelled sample). aa997.3: the route checks use only routes with the use day's own stop
    (geomatch.evidence_routes), the off-route check the homonym distance, and v1's name / type checks are listed as
    unjudged (`v1_name` / `v1_type`) instead of refusing — they were passed when the answer was accepted."""
    p = params or geomatch.strict_params(accept_rule)
    meta = _meta(row)
    query = meta.get("judged") or meta.get("query") or row["name"]    # v2 stores the bare name judged as `judged`
    cname = meta.get("name")
    if not cname:
        return {"ok": True, "reason": None, "unjudged": ["no_meta"], "hint": None}
    hint, hint_km = _hint_of(row)
    iso = guards.iso2(row["country"])
    c = geomatch.Candidate(float(row["lat"]), float(row["lng"]), iso, cname, meta.get("result_type") or "",
                           meta.get("confidence"), meta.get("match_type"), None, meta.get("source"))
    settlement = geomatch.is_settlement(query)
    compact = meta.get("strategy") == "v2"
    sim = (geomatch.name_sim_v2 if compact else geomatch.name_sim)(query, cname)
    n_tok = len(geomatch.tokens_v2(query) if compact else geomatch.tokens(query))
    unjudged: list = []
    if not (sim >= 0.999 or (sim >= 0.5 and n_tok >= 2)):
        if p.rejudge_v1_checks:
            return {"ok": False, "reason": "name", "unjudged": [], "hint": hint}
        unjudged.append("v1_name")
    if c.result_type in geomatch.REJECT_TYPES or (c.result_type == "city" and not settlement):
        if p.rejudge_v1_checks:
            return {"ok": False, "reason": "type", "unjudged": [], "hint": hint}
        unjudged.append("v1_type")
    ev = geomatch.evidence_routes(routes, p)
    # an aa997.1 day-trip accept (off the route radius, within DAY_TRIP_KM): judged with the day-trip radius as v2 does
    day_trip = guards.has_route(ev) and guards.on_route(c.lat, c.lng, ev, query) is False
    if day_trip and (guards.route_km(c.lat, c.lng, ev) or 0.0) > p.rejudge_off_route_km:
        return {"ok": False, "reason": "off_route", "unjudged": [], "hint": hint}
    why = geomatch.strict_reason(query, c, sim, hint[0] if hint else None, hint[1] if hint else None, ev,
                                 settlement=settlement, day_trip=day_trip, unjudged=unjudged, hint_km=hint_km,
                                 params=p)
    if hint is None and guards.has_route(ev):
        unjudged.append("hint_disagree")       # needs the model's point's own position: never judged without it
    return {"ok": why is None, "reason": why, "unjudged": unjudged, "hint": hint}


def rejudge_outcome(j: dict) -> str:
    """kept (passes, every check judged), kept_unjudged (passes but a check that could refuse it was not judged —
    street / kind without the model's point, or no stored answer), refused."""
    if not j["ok"]:
        return "refused"
    # hint_disagree / day_disagree (aa997.4) need the model's point itself: listed, never a reason for kept_unjudged
    return "kept_unjudged" if set(j.get("unjudged") or ()) - {"hint_disagree", "day_disagree"} else "kept"


def _brief(row: dict, c: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "country": row["country"], "lat": row["lat"], "lng": row["lng"],
            **{k: c[k] for k in ("non_place", "route_km", "hk_mo") if c.get(k) is not None}}


async def dry_run(conn, *, located_by: list[str], country: Optional[str], cost_per_request: float,
                  daily_credits: int, recheck: Optional[list[str]] = None, strategy: str = "v1",
                  max_requests: Optional[int] = None, accept_rule: str = geomatch.ACCEPT_RULE_V1) -> dict:
    """The report apply works from — no paid call, no write. `recheck` must already be recheck_for(…, accept_rule)."""
    recheck = recheck_for(recheck, accept_rule)
    rows = await candidates(conn, located_by=located_by, country=country, limit=1_000_000, recheck=recheck)
    routes = await routes_by_destination(conn, rows, **_day_kw(accept_rule))
    towns = await towns_by_destination(conn, rows) if strategy == "v2" else {}
    by_country: dict[str, int] = {}
    non_place: dict[str, int] = {}
    counts = {"rows": len(rows), "with_route": 0, "on_route": 0, "off_route": 0, "no_route": 0, "hk_mo": 0,
              "non_place": 0, "requests": 0, "with_context": 0}
    by_prev: dict[str, int] = {}
    judged = {"rows": 0, "kept": 0, "kept_unjudged": 0, "refused": 0, "refused_by": {}, "unjudged_by": {}}
    samples: dict[str, list] = {"off_route": [], "non_place": [], "hk_mo": [], "rejudge_refused": [],
                                "rejudge_unjudged": []}
    for row in rows:
        prev = _stamp_of(row).get("outcome") or "untouched"
        by_prev[prev] = by_prev.get(prev, 0) + 1
        r_routes = routes.get(row["id"], ())
        if is_confirmed_recheck(row):
            j = rejudge(row, r_routes, accept_rule)
            judged["rows"] += 1
            verdict = rejudge_outcome(j)
            for u in j.get("unjudged") or ():
                judged["unjudged_by"][u] = judged["unjudged_by"].get(u, 0) + 1
            if verdict != "refused":
                judged[verdict] += 1
                if verdict == "kept_unjudged" and len(samples["rejudge_unjudged"]) < SAMPLE:
                    samples["rejudge_unjudged"].append({"id": row["id"], "name": row["name"],
                                                        "country": row["country"], "unjudged": j["unjudged"],
                                                        "candidate": _meta(row).get("name")})
                continue
            judged["refused"] += 1
            judged["refused_by"][j["reason"]] = judged["refused_by"].get(j["reason"], 0) + 1
            if len(samples["rejudge_refused"]) < SAMPLE:
                samples["rejudge_refused"].append({"id": row["id"], "name": row["name"], "country": row["country"],
                                                   "reason": j["reason"],
                                                   "candidate": _meta(row).get("name"),
                                                   "result_type": _meta(row).get("result_type")})
        ctx = context_town(row["name"], towns.get(row["id"], []), row["lat"], row["lng"]) if towns else None
        c = classify_row(row, r_routes, strategy=strategy, context=ctx)
        counts["with_context"] += 1 if ctx else 0
        by_country[row["country"]] = by_country.get(row["country"], 0) + 1
        counts["requests"] += c["requests"]
        if c["non_place"]:
            counts["non_place"] += 1
            non_place[c["non_place"]] = non_place.get(c["non_place"], 0) + 1
            if len(samples["non_place"]) < SAMPLE:
                samples["non_place"].append(_brief(row, c))
        if c["on_route"] is None:
            counts["no_route"] += 1
        else:
            counts["with_route"] += 1
            counts["on_route" if c["on_route"] else "off_route"] += 1
            if not c["on_route"] and len(samples["off_route"]) < SAMPLE:
                samples["off_route"].append(_brief(row, c))
        if c["hk_mo"]:
            counts["hk_mo"] += 1
            if len(samples["hk_mo"]) < SAMPLE:
                samples["hk_mo"].append(_brief(row, c))
    credits = counts["requests"]
    capped = min(credits, max_requests) if max_requests else credits
    return {"mode": "dry_run", "rule_version": RULE_VERSION, "located_by": located_by, "country": country,
            "recheck": recheck_list(recheck), "strategy": strategy, "accept_rule": accept_rule,
            "max_requests": max_requests,
            **counts, "by_previous_outcome": by_prev, "rejudge_confirmed": judged, "non_place_by": non_place,
            "by_country": dict(sorted(by_country.items(), key=lambda kv: -kv[1])),
            "estimate": {"geoapify_requests": credits, "geoapify_credits": credits, "requests_upper_bound": True,
                         "requests_note": ("every query of each row's plan (no native query in relocate); 429 / 5xx "
                                           "retries are not counted — the daily credit guard bounds the real spend"),
                         "capped_requests": capped,
                         "cost_usd": round(capped * cost_per_request, 4), "cost_per_request_usd": cost_per_request,
                         "daily_credits": daily_credits,
                         "days": math.ceil(capped / daily_credits) if daily_credits > 0 and capped else 0,
                         "model_calls": 0, "model_cost_usd": 0.0},
            "samples": samples}


def _stamp(outcome: str, **extra) -> str:
    return json.dumps({"v": RULE_VERSION, "outcome": outcome, "at": datetime.now(timezone.utc).isoformat(), **extra},
                      default=str)


async def _fix_country(conn, row: dict, country: str) -> bool:
    """China → Hong Kong / Macao for a point inside them; skipped (False) when that key already has a row."""
    import asyncpg
    try:
        async with conn.transaction():
            await conn.execute(_COUNTRY_SQL, row["id"], country)
        return True
    except asyncpg.UniqueViolationError:
        return False


def _keep_linear(row: dict, reason: str) -> str:
    """A row already flagged linear_feature keeps that reason: store._write_point un-hides the row on it once a
    geocoder line lands, so overwriting it with off_route / non_place would leave the row hidden for good."""
    return locate.REVIEW_LINEAR_FEATURE if row.get("review_reason") == locate.REVIEW_LINEAR_FEATURE else reason


def _prev(row: dict) -> list:
    return [row["lat"], row["lng"], row.get("located_by")]


def _day_kw(accept_rule: Optional[str]) -> dict:
    """routes_by_destination kwargs: the day's places only for a rule that judges with them (aa997.4), so the
    earlier rules read exactly what they read before."""
    return {"day_places": True} if geomatch.needs_day_places(accept_rule) else {}


def _day_refused(res) -> bool:
    """The geocoder refused a candidate as `day_disagree` (rule aa997.4) on one of its searches."""
    meta = (getattr(res, "meta", None) or {}) if res is not None else {}
    reasons = [r for t in meta.get("tried") or () if isinstance(t, dict) for r in t.get("rejected") or ()]
    return "day_disagree" in geomatch.strict_refusals(reasons)


def _rule_kw(strategy: str, context: Optional[str], accept_rule: str) -> dict:
    kw = {"strategy": strategy, "context": context} if strategy != "v1" else {}
    if is_strict(accept_rule):
        kw["strict"] = accept_rule
    return kw


async def _apply_confirmed_recheck(conn, row: dict, routes: tuple, geocoder, *, hide_unconfirmed: bool,
                                   strategy: str, context: Optional[str], accept_rule: str) -> dict:
    """A Geoapify row taken by recheck (strict rule only): `kept` when its stored answer passes the strict rule,
    `kept_unjudged` when a check that could refuse it needs the model's point and it is unknown (or no answer is
    stored) — both with no call; else searched again (the route, and the stored model's point or the current point as
    hint) — a passing answer within the move cap is written (`reconfirmed`), else `weak_confirm` (the point stays;
    hidden too with hide_unconfirmed)."""
    j = rejudge(row, routes, accept_rule)
    verdict = rejudge_outcome(j)
    if verdict != "refused":
        async with conn.transaction():
            await store.lock_destinations(conn)
            if await _still_due(conn, row) is None:
                return {"outcome": "skipped"}
            extra = {"unjudged": j["unjudged"]} if j.get("unjudged") else {}
            await conn.execute(_STAMP_SQL, row["id"], _stamp(verdict, prev_v=_stamp_of(row).get("v") or "geocode",
                                                             accept_rule=accept_rule, **extra))
        return {"outcome": verdict}
    query = name_filter.classify(row["name"], row.get("locality_norm") or None).query or row["name"]
    item = locate.Item((row["name_norm"], (row["country"] or "").lower(), row.get("locality_norm") or ""),
                       query, row["country"], routes=routes)
    lat, lng = float(row["lat"]), float(row["lng"])
    # the hint: the model's point stored with the answer, else the current point (it passed the bake-off radius of
    # the model's point once — the right area, the wrong feature is what the strict rule refused); the route, when
    # there is one, still decides (review fix: the search used to run with no hint at all when a route existed)
    h_lat, h_lng = j["hint"] if j.get("hint") else (lat, lng)
    res = await geocoder.confirm(query, h_lat, h_lng, row["country"], routes=routes,
                                 **_rule_kw(strategy, context, accept_rule))
    hit = locate._confirmed(item, res, row["country"])
    cap = guards.route_radius(query)
    async with conn.transaction():
        await store.lock_destinations(conn)
        cur = await _still_due(conn, row)
        if cur is None:
            return {"outcome": "skipped"}
        km = round(guards.km(lat, lng, hit.lat, hit.lng), 1) if hit is not None else None
        if hit is not None and km <= cap:
            await store._write_point(conn, row["id"], hit, False)
            shown = await _reveal_if_policy_hidden(conn, row["id"], cur, "reconfirmed")
            fixed = hit.country != row["country"] and await _fix_country(conn, row, hit.country)
            await conn.execute(_STAMP_SQL, row["id"], _stamp("reconfirmed", moved_km=km, prev=_prev(row),
                                                             refused=j["reason"], accept_rule=accept_rule))
            return {"outcome": "reconfirmed", "moved_km": km, "country_fixed": bool(fixed),
                    **({"unhidden": True} if shown else {})}
        await _flag(conn, row["id"], cur, _keep_linear(row, REVIEW_WEAK_CONFIRM), hide_unconfirmed, "weak_confirm")
        meta = (res.meta if res is not None else {}) or {}
        extra = {}
        if hit is not None:      # a passing answer too far from the stored point: not moved without review
            extra = {"move_refused_km": km, "move_cap_km": cap, "candidate": [hit.lat, hit.lng]}
        await conn.execute(_STAMP_SQL, row["id"], _stamp("weak_confirm", refused=j["reason"],
                                                         geocoder=meta.get("outcome"), accept_rule=accept_rule,
                                                         **extra))
    return {"outcome": "weak_confirm", "refused": j["reason"], **({"move_refused_km": km} if hit else {})}


async def apply_row(conn, row: dict, routes: tuple, geocoder, *, hide_unconfirmed: bool, strategy: str = "v1",
                    context: Optional[str] = None, accept_rule: str = geomatch.ACCEPT_RULE_V1) -> dict:
    """One row (module docstring). Returns {outcome, ...}. Raises the geocoder's CreditLimitReached /
    GeocoderUnavailable (the run stops; the row is not stamped and is taken again next run)."""
    if is_confirmed_recheck(row):
        if not is_strict(accept_rule):     # never selected then (recheck_for); defensive: no write, no call
            return {"outcome": "skipped"}
        return await _apply_confirmed_recheck(conn, row, routes, geocoder, hide_unconfirmed=hide_unconfirmed,
                                              strategy=strategy, context=context, accept_rule=accept_rule)
    c = classify_row(row, routes)
    if c["non_place"]:
        reason = _keep_linear(row, f"{name_filter.NON_PLACE_PREFIX}:{c['non_place']}")
        async with conn.transaction():
            await store.lock_destinations(conn)
            cur = await _still_due(conn, row)
            if cur is None:
                return {"outcome": "skipped"}
            await _flag(conn, row["id"], cur, reason, hide_unconfirmed, "non_place")
            await conn.execute(_STAMP_SQL, row["id"], _stamp("non_place", cause=c["non_place"]))
        return {"outcome": "non_place", "cause": c["non_place"]}
    item = locate.Item((row["name_norm"], (row["country"] or "").lower(), row.get("locality_norm") or ""),
                       c["query"], row["country"], routes=routes)
    lat, lng = float(row["lat"]), float(row["lng"])
    res = await geocoder.confirm(c["query"], lat, lng, row["country"], routes=routes,
                                 **_rule_kw(strategy, context, accept_rule))
    hit = locate._confirmed(item, res, row["country"])
    async with conn.transaction():
        await store.lock_destinations(conn)
        cur = await _still_due(conn, row)
        if cur is None:            # fixed by an admin / merged / re-located meanwhile: nothing to do, no stamp
            return {"outcome": "skipped"}
        if hit is not None:
            shown = False
            if guards.may_overwrite(cur["located_by"], cur["geo_status"], hit.located_by):
                await store._write_point(conn, row["id"], hit, False)
                shown = await _reveal_if_policy_hidden(conn, row["id"], cur, "confirmed")
            fixed = hit.country != row["country"] and await _fix_country(conn, row, hit.country)
            km = round(guards.km(lat, lng, hit.lat, hit.lng), 1)
            await conn.execute(_STAMP_SQL, row["id"], _stamp("confirmed", moved_km=km, prev=_prev(row),
                                                             accept_rule=accept_rule))
            return {"outcome": "confirmed", "moved_km": km, "country_fixed": bool(fixed),
                    **({"unhidden": True} if shown else {})}
        meta = (res.meta if res is not None else {}) or {}
        if geomatch.needs_day_places(accept_rule) and _day_refused(res):
            # aa997.4: the geocoder's same-name feature is elsewhere and the day's evidence backs the model's point —
            # kept, listed for review, never hidden (a policy-hidden row is shown again)
            await conn.execute(_FLAG_SQL, row["id"], _keep_linear(row, REVIEW_SAME_NAME), False)
            shown = await _reveal_if_policy_hidden(conn, row["id"], cur, KEPT_MODEL)
            fixed = bool(c["hk_mo"]) and await _fix_country(conn, row, c["hk_mo"])
            await conn.execute(_STAMP_SQL, row["id"], _stamp(KEPT_MODEL, route_km=c["route_km"],
                                                             refused="day_disagree", geocoder=meta.get("outcome"),
                                                             accept_rule=accept_rule))
            return {"outcome": KEPT_MODEL, "country_fixed": bool(fixed), **({"unhidden": True} if shown else {})}
        outcome = locate.REVIEW_OFF_ROUTE if c["on_route"] is False else locate.REVIEW_LLM_POINT
        # an off-route flag wins; otherwise a reason the row already carries (unconfirmed_hard_type …) is kept
        reason = _keep_linear(row, outcome if outcome == locate.REVIEW_OFF_ROUTE
                              else (row.get("review_reason") or outcome))
        await _flag(conn, row["id"], cur, reason, hide_unconfirmed, outcome)
        fixed = bool(c["hk_mo"]) and await _fix_country(conn, row, c["hk_mo"])
        await conn.execute(_STAMP_SQL, row["id"], _stamp(outcome, route_km=c["route_km"],
                                                         geocoder=meta.get("outcome"), accept_rule=accept_rule))
    return {"outcome": outcome, "country_fixed": bool(fixed)}


async def apply(pool, geocoder, *, located_by: list[str], country: Optional[str], limit: int,
                hide_unconfirmed: bool = False, progress=None, recheck: Optional[list[str]] = None,
                strategy: str = "v1", max_requests: Optional[int] = None,
                accept_rule: str = geomatch.ACCEPT_RULE_V1) -> dict:
    """Up to `limit` rows. Result counts per outcome; `stopped_credit_limit` / `stopped_geocoder_unavailable` when the
    geocoder stopped the run, `stopped_request_cap` when this run's requests reached `max_requests` (checked before
    each row, so a run may end up to V2_MAX_QUERIES - 1 requests over); `remaining` = rows still to do."""
    from services.places.geocoder import CreditLimitReached, GeocoderUnavailable
    from shared.cost_guard import BudgetExceeded

    recheck = [v for v in recheck_for(recheck, accept_rule) if v not in RECHECK_DRY_RUN_ONLY]
    async with pool.acquire() as conn:
        rows = await candidates(conn, located_by=located_by, country=country, limit=limit, recheck=recheck)
        routes = await routes_by_destination(conn, rows, **_day_kw(accept_rule))
        towns = await towns_by_destination(conn, rows) if strategy == "v2" else {}
    out: dict = {"mode": "apply", "rule_version": RULE_VERSION, "located_by": located_by, "country": country,
                 "recheck": recheck_list(recheck), "strategy": strategy, "accept_rule": accept_rule,
                 "hide_unconfirmed": hide_unconfirmed,
                 "rows": len(rows), "done": 0, "errors": 0, **{o: 0 for o in OUTCOMES}, "unhidden": 0,
                 "moved_km_max": 0.0,
                 "samples": []}
    start_requests = getattr(geocoder, "requests", 0) or 0
    for n, row in enumerate(rows):
        if max_requests is not None and (getattr(geocoder, "requests", 0) or 0) - start_requests >= max_requests:
            out["stopped_request_cap"] = True
            break
        ctx = context_town(row["name"], towns.get(row["id"], []), row["lat"], row["lng"]) if towns else None
        try:
            async with pool.acquire() as conn:
                r = await apply_row(conn, row, routes.get(row["id"], ()), geocoder,
                                    hide_unconfirmed=hide_unconfirmed, strategy=strategy, context=ctx,
                                    accept_rule=accept_rule)
        except CreditLimitReached as e:
            out["stopped_credit_limit"] = True
            logger.warning("place_relocate_credit_limit", error=str(e)[:200])
            break
        except GeocoderUnavailable as e:
            out["stopped_geocoder_unavailable"] = True
            logger.error("place_relocate_geocoder_unavailable", error=str(e)[:200])
            break
        except BudgetExceeded:     # the run budget stops the run (the job alerts), not one row
            raise
        except Exception as e:  # noqa: BLE001 — one bad row must not stop the run; it is taken again next run
            out["errors"] += 1
            logger.warning("place_relocate_row_failed", destination_id=row["id"], error=str(e)[:200])
            continue
        out["done"] += 1
        out[r["outcome"]] += 1
        out["country_fixed"] += 1 if r.get("country_fixed") else 0
        out["unhidden"] += 1 if r.get("unhidden") else 0
        out["moved_km_max"] = max(out["moved_km_max"], r.get("moved_km") or 0.0)
        notable = (r["outcome"] not in ("confirmed", "kept", "kept_unjudged")
                   or (r.get("moved_km") or 0) > geomatch.ACCEPT_KM)
        if notable and len(out["samples"]) < SAMPLE:
            out["samples"].append({"id": row["id"], "name": row["name"], "country": row["country"], **r})
        if progress is not None and n % 20 == 19:
            await progress(step="relocate", done=n + 1, total=len(rows))
    async with pool.acquire() as conn:
        out["remaining"] = await count_candidates(conn, located_by=located_by, country=country, recheck=recheck)
    return out


# ── hide / unhide: the readers' "hide unconfirmed" policy (no call) ─────────────────────────────────────────────────

def hide_reasons(value) -> list[str]:
    """The reasons a hide run takes: the asked ones among HIDE_REASONS (+ the opt-in HIDE_NON_PLACE); default all of
    HIDE_REASONS. A non-place-only request is honoured (it is an explicit choice)."""
    allowed = HIDE_REASONS + (HIDE_NON_PLACE,)
    vals = [v for v in (value or HIDE_REASONS) if v in allowed]
    return sorted(set(vals)) or list(HIDE_REASONS)


async def hide(pool, *, reasons: list[str], country: Optional[str], confirm: bool, actor: str = "job",
               job_id: Optional[str] = None) -> dict:
    """Rows still flagged with one of `reasons` (geo_status 'ok'): counted by reason / country; with `confirm` each is
    set `suspect` (and its names), stamped `geocoder_meta.hidden = {by, reason, at, job, actor}` for unhide, in
    batches of HIDE_BATCH with one audit row each (AUDIT_HIDE_POLICY). A row a person restored (_RESTORED_TPL) is
    counted (`restored_skipped`), never hidden. No call."""
    async with pool.acquire() as conn:
        found = [dict(r) for r in await conn.fetch(_HIDE_CANDIDATES_SQL, [x for x in reasons if x != HIDE_NON_PLACE],
                                                   country, HIDE_NON_PLACE in reasons, HIDE_NON_PLACE)]
    rows = [r for r in found if not r.get("restored")]
    by_reason: dict[str, int] = {}
    by_country: dict[str, int] = {}
    for r in rows:
        by_reason[r["review_reason"]] = by_reason.get(r["review_reason"], 0) + 1
        by_country[r["country"]] = by_country.get(r["country"], 0) + 1
    out = {"mode": "hide", "rule_version": RULE_VERSION, "reasons": reasons, "country": country, "confirm": confirm,
           "rows": len(rows), "by_reason": by_reason,
           "by_country": dict(sorted(by_country.items(), key=lambda kv: -kv[1])), "hidden": 0,
           "restored_skipped": len(found) - len(rows), "audit_rows": 0,
           "samples": [{"id": r["id"], "name": r["name"], "country": r["country"], "reason": r["review_reason"]}
                       for r in rows[:SAMPLE]]}
    if not confirm:
        return out
    at = datetime.now(timezone.utc).isoformat()
    for start in range(0, len(rows), HIDE_BATCH):
        ids = []
        async with pool.acquire() as conn:
            async with conn.transaction():
                await store.lock_destinations(conn)
                for r in rows[start:start + HIDE_BATCH]:
                    stamp = json.dumps({"by": HIDE_STAMP, "reason": r["review_reason"], "at": at, "job": job_id,
                                        "actor": actor})
                    status = await conn.execute(_HIDE_ROW_SQL, r["id"], stamp, r["review_reason"])
                    if str(status).endswith(" 1"):
                        await conn.execute(_HIDE_NAMES_SQL, r["id"])
                        ids.append(r["id"])
                if ids:
                    await _audit_batch(conn, actor=actor, action=AUDIT_HIDE_POLICY,
                                       resource_id=f"hide:{country or '*'}:{job_id or '-'}",
                                       details={"reasons": list(reasons), "country": country, "job": job_id,
                                                "batch": start // HIDE_BATCH, "hidden": len(ids),
                                                "destination_ids": ids})
                    out["audit_rows"] += 1
        out["hidden"] += len(ids)
    return out


def hide_flagged_outcomes(value) -> list[str]:
    """The relocate outcomes a hide_flagged run takes: the asked ones among HIDE_FLAGGED_OUTCOMES, default
    HIDE_FLAGGED_DEFAULT (llm_point, off_route; weak_confirm only when asked)."""
    vals = [v for v in (value or HIDE_FLAGGED_DEFAULT) if v in HIDE_FLAGGED_OUTCOMES]
    return sorted(set(vals)) or sorted(HIDE_FLAGGED_DEFAULT)


def policy_stamps(value) -> list[str]:
    """The hide stamps an unhide reverts: the asked ones among POLICY_STAMPS, default all of them."""
    vals = [v for v in (value or POLICY_STAMPS) if v in POLICY_STAMPS]
    return sorted(set(vals)) or sorted(POLICY_STAMPS)


def _spread_sample(rows: list[dict], n: int) -> list[dict]:
    """Up to n rows taken round-robin over countries (rows are ordered by country, id): every country shows up."""
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r.get("country") or "", []).append(r)
    out: list[dict] = []
    while len(out) < n and any(by.values()):
        for k in sorted(by):
            if by[k] and len(out) < n:
                out.append(by[k].pop(0))
    return out


def _count(rows: list[dict], key: str) -> dict:
    c: dict = {}
    for r in rows:
        k = r.get(key) if r.get(key) is not None else "-"
        c[k] = c.get(k, 0) + 1
    return dict(sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0]))))


async def _audit_batch(conn, *, actor: str, action: str, resource_id: str, details: dict) -> None:
    """One acp_shared.audit_log row, written last in the batch's transaction (the table's trigger holds a lock)."""
    from services.acp_shared.audit_log import write_audit_log
    await write_audit_log(conn, tenant_id=None, actor=actor[:64], action=action, resource_type=AUDIT_RESOURCE,
                          resource_id=resource_id[:200], details=json.loads(json.dumps(details, default=str)))


def _json_obj(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = None
    return v if isinstance(v, dict) else {}


def _row_digest(r: dict) -> str:
    """The fields a confirm re-checks against its report: id | review_reason | outcome | located_by."""
    key = "|".join(str(r.get(k) or "") for k in ("id", "review_reason", "outcome", "located_by"))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def hide_flagged_selection(*, outcomes, stamp_versions, stamp_accept, located_by, country) -> dict:
    """The selection a hide_flagged report and its confirm must share (normalised the way the report stores it)."""
    return {"outcomes": hide_flagged_outcomes(outcomes), "stamp_versions": sorted(set(stamp_versions or [])),
            "stamp_accept": stamp_accept or None, "located_by": located_by_list(located_by),
            "country": country or None}


def check_hide_flagged_report(row: Optional[dict], selection: dict) -> Optional[str]:
    """Why the report job `row` ({status, result, age_s}) cannot back a confirm of `selection`, or None. Shared by
    the endpoint (the latest report) and the job (the report its payload names)."""
    if row is None:
        return "no_report"
    if row.get("status") in ("queued", "running"):
        return "report_pending"
    if row.get("status") != "succeeded":
        return "report_failed"
    res = _json_obj(row.get("result"))
    if res.get("mode") != "hide_flagged" or res.get("confirm"):
        return "not_a_report"
    got = hide_flagged_selection(outcomes=res.get("outcomes"), stamp_versions=res.get("stamp_versions"),
                                 stamp_accept=res.get("stamp_accept"), located_by=res.get("located_by"),
                                 country=res.get("country"))
    if got != selection:
        return "report_selection_differs"
    ids, digests = res.get("ids"), res.get("row_digests")
    if not isinstance(ids, list) or not isinstance(digests, list) or len(ids) != len(digests):
        return "report_without_ids"
    age = row.get("age_s")
    if age is None or float(age) > REPORT_MAX_AGE_S:
        return "report_stale"
    return None


async def hide_flagged(pool, *, outcomes: list[str], stamp_versions: Optional[list[str]] = None,
                       stamp_accept: Optional[str] = None, located_by: Optional[list[str]] = None,
                       country: Optional[str] = None, confirm: bool = False, dry_run_job: Optional[str] = None,
                       max_rows: Optional[int] = None, actor: str = "job", job_id: Optional[str] = None) -> dict:
    """Rows a relocate run stamped with one of `outcomes` and left on the model's point (module docstring): the report
    (no write; it stores the sorted `ids` and a `row_digests` entry each) or, with `confirm`, hidden in batches with an
    audit row each — only rows of the report `dry_run_job` (≤ `max_rows` of them) still selected with the same
    digest (id | review_reason | outcome | located_by); the others are `dropped_ids`, rows new since the report are
    counted (`new_since_report`) and never hidden. A confirm without its report, or with a report that is not a
    finished report of the same selection younger than REPORT_MAX_AGE_S, is refused. No call."""
    sel = hide_flagged_selection(outcomes=outcomes, stamp_versions=stamp_versions, stamp_accept=stamp_accept,
                                 located_by=located_by, country=country)
    async with pool.acquire() as conn:
        found = [dict(r) for r in await conn.fetch(_HIDE_FLAGGED_SQL, sel["outcomes"], sel["stamp_versions"],
                                                   sel["stamp_accept"], sel["located_by"], sel["country"])]
        report = None
        if confirm and dry_run_job:
            got = await conn.fetchrow(_REPORT_JOB_SQL, dry_run_job)
            report = dict(got) if got is not None else None
    rows = [r for r in found if not r.get("restored")]
    restored = len(found) - len(rows)
    out = {"mode": "hide_flagged", "rule_version": RULE_VERSION, **sel, "confirm": confirm, "rows": len(rows),
           "linked": sum(1 for r in rows if r.get("linked")),
           "by_country": _count(rows, "country"), "by_outcome": _count(rows, "outcome"),
           "by_reason": _count(rows, "review_reason"), "by_stamp_version": _count(rows, "stamp_v"),
           "by_located_by": _count(rows, "located_by"), "restored_skipped": restored, "hidden": 0, "audit_rows": 0,
           "samples": [{"id": r["id"], "name": r["name"], "country": r["country"], "outcome": r["outcome"],
                        "reason": r["review_reason"], "lat": r["lat"], "lng": r["lng"],
                        "route_km": r.get("route_km"), "linked": bool(r.get("linked"))}
                       for r in _spread_sample(rows, SAMPLE)]}
    if not confirm:
        if len(rows) <= MAX_REPORT_IDS:          # what a confirm may hide (rows sorted by country, id)
            ordered = sorted(rows, key=lambda r: r["id"])
            out["ids"] = [r["id"] for r in ordered]
            out["row_digests"] = [_row_digest(r) for r in ordered]
        else:
            out["ids_omitted"] = f"more than {MAX_REPORT_IDS} rows: narrow by country to confirm"
        return out
    out.update({"dry_run_job": dry_run_job, "max_rows": max_rows})
    if not dry_run_job or max_rows is None:
        return {**out, "status": "refused_no_report",
                "detail": "a confirm names its report (dry_run_job) and its row count (max_rows): POST the endpoint"}
    why = check_hide_flagged_report(report, sel)
    res = _json_obj(report["result"]) if not why else {}
    if not why and len(res["ids"]) != max_rows:
        why = "report_row_count_differs"
    if why:
        return {**out, "status": f"refused_{why}",
                "detail": "run the hide_flagged report of this selection again, then confirm it"}
    reported = dict(zip(res["ids"], res["row_digests"]))
    current = {r["id"]: r for r in rows}
    due = [r for r in rows if r["id"] in reported and reported[r["id"]] == _row_digest(r)]
    dropped = sorted(i for i in reported if i not in current or reported[i] != _row_digest(current[i]))
    out["new_since_report"] = sum(1 for r in rows if r["id"] not in reported)
    at = datetime.now(timezone.utc).isoformat()
    selection = {**sel, "dry_run_job": dry_run_job}
    for start in range(0, len(due), HIDE_BATCH):
        batch, ids = due[start:start + HIDE_BATCH], []
        async with pool.acquire() as conn:
            async with conn.transaction():
                await store.lock_destinations(conn)
                for r in batch:
                    stamp = json.dumps({"by": HIDE_FLAGGED_STAMP, "reason": r["review_reason"],
                                        "outcome": r["outcome"], "stamp_v": r.get("stamp_v"), "at": at,
                                        "job": job_id, "report": dry_run_job, "actor": actor})
                    status = await conn.execute(_HIDE_FLAGGED_ROW_SQL, r["id"], stamp, r["review_reason"],
                                                r["outcome"], r["located_by"])
                    if str(status).endswith(" 1"):
                        await conn.execute(_HIDE_NAMES_SQL, r["id"])
                        ids.append(r["id"])
                    else:
                        dropped.append(r["id"])
                if ids:
                    await _audit_batch(conn, actor=actor, action=AUDIT_HIDE,
                                       resource_id=f"hide_flagged:{country or '*'}:{job_id or '-'}",
                                       details={**selection, "job": job_id, "batch": start // HIDE_BATCH,
                                                "hidden": len(ids), "destination_ids": ids})
                    out["audit_rows"] += 1
        out["hidden"] += len(ids)
    dropped = sorted(set(dropped))
    out["changed_since_report"] = len(dropped)
    out["dropped_ids"] = dropped[:MAX_DROPPED_IDS]
    logger.info("place_relocate_hide_flagged", rows=len(rows), hidden=out["hidden"], dropped=len(dropped),
                new_since_report=out["new_since_report"], job_id=job_id)
    return out


async def unhide(pool, *, country: Optional[str], by: Optional[list[str]] = None,
                 destination_ids: Optional[list[str]] = None, actor: str = "job",
                 job_id: Optional[str] = None) -> dict:
    """Revert the rows a hide policy (`by`: its stamps, default every POLICY_STAMPS) marked that are still suspect for
    the reason it hid them: geo_status 'ok', their suspect names 'resolved', the stamp kept with `unhidden_at` /
    `unhidden_by` / `unhidden_job` (the record stays). A row whose review_reason changed since (another path flagged
    it) is left as it is (`kept_reason_changed`). One audit row per batch of rows reverted.
    `destination_ids` = only those rows: the admin's restore — reverted whatever their reason now
    (`restored_reason_changed`) and written `unhidden_by = 'restore:<actor>'`, which later hide / hide_flagged runs
    respect (_RESTORED_TPL). A bulk unhide (by stamp) writes 'unhide:<actor>' and is not sticky: the next hide run may
    hide those rows again. No call."""
    stamps = policy_stamps(by)
    ids_filter = list(destination_ids) if destination_ids else None
    explicit = ids_filter is not None
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_UNHIDE_ROWS_SQL, stamps, country, ids_filter)]
    due = [r for r in rows if explicit or r.get("same_reason") is not False]
    n, audits, at = 0, 0, datetime.now(timezone.utc).isoformat()
    who = f"{'restore' if explicit else 'unhide'}:{actor}"
    for start in range(0, len(due), HIDE_BATCH):
        ids = []
        async with pool.acquire() as conn:
            async with conn.transaction():
                await store.lock_destinations(conn)
                for r in due[start:start + HIDE_BATCH]:
                    status = await conn.execute(_UNHIDE_ROW_SQL, r["id"], at, who, explicit, job_id)
                    if str(status).endswith(" 1"):
                        await conn.execute(_UNHIDE_NAMES_SQL, r["id"])
                        ids.append(r["id"])
                if ids:
                    await _audit_batch(conn, actor=actor, action=AUDIT_UNHIDE,
                                       resource_id=f"unhide:{country or '*'}:{job_id or '-'}",
                                       details={"by": stamps, "country": country, "job": job_id,
                                                "restore": explicit, "batch": start // HIDE_BATCH,
                                                "unhidden": len(ids), "destination_ids": ids})
                    audits += 1
        n += len(ids)
    return {"mode": "unhide", "rule_version": RULE_VERSION, "country": country, "by": stamps,
            "destination_ids": len(ids_filter) if ids_filter else None, "rows": len(rows), "unhidden": n,
            "kept_reason_changed": len(rows) - len(due),
            "restored_reason_changed": sum(1 for r in due if r.get("same_reason") is False), "audit_rows": audits}
