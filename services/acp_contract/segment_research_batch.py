"""services/acp_contract/segment_research_batch.py — AA-648 batch search-demand research, AA-1055 buy side.

Why: DataForSEO prices **per task**, not per keyword (a volume task carries up to 1,000 keywords of one market for one
price). This module runs research as phases over the whole selection, so each paid task carries as much as it can:

  A. map         — the place's keyword map (acp_contract.place_keyword, CIS 393). A place with a map of the current
                   prompt version reuses it (no LLM, no Jev). Otherwise one LLM call per PROPOSE_CHUNK places proposes
                   up to KEYWORDS_PER_PLACE keywords (the plain place name first), Jev judges each (A3-3), and the map
                   is stored right away, the rejected keywords included, so they are never proposed nor bought again.
  B. volumes     — per market the place is stale in: read the fresh store in one query, buy the rest in tasks of up to
                   VOLUME_TASK_MAX cleaned keywords (live, or the Standard queue). A task DataForSEO refused is
                   split in two (down to SPLIT_MIN keywords) before its keywords count as failed; a queued task it
                   accepted (and charged) and that then failed or timed out is never split nor bought again in this
                   run. Each place's volume log row is written as soon as every keyword of it is answered in that
                   market. A place with no usable map is stale in every requested market (its keywords are new).
  C. serp        — ONE SERP per place, in the SERP market (US / English), for its best keyword with volume > 0 in any
                   market. Skipped (and logged fresh) when that keyword's row already holds a fresh SERP with both
                   PAA and related searches. A place with no keyword above 0 is logged fresh ("nothing to buy") only
                   when the SERP-market volume of EVERY keyword is known (fresh in the store or bought this run);
                   otherwise it waits. PAA, related searches and organic domains of the one answer are stored on the
                   keyword's SERP-market row with paa_retrieved_on / serp_location; the place's SERP log row is written
                   as soon as that answer is collected (in Standard-queue mode too, task by task).
  D. suggestions — off by default (AA-1055): places with no volume anywhere get "<place> <country>" seeds,
                   IDEAS_SEEDS_PER_TASK per keywords_for_keywords task (primary market).

No log row is ever written for a place whose purchase failed or was not made (AA-647): an abort, a budget stop or a
DFS error leave it stale for the next run, which resumes where this one stopped.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta

import structlog
from json_repair import repair_json

from services.acp_contract.segment_research import (
    FRESH_FOR,
    PlacePurchaseFailed,
    _RunGuard,
)
from services.seo_intelligence.dataforseo_client import DataForSEOClient, as_dfs_error
from services.seo_intelligence.dfs_queue import QueuedTaskFailed
from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP, LOCATION_CODE_TO_MARKET
from shared.cost_guard import DFS_CALL_ESTIMATE_USD, BudgetExceeded, RunBudget, dfs_research_prices
from shared.dfs_client.call_log import record_dfs_call_with_pool
from shared.llm_client.call_log import billed_fields, record_call_with_pool
from shared.llm_client.client import LLMClient
from shared.llm_client.decide import decide
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

PROPOSE_CHUNK = 15
KEYWORDS_PER_PLACE = 4
VOLUME_TASK_MAX = 1000
# A failed volume task is split in two and retried while its halves keep at least this many keywords (1,000 -> 500
# -> 250 -> 125): one bad keyword costs at most a few extra tasks, never one task per keyword.
SPLIT_MIN = 125
IDEAS_SEEDS_PER_TASK = 20
# A keywords_for_keywords task with only a few obscure seeds returns the seeds themselves and no ideas, at full task
# price (1, 2 and 4 seeds on 28–29/09/2026: 0 ideas each; 8 seeds: 22 with volume). Below this many seeds the task is
# not bought.
IDEAS_MIN_SEEDS = 5
# AA-693 — Jev gates (docs/architecture/at-series-v2-design.md §4.2 A3-3/A3-4). Questions live in
# shared.decision_question (migration 182); they only drop anything once set to `enforce` with calibrated floors
# (AA-661). Until then every verdict is logged as calibration data.
JEV_STAGE = "a3_research"
JEV_KEYWORD_Q = "a3_keyword_belongs"
JEV_IDEA_Q = "a3_idea_traveller"
JEV_CONCURRENCY = 8
SERP_CONCURRENCY = 4
# AA-1055 — one SERP per place, bought in this market only (content is written in English; T9 drops non-English PAA;
# a DE/FR/NL SERP returns mostly German/French/Dutch questions). Design section 4.
SERP_MARKET = "US"
# AA-1055: the stage of every shared.dfs_call_log row a research run writes (paid, refused, failed and store hits).
RESEARCH_STAGE = "a3_search_demand"
SERP_LANGUAGE = "en"
SERP_FRESH_FOR = timedelta(days=365)
# Measured on the 08/11/2026 run (154 US places): 2.565 keywords per place, 77.3 % of places had a keyword with volume
# (so a SERP). The "real" estimate uses them; the cap uses KEYWORDS_PER_PLACE and a SERP for every place.
REAL_KEYWORDS_PER_PLACE = 2.565
SERP_HIT_RATE = 0.773
# One batched propose call (~15 places): ~2k input + ~1k output tokens ≈ $0.007 at Haiku 4.5 prices.
LLM_PROPOSE_ESTIMATE_USD = 0.02
# Google Ads refuses keywords over 10 words or 80 characters.
MAX_KEYWORD_WORDS = 10
MAX_KEYWORD_CHARS = 80

_TIDY = re.compile(r"[^a-z0-9 ]+")
# Letters NFKD does not decompose to ASCII.
_FOLD = str.maketrans({"đ": "d", "Đ": "d", "ß": "ss", "æ": "ae", "ø": "o", "ł": "l", "œ": "oe", "þ": "th"})

PROPOSE_SYSTEM = """\
You propose what travellers type into a search engine about places on travel itineraries.

