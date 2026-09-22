"""services/acp_contract/segment_research.py — AA-515, the demand-research loop.

Ported (adapted, not verbatim — see module docstring items below) from Ms. Thư's
aa-social-media `src/aa_social/stages/research.py`. Full evidence this design is built on:
`docs/claude_audit/AA-515-step0-ranking-investigation.md`,
`AA-515-step0b-demand-research-loop.md`, `AA-515-step0c-multimarket-schema.md`.

**One loop per place, not per Segment** (STEP0b Q1/Q4) — two Segments sharing a
`canonical_place` (e.g. "Kyoto — arrive" and "Kyoto — explore") are handed to ONE LLM ReAct
loop together, never researched twice. Attribution of a bought keyword back to a specific
Segment does NOT happen here — that is `atom_ranking.py`'s `_demand()` port, run fresh every
time ranking computes, reading straight from the `search_demand` cache (STEP0b: deliberately
NOT via embedding-match).

**Adaptations from the reference repo, disclosed (not silent)**:
1. Threading -> asyncio. Ms. Thư's `CoalescingSearchDemand` gathers concurrent OS-thread
   workers behind a `threading.Condition` + a shared rate-limit `_Throttle`. AA-CIS runs one
   asyncio event loop per ECS task, not a thread pool — `_VolumeBatcher` below is the
   asyncio-native equivalent (an `asyncio.Lock` + a linger `asyncio.sleep`), same shape ("one
   request per market for however many loops are asking at once"), not the same primitives.
2. Multi-market fan-out is a genuine AA-CIS extension, not in the reference repo the same way —
   Ms. Thư's brand sells to a FIXED 3 markets, baked into one `BrandAudience`; AA-CIS resolves
   a tenant's markets per-tenant via `resolve_buyer_markets()` (STEP0c) and fans every keyword
   lookup out across all of them, never just one.
3. `serp`/`suggestions` market scope (a decision this build makes, not specified verbatim by
   either STEP0 or the reference repo, which never had >1 market to choose from): `serp` fans
   out across EVERY tenant market per chosen keyword (PAA genuinely differs by market, and the
   call is cheap — $0.002/request per STEP0b) and stays capped at `MAX_SERPS` keyword-choices,
   not `MAX_SERPS × market count` calls. `suggestions` uses only the tenant's single
   highest-priority market (`resolve_buyer_market()`, singular) — a fallback-only tool, capped
   at exactly 1 call per place regardless of market count, to keep its cost bounded and because
   the literal "tối đa 1/place" cap in the build prompt reads as one call, not one per market.
4. No day-fingerprint at this layer — freshness is tracked per (canonical_place, market) in
   `segment_research_log`, checked BEFORE the LLM loop starts (so an already-fresh place costs
   nothing at all, not even a Bedrock call), independent of AA-508's per-day atomize fingerprint.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta

import structlog
from json_repair import repair_json

from services.seo_intelligence.dataforseo_client import DataForSEOClient
from services.seo_intelligence.seed_builder import (
    LOCATION_CODE_TO_MARKET,
    resolve_buyer_market,
    resolve_buyer_markets,
)
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

# Same caps as the reference repo's own constants (research.py), per the build prompt's literal
# instruction to keep them — see this module's docstring item 3 for the one place they diverge
# (serp/suggestions market scope, which the reference repo never had to decide).
MAX_STEPS = 4
MAX_KEYWORDS = 8
MAX_SERPS = 2
MAX_SUGGESTIONS = 1

# 182 days (~6 months) — STEP0b: "the horizon over which travel demand for a place actually
# moves", Ms. Thư's own measured constant, not re-derived here.
FRESH_FOR = timedelta(days=182)

# How many places' ReAct loops run concurrently. Ms. Thư tunes WORKERS=16 against her own
# per-minute DataForSEO account allowance and measured request-count curve (research.py:99-105)
# — not re-measured here (AA-CIS's own account/traffic hasn't been profiled the same way), kept
# far lower as a conservative starting point since this runs inline in a tenant-triggered
# pipeline step, not a standalone batch CLI. Revisit if a tenant's per-run place count grows
# large enough for this to matter.
CONCURRENCY = 4

_TIDY = re.compile(r"[^a-z0-9 ]+")

SYSTEM_PROMPT = """\
You research what people type into a search engine around one moment on a travel itinerary.

