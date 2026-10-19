// AA-253: this route forwarded X-Admin-Secret unconditionally with no
// caller check — requireAdmin() now verifies the real admin JWT before
// any request reaches the backend.
import { NextRequest, NextResponse } from "next/server";
import { requireAdmin, secretNotConfigured, staffCredentialHeaders } from "@/lib/auth-server";
import { API_RULES } from "@/lib/rbac";
import { proxyPath } from "@/lib/bffPath";

const API_URL      = process.env.API_URL      ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET  ?? "";
const API_KEY      = process.env.INTERNAL_API_KEY ?? "";

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const auth = await requireAdmin(req, API_RULES.tourFull);
  if (!auth.ok) return auth.response;

  const { id } = await params;
  // AA-577 follow-up — an encoded "../../openapi.json?" id must not leave /v1/tours/ (lib/bffPath.ts).
  const tourPath = proxyPath([id]);
  if (tourPath === null) {
    return NextResponse.json({ error: "Invalid tour id" }, { status: 400 });
  }

  // AA-816 (C5/C9): /v1/tours/{id}/full is NOT a staff API path (the staff_bearer middleware neither uses
  // nor strips a Bearer there, and the route reads a Bearer as a tenant token), so every session — Cognito
  // included — still sends X-Admin-Secret here and never a Bearer (staffCredentialHeaders decides by path).
  // No page calls this route today; moving it under /admin is the way to drop the secret for it.
  const credential = staffCredentialHeaders(auth, ADMIN_SECRET, `v1/tours/${tourPath}/full`);
  if (!credential) return secretNotConfigured();

  const res = await fetch(`${API_URL}/v1/tours/${tourPath}/full`, {
    headers: {
      ...credential,
      // AA-882 (GĐ3) — the verified staff identity, like every staff BFF (api/core/staff_authz.py).
      "x-admin-user-id": auth.adminId,
      "x-admin-role": auth.role,
      ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
    },
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json({ error: "Failed" }, { status: res.status });
  }

  return NextResponse.json(await res.json());
}
