"""S279 thresholds lane L2 (AA-1096) — the quality values the admin pages show, read from the settings registry.

  GET /admin/quality/thresholds   {pass_line, score_bands {top, pass, low}, seo_bands {meta {min, max},
                                   title {min, max}, slug {max}}, writing_policy_sha, settings_fp}

The pass line, the score colour bands and the SEO bands are settings (CIS 409, Platform › Settings, evidence-gated,
maker-checker). The frontend used to carry a generated copy (frontend/lib/quality-registry.generated.ts SCORE_BANDS /
SEO_BANDS / PASS_LINE); it now reads this route (frontend/lib/qualityThresholds.ts), so an approved change reaches the
Review, Master content, tenant and compare views within one settings cache window (audit 5.4 check 3: no mirrors).

Who may call: every staff role that opens a page showing a quality score (api/core/staff_authz.py POLICY: content.read,
tenant_content.read, tenant_content.book.read or tenant.book.read). Values in force from the settings reader's snapshot:
no DB call on the request path.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.core.staff_authz import staff_policy


class Band(BaseModel):
    min: int
    max: int


class SlugBand(BaseModel):
    max: int


class SeoBands(BaseModel):
    meta: Band
    title: Band
    slug: SlugBand


class QualityThresholds(BaseModel):
    pass_line: float
    score_bands: dict[str, float]
    seo_bands: SeoBands
    writing_policy_sha: str
    settings_fp: str

router = APIRouter(prefix="/admin/quality", tags=["admin-quality"], dependencies=[Depends(staff_policy)])


@router.get("/thresholds", response_model=QualityThresholds,
            summary="Quality pass line, score bands and SEO bands in force (settings registry)")
async def quality_thresholds() -> dict:
    from services.quality import quality_settings

    return quality_settings.ui_values()