You work in a loop. Each turn: say what you concluded from what you have been shown, then \
choose one tool.

- `volumes` — monthly search volume for the keywords you name, looked up in every market this \
tenant sells to. Start here, and start with the place itself, plainly, before you qualify it \
with an activity. Knowing whether anyone searches the place at all is what tells you whether \
the long tail is worth buying.
- `serp` — the first page of results for a keyword, with the questions people also ask about \
it. The expensive call, and available only once a keyword here has measurable volume in at \
least one market. Two keywords per place, whichever steps you spend them on.
- `suggestions` — keywords a search engine associates with a seed. Use it when your own \
phrasings came back with zero volume everywhere and you need the words people really use. Once \
per place, and only while nothing here has measurable volume.
- `done` — stop.

Rules:
- Keywords are what a traveller types into a search engine, not what a brochure says. \
"nakasendo trail" and "magome to tsumago hike", never "unforgettable cedar forest journey".
- Never ask for a lookup you have already been shown the answer to.
- Qualify with the activity once the place has volume: "kyoto temples", "nakasendo luggage \
transfer". A place nobody searches does not get a long tail bought for it.
- Choose `done` as soon as another lookup would tell you nothing new. Stopping early is \
correct; padding the loop is not.

Respond with ONLY a JSON object, no markdown fences: {"thought": "...", "tool": "volumes" | \
"serp" | "suggestions" | "done", "keywords": ["..."]}. "keywords" is the list of keywords this \
tool call should use — empty for `done`.
"""

USER_PROMPT = """\
Place: {place}
What the itineraries do there: {actions}
Markets: {markets}

{transcript}

