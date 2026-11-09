// frontend/lib/redirect.ts
// AA-911 (staff web on ECS, root ADR 0019 d23) — a same-site redirect with a RELATIVE Location header. Behind the ALB
// the Next standalone server builds `request.url` from its own bind address (https://0.0.0.0:3000/...), so
// `NextResponse.redirect(new URL(path, request.url))` would send the browser to 0.0.0.0. A relative Location
// (RFC 9110 §10.2.2) resolves against the URL the browser actually used, on ECS, on Vercel and in `next dev` alike.
// Only paths: callers pass a path they built or validated (safeNextPath), never a full URL.
import { NextResponse } from "next/server";

export function redirectToPath(path: string, status: 303 | 307 = 303): NextResponse {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("\\")) {
    throw new Error("redirectToPath: a same-site path is required");
  }
  return new NextResponse(null, { status, headers: { Location: path } });
}
