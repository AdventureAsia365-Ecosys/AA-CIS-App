"""services.acp_shared.question_topup — AA-779 (B): admin-only SERP top-up of real search questions.

The S245 r7 checkpoint gave every blog only 2 usable real questions: the places of the route had few
People Also Ask (PAA) questions in `acp_contract.search_demand`, and the town Mörön's keyword ('moron')
returned questions about the insult. This module buys ONE Google SERP per place that still has fewer
than `MIN_REAL_PER_PLACE` real questions, for the query '<place> <country>' (the country makes Google
read the town, not the word), with the PAA box expanded `people_also_ask_click_depth=2`, and stores the
questions in `search_demand` under `real_questions.topup_keyword(place, country)` and its own market
`real_questions.TOPUP_MARKET` (never the Segment research cache's US row). A place whose SERP came back
with no question still gets a row (empty PAA): it was bought, a rerun skips it. The chain reads the rows
back as the `serp_topup` tier (`real_questions.SerpTopupSource`); the same off-meaning, language and scope
filters apply.

Spend rules (AA-646, AA-779 scope):
  - Never called by a tenant action, a router or a job handler: only `scripts/aa779_serp_topup.py`,
    run by an admin (ECS exec). A unit test guards that nothing else imports this module.
  - Dry run is the default and makes no paid call: it prints the place list and the estimated cost.
  - `TOPUP_CAP_USD` ($3) is the cap for the top-up IN ALL, across every run: each call is logged to
    `shared.dfs_call_log` under its own endpoint label (`SERP_PAA_DEEP_ENDPOINT`), a run first sums what
    earlier runs spent there and may spend only min(cap, $3 − that); with nothing left it is refused. A
    caller cap above $3 is refused. The budget (shared.cost_guard, provider 'dfs', kind
    `aa779_serp_topup`) is checked BEFORE every call with a conservative estimate and charged with
    DataForSEO's real cost after it; the provider's daily DFS cap still applies.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Sequence

import structlog

from services.acp_shared.real_questions import (
    SOURCE_SERP_TOPUP, TOPUP_MARKET, QuestionScope, PlacePaaSource, SegmentMatchSource, SerpTopupSource,
    chain_filter_names, merge, place_words, topup_keyword,
)

logger = structlog.get_logger()

TOPUP_CAP_USD = 3.0           # hard cap for the whole top-up (AA-779 scope), never raised by a caller
TOPUP_CLICK_DEPTH = 2         # PAA box expanded two levels
TOPUP_LOCATION_CODE = 2840    # United States
TOPUP_LANGUAGE_CODE = "en"
# Pre-call estimate for one SERP with PAA click depth 2. DataForSEO bills a live advanced SERP at about
# $0.002 plus a surcharge per PAA click; 0.01 is deliberately high so the cap can never be overshot by
# the estimate (the real cost from the response is what is charged).
TOPUP_CALL_ESTIMATE_USD = 0.01
# shared.dfs_call_log label of the top-up's calls (dataforseo_client.SERP_PAA_DEEP_ENDPOINT; kept as a
# literal so this module imports no DFS client at load time).
SERP_PAA_DEEP_ENDPOINT = "serp_advanced_paa_deep"
MIN_REAL_PER_PLACE = 3        # a place with fewer real questions than this gets a SERP
JOB_KIND = "aa779_serp_topup"

# Places of blog work a tenant can still write (Subjects) and of blog requests already made.
_BLOG_WORK_SQL = """
    SELECT 'subject' AS kind, s.subject_id::text AS ref, r.tour_id, r.ordered_segment_ids AS segment_ids,
           s.segment_id
    FROM acp_shared.subject s
    LEFT JOIN acp_contract.route r ON r.route_id = s.route_id
    WHERE s.channel = 'blog' AND s.state IN ('proposed', 'picked')
    UNION ALL
    SELECT 'request', q.request_id::text, q.trip_id, q.route_segment_ids, NULL
    FROM acp_shared.angle_gate_request q
    WHERE q.channel = 'blog'
"""
# Each Segment (merged ids resolved through the alias) with its place and the countries of its
# atoms on that tour (any tour when the work has none).
_SEGMENT_PLACES_SQL = """
    SELECT s.segment_id, s.canonical_place,
           array_remove(array_agg(DISTINCT rt.country), NULL) AS countries
    FROM acp_contract.atom_segment s
    JOIN acp_contract.atom_segment_member m ON m.segment_id = s.segment_id
    JOIN acp_contract.tour_atoms ta ON ta.atom_id = m.atom_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    WHERE s.segment_id IN (
        SELECT COALESCE(al.segment_id_canonical, x.id)
        FROM unnest($1::text[]) AS x(id)
        LEFT JOIN acp_contract.atom_segment_alias al ON al.segment_id_old = x.id
    ) AND ($2::uuid IS NULL OR ta.tour_id = $2::uuid)
    GROUP BY s.segment_id, s.canonical_place