For EACH place you are given, return up to 4 search keywords:
- The first keyword is the place itself, written the way people search it ("taktsang monastery",
  not "the sacred Tiger's Nest"). Add the country only if the name alone is ambiguous.
- The others qualify the place with what the itineraries do there ("taktsang monastery hike",
  "paro festival dates"). Real search phrasing, never brochure language.
- Lowercase, no punctuation, 1-6 words each.

Respond with ONLY a JSON object, no markdown fences:
{"places": [{"place": "<the place exactly as given>", "keywords": ["...", "..."]}]}
"""
# AA-1055 — the map's prompt_version: a changed prompt re-proposes every place on its next run.
PROMPT_VERSION = "a3_propose@" + hashlib.sha256(PROPOSE_SYSTEM.encode("utf-8")).hexdigest()[:12]

_UPSERT_VOLUME_SQL = """
    INSERT INTO acp_contract.search_demand (keyword, market, search_volume, retrieved_on)
    VALUES ($1, $2, $3, now())
    ON CONFLICT (keyword, market) DO UPDATE SET
        search_volume = excluded.search_volume, retrieved_on = excluded.retrieved_on
"""
# AA-1055 — one SERP on the keyword's SERP-market row. A row created here has no measured volume yet: search_volume
# NULL and retrieved_on at the epoch, so the volume store sees it as stale and buys the volume on its next pass. An
# answer with no question keeps the row's earlier PAA (a SERP with no PAA box says nothing against it); related
# searches are always replaced (an empty list is a fresh answer, NULL means never stored).
_UPSERT_SERP_SQL = """
    INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask, retrieved_on,
                                            serp_domains, related_searches, paa_retrieved_on, serp_location)
    VALUES ($1, $2, NULL, $3::jsonb, 'epoch'::timestamptz, $4::jsonb, $5::jsonb, now(), $2)
    ON CONFLICT (keyword, market) DO UPDATE SET
        people_also_ask = CASE WHEN excluded.people_also_ask <> '[]'::jsonb THEN excluded.people_also_ask
                               ELSE acp_contract.search_demand.people_also_ask END,
        serp_domains = COALESCE(excluded.serp_domains, acp_contract.search_demand.serp_domains),
        related_searches = excluded.related_searches,
        paa_retrieved_on = excluded.paa_retrieved_on,
        serp_location = excluded.serp_location
"""
_PLACE_COUNTRY_SQL = """
    SELECT s.canonical_place, mode() WITHIN GROUP (ORDER BY rt.country) AS country
    FROM acp_contract.atom_segment s
    JOIN acp_contract.atom_segment_member m ON m.segment_id = s.segment_id
    JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = m.atom_id
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    WHERE ta.owner_scope = 'platform' AND s.canonical_place = ANY($1::text[])
    GROUP BY s.canonical_place
"""
LOG_RESEARCHED_SQL = """
    INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier, researched_at)
    VALUES ($1, $2, $3, now())
    ON CONFLICT (canonical_place, market, tier) DO UPDATE SET researched_at = now()
"""
_MAPS_SQL = """
    SELECT subject, keyword, rank, jev_zone, jev_mode, prompt_version
    FROM acp_contract.place_keyword
    WHERE subject_kind = 'place' AND subject = ANY($1::text[])
    ORDER BY subject, rank, keyword
"""
_DELETE_MAP_SQL = """
    DELETE FROM acp_contract.place_keyword
    WHERE subject_kind = 'place' AND subject = ANY($1::text[]) AND source IN ('llm_propose', 'place_name')
"""
_INSERT_MAP_SQL = """
    INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, rank, source, prompt_version,
                                            jev_zone, jev_mode, jev_probability, proposed_at)
    VALUES ('place', $1, $2, $3, $4, $5, $6, $7, $8, now())
    ON CONFLICT (subject_kind, subject, keyword) DO UPDATE SET
        rank = excluded.rank, source = excluded.source, prompt_version = excluded.prompt_version,
        jev_zone = excluded.jev_zone, jev_mode = excluded.jev_mode, jev_probability = excluded.jev_probability,
        proposed_at = excluded.proposed_at
"""
# serp_fresh: the row holds a SERP of the last SERP_FRESH_FOR with related searches; volume_fresh: its volume was
# measured within FRESH_FOR (a SERP-only row has retrieved_on at the epoch, so it is not).
_SERP_STATE_SQL = """
    SELECT keyword, market, search_volume,
           (paa_retrieved_on IS NOT NULL AND paa_retrieved_on > now() - $2::interval
            AND related_searches IS NOT NULL) AS serp_fresh,
           (retrieved_on > now() - $3::interval) AS volume_fresh
    FROM acp_contract.search_demand WHERE keyword = ANY($1::text[])
