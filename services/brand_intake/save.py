"""AA-1080 — the brand-identity save helpers the tenant POST and the staff PUT share.

- Signature words (AA-800) live in `tenant_brand_rules.custom_validators` as `{"signature_words": [...]}`, the
  shape `signature_vocabulary.explicit_signature_words` reads. Before AA-1080 no save path wrote the column (a new
  version even dropped it), so every brand had 0 explicit signature words. Each save now carries the previous
  version's object over (other keys, such as the admin voice rubric override of AA-790, included) and replaces only
  `signature_words` when the client sends the list (`CUSTOM_VALIDATORS_VALUE_SQL`).
- Every declared country without search data gets one `shared.unmapped_market_requests` row at save time
  (AA-629 Tier 1), not only when the Slate later runs and not only when no declared country is supported.
"""
from __future__ import annotations

import json

import structlog

logger = structlog.get_logger()

SIGNATURE_WORDS_MAX = 20

_PREVIOUS_SQL = ("(SELECT p.custom_validators FROM shared.tenant_brand_rules p "
                 "WHERE p.tenant_id = {tenant} AND p.version = {version} - 1 ORDER BY p.updated_at DESC LIMIT 1)")
# The VALUES expression for custom_validators in a brand INSERT ({tenant} = the tenant_id parameter, {version} the
# new version's, {param} the signature-word list as JSON text or NULL). The previous version's object (the tenant's
# latest row, whatever its brand_name) is kept, other keys included (the admin voice rubric override, AA-790): a
# sent list replaces only its `signature_words` key; NULL (the client did not send the field) carries the previous
# value over as is.
CUSTOM_VALIDATORS_VALUE_SQL = (
    "CASE WHEN {param}::jsonb IS NULL THEN " + _PREVIOUS_SQL + " ELSE "
    "(CASE WHEN jsonb_typeof(" + _PREVIOUS_SQL + ") = 'object' THEN " + _PREVIOUS_SQL + " ELSE '{{}}'::jsonb END)"
    " || jsonb_build_object('signature_words', {param}::jsonb) END"
)


def signature_words_json(words: list[str] | None) -> str | None:
    """The signature-word list a save sends, as JSON text (sanitised like every brand list field, deduped
    case-insensitively), or None when the client did not send the field (carry the previous value over). An
    empty list is a real value: the tenant cleared its words."""
    if words is None:
        return None
    from shared.validators.prompt_sanitize import MAX_SHORT_FIELD_LEN, sanitize_list
    clean = sanitize_list(words, MAX_SHORT_FIELD_LEN, max_items=SIGNATURE_WORDS_MAX)
    seen: dict[str, str] = {}
    for w in clean:
        seen.setdefault(w.lower(), w)
    return json.dumps(list(seen.values()))


async def record_unsupported_markets(pool, tenant_id, target_market: dict) -> list[str]:
    """One unmapped-market request per declared country without search data (seed_builder.unsupported_countries).
    Best effort on the caller's behalf: a failed write is logged and never fails the save. Returns the countries
    it tried. Runs on the main pool (cis_app holds the table's grants; the tenant pool has none, CIS 164)."""
    from services.seo_intelligence.seed_builder import unsupported_countries
    from shared.dfs_client.unmapped_market import record_unmapped_market_request

    countries = unsupported_countries(target_market)
    if not countries:
        return []
    try:
        async with pool.acquire() as conn:
            for code in countries:
                await record_unmapped_market_request(conn, tenant_id, code)
    except Exception as exc:  # noqa: BLE001 — a side record, never the save itself
        logger.warning("unmapped_market_request_record_failed", tenant_id=str(tenant_id),
                       countries=countries, error=str(exc)[:200])
    return countries