"""
# Market TOPUP_MARKET only (its own rows; no Segment research row is ever touched). An empty PAA is a
# "bought, nothing came back" marker that keeps an earlier non-empty PAA.
_STORE_SQL = """
    INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask, retrieved_on,
                                            serp_domains)
    VALUES ($1, $2, NULL, $3::jsonb, now(), $4::jsonb)
    ON CONFLICT (keyword, market) DO UPDATE SET
        people_also_ask = CASE WHEN jsonb_array_length(excluded.people_also_ask) > 0
                               THEN excluded.people_also_ask ELSE acp_contract.search_demand.people_also_ask END,
        serp_domains = COALESCE(excluded.serp_domains, acp_contract.search_demand.serp_domains),
        retrieved_on = excluded.retrieved_on
"""
# Keywords the top-up already bought (a row in its market, with or without questions).
_BOUGHT_SQL = """
    SELECT keyword FROM acp_contract.search_demand WHERE market = $1 AND keyword = ANY($2::text[])
"""
# What earlier runs spent (a call with no cost in its response counts at the estimate).
_PRIOR_SPEND_SQL = """
    SELECT COALESCE(sum(COALESCE(cost_usd, $2)), 0) AS spent
    FROM shared.dfs_call_log WHERE endpoint = $1 AND fetched_live
"""


@dataclass
class PlaceNeed:
    """One place of blog work and how many real questions the chain already has for it."""
    place: str
    country: str
    keyword: str
    query: str
    segment_ids: list[str] = field(default_factory=list)
    real_now: int = 0
    topped_up: bool = False      # the top-up already bought this place (a row, even with no question)
    used_by: int = 0             # how many Subjects / requests name this place

    @property
    def needs_serp(self) -> bool:
        return not self.topped_up and self.real_now < MIN_REAL_PER_PLACE


def _ids(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [str(v) for v in value or [] if v]


def place_key(place: str, country: str) -> str:
    return topup_keyword(place, country)


def collect_needs(work_places: Sequence[tuple[str, str, str]]) -> dict[str, PlaceNeed]:
    """(place, country, segment_id) rows of every blog work item → one PlaceNeed per
    '<place> <country>' key, with its Segments and how many items name it. Pure."""
    out: dict[str, PlaceNeed] = {}
    for place, country, segment_id in work_places:
        if not place or not country or not place_words([place], [country]):
            continue  # no distinctive word ('Lake', the country): the chain could never claim the row
        key = place_key(place, country)
        need = out.get(key)
        if need is None:
            need = out[key] = PlaceNeed(place=place, country=country, keyword=key, query=f"{place} {country}")
        if segment_id and segment_id not in need.segment_ids:
            need.segment_ids.append(segment_id)
        need.used_by += 1
    return out


def plan(needs: Sequence[PlaceNeed], cap_usd: float = TOPUP_CAP_USD,
         estimate_usd: float = TOPUP_CALL_ESTIMATE_USD) -> dict:
    """Which places get a SERP and what it costs at most. Places named by more blog work first, then
    the ones with fewer real questions; at most floor(cap / estimate) calls. Pure."""
    cap_usd = max(min(float(cap_usd), TOPUP_CAP_USD), 0.0)
    targets = sorted((n for n in needs if n.needs_serp), key=lambda n: (-n.used_by, n.real_now, n.keyword))
    max_calls = int(math.floor(cap_usd / estimate_usd + 1e-9)) if estimate_usd > 0 else 0
    chosen = targets[:max_calls]
    return {
        "places_seen": len(needs),
        "places_enough": sum(1 for n in needs if not n.needs_serp),
        "places_needing_serp": len(targets),
        "planned_calls": len(chosen),
        "estimated_cost_usd": round(len(chosen) * estimate_usd, 4),
        "cap_usd": cap_usd,
        "left_out_by_cap": [n.keyword for n in targets[max_calls:]],
        "targets": [asdict(n) for n in chosen],
    }


async def _count_real(conn, need: PlaceNeed) -> tuple[int, bool]:
    """How many real questions the chain's DB tiers already give this one place (after its
    language / off-meaning filters), and whether the top-up already bought it (a row in
    TOPUP_MARKET, even one with no question)."""
    scope = QuestionScope(segment_ids=tuple(need.segment_ids), places=(need.place,), countries=(need.country,))
    tiers = []
    topped = False
    for source in (SegmentMatchSource(), PlacePaaSource(), SerpTopupSource()):
        try:
            questions = await source.fetch(conn, scope)
        except Exception as e:  # noqa: BLE001 — a failed read counts as none (the place may get a SERP)
            logger.warning("topup_count_failed", source=source.name, place=need.place, error=str(e)[:200])
            questions = []
        if source.name == SOURCE_SERP_TOPUP and questions:
            topped = True
        tiers.append((source.name, questions))
    if not topped:
        # not caught: a failed read aborts the run (safer than buying a place twice)
        topped = bool(await conn.fetch(_BOUGHT_SQL, TOPUP_MARKET, [need.keyword]))
    return len(merge(tiers, **chain_filter_names(scope))), topped


async def find_needs(pool) -> list[PlaceNeed]:
    """Every place of blog work (Subjects a tenant can still write + blog requests) with its current
    real-question count. SELECTs only, $0."""
    rows: list[tuple[str, str, str]] = []
    async with pool.acquire() as conn:
        work = await conn.fetch(_BLOG_WORK_SQL)
        for w in work:
            seg_ids = _ids(w["segment_ids"]) or ([w["segment_id"]] if w["segment_id"] else [])
            if not seg_ids:
                continue
            for r in await conn.fetch(_SEGMENT_PLACES_SQL, seg_ids, w["tour_id"]):
                for country in r["countries"] or []:
                    rows.append((r["canonical_place"], country, r["segment_id"]))
        needs = list(collect_needs(rows).values())
        for need in needs:
            need.real_now, need.topped_up = await _count_real(conn, need)
    return needs


async def prior_spend(pool) -> float:
    """What earlier top-up runs spent (shared.dfs_call_log, endpoint SERP_PAA_DEEP_ENDPOINT)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_PRIOR_SPEND_SQL, SERP_PAA_DEEP_ENDPOINT, TOPUP_CALL_ESTIMATE_USD)
    return round(float(rows[0]["spent"] or 0) if rows else 0.0, 4)