"""


@dataclass
class _Place:
    name: str
    actions: list[str]
    markets: list[str]                 # markets whose volume tier is stale and requested this run
    keywords: list[str] = field(default_factory=list)
    volumes: dict[str, dict[str, int | None]] = field(default_factory=dict)  # kw -> market -> vol
    failed: bool = False
    serp: bool = False                 # the SERP tier is stale and requested this run
    has_map: bool = False              # keywords came from a stored map of the current prompt version
    logged: set = field(default_factory=set)  # (market, tier) rows written this run
    changed: bool = False              # this run stored new demand data for the place (volume or SERP)
    verdicts: dict = field(default_factory=dict)  # keyword -> Jev Verdict of this run's proposal (for the map)

    def has_volume(self) -> bool:
        return any(v for per in self.volumes.values() for v in per.values())


def _tidy(text: str) -> str:
    """Lowercase ASCII words: accents folded ("Hội An" -> "hoi an", until AA-1055 it became "h i an"), everything
    that is not a letter or digit becomes a space."""
    folded = unicodedata.normalize("NFKD", (text or "").translate(_FOLD))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(_TIDY.sub(" ", folded.lower()).split())


def clean_keyword(text: str) -> str | None:
    """The keyword as it may be sent to DataForSEO, or None when it can not be (AA-1055: an unusable keyword fails the
    whole 1,000-keyword task): tidied, 1 to MAX_KEYWORD_WORDS words, at most MAX_KEYWORD_CHARS characters, not only
    digits."""
    kw = _tidy(text)
    if not kw or kw.replace(" ", "").isdigit():
        return None
    if len(kw.split()) > MAX_KEYWORD_WORDS or len(kw) > MAX_KEYWORD_CHARS:
        return None
    return kw


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _prices(queue: str) -> tuple[float, float]:
    """(volume task, SERP) unit price of the endpoint the run buys through (`queue` "live" | "standard"), from the
    one price table the run budget's check before every paid post reads too (shared/cost_guard.py)."""
    return dfs_research_prices(queue)


def estimate_plan(places: list[_Place], use_suggestions: bool = False, queue: str = "live") -> dict:
    """AA-1055 — the spend of one run over `places`, two figures (design section 6):
      - real: keywords per place = the stored map's size, else REAL_KEYWORDS_PER_PLACE (measured); a SERP for
        SERP_HIT_RATE of the places that need one;
      - cap:  KEYWORDS_PER_PLACE per place and a SERP for every place that needs one.
    Both ignore the store (a cached keyword is not bought): an upper bound for the paid part. The balance check and
    the USD cap read `dfs_usd_cap`. Both are priced at the unit prices of the endpoint the run buys through (`queue`,
    shared.cost_guard.dfs_research_prices), returned as `unit_usd`; keyword suggestions are always bought live."""
    volume_price, serp_price = _prices(queue)
    unit_usd = {"volume_task": volume_price, "serp": serp_price}
    if not places:
        return {"places": 0, "llm_calls": 0, "volume_tasks": 0, "volume_tasks_cap": 0, "serp_calls": 0,
                "serp_calls_cap": 0, "idea_tasks": 0, "dfs_usd": 0.0, "dfs_usd_cap": 0.0, "llm_usd": 0.0,
                "queue": queue, "unit_usd": unit_usd}
    real_kw: dict[str, float] = {}
    cap_kw: dict[str, int] = {}
    for p in places:
        size = len(p.keywords) if p.has_map else None
        for m in p.markets:
            real_kw[m] = real_kw.get(m, 0.0) + (size if size is not None else REAL_KEYWORDS_PER_PLACE)
            cap_kw[m] = cap_kw.get(m, 0) + (size if size is not None else KEYWORDS_PER_PLACE)
    volume_tasks = sum(math.ceil(n / VOLUME_TASK_MAX) for n in real_kw.values() if n > 0)
    volume_tasks_cap = sum(math.ceil(n / VOLUME_TASK_MAX) for n in cap_kw.values() if n > 0)
    serp_need = sum(1 for p in places if p.serp)
    serp_calls = math.ceil(serp_need * SERP_HIT_RATE)
    proposing = sum(1 for p in places if not p.has_map)
    llm_calls = math.ceil(proposing / PROPOSE_CHUNK)
    idea_tasks = math.ceil(len(places) / IDEAS_SEEDS_PER_TASK) if use_suggestions else 0
    ideas = idea_tasks * DFS_CALL_ESTIMATE_USD["keywords_for_keywords"]
    return {
        "places": len(places), "llm_calls": llm_calls,
        "volume_tasks": volume_tasks, "volume_tasks_cap": volume_tasks_cap,
        "serp_calls": serp_calls, "serp_calls_cap": serp_need, "idea_tasks": idea_tasks,
        "dfs_usd": round(volume_tasks * volume_price + serp_calls * serp_price + ideas, 4),
        "dfs_usd_cap": round(volume_tasks_cap * volume_price + serp_need * serp_price + ideas, 4),
        "llm_usd": round(llm_calls * LLM_PROPOSE_ESTIMATE_USD, 4), "queue": queue, "unit_usd": unit_usd,
    }


