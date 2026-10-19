# AA-129 Final Pass — Implementation Notes
# Updated: Session fix/aa-129-brand-final (27/05/2026)

## Decisions

### B2 Root Cause
`frontend/app/api/admin/[...path]/route.ts` exports GET/POST/PATCH/DELETE but NOT PUT.
`PUT /api/admin/brands/{name}` returns 405 → "Save failed" in UI.
Fix: add `export const PUT = handler;`.

### B4 Root Cause
`list_brands` WHERE clause has no `is_active = true` filter.
Deleted brands (all versions is_active=false) still appear in dropdown.
Fix: add `AND is_active = true` to WHERE clause.

### B3 Root Cause
Two parts: (1) same as B4 — after delete+reload, brand reappears because list_brands returns inactive brands. (2) Frontend deleteBrand shows no error if delete fails.

### B1 (403 on parse-docx)
Proxy code already correctly handles multipart (arrayBuffer + Content-Type forwarded).
403 is a Vercel ADMIN_SECRET env var mismatch or not-set issue, not a code bug.
No code change needed — flagging for ops verification.

### Compare Modal — Full Screen
Sidebar is 240px. Modal uses `position: fixed; left: 240px; width: calc(100vw - 240px)` to avoid sidebar overlap. Each column gets its own scroll container with `overflowY: auto`.

### Sticky Section Layout
Uses CSS flex column + overflow:hidden on the outer container to create independent scroll regions. Stats: fixed height. Tours: flex:1. Pipeline runs: fixed height with inner scroll.

### Score Bars
get_tour_version_detail already JOINs quality_scores. Added score_brand, score_seo, score_structure to the SELECT and response.

### metadata JSONB
generated_content has a metadata column (JSONB). Contains brand_rule_id, seo_mode, dataforseo_used. brand_name resolved via LEFT JOIN shared.tenant_brand_rules ON tbr.id = (metadata->>'brand_rule_id')::uuid. Nullable — gracefully handled.

## Changed

- route.ts: added `export const PUT = handler;`
- list_brands: WHERE clause now has `AND is_active = true`
- brand/page.tsx: deleteBrand shows error message on failure
- get_tour_version_detail: added score_brand/seo/structure, metadata, brand_name, top_keywords
- VersionCompareModal: full-screen, 2-col independent scroll, score bars, rewrite config, all content fields
- master-content/page.tsx: sticky 3-section layout, country/score/version filters

## Tradeoffs

- list_brands `AND is_active=true` — if user accidentally marks all versions inactive, brand disappears. Acceptable since delete is intentional.
- top_keywords via LATERAL JOIN — adds query complexity, mitigated by LIMIT 1.
- Full-screen compare modal covers the main content area but not the sidebar; this is intentional (sidebar stays navigable).

## Should Know

- Migrations 043-045 must be applied to dev DB before UAT (brand_name column, constraint changes).
- B1 (403) cannot be fixed in code — ops must verify Vercel ADMIN_SECRET env var matches backend secret `cis-admin-10ec56e26d5fd322e7cac2dbec7f3903`.
- The VersionDetail interface now has more fields; the old CompareModal in master-content was replaced with VersionCompareModal inline.

---

## Session fix/aa-129-brand-final (27/05/2026)

### B1-new: Delete brand — hard delete
- Changed from `UPDATE SET is_active=false` to `DELETE FROM` — brands must be fully removed
- Confirm dialog text updated to reflect permanent deletion
- Migration 047 not needed for delete fix (no schema change)

### B2-new: Version activate stuck in read-only
- Root cause: `setViewingVersion(null)` was called BEFORE `loadBrands()` + `selectBrand()` — selectBrand internally calls `setMsg(null)` which cleared the success message too
- Fix: moved `setViewingVersion(null)` and `setMsg(...)` to AFTER `await selectBrand(selected)` so read-only exits only after fresh data is loaded and message is not overwritten

### B3-new: Good examples empty after save
- **Parse issue**: mammoth renders DOCX heading styles as `<h1>–<h6>` HTML elements, not `<p>` — `querySelectorAll("p")` missed them entirely. Fixed by selecting `"p, h1, h2, h3, h4, h5, h6"`.
- **Save issue**: `good_examples` field existed in `BrandCreateRequest` Pydantic model but was NOT in the INSERT statement for create or update. Also not in SELECT for get_brand. Fixed all three.
- **Migration 047**: `ALTER TABLE shared.tenant_brand_rules ADD COLUMN IF NOT EXISTS good_examples TEXT` — must be applied before UAT.
