// portal/app/api/tenant/[...path]/route.ts
//
// AA-885 — the portal's tenant BFF proxy: the TENANT branch of the admin app's old
// frontend/app/api/tenant/[...path]/route.ts, and nothing else. The portal app has no staff session,
// no admin secret and no staff branch; the admin app's copy of this route is staff-only since AA-885.
//
// Same behaviour as before for a tenant:
//   - AA-656: the tenant JWT (httpOnly cis_tenant_token, AA-427) is verified against the backend
//     before it is forwarded (lib/auth-server.ts requireTenant, cached per AA-551);
//   - the verified token goes upstream as `Authorization: Bearer`;
//   - AA-577 follow-up: the backend path is built with lib/bffPath.ts proxyPath() (no dot, empty or
//     slash-carrying segment can rewrite it after the checks);
//   - AA-441: multipart/form-data bodies pass through as bytes (brand-guide upload), JSON as text;
//   - S241: the upstream body is streamed through (lib/proxyResponse.ts, Content-Type only).
import { NextRequest, NextResponse } from "next/server";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { API_URL, requireTenant } from "@/lib/auth-server";
import { proxyPath } from "@/lib/bffPath";
import { streamUpstream } from "@/lib/proxyResponse";

async function handler(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  // AA-911 — refuse a cross-site write before any cookie or body is read (lib/sameOrigin.ts).
  const refused = refuseCrossSiteWrite(req);
  if (refused) return refused;
  const auth = await requireTenant(req);
  if (!auth.ok) return auth.response;

  const { path } = await params;
  const upstreamPath = proxyPath(path);
  if (upstreamPath === null) {
    return NextResponse.json({ detail: "Invalid path" }, { status: 400 });
  }
  const url = `${API_URL}/${upstreamPath}${req.nextUrl.search}`;

  const headers: Record<string, string> = { Authorization: `Bearer ${auth.token}` };

  const contentType = req.headers.get("content-type") ?? "";
  const isMultipart = contentType.includes("multipart/form-data");

  let body: ArrayBuffer | string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    if (isMultipart) {
      headers["Content-Type"] = contentType;
      body = await req.arrayBuffer();
    } else {
      headers["Content-Type"] = "application/json";
      try { body = await req.text(); } catch { /* empty body */ }
    }
  }

  try {
    const res = await fetch(url, { method: req.method, headers, body });
    return streamUpstream(res);
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
}

export const GET    = handler;
export const POST   = handler;
export const PATCH  = handler;
export const DELETE = handler;
