// AA-253: this route forwarded X-Admin-Secret unconditionally with no
// caller check — requireAdmin() now verifies the real admin JWT before
// any request reaches the backend.
import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/auth-server";
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

  const res = await fetch(`${API_URL}/v1/tours/${tourPath}/full`, {
    headers: {
      "X-Admin-Secret": ADMIN_SECRET,
      ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
    },
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json({ error: "Failed" }, { status: res.status });
  }

  return NextResponse.json(await res.json());
}