def estimate_cost(n_places: int, n_markets: int, use_suggestions: bool = False, queue: str = "live") -> dict:
    """The estimate for `n_places` places with no map, stale in `n_markets` markets and needing their SERP."""
    markets = [f"m{i}" for i in range(max(n_markets, 0))]
    return estimate_plan([_Place(f"p{i}", [], markets, serp=n_markets > 0) for i in range(max(n_places, 0))],
                         use_suggestions, queue)


def select_within_usd(places: list[_Place], max_usd: float | None, use_suggestions: bool = False,
                      queue: str = "live") -> list[_Place]:
    """AA-1055 — the longest prefix of `places` whose capped DFS estimate stays within `max_usd` (replaces the old
    200-place cap). None = no cap. The estimate grows with the prefix, so a binary search finds it."""
    if max_usd is None:
        return list(places)
    lo, hi = 0, len(places)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_plan(places[:mid], use_suggestions, queue)["dfs_usd_cap"] <= max_usd:
            lo = mid
        else:
            hi = mid - 1
    return list(places[:lo])


def _parse_proposals(raw: str) -> dict[str, list[str]]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = repair_json(text, return_objects=True)
    out: dict[str, list[str]] = {}
    items = parsed.get("places") if isinstance(parsed, dict) else None
    for item in items or []:
        if not isinstance(item, dict) or not item.get("place"):
            continue
        kws = [clean_keyword(k) for k in (item.get("keywords") or []) if isinstance(k, str)]
        out[str(item["place"]).casefold()] = [k for k in kws if k]
    return out


async def load_maps(pool, names: list[str]) -> dict[str, list[dict]]:
    """place -> its stored map rows (rank order). Empty for a place with no map."""
    if not names:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_MAPS_SQL, list(names))
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["subject"], []).append(dict(r))
    return out


def keywords_from_map(rows: list[dict]) -> list[str] | None:
    """The buyable keywords of a stored map of the current prompt version (enforced Jev rejects left out), or None
    when the map is missing or from another prompt version (then the place is proposed again)."""
    if not rows or any(r.get("prompt_version") != PROMPT_VERSION for r in rows):
        return None
    kws = [r["keyword"] for r in rows
           if not (r.get("jev_zone") == "reject" and r.get("jev_mode") == "enforce")]
    return [k for k in (clean_keyword(k) for k in kws) if k][:KEYWORDS_PER_PLACE]


async def _gate_keywords(places: list[_Place], pool) -> dict:
    """AA-693 (A3-3): before any DFS purchase, ask Jev whether each proposed keyword is about this particular place
    rather than the kind of thing it is (Ms. Thư's ticket 03: "Nyuto Hot Spring" → "hot springs" 368k/mo). The plain
    place name is never asked — it is the place by definition. A keyword is dropped only on an enforced, confident
    reject; grey/error/shadow keep it. AA-1055: every verdict is kept on the place (`verdicts`) for the map."""
    pairs = [(p, kw) for p in places if not p.failed for kw in p.keywords if kw != _tidy(p.name)]
    if not pairs:
        return {"jev_keywords_checked": 0, "jev_keywords_rejected": 0}
    sem = asyncio.Semaphore(JEV_CONCURRENCY)

    async def _one(p: _Place, kw: str):
        async with sem:
            return p, kw, await decide(JEV_STAGE, f"kw:{p.name}:{kw}", {"place": p.name, "keyword": kw},
                                       [JEV_KEYWORD_Q], pool=pool)

    rejected = 0
    for p, kw, dec in await asyncio.gather(*[_one(p, kw) for p, kw in pairs]):
        p.verdicts[kw] = dec.verdicts.get(JEV_KEYWORD_Q)
        if dec.rejected(JEV_KEYWORD_Q):
            p.keywords = [k for k in p.keywords if k != kw]
            rejected += 1
    return {"jev_keywords_checked": len(pairs), "jev_keywords_rejected": rejected}


async def _store_maps(places: list[_Place], proposed: dict[str, list[str]], pool) -> None:
    """Replace the proposed map of `places` (the plain name + the LLM's keywords with their Jev verdicts, rejected
    ones included). A map row of another source (seo_context_copy, country_activity) is never touched."""
    rows = []
    for p in places:
        plain = _tidy(p.name)
        verdicts = p.verdicts
        for rank, kw in enumerate(proposed.get(p.name, [])):
            v = verdicts.get(kw)
            if kw == plain:
                rows.append((p.name, kw, rank, "place_name", PROMPT_VERSION, "not_asked", None, None))
            else:
                rows.append((p.name, kw, rank, "llm_propose", PROMPT_VERSION, v.zone if v else None,
                             v.mode if v else None, v.probability if v else None))
    if not rows:
        return
    async with pool.acquire() as conn:
        await conn.execute(_DELETE_MAP_SQL, [p.name for p in places])
        await conn.executemany(_INSERT_MAP_SQL, rows)


