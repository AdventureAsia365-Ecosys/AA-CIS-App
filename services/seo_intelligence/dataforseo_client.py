from shared.secrets import get_dataforseo_creds
from shared.dfs_client.call_log import record_dfs_call_sync, extract_cost
import httpx
import structlog

logger = structlog.get_logger()

DATAFORSEO_BASE = "https://api.dataforseo.com/v3"

# AA-197: defaults kept only as fallbacks; callers pass buyer-market resolved values.
DEFAULT_LOCATION_CODE = 2840          # United States
DEFAULT_LOCATION_NAME = "United States"
DEFAULT_LANGUAGE_CODE = "en"


class DataForSEOClient:
    def __init__(self, login: str = None, password: str = None,
                 tenant_id: str = None, tour_id: str = None):
        if not login or not password:
            login, password = get_dataforseo_creds()
        self.login    = login
        self.password = password
        # AA-618 — optional attribution context for shared.dfs_call_log. Set by the caller that
        # has it in scope (process_seo has tenant_id+tour_id; segment_research/research have
        # neither meaningfully — platform-wide). Every live HTTP method below logs one row with
        # the real DFS `cost` from the response; cache hits are logged by the caller instead.
        self.tenant_id = tenant_id
        self.tour_id   = tour_id

    def _log_live(self, endpoint: str, response: dict, keyword: str = None,
                  location_code: int = None, keyword_count: int = None) -> None:
        """AA-618 — record one live DFS call (fetched_live=True) with the real cost from the
        response. Fire-and-forget; never raises into the fetch path."""
        record_dfs_call_sync(
            endpoint=endpoint, fetched_live=True, cost_usd=extract_cost(response),
            tenant_id=self.tenant_id, tour_id=self.tour_id, keyword=keyword,
            location_code=location_code, keyword_count=keyword_count,
        )

    def _auth(self) -> tuple[str, str]:
        return (self.login, self.password)

    async def fetch_balance(self) -> dict:
        """AA-627 — read the DataForSEO account balance. This is the ONLY GET endpoint here
        (every other method POSTs a task) and it is FREE (DFS returns cost:0). Response shape:
        `{"tasks":[{"result":[{"money":{"balance": <float>, "total": <float>, "currency": ...}}]}]}`.

        Returns the parsed `money` dict (balance/currency/...). Raises on HTTP/parse error — the
        caller (the daily-check endpoint) decides how to handle a failed read; unlike the fetch
        path we do NOT swallow here because a silent failure is exactly the blind spot AA-627 is
        meant to remove. Logs one dfs_call_log row (cost 0) so External Spend still counts it."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{DATAFORSEO_BASE}/appendix/user_data",
                auth=self._auth(),
            )
            resp.raise_for_status()
            data = resp.json()
        # cost:0 — free read; logged so the External Spend call counts stay complete.
        self._log_live("appendix_user_data", data, keyword_count=0)
        money = self._parse_money(data)
        logger.info("dfs_balance_fetched", balance=money.get("balance"))
        return money

    def _parse_money(self, data: dict) -> dict:
        """Pull the money{} block out of a user_data response. Returns {} if the shape is
        unexpected (caller treats an empty/absent balance as a failed read, not $0)."""
        try:
            money = data["tasks"][0]["result"][0]["money"]
            return money if isinstance(money, dict) else {}
        except (KeyError, IndexError, TypeError):
            return {}

    async def fetch_keywords(
        self,
        seed: str,
        location_code: int = DEFAULT_LOCATION_CODE,
        location_name: str = DEFAULT_LOCATION_NAME,
        language_code: str = DEFAULT_LANGUAGE_CODE,
    ) -> dict:
        # AA-197: seed is pre-built by seed_builder — DO NOT append "tours" here.
        payload = [{
            "language_code": language_code,
            "location_code": location_code,
            "keywords":      [seed],
        }]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{DATAFORSEO_BASE}/keywords_data/google_ads/search_volume/live",
                auth=self._auth(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        self._log_live("search_volume", data, keyword=seed, location_code=location_code, keyword_count=1)
        logger.info("dfs_keywords_fetched", seed=seed, location=location_name)
        return self._parse_keywords(data)

    async def fetch_volumes_bulk(
        self,
        keywords: list[str],
        location_code: int = DEFAULT_LOCATION_CODE,
        language_code: str = DEFAULT_LANGUAGE_CODE,
    ) -> dict[str, int | None]:
        """AA-515 — search volume for MANY keywords, ONE market, in ONE request.

        Confirmed via DataForSEO's own docs (AA-515 STEP0c) that the Live search_volume
        endpoint takes exactly one task per call and one location per task, but that one task
        carries up to 1000 keywords at the SAME flat per-request price as one keyword — this is
        the coalescing lever services/acp_contract/segment_research.py's research loop batches
        concurrent asks onto, unlike fetch_keywords() above (single seed, truncates to top 10,
        built for T2's one-seed-per-tour use, not reused here).

        Returns every keyword in `keywords`, even ones DataForSEO didn't return a row for
        (mapped to None, "measured as no data" per this repo's own null-handling convention —
        services/acp_shared/dfs_relevance.py already treats a present-but-null search_volume as
        a real, distinct case from "no row at all"). Never raises — a request failure returns
        every keyword mapped to None, same self-contained-on-error shape fetch_keyword_ideas()
        above already uses, so a network hiccup degrades one place's research to "no measured
        demand this run" rather than aborting the whole loop.
        """
        if not keywords:
            return {}
        payload = [{
            "language_code": language_code,
            "location_code": location_code,
            "keywords":      keywords[:1000],
        }]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{DATAFORSEO_BASE}/keywords_data/google_ads/search_volume/live",
                    auth=self._auth(),
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("dfs_volumes_bulk_failed", count=len(keywords), error=str(e))
            return {kw: None for kw in keywords}

        self._log_live("search_volume_bulk", data, location_code=location_code,
                       keyword_count=len(keywords[:1000]))
        out: dict[str, int | None] = {kw: None for kw in keywords}
        try:
            results = data["tasks"][0]["result"] or []
        except (KeyError, IndexError, TypeError):
            results = []
        for row in results:
            if isinstance(row, dict) and row.get("keyword") in out:
                out[row["keyword"]] = row.get("search_volume")
        logger.info("dfs_volumes_bulk_fetched", count=len(keywords), location=location_code)
        return out

    async def _serp_advanced(
        self,
        seed: str,
        location_code: int = DEFAULT_LOCATION_CODE,
        language_code: str = DEFAULT_LANGUAGE_CODE,
    ) -> dict:
        # One SERP call serves both People-Also-Ask and related searches (cost-neutral).
        payload = [{
            "language_code": language_code,
            "location_code": location_code,
            "keyword":       seed,
        }]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{DATAFORSEO_BASE}/serp/google/organic/live/advanced",
                auth=self._auth(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        self._log_live("serp_advanced", data, keyword=seed, location_code=location_code, keyword_count=1)
        return data

    async def fetch_people_also_ask(
        self,
        seed: str,
        location_code: int = DEFAULT_LOCATION_CODE,
        language_code: str = DEFAULT_LANGUAGE_CODE,
    ) -> list[str]:
        data = await self._serp_advanced(seed, location_code, language_code)
        return self._parse_paa(data)

    async def fetch_related(
        self,
        seed: str,
        location_code: int = DEFAULT_LOCATION_CODE,
        language_code: str = DEFAULT_LANGUAGE_CODE,
    ) -> list[str]:
        data = await self._serp_advanced(seed, location_code, language_code)
        return self._parse_related(data)

    async def fetch_keyword_ideas(
        self,
        seed: str,
        location_code: int = DEFAULT_LOCATION_CODE,
        language_code: str = DEFAULT_LANGUAGE_CODE,
    ) -> list[dict]:
        # Real keyword ideas (~100s of rows) with volume/competition/cpc. Self-contained: [] on error.
        payload = [{
            "keywords":      [seed],
            "location_code": location_code,
            "language_code": language_code,
            "limit":         25,
        }]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{DATAFORSEO_BASE}/keywords_data/google_ads/keywords_for_keywords/live",
                    auth=self._auth(),
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("dfs_ideas_failed", error=str(e))
            return []
        self._log_live("keywords_for_keywords", data, keyword=seed, location_code=location_code, keyword_count=1)
        return self._parse_keyword_ideas(data)

    async def fetch_all(
        self,
        seed: str,
        location_code: int = DEFAULT_LOCATION_CODE,
        location_name: str = DEFAULT_LOCATION_NAME,
        language_code: str = DEFAULT_LANGUAGE_CODE,
        activity: str = None,
    ) -> dict:
        try:
            keywords = await self.fetch_keywords(seed, location_code, location_name, language_code)
        except Exception as e:
            logger.warning("dfs_keywords_failed", error=str(e))
            keywords = {}

        paa: list[str] = []
        related: list[str] = []
        try:
            # Single SERP call → parse PAA + related (no second HTTP request).
            serp = await self._serp_advanced(seed, location_code, language_code)
            paa = self._parse_paa(serp)
            related = self._parse_related(serp)
        except Exception as e:
            logger.warning("dfs_serp_failed", error=str(e))

        # AA-197: real keyword ideas (full dicts w/ volume/competition/cpc) — never raises.
        keyword_ideas = await self.fetch_keyword_ideas(seed, location_code, language_code)

        keywords = keywords if isinstance(keywords, dict) else {}
        top_keywords = keywords.get("top_keywords", [])
        # AA-197 #4: promote ideas to primary keywords when search_volume returns none,
        # so prompts.py always has a keyword to lead with.
        if not top_keywords and keyword_ideas:
            top_keywords = [i["keyword"] for i in keyword_ideas[:10]]
            keywords = {**keywords, "top_keywords": top_keywords}

        return {
            "keywords":         keywords,
            "people_also_ask":  paa,
            "related_keywords": related,
            "keyword_ideas":    keyword_ideas,
            "destination":      seed,
            "activity":         activity,
        }

    def _parse_keywords(self, data: dict) -> dict:
        try:
            results = data["tasks"][0]["result"] or []
            # DataForSEO search_volume returns list of keyword objects directly
            items = [r for r in results if isinstance(r, dict) and "keyword" in r]
            if not items:
                return {}
            return {
                "top_keywords":   [i["keyword"] for i in items[:10]],
                "search_volumes": {i["keyword"]: i.get("search_volume", 0) for i in items[:10]},
            }
        except (KeyError, IndexError, TypeError):
            return {}

    def _parse_paa(self, data: dict) -> list[str]:
        questions = []
        try:
            items = data["tasks"][0]["result"][0]["items"]
            for item in items:
                if item.get("type") == "people_also_ask":
                    for q in item.get("items", []):
                        questions.append(q.get("title", ""))
        except (KeyError, IndexError, TypeError):
            pass
        return [q for q in questions if q][:10]

    def _parse_keyword_ideas(self, data: dict) -> list[dict]:
        # keywords_for_keywords: tasks[0].result[] flat list of idea objects. Dedupe casefold, ≤25.
        try:
            results = data["tasks"][0]["result"] or []
        except (KeyError, IndexError, TypeError):
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for el in results:
            if not isinstance(el, dict):
                continue
            kw = el.get("keyword")
            if not kw:
                continue
            key = kw.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "keyword":           kw,
                "search_volume":     el.get("search_volume"),
                "competition":       el.get("competition"),
                "competition_index": el.get("competition_index"),
                "cpc":               el.get("cpc"),
            })
            if len(out) >= 25:
                break
        return out

    def _parse_related(self, data: dict) -> list[str]:
        related = []
        try:
            items = data["tasks"][0]["result"][0]["items"]
            for item in items:
                if item.get("type") == "related_searches":
                    for r in item.get("items", []):
                        # related_searches items are plain strings or {title}/{keyword} dicts
                        if isinstance(r, str):
                            related.append(r)
                        elif isinstance(r, dict):
                            related.append(r.get("title") or r.get("keyword") or "")
        except (KeyError, IndexError, TypeError):
            pass
        return [r for r in related if r][:10]
