// portal/app/api/tenant/me/route.ts (AA-885: moved from the admin app)
// AA-427 — "/me" for the tenant portal.
//
// portal/page.tsx used to read cis_tenant_name / cis_tenant_plan straight out
// of document.cookie. Once those cookies are httpOnly, client JS can no
// longer see them at all. This route runs server-side (where the httpOnly
// cookie IS visible via next/headers), re-verifies the tenant JWT against
// the backend — the same call requireTenant() already made, now extended to
// surface name/plan_tier too — and hands the client back only the display
// fields it actually needs. The token itself never reaches the response.
import { NextRequest, NextResponse } from "next/server";
import { requireTenant } from "@/lib/auth-server";

export async function GET(req: NextRequest) {
  const auth = await requireTenant(req);
  if (!auth.ok) return auth.response;

  return NextResponse.json({
    tenant_id: auth.tenantId,
    tenant_name: auth.name,
    plan_tier: auth.planTier,
  });
}