async def _propose(places: list[_Place], pool, guard: _RunGuard, llm_budget: RunBudget | None) -> dict:
    """Phase A for the places without a usable map: propose, gate and store, one chunk at a time (a chunk's map is
    stored before the next chunk's LLM call, so a stopped job keeps what it already paid for)."""
    llm = LLMClient()
    calls, cost = 0, 0.0
    jev = {"jev_keywords_checked": 0, "jev_keywords_rejected": 0}
    for chunk in _chunks([p for p in places if not p.has_map], PROPOSE_CHUNK):
        if guard.aborted:
            for p in chunk:
                p.failed = True
            continue
        if llm_budget is not None:
            try:
                llm_budget.check(LLM_PROPOSE_ESTIMATE_USD)
            except BudgetExceeded as exc:
                guard.record(exc)
                for p in chunk:
                    p.failed = True
                continue
        listing = "\n".join(
            f"- {p.name} (itineraries: {', '.join(sorted(set(p.actions))[:6]) or 'visit'})" for p in chunk
        )
        request = LLMRequest(system_prompt=PROPOSE_SYSTEM, user_prompt=f"Places:\n{listing}",
                             model_tier="haiku", max_tokens=4096)
        resp = await asyncio.to_thread(llm.generate, request)
        calls += 1
        cost += resp.cost_usd
        if llm_budget is not None:
            llm_budget.charge(resp.cost_usd)
        proposals = _parse_proposals(resp.content)
        await record_call_with_pool(
            pool, stage="a3_search_demand", role="writer", model=resp.model_used,
            tokens_in=resp.input_tokens, tokens_out=resp.output_tokens, cost_usd=resp.cost_usd,
            quality_signal={"phase": "propose", "places": len(chunk), "parsed": len(proposals),
                            "prompt_version": PROMPT_VERSION},
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None), **billed_fields(resp),
        )
        proposed: dict[str, list[str]] = {}
        for p in chunk:
            kws = proposals.get(p.name.casefold()) or []
            plain = clean_keyword(p.name)
            # The plain place name is always researched, even if the LLM output was unusable.
            ordered = list(dict.fromkeys([k for k in ([plain] if plain else []) + kws if k]))
            p.keywords = ordered[:KEYWORDS_PER_PLACE]
            proposed[p.name] = list(p.keywords)
        stats = await _gate_keywords(chunk, pool)
        for k in jev:
            jev[k] += stats[k]
        try:
            await _store_maps(chunk, proposed, pool)
        except Exception as exc:  # noqa: BLE001 — the map is a saving, never a reason to lose the purchase
            logger.warning("place_keyword_store_failed", places=len(chunk), error=str(exc)[:200])
    return {"llm_calls": calls, "llm_usd": round(cost, 5), **jev}


async def _write_logs(pool, rows: list[tuple[str, str, str]]) -> None:
    if rows:
        async with pool.acquire() as conn:
            await conn.executemany(LOG_RESEARCHED_SQL, rows)


async def _buy_volumes(places: list[_Place], markets: list[tuple[int, str, str]], client: DataForSEOClient,
                       pool, guard: _RunGuard, queue=None) -> dict:
    """Phase B. `queue` = a dfs_queue.StandardQueue, None = the live endpoint."""
    stats = {"volume_tasks": 0, "keywords_bought": 0, "keywords_cached": 0, "volume_splits": 0}

    async def _market(loc: int, lang: str) -> None:
        """One market: read the fresh store, buy the rest, log each place as soon as it is complete."""
        market = LOCATION_CODE_TO_MARKET[loc]
        active = [p for p in places if not p.failed and market in p.markets and p.keywords]
        need = sorted({kw for p in active for kw in p.keywords})
        if not need:
            return
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT keyword, search_volume FROM acp_contract.search_demand "
                "WHERE market = $1 AND keyword = ANY($2::text[]) AND retrieved_on > now() - $3::interval",
                market, need, FRESH_FOR,
            )
        answers: dict[str, int | None] = {r["keyword"]: r["search_volume"] for r in rows}
        if answers:
            stats["keywords_cached"] += len(answers)
            await record_dfs_call_with_pool(
                pool, endpoint="search_volume_bulk", cache_hit=True, cost_usd=0.0, location_code=loc,
                keyword_count=len(answers), meta={"job": "segment_research", "phase": "volumes"},
                stage=client.stage or RESEARCH_STAGE,   # AA-1055: the run's stage, as on its paid rows
            )
        failed_kw: set[str] = set()
        bought: set[str] = set()

        async def _settle() -> None:
            """Write the volume log of every place of this market whose keywords are all answered."""
            done = []
            for p in active:
                if p.failed or (market, "volume") in p.logged:
                    continue
                if any(kw in failed_kw for kw in p.keywords):
                    p.failed = True
                    continue
                if all(kw in answers for kw in p.keywords):
                    for kw in p.keywords:
                        p.volumes.setdefault(kw, {})[market] = answers.get(kw)
                    p.changed = p.changed or any(kw in bought for kw in p.keywords)
                    p.logged.add((market, "volume"))
                    done.append((p.name, market, "volume"))
            await _write_logs(pool, done)

        async def _buy(chunk: list[str]) -> None:
            if guard.aborted:
                failed_kw.update(chunk)
                return
            try:
                if queue is not None:
                    volumes = await queue.volumes(chunk, loc, lang)
                else:
                    volumes = await client.fetch_volumes_bulk(chunk, loc, lang, raise_on_error=True)
            except BudgetExceeded as exc:
                guard.record(exc)
                failed_kw.update(chunk)
                return
            except Exception as exc:
                guard.record(exc)
                err = as_dfs_error(exc)
                # A queued task DataForSEO accepted was charged at post: it failed or timed out after that, so
                # posting its halves would pay for the same keywords again. Its keywords fail (no log row) and the
                # next run buys them.
                if err.fatal or guard.aborted or isinstance(err, QueuedTaskFailed) or len(chunk) < 2 * SPLIT_MIN:
                    failed_kw.update(chunk)
                    return
                # AA-1055 — a task DataForSEO refused is split in two before its keywords count as failed.
                stats["volume_splits"] += 1
                logger.warning("segment_research_volume_split", market=market, keywords=len(chunk),
                               error=str(err)[:200])
                mid = len(chunk) // 2
                await _buy(chunk[:mid])
                await _buy(chunk[mid:])
                return
            stats["volume_tasks"] += 1
            stats["keywords_bought"] += len(chunk)
            async with pool.acquire() as conn:
                await conn.executemany(_UPSERT_VOLUME_SQL, [(kw, market, volumes.get(kw)) for kw in chunk])
            answers.update({kw: volumes.get(kw) for kw in chunk})
            bought.update(chunk)
            await _settle()          # each place is logged as soon as it is complete

        await _settle()              # places fully answered by the store
        chunks = list(_chunks([kw for kw in need if kw not in answers], VOLUME_TASK_MAX))
        if queue is not None:
            # Queued tasks are worked on in parallel by DataForSEO: post them all, then wait (free polls). The
            # live endpoint stays one task at a time, so the budget check before each call stays exact.
            await asyncio.gather(*[_buy(chunk) for chunk in chunks])
        else:
            for chunk in chunks:
                await _buy(chunk)
        await _settle()

    for loc, _name, lang in markets:
        await _market(loc, lang)
    return stats


