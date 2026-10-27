// frontend/lib/apps.ts
// AA-883 (S241 admin console, design §4 + §6.1) — the typed registry of the ecosystem's apps, the
// only place cross-app URLs are built. Code, not a DB table (ADR 0008 §5): a fourth app is one PR.
//
// Dev values are constants (same pattern as AA-Booking frontend/lib/config.ts DEV_CIS_ADMIN_URL), so
// Dev needs no new env var; a NEXT_PUBLIC_* override exists for when there is a prod hostname. The
// overrides must be referenced literally (Next inlines NEXT_PUBLIC_* at build time).
//
// Deep links are built from here only: no free-form return URL (open redirect) and never a token in
// the URL. Staff sign in once (Cognito WORKFORCE SSO, ADR 0006a); each app still checks its own role.

export const DEV_BOOKING_ADMIN_URL = "https://booking.lumiguides.it.com"; // custom domain (AA-818); the Vercel alias still works
export const DEV_TRIPPLANNER_URL = "https://tripplanner.lumiguides.it.com"; // custom domain

function trimSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

/** AA-Booking staff admin (separate Vercel project, signs in with the CIS staff account). */
export const BOOKING_ADMIN_URL = trimSlash(process.env.NEXT_PUBLIC_BOOKING_ADMIN_URL || DEV_BOOKING_ADMIN_URL);

/** TripPlanner public site (no admin of its own, design §6.4). */
export const TRIPPLANNER_URL = trimSlash(process.env.NEXT_PUBLIC_TRIPPLANNER_URL || DEV_TRIPPLANNER_URL);

/** AA-885 — the tenant portal, its own Next.js app (portal/) and Vercel project (aa-cis-portal). Code
 * constant, no env var. Used only by next.config.ts (308 from the old /portal and /tenant-login URLs);
 * the staff /login page does not link to the portal (separate app, separate host). */
export const PORTAL_BASE_URL = "https://portal.lumiguides.it.com";

/** AA-979 P2b (runbook §4.3) — the public trip page /trip/<tourId> moved to the AA-Partner CRM web on the client host
 * (Dev go-dev.). Code constant, no env var (same as PORTAL_BASE_URL). Used only by next.config.ts: the admin host
 * answers 307 /trip/:path* -> here (301 only at the P2b clean-up: a cached 301 cannot be rolled back, critic S5). The
 * API builds published page URLs from its own TRIP_PAGE_BASE_URL (api/routers/v1_trip_page.py). */
export const TRIP_PAGE_BASE_URL = "https://go-dev.lumiguides.it.com";

// No "Tenant portal" entry: the portal needs a tenant session (cis_tenant_token, host-only on the
// portal host since AA-885), so a staff member cannot open it. It comes back with a real support view
// (tenant picker / impersonation); until then tenant support is Tenant 360 (/admin/tenants).

export type AppId = "booking" | "tripplanner";

export interface AppLink {
  id: AppId;
  label: string;
  href: string;
  /** Short hint shown as the link tooltip. */
  hint: string;
  /** Opens in a new tab (another app / origin). */
  external: boolean;
}

export const APP_LINKS: AppLink[] = [
  {
    id: "booking",
    label: "Booking admin",
    href: BOOKING_ADMIN_URL,
    hint: "AA-Booking staff admin (opens in a new tab; sign in with your CIS account until Cognito)",
    external: true,
  },
  {
    id: "tripplanner",
    label: "TripPlanner",
    href: TRIPPLANNER_URL,
    hint: "TripPlanner public site (opens in a new tab)",
    external: true,
  },
];

/** Booking admin page of one staff account (Booking route app/(staff)/staff/[cisUserId]). */
export function bookingStaffUrl(cisUserId: string): string {
  return `${BOOKING_ADMIN_URL}/staff/${encodeURIComponent(cisUserId)}`;
}

// ── AA-884 (S241 GĐ7) — Platform › Apps: one entry per ecosystem app, for the Apps / Cost pages ──
// Dev constants (no NEXT_PUBLIC env, APP_LINKS above unchanged). The backend has its own registry for
// what it probes (api/core/app_registry.py); this one only builds links. The ids match the backend's
// card ids (GET /admin/platform/apps → apps[].id).

const GITHUB_ORG_URL = "https://github.com/AdventureAsia365-Ecosys";

/** The CIS admin console itself (this app, Vercel aa-cis-frontend). */
export const CIS_ADMIN_URL = "https://aa-cis.lumiguides.it.com";

/** AA-Partner's Dev partner host (CloudFront; the agency CRM web, AA-CIS-Infra crm_edge.tf). AA-936. */
export const CRM_PARTNER_URL = "https://partner-dev.lumiguides.it.com";

export type PlatformAppId = "cis" | AppId | "crm";

export interface PlatformApp {
  id: PlatformAppId;
  label: string;
  /** The app's staff-facing frontend (CIS admin, Booking admin, TripPlanner public site). */
  adminUrl: string;
  adminLabel: string;
  repoUrl: string;
  /** GitHub commit page of `sha`, or null when the SHA is missing / "unknown" (local or CI process). */
  commitUrl: (sha: string | null | undefined) => string | null;
}

function platformApp(id: PlatformAppId, label: string, adminUrl: string, adminLabel: string, repo: string): PlatformApp {
  const repoUrl = `${GITHUB_ORG_URL}/${repo}`;
  return {
    id, label, adminUrl, adminLabel, repoUrl,
    commitUrl: (sha) => (sha && sha !== "unknown" && /^[0-9a-f]{7,40}$/i.test(sha) ? `${repoUrl}/commit/${sha}` : null),
  };
}

export const APP_REGISTRY: Record<PlatformAppId, PlatformApp> = {
  cis: platformApp("cis", "CIS (content + catalog)", CIS_ADMIN_URL, "CIS admin", "AA-CIS-App"),
  booking: platformApp("booking", "Booking", BOOKING_ADMIN_URL, "Booking admin", "AA-Booking"),
  tripplanner: platformApp("tripplanner", "TripPlanner", TRIPPLANNER_URL, "TripPlanner site", "AA-TripPlanner-Web"),
  crm: platformApp("crm", "Partner CRM", CRM_PARTNER_URL, "CRM agency app", "AA-Partner"),
};

export const PLATFORM_APP_IDS: PlatformAppId[] = ["cis", "booking", "tripplanner", "crm"];

export function isPlatformAppId(id: string): id is PlatformAppId {
  return (PLATFORM_APP_IDS as string[]).includes(id);
}

/** Booking admin's own audit log (Booking writes its own trail; CIS never reads the booking schema). */
export const BOOKING_AUDIT_LOG_URL = `${BOOKING_ADMIN_URL}/audit-log`;
