// AA-253: this route forwarded X-Admin-Secret unconditionally with no
// caller check — requireAdmin() now verifies the real admin JWT before
// any request reaches the backend.
import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/auth-server";
import { API_RULES } from "@/lib/rbac";
import { proxyPath } from "@/lib/bffPath";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET ?? "";

async function handler(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const auth = await requireAdmin(req, API_RULES.pipeline);
  if (!auth.ok) return auth.response;

  if (!ADMIN_SECRET) {
    return NextResponse.json({ detail: "Admin secret not configured" }, { status: 503 });
  }

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
    "X-Admin-Secret": ADMIN_SECRET,
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
    const resContentType = res.headers.get("content-type") ?? "application/json";
    const data = await res.arrayBuffer();
    return new NextResponse(data, {
      status: res.status,
      headers: { "Content-Type": resContentType },
    });
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
}

export const GET    = handler;
export const POST   = handler;
export const PUT    = handler;
export const PATCH  = handler;
export const DELETE = handler;