Step {step} of {max_steps}. What did you learn, and what next?
"""


@dataclass
class ResearchStep:
    thought: str
    tool: str
    keywords: list[str] = field(default_factory=list)


class ResearchLoopError(Exception):
    """The LLM response for one ReAct turn could not be parsed, even after json-repair
    salvage. Caught per-place by the caller — one place's malformed turn ends that place's
    loop early (whatever it already bought is still saved), never aborts the whole run."""


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def _parse_step(raw: str) -> ResearchStep:
    text = _strip_fences(raw)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = repair_json(text, return_objects=True)
    if not isinstance(parsed, dict) or parsed.get("tool") not in (
        "volumes", "serp", "suggestions", "done",
    ):
        raise ResearchLoopError(f"Could not parse a valid research step from: {raw[:300]!r}")
    keywords = parsed.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    return ResearchStep(
        thought=str(parsed.get("thought", "")),
        tool=parsed["tool"],
        keywords=[str(k) for k in keywords if k],
    )


class _VolumeBatcher:
    """Gathers concurrent `volumes` asks for ONE market into as few DataForSEO requests as
    possible — the asyncio-native equivalent of Ms. Thư's `CoalescingSearchDemand`, see this
    module's docstring item 1. The first ask into an empty window starts a linger timer; every
    ask that arrives before it fires joins the same batch; the timer firing sends ONE bulk
    `fetch_volumes_bulk()` call and resolves every waiter.
    """

    def __init__(self, client: DataForSEOClient, location_code: int, language_code: str,
                 linger: float = 5.0) -> None:
        self._client = client
        self._location_code = location_code
        self._language_code = language_code
        self._linger = linger
        self._lock = asyncio.Lock()
        self._pending: dict[str, list[asyncio.Future]] = {}
        self._flush_task: asyncio.Task | None = None

    async def ask(self, keyword: str) -> int | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        async with self._lock:
            self._pending.setdefault(keyword, []).append(fut)
            if self._flush_task is None:
                self._flush_task = asyncio.create_task(self._flush_after_linger())
        return await fut

    async def _flush_after_linger(self) -> None:
        await asyncio.sleep(self._linger)
        async with self._lock:
            batch, self._pending = self._pending, {}
            self._flush_task = None
        if not batch:
            return
        try:
            volumes = await self._client.fetch_volumes_bulk(
                list(batch.keys()), self._location_code, self._language_code,
            )
        except Exception as e:  # pragma: no cover — fetch_volumes_bulk itself never raises
            volumes = {}
            for futs in batch.values():
                for fut in futs:
                    if not fut.done():
                        fut.set_exception(e)
            return
        for keyword, futs in batch.items():
            value = volumes.get(keyword)
            for fut in futs:
                if not fut.done():
                    fut.set_result(value)


@dataclass
class PlaceResearchResult:
    place: str
    markets_researched: list[str]
    keywords_bought: int
    llm_calls: int
    cost_usd: float
    skipped: bool = False


async def _cached_volume(conn, keyword: str, market: str) -> tuple[bool, int | None]:
    """(found, volume). `found=False` means no fresh cached row — go buy it."""
    row = await conn.fetchrow(
        """
        SELECT search_volume FROM acp_contract.search_demand
        WHERE keyword = $1 AND market = $2 AND retrieved_on > now() - $3::interval
        """,
        keyword, market, FRESH_FOR,
    )
    if row is None:
        return False, None
    return True, row["search_volume"]


async def _store_volume(conn, keyword: str, market: str, volume: int | None) -> None:
    await conn.execute(
        """
        INSERT INTO acp_contract.search_demand (keyword, market, search_volume, retrieved_on)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (keyword, market) DO UPDATE SET
            search_volume = excluded.search_volume, retrieved_on = excluded.retrieved_on
        """,
        keyword, market, volume,
    )


async def _store_paa(conn, keyword: str, market: str, questions: list[str]) -> None:
    if not questions:
        return
    await conn.execute(
        """
        UPDATE acp_contract.search_demand
        SET people_also_ask = $3::jsonb
        WHERE keyword = $1 AND market = $2
        """,
        keyword, market, json.dumps(questions[:10]),
    )
    # AA-630 — this keyword's PAA just changed; invalidate (set NULL) the questions_count
    # cache (migration 163, AA-610 Sub 2) for every Segment that would even be a candidate for
    # it, so the next recompute lands this fresh PAA instead of silently reusing a stale count.
    # Best-effort: a failure here must never break the harvest itself (the fresh PAA is already
    # written above; a missed invalidation just means that Segment's questions axis stays as
    # stale as it already was, not a new problem this write introduces).
    try:
        from services.acp_contract.atom_ranking import invalidate_questions_cache_for_keyword
        await invalidate_questions_cache_for_keyword(conn, keyword)
    except Exception as exc:
        logger.warning("questions_cache_invalidate_failed", keyword=keyword, error=str(exc))


async def _volumes_tool(
    keywords: list[str], market_codes: list[str],
    batchers: dict[str, _VolumeBatcher], pool,
) -> dict[str, dict[str, int | None]]:
    """keyword -> {market: volume}, reading the cache first and only asking the batcher (which
    may make a real DataForSEO call) for whatever wasn't already fresh."""
    out: dict[str, dict[str, int | None]] = {kw: {} for kw in keywords}
    to_buy: list[tuple[str, str]] = []
    async with pool.acquire() as conn:
        for kw in keywords:
            for market in market_codes:
                found, volume = await _cached_volume(conn, kw, market)
                if found:
                    out[kw][market] = volume
                else:
                    to_buy.append((kw, market))
    if not to_buy:
        return out
    results = await asyncio.gather(*[
        batchers[market].ask(kw) for kw, market in to_buy
    ])
    async with pool.acquire() as conn:
        for (kw, market), volume in zip(to_buy, results):
            out[kw][market] = volume
            await _store_volume(conn, kw, market, volume)
    return out


