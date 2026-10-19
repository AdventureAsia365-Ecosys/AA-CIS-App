// frontend/lib/sameOrigin.ts
// AA-816 — one same-origin check for state-changing auth routes (first user: staff sign-out), the same rule as
// AA-Booking frontend/lib/sameOrigin.ts. SameSite=Lax cookies already stop most cross-site writes; this refuses
// the rest, e.g. another site POSTing to /api/auth/admin-logout to sign staff out (logout CSRF).
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
