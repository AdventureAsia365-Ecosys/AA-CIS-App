// AA-253: despite the "tenant" path segment, this route reads/writes the
// AA-internal admin's brand rules (backend hardcodes the AA-internal
// tenant_id — see api/routers/admin_pipeline.py get_brand_identity). It is
// an admin route, gated with requireAdmin() like the other X-Admin-Secret
// proxies, not requireTenant().
import { NextRequest, NextResponse } from "next/server";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { requireAdmin, secretNotConfigured, staffCredentialHeaders } from "@/lib/auth-server";
import { API_RULES } from "@/lib/rbac";

const API_URL      = process.env.API_URL      ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET  ?? "";
const API_KEY      = process.env.INTERNAL_API_KEY ?? "";

// AA-882 (GĐ3) — forward the verified staff identity, as /api/admin/[...path] does. POST
// /admin/brand-identity needs content.settings.write (api/core/staff_authz.py POLICY): without
// x-admin-role the backend sees the machine principal and refuses the write.
// AA-816 (C9): null when a legacy session needs the admin secret and none is configured (503).
function apiHeaders(
  auth: { adminId: string; role: string; bearer: string | null },
  extra: Record<string, string> = {},
): Record<string, string> | null {
  const credential = staffCredentialHeaders(auth, ADMIN_SECRET, "admin/brand-identity");
  if (!credential) return null;
  return {
    "x-admin-user-id": auth.adminId,
    "x-admin-role": auth.role,
    ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
    ...credential, // Cognito Bearer only, or X-Admin-Secret for a legacy session
    ...extra,
  };
}

export async function GET(req: NextRequest) {
  const auth = await requireAdmin(req, API_RULES.brandIdentity);
  if (!auth.ok) return auth.response;

  const headers = apiHeaders(auth);
  if (!headers) return secretNotConfigured();
  const res = await fetch(`${API_URL}/admin/brand-identity`, {
    headers,
    cache: "no-store",
  });
  if (!res.ok) return NextResponse.json({ error: "Failed" }, { status: res.status });
  return NextResponse.json(await res.json());
}

export async function POST(req: NextRequest) {
  // AA-911 — refuse a cross-site write before any cookie or body is read (lib/sameOrigin.ts).
  const refused = refuseCrossSiteWrite(req);
  if (refused) return refused;
  const auth = await requireAdmin(req, API_RULES.brandIdentity);
  if (!auth.ok) return auth.response;

  const headers = apiHeaders(auth, { "Content-Type": "application/json" });
  if (!headers) return secretNotConfigured();
  const body = await req.json();
  const res = await fetch(`${API_URL}/admin/brand-identity`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) return NextResponse.json({ error: "Failed" }, { status: res.status });
  return NextResponse.json(await res.json());
}
