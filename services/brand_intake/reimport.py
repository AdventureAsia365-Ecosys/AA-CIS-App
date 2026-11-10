"""AA-1080 — re-import a tenant's stored T0 brief into its brand pack, filling only what the old import left empty.

Used by scripts/aa1080/brief_reimport.py (the live step: the 4 AA-741 audit tenants). The brief is the newest .docx the
tenant uploaded (`brand-identity/<tenant_id>/…` in the bronze bucket, POST /admin/brand-identity/upload). It is parsed
with the T0 parser (services/brand_intake/brief.py) and, for the three brand-pack fields the old import never wrote,
the parsed value is used ONLY where the active version is empty: `system_prompt`, `default_cta`, and
`custom_validators.signature_words`. Every other field of the active version is kept as the tenant saved it. A fill
is a new version (n + 1, the previous one deactivated, the same advisory lock as the POST), so the history shows it.
"""
from __future__ import annotations

import json
from typing import Any

from services.acp_shared.signature_vocabulary import explicit_signature_words

FILLED_FIELDS = ("system_prompt", "default_cta", "signature_words")

ACTIVE_SQL = """
    SELECT * FROM shared.tenant_brand_rules
    WHERE tenant_id = $1::uuid AND is_active = true
    ORDER BY (brand_name = 'default') DESC, version DESC
    LIMIT 1
"""
LOCK_SQL = "SELECT pg_advisory_xact_lock($1::int, hashtext($2::text || ':brand_identity'))"
MAX_VERSION_SQL = "SELECT COALESCE(MAX(version), 0) AS v FROM shared.tenant_brand_rules WHERE tenant_id = $1::uuid"
DEACTIVATE_SQL = "UPDATE shared.tenant_brand_rules SET is_active = false WHERE tenant_id = $1::uuid"
# Copies the active row ($2 = its id) as version $3 with the three filled values; every other column is kept.
COPY_SQL = """
    INSERT INTO shared.tenant_brand_rules
        (tenant_id, system_prompt, style_guide, forbidden_words, custom_validators, version, is_active, updated_at,
         brand_type, core_idea, customer_segment, customer_mindset, voice_examples, source_docx_s3_key,
         rewrite_language, target_markets, brand_name, good_examples, default_cta)
    SELECT tenant_id, $4, style_guide, forbidden_words, $5::jsonb, $3, true, now(),
           brand_type, core_idea, customer_segment, customer_mindset, voice_examples, COALESCE($7, source_docx_s3_key),
           rewrite_language, target_markets, brand_name, good_examples, $6
    FROM shared.tenant_brand_rules WHERE tenant_id = $1::uuid AND id = $2::uuid
"""
LOCK_CLASS = 859  # admin_pipeline.AA859_BRAND_LOCK_CLASS


def _validators_obj(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else None
        except json.JSONDecodeError:
            value = None
    return dict(value) if isinstance(value, dict) else {}


def plan_fill(active: dict, parsed_fields: dict) -> dict:
    """{"fill": {field: value}, "kept": [fields already set], "custom_validators": <new object or None>}.

    Pure. A field is filled only when the active version has it empty and the brief gives a value; nothing is
    overwritten. `custom_validators` keeps every other key of the active object (voice_rubric, AA-790)."""
    fill: dict[str, Any] = {}
    kept: list[str] = []
    for field in ("system_prompt", "default_cta"):
        if (active.get(field) or "").strip():
            kept.append(field)
        elif (parsed_fields.get(field) or "").strip():
            fill[field] = parsed_fields[field].strip()
    current_words = explicit_signature_words(active.get("custom_validators"))
    new_validators = None
    if current_words:
        kept.append("signature_words")
    elif parsed_fields.get("signature_words"):
        fill["signature_words"] = list(parsed_fields["signature_words"])
        new_validators = {**_validators_obj(active.get("custom_validators")),
                          "signature_words": fill["signature_words"]}
    return {"fill": fill, "kept": kept, "custom_validators": new_validators}


async def apply_fill(conn, tenant_id: str, active: dict, plan: dict, s3_key: str | None = None) -> int | None:
    """Write the plan as a new version in one transaction; returns the new version, or None when nothing to fill."""
    if not plan["fill"]:
        return None
    fill = plan["fill"]
    validators = plan["custom_validators"]
    if validators is None:
        validators = _validators_obj(active.get("custom_validators")) or None
    async with conn.transaction():
        await conn.execute(LOCK_SQL, LOCK_CLASS, str(tenant_id))
        version = int(await conn.fetchval(MAX_VERSION_SQL, str(tenant_id))) + 1
        await conn.execute(DEACTIVATE_SQL, str(tenant_id))
        await conn.execute(COPY_SQL, str(tenant_id), str(active["id"]), version,
                           fill.get("system_prompt", active.get("system_prompt")),
                           None if validators is None else json.dumps(validators),
                           fill.get("default_cta", active.get("default_cta")), s3_key)
    return version
