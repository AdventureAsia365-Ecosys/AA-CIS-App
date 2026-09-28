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

from services.seo_intelligence.dataforseo_client import DataForSEOClient, as_dfs_error
from services.seo_intelligence.seed_builder import (
    LOCATION_CODE_TO_MARKET,
    resolve_buyer_market,
    resolve_buyer_markets,
)
from shared.cost_guard import BudgetExceeded, RunBudget
from shared.llm_client.call_log import record_call_with_pool
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

# AA-649 — pre-call estimate for one research-loop Haiku turn. Observed average on 25/09/2026:
# $10.27 / 7,856 calls ≈ $0.0013; rounded up for the budget check.
LLM_CALL_ESTIMATE_USD = 0.005

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


class PlacePurchaseFailed(Exception):
    """AA-647 — a DataForSEO purchase for this place failed. Nothing about the place may be recorded
    as fresh: its `segment_research_log` row is not written, so the next run retries it."""


@dataclass
class _RunGuard:
    """AA-647 — run-level circuit breaker. A fatal DFS error (auth/payment, e.g. the balance ran
    out) stops the whole run: every place not yet started is skipped before its first LLM call.
    On 25/09/2026 the balance ran out mid-run and the old code kept going for ~2 more hours,
    caching 14,691 failed lookups as NULL volume."""
    aborted: bool = False
    reason: str | None = None

    def record(self, exc: BaseException) -> None:
        if isinstance(exc, BudgetExceeded):  # AA-649 — a budget stop ends the run like a fatal DFS error
            if not self.aborted:
                self.aborted = True
                self.reason = str(exc)
                logger.warning("segment_research_budget_stop", reason=self.reason)
            return
        err = as_dfs_error(exc) if isinstance(exc, Exception) else None
        if err is not None and err.fatal and not self.aborted:
            self.aborted = True
            self.reason = str(err)
            logger.error("segment_research_aborted", reason=self.reason, status_code=err.status_code)


class _VolumeBatcher:
    """Gathers concurrent `volumes` asks for ONE market into as few DataForSEO requests as
    possible — the asyncio-native equivalent of Ms. Thư's `CoalescingSearchDemand`, see this
    module's docstring item 1. The first ask into an empty window starts a linger timer; every
    ask that arrives before it fires joins the same batch; the timer firing sends ONE bulk
    `fetch_volumes_bulk()` call and resolves every waiter.

    AA-647 — a failed bulk call resolves every waiter with the exception (never with None), so
    the caller cannot mistake a failure for "no search volume".
    """

    def __init__(self, client: DataForSEOClient, location_code: int, language_code: str,
                 linger: float = 5.0, guard: _RunGuard | None = None) -> None:
        self._client = client
        self._location_code = location_code
        self._language_code = language_code
        self._linger = linger
        self._guard = guard
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
                raise_on_error=True,
            )
        except Exception as e:
            if self._guard is not None:
                self._guard.record(e)
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
    # AA-647 — a DFS purchase failed (or the run was aborted); no research_log row was written.
    failed: bool = False


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


async def _store_serp_domains(conn, keyword: str, market: str, domains: list[str]) -> None:
    """AA-631 (Debate `contested` standard) — persist this (keyword, market)'s ranked organic
    domains, same table/key as `_store_paa()`. Mirrors `_store_paa()`'s own shape: no-op on
    empty (nothing to store, matches `_store_paa()`'s early-return convention), plain UPDATE
    keyed on (keyword, market) (relies on `_store_volume()` having already inserted the row —
    identical assumption `_store_paa()` already makes, see its own docstring), and invalidates
    (sets NULL) the `contested` cache (migration 165) for every Segment that would even be a
    candidate for this keyword, same best-effort-never-break-the-harvest convention as AA-630's
    `questions_count` invalidation."""
    if not domains:
        return
    await conn.execute(
        """
        UPDATE acp_contract.search_demand
        SET serp_domains = $3::jsonb
        WHERE keyword = $1 AND market = $2
        """,
        keyword, market, json.dumps(domains),
    )
    try:
        from services.acp_contract.atom_ranking import invalidate_contested_cache_for_keyword
        await invalidate_contested_cache_for_keyword(conn, keyword)
    except Exception as exc:
        logger.warning("contested_cache_invalidate_failed", keyword=keyword, error=str(exc))


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
    ], return_exceptions=True)
    # AA-647 — only real answers are cached. A failed lookup is neither stored nor reported as
    # "no volume"; the place is marked failed so it stays stale for the next run.
    failures = [r for r in results if isinstance(r, BaseException)]
    async with pool.acquire() as conn:
        for (kw, market), volume in zip(to_buy, results):
            if isinstance(volume, BaseException):
                continue
            out[kw][market] = volume
            await _store_volume(conn, kw, market, volume)
    if failures:
        raise PlacePurchaseFailed(f"volumes: {failures[0]}") from failures[0]
    return out