async def _serp_tool(
    client: DataForSEOClient, keyword: str, market_codes: list[str],
    location_by_market: dict[str, tuple[int, str]], pool,
) -> list[str]:
    """PAA questions for one keyword, fanned out across every tenant market (docstring item 3),
    deduped. Always makes a real call per market — PAA/first-page freshness isn't cache-checked
    the way volume is (matches the reference repo's own SERP-vs-volume freshness split,
    `FRESH_FOR` governs the loop-level skip in `segment_research_log`, not a per-call cache
    here — a place researched this run always buys a fresh first page for its 2 chosen
    keywords)."""
    seen: list[str] = []
    async with pool.acquire() as conn:
        for market in market_codes:
            location_code, language_code = location_by_market[market]
            try:
                paa = await client.fetch_people_also_ask(keyword, location_code, language_code)
            except Exception:
                paa = []
            for q in paa:
                if q not in seen:
                    seen.append(q)
            await _store_paa(conn, keyword, market, paa)
    return seen


async def _suggestions_tool(
    client: DataForSEOClient, keyword: str, primary_location: int, primary_language: str,
) -> list[str]:
    """Keyword suggestions, primary market only (docstring item 3)."""
    try:
        ideas = await client.fetch_keyword_ideas(keyword, primary_location, primary_language)
    except Exception:
        ideas = []
    return [i["keyword"] for i in ideas if i.get("keyword")][:10]


async def _research_place(
    place: str, actions: list[str], market_codes: list[str],
    markets: list[tuple[int, str, str]], batchers: dict[str, _VolumeBatcher],
    client: DataForSEOClient, pool, sem: asyncio.Semaphore,
) -> PlaceResearchResult:
    location_by_market = {
        LOCATION_CODE_TO_MARKET[loc]: (loc, lang) for loc, _name, lang in markets
    }
    primary_code, _primary_name, primary_lang = markets[0]

    async with sem:
        llm_client = LLMClient()
        transcript_lines: list[str] = []
        keywords_named: set[str] = set()
        measured: dict[str, dict[str, int | None]] = {}
        serps_spent = 0
        suggestions_spent = 0
        llm_calls = 0
        cost_usd = 0.0

        for step_num in range(1, MAX_STEPS + 1):
            prompt = USER_PROMPT.format(
                place=place,
                actions=", ".join(sorted(set(actions))),
                markets=", ".join(market_codes),
                transcript="\n".join(transcript_lines) or "(nothing bought yet)",
                step=step_num, max_steps=MAX_STEPS,
            )
            request = LLMRequest(
                system_prompt=SYSTEM_PROMPT, user_prompt=prompt,
                model_tier="haiku", max_tokens=1024,
            )
            resp = await asyncio.to_thread(llm_client.generate, request)
            llm_calls += 1
            cost_usd += resp.cost_usd
            try:
                turn = _parse_step(resp.content)
            except ResearchLoopError:
                logger.warning("segment_research_bad_step", place=place, raw=resp.content[:200])
                break

            if turn.tool == "done":
                break

            if turn.tool == "volumes":
                remaining = MAX_KEYWORDS - len(keywords_named)
                asked = [k for k in turn.keywords if k not in keywords_named][:max(remaining, 0)]
                if not asked:
                    transcript_lines.append("You: (no new keywords — budget spent) -> stop.")
                    break
                keywords_named.update(asked)
                bought = await _volumes_tool(asked, market_codes, batchers, pool)
                measured.update(bought)
                for kw, per_market in bought.items():
                    summary = ", ".join(f"{m}: {v if v is not None else 'no data'}"
                                         for m, v in per_market.items())
                    transcript_lines.append(f"volumes({kw!r}) -> {summary}")

            elif turn.tool == "serp":
                if serps_spent >= MAX_SERPS:
                    transcript_lines.append("You: (serp budget spent) -> stop.")
                    break
                kw = turn.keywords[0] if turn.keywords else None
                has_volume = kw and any(
                    v for v in measured.get(kw, {}).values() if v
                )
                if not kw or not has_volume:
                    transcript_lines.append(
                        f"serp({kw!r}) refused — no measured volume for this keyword yet."
                    )
                    continue
                serps_spent += 1
                questions = await _serp_tool(client, kw, market_codes, location_by_market, pool)
                transcript_lines.append(f"serp({kw!r}) -> {len(questions)} PAA questions")

            elif turn.tool == "suggestions":
                any_volume = any(v for per in measured.values() for v in per.values() if v)
                if suggestions_spent >= MAX_SUGGESTIONS or any_volume:
                    transcript_lines.append("You: (suggestions refused — budget spent, or "
                                             "something already has volume) -> stop.")
                    continue
                suggestions_spent += 1
                seed = turn.keywords[0] if turn.keywords else place
                ideas = await _suggestions_tool(client, seed, primary_code, primary_lang)
                transcript_lines.append(f"suggestions({seed!r}) -> {ideas}")

        async with pool.acquire() as conn:
            for market in market_codes:
                await conn.execute(
                    """
                    INSERT INTO acp_contract.segment_research_log
                        (canonical_place, market, researched_at)
                    VALUES ($1, $2, now())
                    ON CONFLICT (canonical_place, market) DO UPDATE SET researched_at = now()
                    """,
                    place, market,
                )

        logger.info(
            "segment_research_place_done", place=place, keywords=len(keywords_named),
            llm_calls=llm_calls, cost_usd=round(cost_usd, 5),
        )
        return PlaceResearchResult(
            place=place, markets_researched=market_codes,
            keywords_bought=len(keywords_named), llm_calls=llm_calls, cost_usd=cost_usd,
        )


