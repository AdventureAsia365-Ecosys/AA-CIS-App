// frontend/app/api/view-as/v1/[...path]/route.ts — AA-1035 R2 slice 2 (root ADR 0019 d5): the reads of a read-only
// "view as tenant" session. GET only (no other method is exported: Next answers 405), only the allowed tenant reads
// (lib/viewAs.ts VIEW_AS_READ_ROUTES, the mirror of api/core/view_as.py READ_ROUTES) that the signed-in member's role
// may make (VIEW_AS_ROUTE_ROLES, the mirror of ROUTE_CAPS: support never reads the plan / usage), only for the staff
// member whose session the httpOnly aa_view_as cookie holds. Forwards the token as the Bearer to CIS /v1/<path> with
// the member's verified staff role (VIEW_AS_ROLE_HEADER); the API's view-as gate checks the session on every request,
// ends it when that role is no longer the role it was started with (the token's srl), refuses anything else and logs
// each read. A changed role is forwarded on any allowed read path (viewAsForward) so the API ends and logs the session.
import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/auth-server";
import { proxyPath } from "@/lib/bffPath";
import { streamUpstream } from "@/lib/proxyResponse";
import { VIEW_AS_COOKIE, VIEW_AS_ROLES, VIEW_AS_ROLE_HEADER, viewAsClaims, viewAsForward } from "@/lib/viewAs";
import type { StaffRole } from "@/lib/rbac";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";

export async function GET(req: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const auth = await requireAdmin(req, VIEW_AS_ROLES as unknown as StaffRole[]);
  if (!auth.ok) return auth.response;
  const token = req.cookies.get(VIEW_AS_COOKIE)?.value;
  const claims = viewAsClaims(token);
  if (!token || !claims || claims.staffId !== auth.adminId) {
    return NextResponse.json({ detail: "view_as_ended", message: "No view-as session is open." }, { status: 401 });
  }
  const { path } = await params;
  const safe = proxyPath(path);
  const backendPath = safe === null ? null : `/v1/${safe}`;
  if (backendPath === null || !viewAsForward(backendPath, auth.role, claims.startRole)) {
    return NextResponse.json({ detail: "view_as_read_only" }, { status: 403 });
  }
  try {
    const res = await fetch(`${API_URL}${backendPath}${req.nextUrl.search}`, {
      headers: { Authorization: `Bearer ${token}`, [VIEW_AS_ROLE_HEADER]: auth.role },
    });
    return streamUpstream(res);
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
}