async def _serp_tool(
    client: DataForSEOClient, keyword: str, market_codes: list[str],
    location_by_market: dict[str, tuple[int, str]], pool, guard: _RunGuard | None = None,
) -> list[str]:
    """PAA questions for one keyword, fanned out across every tenant market (docstring item 3),
    deduped. Always makes a real call per market — PAA/first-page freshness isn't cache-checked
    the way volume is (matches the reference repo's own SERP-vs-volume freshness split,
    `FRESH_FOR` governs the loop-level skip in `segment_research_log`, not a per-call cache
    here — a place researched this run always buys a fresh first page for its 2 chosen
    keywords).

    AA-631 — the same SERP response also carries ranked organic domains (Debate's `contested`
    standard's raw signal). Calls `client._serp_advanced()` directly (not the narrower
    `fetch_people_also_ask()` wrapper this used before AA-631) so ONE HTTP call feeds both
    `_store_paa()` and `_store_serp_domains()` — zero new DataForSEO cost, same "one SERP call
    serves both" principle `_serp_advanced()`'s own docstring already established for PAA+
    related-searches, now extended to a 3rd free rider."""
    seen: list[str] = []
    async with pool.acquire() as conn:
        for market in market_codes:
            location_code, language_code = location_by_market[market]
            try:
                serp = await client._serp_advanced(keyword, location_code, language_code)
            except Exception as exc:
                # AA-647 — a failed SERP call is not "0 PAA": nothing is stored for this market
                # and the place is marked failed (stays stale, retried next run).
                if guard is not None:
                    guard.record(exc)
                raise PlacePurchaseFailed(f"serp({keyword!r}, {market}): {exc}") from exc
            try:
                paa = client._parse_paa(serp)
                domains = client._parse_organic_domains(serp)
            except Exception:
                paa, domains = [], []
            for q in paa:
                if q not in seen:
                    seen.append(q)
            await _store_paa(conn, keyword, market, paa)
            await _store_serp_domains(conn, keyword, market, domains)
    return seen


async def _suggestions_tool(
    client: DataForSEOClient, keyword: str, primary_location: int, primary_language: str,
    guard: _RunGuard | None = None,
) -> list[str]:
    """Keyword suggestions, primary market only (docstring item 3). AA-647: a failed call raises
    PlacePurchaseFailed instead of looking like "no suggestions"."""
    try:
        ideas = await client.fetch_keyword_ideas(
            keyword, primary_location, primary_language, raise_on_error=True,
        )
    except Exception as exc:
        if guard is not None:
            guard.record(exc)
        raise PlacePurchaseFailed(f"suggestions({keyword!r}): {exc}") from exc
    return [i["keyword"] for i in ideas if i.get("keyword")][:10]