def _parse_serp(client: DataForSEOClient, data: dict) -> tuple[list[str], list[str], list[str]]:
    try:
        return client._parse_paa(data), client._parse_related(data), client._parse_organic_domains(data)
    except Exception:  # noqa: BLE001 — an unreadable answer stores nothing new
        return [], [], []


async def _store_serp(pool, keyword: str, market: str, paa: list[str], related: list[str],
                      domains: list[str]) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_UPSERT_SERP_SQL, keyword, market, json.dumps(paa[:10]),
                           json.dumps(domains) if domains else None, json.dumps(related[:10]))
        # AA-630 / AA-631 — the keyword's PAA and domains changed: the Segments that could land them recompute.
        try:
            from services.acp_contract.atom_ranking import (
                invalidate_contested_cache_for_keyword,
                invalidate_questions_cache_for_keyword,
            )
            if paa:
                await invalidate_questions_cache_for_keyword(conn, keyword)
            if domains:
                await invalidate_contested_cache_for_keyword(conn, keyword)
        except Exception as exc:  # noqa: BLE001 — best effort, never breaks the purchase
            logger.warning("serp_cache_invalidate_failed", keyword=keyword, error=str(exc)[:200])


async def _buy_serps(places: list[_Place], client: DataForSEOClient, pool, guard: _RunGuard, queue=None) -> dict:
    """Phase C: one SERP per place (module docstring)."""
    stats = {"serp_calls": 0, "serp_fresh_skips": 0, "serp_no_volume": 0, "serp_waiting_volume": 0}
    wanted = [p for p in places if p.serp and not p.failed and p.keywords]
    if not wanted:
        return stats
    loc = DFS_LOCATION_MAP[SERP_MARKET][0]
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SERP_STATE_SQL, sorted({kw for p in wanted for kw in p.keywords}), SERP_FRESH_FOR,
                                FRESH_FOR)
    best_volume: dict[str, int] = {}
    fresh: set[str] = set()
    known: set[str] = set()     # keywords whose SERP_MARKET volume is measured (None = DataForSEO has no row = 0)
    for r in rows:
        best_volume[r["keyword"]] = max(best_volume.get(r["keyword"], 0), r["search_volume"] or 0)
        if r["market"] == SERP_MARKET and r["serp_fresh"]:
            fresh.add(r["keyword"])
        if r["market"] == SERP_MARKET and r["volume_fresh"]:
            known.add(r["keyword"])
    for p in wanted:   # this run's answers too (a mock or a store write that has not landed)
        for kw, per in p.volumes.items():
            best_volume[kw] = max(best_volume.get(kw, 0), *[(v or 0) for v in per.values()], 0)
            if SERP_MARKET in per:
                known.add(kw)

    to_buy: list[tuple[_Place, str]] = []
    logs: list[tuple[str, str, str]] = []
    for p in wanted:
        ranked = sorted(((best_volume.get(kw, 0), -i, kw) for i, kw in enumerate(p.keywords)), reverse=True)
        top = ranked[0] if ranked else (0, 0, None)
        if top[0] <= 0:
            if all(kw in known for kw in p.keywords):
                # Nobody searches any keyword of the place where we measured each one: nothing to buy, fresh for a
                # year.
                stats["serp_no_volume"] += 1
                logs.append((p.name, SERP_MARKET, "serp"))
                p.logged.add((SERP_MARKET, "serp"))
            else:
                # A keyword's SERP-market volume is not known yet (a new map, a failed or skipped volume task, a
                # run without the volume tier): the place stays stale and the next run decides.
                stats["serp_waiting_volume"] += 1
            continue
        if top[2] in fresh:
            stats["serp_fresh_skips"] += 1
            logs.append((p.name, SERP_MARKET, "serp"))
            p.logged.add((SERP_MARKET, "serp"))
            continue
        to_buy.append((p, top[2]))
    await _write_logs(pool, logs)
    if not to_buy:
        return stats

    async def _done(p: _Place, kw: str, data: dict) -> None:
        paa, related, domains = _parse_serp(client, data)
        await _store_serp(pool, kw, SERP_MARKET, paa, related, domains)
        stats["serp_calls"] += 1
        p.changed = True
        p.logged.add((SERP_MARKET, "serp"))
        await _write_logs(pool, [(p.name, SERP_MARKET, "serp")])

    if queue is not None:
        if guard.aborted:
            for p, _kw in to_buy:
                p.failed = True
            return stats
        settled: set[int] = set()

        async def _collected(i: int, got) -> None:
            """Called by the queue as each task is collected: the paid SERP is stored and its place logged right
            away, so a worker restart during the long wait loses nothing already answered."""
            settled.add(i)
            p, kw = to_buy[i]
            if got is None or isinstance(got, Exception):
                if isinstance(got, Exception):
                    guard.record(got)        # a budget stop or a fatal answer aborts the run
                p.failed = True
                return
            try:
                await _done(p, kw, got)
            except Exception as exc:  # noqa: BLE001 — one failed store must not stop collecting the others
                p.failed = True
                logger.warning("segment_research_serp_store_failed", place=p.name, error=str(exc)[:200])

        try:
            await queue.serps([(kw, loc, SERP_LANGUAGE) for _p, kw in to_buy], on_result=_collected)
        except Exception as exc:
            guard.record(exc)
        for i, (p, _kw) in enumerate(to_buy):
            if i not in settled:     # never collected (a fatal stop before any post, a lost task)
                p.failed = True
        return stats

    sem = asyncio.Semaphore(SERP_CONCURRENCY)

    async def _one(p: _Place, kw: str) -> None:
        async with sem:
            if guard.aborted:
                p.failed = True
                return
            try:
                data = await client._serp_advanced(kw, loc, SERP_LANGUAGE)
            except Exception as exc:
                # AA-647 — a failed SERP is not "0 PAA": nothing stored, no log row, retried next run.
                guard.record(exc)
                p.failed = True
                logger.warning("segment_research_serp_failed", place=p.name, error=str(exc)[:200])
                return
            await _done(p, kw, data)

    await asyncio.gather(*[_one(p, kw) for p, kw in to_buy])
    return stats


