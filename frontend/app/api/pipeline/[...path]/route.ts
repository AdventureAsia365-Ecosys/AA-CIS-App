// AA-253: this route forwarded X-Admin-Secret unconditionally with no
// caller check — requireAdmin() now verifies the real admin JWT before
// any request reaches the backend.
import { NextRequest, NextResponse } from "next/server";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { requireAdmin, secretNotConfigured, staffCredentialHeaders } from "@/lib/auth-server";
import { API_RULES } from "@/lib/rbac";
import { proxyPath } from "@/lib/bffPath";
import { streamUpstream } from "@/lib/proxyResponse";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET ?? "";

async function handler(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  // AA-911 — refuse a cross-site write before any cookie or body is read (lib/sameOrigin.ts).
  const refused = refuseCrossSiteWrite(req);
  if (refused) return refused;
  const auth = await requireAdmin(req, API_RULES.pipeline);
  if (!auth.ok) return auth.response;

  // AA-816 (C9): a Cognito session sends its Bearer and no X-Admin-Secret; a legacy session the secret.
  const credential = staffCredentialHeaders(auth, ADMIN_SECRET, "v1/pipeline");
  if (!credential) return secretNotConfigured();

  const { path } = await params;
  // AA-577 follow-up — no segment may climb out of /v1/pipeline/ (lib/bffPath.ts).
  const upstreamPath = proxyPath(path);
  if (upstreamPath === null) {
    return NextResponse.json({ detail: "Invalid path" }, { status: 400 });
  }
  const search = req.nextUrl.search;
  const url = `${API_URL}/v1/pipeline/${upstreamPath}${search}`;

  const contentType = req.headers.get("content-type") ?? "";
  const isMultipart = contentType.includes("multipart/form-data");

  const outHeaders: Record<string, string> = {
    "x-admin-user-id": auth.adminId,
    // AA-882 (GĐ3) — the verified JWT role, as /api/admin/[...path] sends it. /v1/pipeline runs
    // staff_policy_if_staff (api/core/staff_authz.py): without a role the backend sees the machine
    // principal, which no /v1/pipeline write allows.
    "x-admin-role": auth.role,
    ...credential, // AA-816 (C9): Cognito Bearer only, or X-Admin-Secret for a legacy session
  };

  let body: ArrayBuffer | string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    if (isMultipart) {
      outHeaders["Content-Type"] = contentType;
      body = await req.arrayBuffer();
    } else {
      outHeaders["Content-Type"] = "application/json";
      try { body = await req.text(); } catch { /* empty body */ }
    }
  }

  try {
    const res = await fetch(url, { method: req.method, headers: outHeaders, body });
    // S241 — stream the body through (lib/proxyResponse.ts: same status, Content-Type only).
    return streamUpstream(res);
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
}

export const GET    = handler;
export const POST   = handler;
export const PUT    = handler;
export const PATCH  = handler;
export const DELETE = handler;
