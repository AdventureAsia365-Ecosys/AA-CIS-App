// frontend/lib/sameOrigin.ts (byte copy: portal/lib/sameOrigin.ts, guarded by tests/unit/test_aa885_portal_split.py)
// AA-816 — one same-origin check for state-changing auth routes (first user: staff sign-out), the same rule as
// AA-Booking frontend/lib/sameOrigin.ts. SameSite=Lax cookies already stop most cross-site writes; this refuses
// the rest, e.g. another site POSTing to /api/auth/admin-logout to sign staff out (logout CSRF).
//
// AA-911 — SameSite=Lax is not enough on its own: `it.com` is a public suffix, so every *.lumiguides.it.com host
// (the other apps, and the fixed Preview hosts that serve unmerged PR code) is the SAME SITE as this one and its
// requests carry our Lax cookies. Every BFF write route therefore calls isCrossSiteWrite() first.
//
// `strict`: a request with neither `Origin` nor `Sec-Fetch-Site` is refused too. Every current browser sends one
// of them on a POST, so only non-browser clients hit that branch.
//
// No imports: the type below is the part of NextRequest this reads, so the check is testable without Next.

export interface OriginCheckRequest {
  headers: { get(name: string): string | null };
  nextUrl: { origin: string };
}

export function isSameOrigin(req: OriginCheckRequest, { strict = false }: { strict?: boolean } = {}): boolean {
  const origin = req.headers.get("origin");
  if (origin) return origin === req.nextUrl.origin;
  const site = req.headers.get("sec-fetch-site");
  if (site) return site === "same-origin";
  return !strict;
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export interface WriteCheckRequest extends OriginCheckRequest {
  method: string;
}

/**
 * AA-911 — true for a state-changing request (any method but GET/HEAD/OPTIONS) that did not come from this
 * origin. Non-strict on purpose: a browser always sends `Origin` (or `Sec-Fetch-Site`) on such a request, so a
 * request with neither is a non-browser client, which carries no victim's cookies.
 */
export function isCrossSiteWrite(req: WriteCheckRequest): boolean {
  if (SAFE_METHODS.has(req.method.toUpperCase())) return false;
  return !isSameOrigin(req);
}
