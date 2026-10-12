"""
services.acp_angle_gate.brand_audience — T8 workflow step 3: "Tự động lấy fixed brand audience".

STEP0 (docs/claude_audit/AA-449-00-step0-t8-angle-gate-investigation.md §6) found the real
source: shared.tenant_brand_rules.customer_segment/customer_mindset (migration 018, AA-85/AA-82)
— already populated per-tenant since T0 Brand Setup (docx-parse or manual seed), but NOT exposed
by any existing tenant-facing endpoint (BrandTab.tsx's BrandData interface only carries
system_prompt/style_guide/forbidden_words — the audience data is baked as prose INSIDE
system_prompt, not available as a separate field). This is the first tenant-facing read of these
2 columns directly.

Deliberately reads customer_segment + customer_mindset only (not the full system_prompt, which
also carries tone/style/forbidden-words guidance irrelevant to "who is the audience") and not
brand_type/core_idea (STEP0 didn't identify those as part of "audience" specifically — segment =
who they are, mindset = what they think/want, which is what an angle-generation prompt actually
needs to write TO someone, not a general brand description)."""
from __future__ import annotations

from uuid import UUID

from services.acp_shared.brand_pack import BrandPack, fetch_brand_pack

# AA-771 (S233) — the audience is now part of the full brand pack (services/acp_shared/
# brand_pack.py): T8 and T9 need the core idea, voice, style guide and forbidden words too, not
# only segment + mindset. `BrandAudience` stays as the name callers already import.
BrandAudience = BrandPack


async def fetch_brand_audience(tenant_id: UUID, pool) -> BrandAudience:
    """The tenant's brand pack ({} values None/empty when the tenant has no active
    tenant_brand_rules row yet)."""
    return await fetch_brand_pack(tenant_id, pool)


__all__ = ["BrandAudience", "fetch_brand_audience"]