async def _research_place(
    place: str, actions: list[str], market_codes: list[str],
    markets: list[tuple[int, str, str]], batchers: dict[str, _VolumeBatcher],
    client: DataForSEOClient, pool, sem: asyncio.Semaphore,
    guard: _RunGuard | None = None, llm_budget: RunBudget | None = None,
) -> PlaceResearchResult:
    location_by_market = {
        LOCATION_CODE_TO_MARKET[loc]: (loc, lang) for loc, _name, lang in markets
    }
    primary_code, _primary_name, primary_lang = markets[0]
    guard = guard or _RunGuard()

    async with sem:
        if guard.aborted:
            # AA-647 — the run already hit a fatal DFS error: skip before spending an LLM call.
            return PlaceResearchResult(
                place=place, markets_researched=[], keywords_bought=0, llm_calls=0,
                cost_usd=0.0, skipped=True, failed=True,
            )
        llm_client = LLMClient()
        purchase_failed = False
        transcript_lines: list[str] = []
        keywords_named: set[str] = set()
        measured: dict[str, dict[str, int | None]] = {}
        serps_spent = 0
        suggestions_spent = 0
        llm_calls = 0
        cost_usd = 0.0

        for step_num in range(1, MAX_STEPS + 1):
            if guard.aborted:
                purchase_failed = True
                break
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
            if llm_budget is not None:
                try:
                    llm_budget.check(LLM_CALL_ESTIMATE_USD)  # AA-649
                except BudgetExceeded as exc:
                    guard.record(exc)
                    purchase_failed = True
                    break
            resp = await asyncio.to_thread(llm_client.generate, request)
            llm_calls += 1
            cost_usd += resp.cost_usd
            if llm_budget is not None:
                llm_budget.charge(resp.cost_usd)
            try:
                turn = _parse_step(resp.content)
            except ResearchLoopError:
                turn = None
            # AA-635 — this loop used to be the one LLMClient caller that never wrote
            # shared.llm_call_log, so its Bedrock spend showed on the AWS bill but not on the
            # External Spend page. quality_signal = which tool the turn chose (None = unparseable).
            # role="writer": the turn generates the keyword phrasing; llm_call_log.role has a
            # CHECK (writer/judge/validate, migration 137) so a new role name would be rejected.
            await record_call_with_pool(
                pool, stage="a3_search_demand", role="writer", model=resp.model_used,
                tokens_in=resp.input_tokens, tokens_out=resp.output_tokens, cost_usd=resp.cost_usd,
                quality_signal={"step": step_num, "tool": turn.tool if turn else None,
                                "parsed": turn is not None},
                stop_reason=getattr(resp, "stop_reason", None),
                account=getattr(resp, "satellite_account", None),
                fallback_used=getattr(resp, "fallback_used", None),
            )
            if turn is None:
                logger.warning("segment_research_bad_step", place=place, raw=resp.content[:200])
                break

            if turn.tool == "done":
                break

            try:
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
                    questions = await _serp_tool(
                        client, kw, market_codes, location_by_market, pool, guard,
                    )
                    transcript_lines.append(f"serp({kw!r}) -> {len(questions)} PAA questions")

                elif turn.tool == "suggestions":
                    any_volume = any(v for per in measured.values() for v in per.values() if v)
                    if suggestions_spent >= MAX_SUGGESTIONS or any_volume:
                        transcript_lines.append("You: (suggestions refused — budget spent, or "
                                                 "something already has volume) -> stop.")
                        continue
                    suggestions_spent += 1
                    seed = turn.keywords[0] if turn.keywords else place
                    ideas = await _suggestions_tool(client, seed, primary_code, primary_lang, guard)
                    transcript_lines.append(f"suggestions({seed!r}) -> {ideas}")
            except PlacePurchaseFailed as exc:
                # AA-647 — stop this place; whatever real answers were bought are already cached,
                # but the place itself is NOT marked researched, so the next run retries it.
                purchase_failed = True
                logger.warning("segment_research_place_failed", place=place, error=str(exc))
                break

        if not purchase_failed:
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
            llm_calls=llm_calls, cost_usd=round(cost_usd, 5), failed=purchase_failed,
        )
        return PlaceResearchResult(
            place=place, markets_researched=[] if purchase_failed else market_codes,
            keywords_bought=len(keywords_named), llm_calls=llm_calls, cost_usd=cost_usd,
            failed=purchase_failed,
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


_SCOPED_PLACES_SQL = """
    SELECT DISTINCT s.canonical_place, s.canonical_action
    FROM acp_contract.atom_segment s
    JOIN acp_contract.atom_segment_member m ON m.segment_id = s.segment_id
    JOIN acp_contract.tour_atoms ta ON ta.atom_id = m.atom_id
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    WHERE ta.owner_scope = 'platform'
      AND ($1::uuid[] IS NULL OR ta.tour_id = ANY($1::uuid[]))
      AND ($2::text IS NULL OR lower(rt.country) = lower($2::text))
"""


async def _places_in_scope(
    pool, places: list[str] | None, tour_ids: list[str] | None, country: str | None,
) -> dict[str, list[str]]:
    """AA-646 — canonical place -> its canonical actions, limited to the requested scope.
    No scope at all means the whole platform (the caller still caps it with `max_places`)."""
    async with pool.acquire() as conn:
        if tour_ids or country:
            rows = await conn.fetch(_SCOPED_PLACES_SQL, tour_ids or None, country or None)
        else:
            rows = await conn.fetch(
                "SELECT canonical_place, canonical_action FROM acp_contract.atom_segment"
            )
    wanted = set(places) if places else None
    by_place: dict[str, list[str]] = {}
    for r in rows:
        if wanted is not None and r["canonical_place"] not in wanted:
            continue
        by_place.setdefault(r["canonical_place"], []).append(r["canonical_action"])
    return by_place


async def run_segment_research(
    target_market: dict, pool, *,
    places: list[str] | None = None,
    tour_ids: list[str] | None = None,
    country: str | None = None,
    max_places: int | None = None,
    dry_run: bool = False,
    dfs_budget: RunBudget | None = None,
    llm_budget: RunBudget | None = None,
    strategy: str = "batch",
    use_suggestions: bool = True,
) -> dict:
    """Research the stale places in the requested scope, for the requested markets.

    AA-646 — this used to run after EVERY tenant T2 rewrite over the whole platform (AA-545 made
    Segments platform-wide, so "this tenant's Segments" became "every Segment"): one rewrite on
    25/09/2026 swept 2,215 places x AU/US/UK and spent $49 of DataForSEO. It is now admin-triggered
    only (`POST /admin/segment-research/run`), with an explicit scope and a `max_places` cap.
    `target_market` uses the tenant-config shape, e.g. `{"countries": ["US", "UK"]}`.

    `dry_run=True` returns the scope and stale counts without any LLM or DataForSEO call.

    AA-647 — a fatal DataForSEO error (auth/payment) aborts the run; places whose purchases failed
    are not marked researched, so they are retried by the next run.

    AA-649 — `dfs_budget` / `llm_budget` (shared.cost_guard.RunBudget) are checked before every
    paid DataForSEO / Haiku call; a breach stops the run the same way a fatal DFS error does.

    AA-648 — `strategy="batch"` (default) runs `segment_research_batch.research_batch()`: few large
    paid tasks instead of the per-place ReAct loop's many small ones. `strategy="loop"` keeps the
    original per-place loop (for comparison; slated for removal once batch is proven on real runs).
    """
    if strategy not in ("batch", "loop"):
        raise ValueError(f"unknown strategy {strategy!r}")
    markets = resolve_buyer_markets(target_market)
    market_codes = [LOCATION_CODE_TO_MARKET[loc] for loc, _name, _lang in markets]
    location_by_market = {
        LOCATION_CODE_TO_MARKET[loc]: (loc, lang) for loc, _name, lang in markets
    }

    by_place = await _places_in_scope(pool, places, tour_ids, country)

    stale: list[tuple[str, list[str]]] = []
    async with pool.acquire() as conn:
        for place in sorted(by_place):
            missing = await _stale_markets(conn, place, market_codes)
            if missing:
                stale.append((place, missing))
    stale_total = len(stale)
    if max_places is not None:
        stale = stale[:max(max_places, 0)]

    summary = {
        "markets": market_codes,
        "places_in_scope": len(by_place),
        "places_stale": stale_total,
        "places_selected": len(stale),
        "strategy": strategy,
    }
    if strategy == "batch":
        from services.acp_contract.segment_research_batch import estimate_cost
        summary["estimate"] = estimate_cost(len(stale), len(market_codes), use_suggestions)
    if dry_run or not stale:
        return {**summary, "places_researched": 0, "places_failed": 0, "cost_usd": 0.0,
                "aborted": False, "abort_reason": None}

    guard = _RunGuard()
    client = DataForSEOClient(budget=dfs_budget)

    if strategy == "batch":
        from services.acp_contract.segment_research_batch import research_batch
        batch_result = await research_batch(
            [(place, by_place[place], missing) for place, missing in stale], markets, pool, client, guard,
            llm_budget=llm_budget, use_suggestions=use_suggestions,
        )
        return {
            **summary, **batch_result,
            "aborted": guard.aborted,
            "abort_reason": guard.reason,
            "dfs_spent_usd": round(dfs_budget.run_spent, 4) if dfs_budget else None,
            "budgets": [b.summary() for b in (dfs_budget, llm_budget) if b is not None],
        }

    # strategy == "loop" — the original per-place ReAct loop.
    batchers = {
        market: _VolumeBatcher(client, location_by_market[market][0], location_by_market[market][1],
                               guard=guard)
        for market in market_codes
    }
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*[
        _research_place(
            place, by_place[place], missing_markets, markets, batchers, client, pool, sem, guard,
            llm_budget,
        )
        for place, missing_markets in stale
    ])

    return {
        **summary,
        "places_researched": sum(1 for r in results if not r.failed),
        "places_failed": sum(1 for r in results if r.failed),
        "keywords_bought": sum(r.keywords_bought for r in results),
        "llm_calls": sum(r.llm_calls for r in results),
        "cost_usd": round(sum(r.cost_usd for r in results), 5),
        "aborted": guard.aborted,
        "abort_reason": guard.reason,
        "dfs_spent_usd": round(dfs_budget.run_spent, 4) if dfs_budget else None,
        "budgets": [b.summary() for b in (dfs_budget, llm_budget) if b is not None],
    }


__all__ = ["run_segment_research", "resolve_buyer_market"]
