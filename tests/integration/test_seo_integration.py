"""
Integration tests — S2: SEO Intelligence Service
Tests: DataForSEO → seo_context insert, ElastiCache hit/miss, cache TTL
"""

import uuid
import json
import pytest
from conftest import SAMPLE_TOUR, SAMPLE_SEO, BATCH_ID, TENANT_ID


def _insert_raw_tour(db_conn, tour_id=None) -> str:
    """Helper: insert a raw tour, return tour_id."""
    tid = tour_id or str(uuid.uuid4())
    cur = db_conn.cursor()
    cur.execute("""
        INSERT INTO silver_aa_internal.raw_tours
            (tour_id, tenant_id, batch_id, country, src_name, src_subtitle, src_summary,
             src_highlights, src_itineraries, pipeline_status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, '[]', '[]', 'ingested')
    """, (tid, TENANT_ID, BATCH_ID, SAMPLE_TOUR["country"], SAMPLE_TOUR["src_name"],
          SAMPLE_TOUR["src_subtitle"], SAMPLE_TOUR["src_summary"]))
    cur.close()
    return tid


class TestSEOContextRepository:
    """Test: seo_context insert + FK integrity."""

    def test_insert_seo_context(self, db_conn):
        tour_id = _insert_raw_tour(db_conn)
        cur = db_conn.cursor()
        seo_id = str(uuid.uuid4())
        cache_key = f"{TENANT_ID}:vietnam:ha-long-bay:en_US"
        cur.execute("""
            INSERT INTO silver_aa_internal.seo_context
                (id, tour_id, keyword_search, keyword_ideas, demographics, trends,
                 cache_key, provider)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            seo_id, tour_id,
            SAMPLE_SEO["keyword_search"],
            json.dumps(SAMPLE_SEO["keyword_ideas"]),
            json.dumps(SAMPLE_SEO["demographics"]),
            json.dumps(SAMPLE_SEO["trends"]),
            cache_key,
            SAMPLE_SEO["provider"],
        ))
        cur.execute(
            "SELECT tour_id, keyword_search, provider FROM silver_aa_internal.seo_context WHERE id = %s",
            (seo_id,)
        )
        row = cur.fetchone()
        cur.close()
        assert str(row[0]) == tour_id
        assert row[1] == "ha long bay cruise vietnam"
        assert row[2] == "dataforseo"

    def test_seo_context_fk_rejects_unknown_tour(self, db_conn):
        """seo_context.tour_id must reference an existing raw_tours row."""
        cur = db_conn.cursor()
        with pytest.raises(Exception):  # FK violation
            cur.execute("""
                INSERT INTO silver_aa_internal.seo_context
                    (tour_id, keyword_search, keyword_ideas, demographics, trends, provider)
                VALUES (%s, %s, '[]', '{}', '{}', 'dataforseo')
            """, (str(uuid.uuid4()), str(uuid.uuid4())))
        cur.close()

    def test_pipeline_status_updated_after_seo(self, db_conn):
        tour_id = _insert_raw_tour(db_conn)
        cur = db_conn.cursor()
        cur.execute("""
            INSERT INTO silver_aa_internal.seo_context
                (tour_id, keyword_search, keyword_ideas, demographics, trends, provider)
            VALUES (%s, %s, '[]', '{}', '{}', 'dataforseo')
        """, (tour_id, "ha long bay cruise"))
        cur.execute("""
            UPDATE silver_aa_internal.raw_tours
            SET pipeline_status = 'seo_done'
            WHERE tour_id = %s
        """, (tour_id,))
        cur.execute(
            "SELECT pipeline_status FROM silver_aa_internal.raw_tours WHERE tour_id = %s",
            (tour_id,)
        )
        assert cur.fetchone()[0] == "seo_done"
        cur.close()

    def test_keyword_ideas_jsonb_query(self, db_conn):
        """JSONB field: query keyword with highest volume."""
        tour_id = _insert_raw_tour(db_conn)
        cur = db_conn.cursor()
        cur.execute("""
            INSERT INTO silver_aa_internal.seo_context
                (tour_id, keyword_search, keyword_ideas, demographics, trends, provider)
            VALUES (%s, %s, %s, '{}', '{}', 'dataforseo')
        """, (tour_id, "ha long bay cruise", json.dumps(SAMPLE_SEO["keyword_ideas"])))
        # JSONB query: find keyword with volume > 5000
        cur.execute("""
            SELECT id
            FROM silver_aa_internal.seo_context
            WHERE tour_id = %s
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(keyword_ideas) AS kw
                  WHERE (kw->>'volume')::int > 5000
              )
        """, (tour_id,))
        assert cur.fetchone() is not None
        cur.close()


class TestSEORedisCache:
    """Test: Redis cache hit/miss/TTL for SEO context."""

    def test_cache_miss_then_set(self, redis_client):
        cache_key = f"seo:{TENANT_ID}:vietnam:ha-long-bay:en_US"
        # Miss
        assert redis_client.get(cache_key) is None
        # Set
        redis_client.setex(cache_key, 86400, json.dumps(SAMPLE_SEO))
        # Hit
        cached = json.loads(redis_client.get(cache_key))
        assert cached["keyword_search"] == SAMPLE_SEO["keyword_search"]

    def test_cache_ttl_is_set(self, redis_client):
        cache_key = f"seo:{TENANT_ID}:cambodia:angkor-wat:en_US"
        # AA-625: SEO cache TTL is 7 days (604800s), raised from 24h — see
        # services/seo_intelligence/handler.py::_SEO_CACHE_TTL_SECONDS.
        ttl_seconds = 7 * 24 * 3600
        redis_client.setex(cache_key, ttl_seconds, json.dumps(SAMPLE_SEO))
        ttl = redis_client.ttl(cache_key)
        # TTL should be close to 7 days — allow 5s tolerance
        assert ttl_seconds - 10 <= ttl <= ttl_seconds

    def test_cache_key_format(self, redis_client):
        """Cache key convention: seo:{tenant_id}:{country}:{activity}:{market}"""
        keys = [
            f"seo:{TENANT_ID}:vietnam:ha-long-bay:en_US",
            f"seo:{TENANT_ID}:thailand:chiang-mai-trekking:en_AU",
            f"seo:{TENANT_ID}:cambodia:angkor-wat:en_UK",
        ]
        for k in keys:
            redis_client.setex(k, 3600, json.dumps({"stub": True}))

        # Scan pattern
        found = list(redis_client.scan_iter(f"seo:{TENANT_ID}:*"))
        assert len(found) == 3

    def test_cache_eviction_on_delete(self, redis_client):
        cache_key = f"seo:{TENANT_ID}:laos:luang-prabang:en_US"
        redis_client.setex(cache_key, 3600, json.dumps(SAMPLE_SEO))
        assert redis_client.get(cache_key) is not None
        redis_client.delete(cache_key)
        assert redis_client.get(cache_key) is None

    def test_pipeline_job_status_in_redis(self, redis_client):
        """Content UI polls pipeline progress via Redis every 2s."""
        job_key = f"pipeline:job:{BATCH_ID}"
        progress = {
            "batch_id": BATCH_ID,
            "total": 10,
            "processed": 3,
            "step": "seo_intelligence",
            "pct": 30,
        }
        redis_client.setex(job_key, 3600, json.dumps(progress))
        fetched = json.loads(redis_client.get(job_key))
        assert fetched["pct"] == 30
        assert fetched["step"] == "seo_intelligence"
