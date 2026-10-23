import asyncpg
import structlog

logger = structlog.get_logger()


class PublishedCatalogRepository:
    """Repository for gold_{tenant}.published_tours table."""

    def __init__(self, conn: asyncpg.Connection, tenant_slug: str = "aa_internal"):
        self.conn = conn
        self.silver = f"silver_{tenant_slug}"
        self.gold   = f"gold_{tenant_slug}"

    async def insert(self, data: dict) -> str:
        row = await self.conn.fetchrow(f"""
            INSERT INTO {self.gold}.published_tours (
                tour_id, generated_content_id, tenant_id,
                aa_name, aa_subtitle, aa_summary, aa_description,
                aa_highlights, aa_itineraries, mobile_card_text,
                seo_title, seo_meta, seo_keywords_used, og_tags,
                quality_score, quality_score_id,
                s3_gold_path, approved_by
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, $18
            )
            ON CONFLICT (tour_id) DO UPDATE SET
                generated_content_id = EXCLUDED.generated_content_id,
                tenant_id            = EXCLUDED.tenant_id,
                aa_name              = EXCLUDED.aa_name,
                aa_subtitle          = EXCLUDED.aa_subtitle,
                aa_summary           = EXCLUDED.aa_summary,
                aa_description       = EXCLUDED.aa_description,
                aa_highlights        = EXCLUDED.aa_highlights,
                aa_itineraries       = EXCLUDED.aa_itineraries,
                mobile_card_text     = EXCLUDED.mobile_card_text,
                seo_title            = EXCLUDED.seo_title,
                seo_meta             = EXCLUDED.seo_meta,
                seo_keywords_used    = EXCLUDED.seo_keywords_used,
                og_tags              = EXCLUDED.og_tags,
                quality_score        = EXCLUDED.quality_score,
                quality_score_id     = EXCLUDED.quality_score_id,
                s3_gold_path         = EXCLUDED.s3_gold_path,
                approved_by          = EXCLUDED.approved_by,
                -- AA-645 d06 (CIS 273): a new version of a published tour keeps its first published_at
                -- (Browse pool / TripPlanner order); the change is recorded in updated_at. A first
                -- publish (the INSERT branch) gets both from their now() defaults.
                updated_at           = NOW()
            RETURNING id::text
        """,
            data["tour_id"],
            data["generated_content_id"],
            data.get("tenant_id", "00000000-0000-0000-0000-000000000001"),
            data["aa_name"],
            data.get("aa_subtitle"),
            data.get("aa_summary"),
            data.get("aa_description"),
            data.get("aa_highlights", "[]"),
            data.get("aa_itineraries"),
            data.get("mobile_card_text"),
            data.get("seo_title"),
            data.get("seo_meta"),
            data.get("seo_keywords_used", "[]"),
            data.get("og_tags", "{}"),
            data.get("quality_score"),
            data.get("quality_score_id"),
            data.get("s3_gold_path"),
            data.get("approved_by", "auto"),
        )
        return row["id"]

    async def get_by_id(self, id: str) -> dict | None:
        row = await self.conn.fetchrow(
            f"SELECT * FROM {self.gold}.published_tours WHERE id = $1::uuid", id
        )
        return dict(row) if row else None

    async def get_by_tour_id(self, tour_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            f"SELECT * FROM {self.gold}.published_tours WHERE tour_id = $1::uuid", tour_id
        )
        return dict(row) if row else None

    async def list(self, limit: int = 50, offset: int = 0) -> list:
        rows = await self.conn.fetch(
            f"SELECT * FROM {self.gold}.published_tours ORDER BY published_at DESC LIMIT $1 OFFSET $2",
            limit, offset
        )
        return [dict(r) for r in rows]

    async def list_by_country(self, country: str, limit: int = 50) -> list:
        rows = await self.conn.fetch(f"""
            SELECT pt.* FROM {self.gold}.published_tours pt
            JOIN {self.silver}.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE rt.country = $1
            ORDER BY pt.published_at DESC
            LIMIT $2
        """, country, limit)
        return [dict(r) for r in rows]

    @staticmethod
    def generate_slug(name: str, country: str = "") -> str:
        """Generate URL-friendly slug from tour name and country."""
        import re
        import unicodedata
        # Normalize unicode (ộ → o, etc.)
        text = unicodedata.normalize("NFKD", name.lower())
        text = text.encode("ascii", "ignore").decode("ascii")
        if country:
            country_part = unicodedata.normalize("NFKD", country.lower())
            country_part = country_part.encode("ascii", "ignore").decode("ascii")
            text = f"{text}-{country_part}"
        # Replace non-alphanumeric with dash
        text = re.sub(r"[^a-z0-9]+", "-", text)
        # Strip leading/trailing dashes
        text = text.strip("-")
        # Max 100 chars
        return text[:100]
