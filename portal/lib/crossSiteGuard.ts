// frontend/lib/crossSiteGuard.ts (byte copy: portal/lib/crossSiteGuard.ts, guarded by tests/unit/test_aa885_portal_split.py)
// AA-911 — the one refusal every BFF write route returns before it reads a cookie or a body. The rule lives in
// lib/sameOrigin.ts isCrossSiteWrite(); tests/unit/test_aa911_bff_cross_site.py checks every write route calls this.
import { NextResponse } from "next/server";
import { isCrossSiteWrite, type WriteCheckRequest } from "./sameOrigin";

/** A 403 for a cross-site write, or null when the request may go on. */
export function refuseCrossSiteWrite(req: WriteCheckRequest): NextResponse | null {
  if (!isCrossSiteWrite(req)) return null;
  const refused = NextResponse.json({ detail: "Cross-site request refused" }, { status: 403 });
  refused.headers.set("Cache-Control", "no-store");
  return refused;
}