async def _place_countries(pool, names: list[str]) -> dict[str, str]:
    """Most common tour country per canonical place (platform tours), e.g. {"Mongar": "bhutan"}."""
    if not names:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_PLACE_COUNTRY_SQL, names)
    return {r["canonical_place"]: _tidy(r["country"]) for r in rows if r["country"]}


def _idea_seed(place: _Place, country: str | None) -> str:
    """keywords_for_keywords seed for a zero-volume place: "<place> <country>".

    The place's own best keyword is a bad seed here — the place is only in this pass because that keyword had no
    volume, and Google Ads answers a task of only unknown seeds with the seeds themselves and no ideas (both runs on
    28/09/2026: 4/4 and 2/2 echoed back, 0 stored). Adding the country gives Google a term it knows ("mongar bhutan"
    40/mo where "mongar" alone had none). DFS rejects seeds over 10 words."""
    seed = _tidy(place.name)
    if country and country not in seed.split():
        seed = f"{seed} {country}"
    return " ".join(seed.split()[:10])


# AA-693 (S206) — a lodging word decides without Jev. The first enforced run (10 Bhutan places) stored "hotel bhutan",
# "druk hotel paro", "galing resort paro"… at Jev p 0.2–0.9 (the keyword carries a place name, so Jev reads it as a
# trip search). On the 132-row calibration sample this rule matches all 20 lodging rows and nothing else once
# "<airport/station> to hotel" (getting there) is excepted. Brand-only lodging names ("amankora", "como punakha") have
# no such word and are not caught here.
from services.seo_intelligence.seed_builder import is_lodging_search  # noqa: E402  (S207: shared rule)


async def _gate_ideas(rows: list[tuple[str, str, int]], seeds: list[str], pool) -> list[tuple[str, str, int]]:
    """AA-693 (A3-4): keep only keyword ideas a traveller would search to experience a destination — not
    hotel/resort/homestay or booking searches (S203 probe: `rkpo green resort phobjikha`). A lodging word drops the
    idea outright; the rest go to Jev under the same enforce-only rule as `_gate_keywords`."""
    rows = [r for r in rows if not is_lodging_search(r[0])]
    if not rows:
        return rows
    sem = asyncio.Semaphore(JEV_CONCURRENCY)
    context = ", ".join(seeds[:5])

    async def _one(row):
        async with sem:
            return row, await decide(JEV_STAGE, f"idea:{row[1]}:{row[0]}",
                                     {"keyword": row[0], "seed_places": context}, [JEV_IDEA_Q], pool=pool)

    return [row for row, dec in await asyncio.gather(*[_one(r) for r in rows]) if not dec.rejected(JEV_IDEA_Q)]