async def _stale_markets(conn, place: str, market_codes: list[str]) -> list[str]:
    fresh_rows = await conn.fetch(
        """
        SELECT market FROM acp_contract.segment_research_log
        WHERE canonical_place = $1 AND market = ANY($2::text[])
          AND researched_at > now() - $3::interval
        """,
        place, market_codes, FRESH_FOR,
    )
    fresh = {r["market"] for r in fresh_rows}
    return [m for m in market_codes if m not in fresh]


async def run_segment_research(tenant_id: str, target_market: dict, pool) -> dict:
    """Research every place behind this tenant's current Segments that isn't already fresh.

    Recomputes over the tenant's WHOLE Segment set (like `run_segment_matching()` and the
    ranking module below), not just one just-atomized tour — cheap to re-check because a
    place already fresh in `segment_research_log` costs one index lookup, not an LLM call.
    """
    markets = resolve_buyer_markets(target_market)
    market_codes = [LOCATION_CODE_TO_MARKET[loc] for loc, _name, _lang in markets]
    location_by_market = {
        LOCATION_CODE_TO_MARKET[loc]: (loc, lang) for loc, _name, lang in markets
    }

    async with pool.acquire() as conn:
        # AA-545 — atom_segment dropped tenant_id (platform-wide now); reads every platform
        # Segment regardless of `tenant_id` (still accepted as a parameter — resolves this
        # tenant's own `target_market`, out of AA-545's 4-layer scope to re-trigger from A3, see
        # docs/implementation-notes/AA-545.md Decision 3). `tenant_id` param itself is otherwise
        # unused now, kept for call-site compatibility per that same decision.
        rows = await conn.fetch(
            "SELECT canonical_place, canonical_action FROM acp_contract.atom_segment"
        )
    by_place: dict[str, list[str]] = {}
    for r in rows:
        by_place.setdefault(r["canonical_place"], []).append(r["canonical_action"])

    stale: list[tuple[str, list[str]]] = []
    async with pool.acquire() as conn:
        for place in by_place:
            missing = await _stale_markets(conn, place, market_codes)
            if missing:
                stale.append((place, missing))

    if not stale:
        return {"places_total": len(by_place), "places_researched": 0, "cost_usd": 0.0}

    client = DataForSEOClient()
    batchers = {
        market: _VolumeBatcher(client, location_by_market[market][0], location_by_market[market][1])
        for market in market_codes
    }
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*[
        _research_place(
            place, by_place[place], missing_markets, markets, batchers, client, pool, sem,
        )
        for place, missing_markets in stale
    ])

    return {
        "places_total": len(by_place),
        "places_researched": len(results),
        "keywords_bought": sum(r.keywords_bought for r in results),
        "llm_calls": sum(r.llm_calls for r in results),
        "cost_usd": round(sum(r.cost_usd for r in results), 5),
    }


__all__ = ["run_segment_research", "resolve_buyer_market"]
