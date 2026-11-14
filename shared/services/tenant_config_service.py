"""
Tenant Config Service
PRD v4: Manages tenant lifecycle and configuration.
All services query this before processing to load tenant-specific config.

Cache strategy:
  - Redis TTL 5 min (300s) per config type per tenant
  - Cache key: config:{type}:{tenant_id}
  - Cache miss → DB query → cache set
  - Cache invalidate on UPDATE
"""

import json
import asyncpg
import structlog
from dataclasses import dataclass, field
from typing import Optional

logger = structlog.get_logger()


def _parse_json(value, default):
    """asyncpg returns JSONB as str — parse if needed."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    import json
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default

CACHE_TTL = 300  # technical-constant: 5 minutes


@dataclass
class BrandRules:
    tenant_id: str
    system_prompt: str = ""
    style_guide: str = ""
    forbidden_words: list = field(default_factory=list)
    custom_validators: list = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "tenant_id":         self.tenant_id,
            "system_prompt":     self.system_prompt,
            "style_guide":       self.style_guide,
            "forbidden_words":   self.forbidden_words,
            "custom_validators": self.custom_validators,
            "version":           self.version,
        }


@dataclass
class SEOConfig:
    tenant_id: str
    seo_provider: str = "dataforseo"
    custom_keywords: list = field(default_factory=list)
    target_market: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tenant_id":       self.tenant_id,
            "seo_provider":    self.seo_provider,
            "custom_keywords": self.custom_keywords,
            "target_market":   self.target_market,
            "overrides":       self.overrides,
        }


@dataclass
class ExportConfig:
    tenant_id: str
    webhook_url: Optional[str] = None
    export_format: str = "json"
    field_mapping: dict = field(default_factory=dict)
    auth_header: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "tenant_id":     self.tenant_id,
            "webhook_url":   self.webhook_url,
            "export_format": self.export_format,
            "field_mapping": self.field_mapping,
            "auth_header":   self.auth_header,
        }


class TenantConfigService:
    """
    Loads and caches tenant configuration.
    Used by all pipeline services before processing.
    """

    def __init__(self, conn: asyncpg.Connection, cache=None):
        """
        conn:  asyncpg connection
        cache: RedisCache instance (optional — falls back to DB only)
        """
        self.conn  = conn
        self.cache = cache

    # ── Brand Rules ───────────────────────────────────────────

    async def get_brand_rules(self, tenant_id: str) -> BrandRules:
        cache_key = f"config:brand:{tenant_id}"

        # L1: Redis cache
        if self.cache:
            cached = await self.cache.get(cache_key)
            if cached:
                logger.debug("brand_rules.cache_hit", tenant_id=tenant_id)
                data = json.loads(cached)
                return BrandRules(**data)

        # L2: DB
        row = await self.conn.fetchrow("""
            SELECT system_prompt, style_guide, forbidden_words,
                   custom_validators, version
            FROM shared.tenant_brand_rules
            WHERE tenant_id = $1 AND is_active = TRUE
        """, tenant_id)

        if not row:
            logger.warning("brand_rules.not_found", tenant_id=tenant_id)
            return BrandRules(tenant_id=tenant_id)

        rules = BrandRules(
            tenant_id=tenant_id,
            system_prompt=row["system_prompt"] or "",
            style_guide=row["style_guide"] or "",
            forbidden_words=_parse_json(row["forbidden_words"], []),
            custom_validators=_parse_json(row["custom_validators"], []),
            version=row["version"],
        )

        if self.cache:
            await self.cache.set(cache_key, json.dumps(rules.to_dict()), ex=CACHE_TTL)

        return rules

    async def update_brand_rules(self, tenant_id: str, data: dict) -> bool:
        """Update brand rules and invalidate cache."""
        await self.conn.execute("""
            UPDATE shared.tenant_brand_rules
            SET system_prompt   = COALESCE($2, system_prompt),
                style_guide     = COALESCE($3, style_guide),
                forbidden_words = COALESCE($4, forbidden_words),
                version         = version + 1,
                updated_at      = NOW()
            WHERE tenant_id = $1 AND is_active = TRUE
        """,
            tenant_id,
            data.get("system_prompt"),
            data.get("style_guide"),
            json.dumps(data["forbidden_words"]) if "forbidden_words" in data else None,
        )
        if self.cache:
            await self.cache.delete(f"config:brand:{tenant_id}")
        return True

    # ── SEO Config ────────────────────────────────────────────

    async def get_seo_config(self, tenant_id: str) -> SEOConfig:
        cache_key = f"config:seo:{tenant_id}"

        if self.cache:
            cached = await self.cache.get(cache_key)
            if cached:
                logger.debug("seo_config.cache_hit", tenant_id=tenant_id)
                data = json.loads(cached)
                return SEOConfig(**data)

        row = await self.conn.fetchrow("""
            SELECT seo_provider, custom_keywords, target_market, overrides
            FROM shared.tenant_seo_config
            WHERE tenant_id = $1
        """, tenant_id)

        if not row:
            logger.warning("seo_config.not_found", tenant_id=tenant_id)
            return SEOConfig(tenant_id=tenant_id)

        config = SEOConfig(
            tenant_id=tenant_id,
            seo_provider=row["seo_provider"] or "dataforseo",
            custom_keywords=_parse_json(row["custom_keywords"], []),
            target_market=_parse_json(row["target_market"], {}),
            overrides=_parse_json(row["overrides"], {}),
        )

        if self.cache:
            await self.cache.set(cache_key, json.dumps(config.to_dict()), ex=CACHE_TTL)

        return config

    # ── Export Config ─────────────────────────────────────────

    async def get_export_config(self, tenant_id: str) -> ExportConfig:
        cache_key = f"config:export:{tenant_id}"

        if self.cache:
            cached = await self.cache.get(cache_key)
            if cached:
                data = json.loads(cached)
                return ExportConfig(**data)

        row = await self.conn.fetchrow("""
            SELECT webhook_url, export_format, field_mapping, auth_header
            FROM shared.tenant_export_config
            WHERE tenant_id = $1
        """, tenant_id)

        if not row:
            return ExportConfig(tenant_id=tenant_id)

        config = ExportConfig(
            tenant_id=tenant_id,
            webhook_url=row["webhook_url"],
            export_format=row["export_format"] or "json",
            field_mapping=_parse_json(row["field_mapping"], {}),
            auth_header=row["auth_header"],
        )

        if self.cache:
            await self.cache.set(cache_key, json.dumps(config.to_dict()), ex=CACHE_TTL)

        return config

    # ── Tenant validation ─────────────────────────────────────

    async def get_tenant(self, tenant_id: str) -> Optional[dict]:
        """Check tenant exists and is active."""
        cache_key = f"config:tenant:{tenant_id}"

        if self.cache:
            cached = await self.cache.get(cache_key)
            if cached:
                return json.loads(cached)

        row = await self.conn.fetchrow("""
            SELECT tenant_id, name, plan_tier, rate_limit_rpm, is_active
            FROM shared.tenants
            WHERE tenant_id = $1
        """, tenant_id)

        if not row:
            return None

        tenant = {k: str(v) if hasattr(v, 'hex') else v for k, v in dict(row).items()}
        if self.cache:
            await self.cache.set(cache_key, json.dumps(tenant), ex=CACHE_TTL)

        return tenant

    async def is_active_tenant(self, tenant_id: str) -> bool:
        tenant = await self.get_tenant(tenant_id)
        return tenant is not None and tenant.get("is_active", False)