async def _buy_suggestions(places: list[_Place], markets: list[tuple[int, str, str]], client: DataForSEOClient,
                           pool, guard: _RunGuard) -> dict:
    loc, _name, lang = markets[0]  # primary market only (same rule as the per-place loop)
    market = LOCATION_CODE_TO_MARKET[loc]
    zero = [p for p in places if not p.failed and p.markets and not p.has_volume()]
    stats = {"idea_tasks": 0, "ideas_stored": 0}
    countries = await _place_countries(pool, [p.name for p in zero])
    for chunk in _chunks(zero, IDEAS_SEEDS_PER_TASK):
        if guard.aborted:
            for p in chunk:
                p.failed = True
            continue
        seeds = list(dict.fromkeys(_idea_seed(p, countries.get(p.name)) for p in chunk))
        if len(seeds) < IDEAS_MIN_SEEDS:
            stats["idea_tasks_skipped"] = stats.get("idea_tasks_skipped", 0) + 1
            logger.info("segment_research_ideas_skipped", seeds=len(seeds), min_seeds=IDEAS_MIN_SEEDS,
                        seed_sample=seeds[:5])
            continue
        try:
            ideas = await client.fetch_keyword_ideas_multi(seeds, loc, lang)
        except Exception as exc:
            guard.record(exc)
            for p in chunk:
                p.failed = True
            continue
        stats["idea_tasks"] += 1
        stats["ideas_returned"] = stats.get("ideas_returned", 0) + len(ideas)
        rows = [(kw, market, i.get("search_volume")) for i in ideas
                if i.get("keyword") and i.get("search_volume") and (kw := clean_keyword(i["keyword"]))]
        kept = await _gate_ideas(rows, seeds, pool)
        stats["jev_ideas_rejected"] = stats.get("jev_ideas_rejected", 0) + len(rows) - len(kept)
        rows = kept
        logger.info("segment_research_ideas_task", seeds=len(seeds), ideas_returned=len(ideas),
                    ideas_with_volume=len(rows), seed_sample=seeds[:5], sample=[r[0] for r in rows[:5]])
        if rows:
            async with pool.acquire() as conn:
                await conn.executemany(_UPSERT_VOLUME_SQL, rows)
            stats["ideas_stored"] += len(rows)
    return stats


async def research_batch(
    places: list[_Place],
    markets: list[tuple[int, str, str]],
    pool, client: DataForSEOClient, guard: _RunGuard, *,
    llm_budget: RunBudget | None = None,
    use_suggestions: bool = False,
    queue=None,
) -> dict:
    """Research `places` (selected, with their stale markets / SERP need and stored maps) in phases."""
    llm_stats = await _propose(places, pool, guard, llm_budget)
    volume_stats = await _buy_volumes(places, markets, client, pool, guard, queue)
    serp_stats = await _buy_serps(places, client, pool, guard, queue)
    idea_stats = (await _buy_suggestions(places, markets, client, pool, guard)
                  if use_suggestions else {"idea_tasks": 0, "ideas_stored": 0})
    idea_stats.setdefault("ideas_returned", 0)

    researched = [p for p in places if p.logged]
    complete = [p for p in places if not p.failed
                and all((m, "volume") in p.logged for m in p.markets)
                and (not p.serp or (SERP_MARKET, "serp") in p.logged)]
    logger.info("segment_research_batch_done", places=len(places), researched=len(researched),
                complete=len(complete), aborted=guard.aborted, **llm_stats, **volume_stats, **serp_stats,
                **idea_stats)
    return {
        "places_researched": len(researched),
        "places_complete": len(complete),
        "places_failed": sum(1 for p in places if p.failed),
        "places_changed": sorted(p.name for p in places if p.changed),
        "keywords_bought": volume_stats["keywords_bought"],
        "keywords_cached": volume_stats["keywords_cached"],
        "volume_tasks": volume_stats["volume_tasks"],
        "volume_splits": volume_stats["volume_splits"],
        **serp_stats,
        "idea_tasks": idea_stats["idea_tasks"],
        "ideas_stored": idea_stats["ideas_stored"],
        "ideas_returned": idea_stats["ideas_returned"],
        "jev_keywords_checked": llm_stats["jev_keywords_checked"],
        "jev_keywords_rejected": llm_stats["jev_keywords_rejected"],
        "jev_ideas_rejected": idea_stats.get("jev_ideas_rejected", 0),
        "maps_reused": sum(1 for p in places if p.has_map),
        "llm_calls": llm_stats["llm_calls"],
        "cost_usd": llm_stats["llm_usd"],
    }


__all__ = ["research_batch", "estimate_plan", "estimate_cost", "select_within_usd", "clean_keyword", "load_maps",
           "keywords_from_map", "PROMPT_VERSION", "SERP_MARKET", "SERP_FRESH_FOR", "PlacePurchaseFailed"]