async def store_questions(conn, keyword: str, questions: Sequence[str], domains: Sequence[str]) -> None:
    """The place's top-up row (market TOPUP_MARKET): its questions, or an empty list as the "bought,
    nothing came back" marker. No Segment `questions_count` cache is invalidated: question landing
    reads rows with a volume only (atom_ranking), and these have none — the chain reads them as is."""
    await conn.execute(_STORE_SQL, keyword, TOPUP_MARKET, json.dumps(list(questions)[:20]),
                       json.dumps(list(domains)) if domains else None)


async def run_topup(pool, *, dry_run: bool = True, cap_usd: float = TOPUP_CAP_USD,
                    client=None, budget=None) -> dict:
    """The top-up. `dry_run` (default): the plan only, no paid call, no write. A real run buys one
    SERP per planned place under a hard budget of min(cap_usd, $3 − what earlier runs spent) and
    stores what comes back (an empty marker row for a place with no question)."""
    if cap_usd > TOPUP_CAP_USD:
        raise ValueError(f"cap_usd {cap_usd} is above the AA-779 hard cap ${TOPUP_CAP_USD:.2f}")
    prior = await prior_spend(pool)
    left = round(TOPUP_CAP_USD - prior, 4)
    run_cap = min(float(cap_usd), left)
    needs = await find_needs(pool)
    result = plan(needs, max(run_cap, 0.0))
    result.update({"dry_run": dry_run, "prior_spent_usd": prior, "left_usd": max(left, 0.0)})
    if run_cap <= 0:
        result.update({"planned_calls": 0, "targets": [], "estimated_cost_usd": 0.0,
                       "refused": f"the AA-779 top-up already spent ${prior:.4f} of its ${TOPUP_CAP_USD:.2f} cap"})
        return result
    if dry_run:
        return result

    from services.seo_intelligence.dataforseo_client import DataForSEOClient, as_dfs_error
    from shared.cost_guard import BudgetExceeded, load_run_budget

    if budget is None:
        budget = await load_run_budget(pool, "dfs", JOB_KIND)
    budget.per_run_usd = min(run_cap, budget.per_run_usd) if budget.per_run_usd is not None else run_cap
    budget.hard_stop = True
    client = client or DataForSEOClient(budget=budget)
    if getattr(client, "budget", None) is None:
        client.budget = budget
    bought, stored, empty, failed, stop = [], 0, 0, [], None
    for target in result["targets"]:
        try:
            budget.check(TOPUP_CALL_ESTIMATE_USD)
        except BudgetExceeded as e:
            stop = e.message
            break
        try:
            questions, domains = await client.fetch_people_also_ask_deep(
                target["query"], click_depth=TOPUP_CLICK_DEPTH, location_code=TOPUP_LOCATION_CODE,
                language_code=TOPUP_LANGUAGE_CODE)
        except BudgetExceeded as e:
            stop = e.message
            break
        except Exception as e:  # noqa: BLE001 — one failed SERP: nothing stored, try the next place
            err = as_dfs_error(e)
            failed.append({"keyword": target["keyword"], "error": str(err)[:200]})
            if err.fatal:
                stop = f"fatal DataForSEO error: {err}"
                break
            continue
        bought.append({"keyword": target["keyword"], "query": target["query"], "questions": questions})
        async with pool.acquire() as conn:  # bought either way: a rerun must not buy it again
            await store_questions(conn, target["keyword"], questions, domains)
        if questions:
            stored += 1
        else:
            empty += 1
    result.update({"bought": bought, "stored_places": stored, "empty_places": empty, "failed": failed,
                   "stopped": stop, "spent_usd": round(budget.run_spent, 4),
                   "total_spent_usd": round(prior + budget.run_spent, 4)})
    return result


__all__ = [
    "TOPUP_CAP_USD", "TOPUP_CLICK_DEPTH", "TOPUP_CALL_ESTIMATE_USD", "MIN_REAL_PER_PLACE", "PlaceNeed",
    "SERP_PAA_DEEP_ENDPOINT", "collect_needs", "plan", "find_needs", "prior_spend", "store_questions",
    "run_topup",
]
