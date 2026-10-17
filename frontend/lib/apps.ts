// frontend/lib/apps.ts
// AA-883 (S241 admin console, design §4 + §6.1) — the typed registry of the ecosystem's apps, the
// only place cross-app URLs are built. Code, not a DB table (ADR 0008 §5): a fourth app is one PR.
//
// Dev values are constants (same pattern as AA-Booking frontend/lib/config.ts DEV_CIS_ADMIN_URL), so
// Dev needs no new env var; a NEXT_PUBLIC_* override exists for when there is a prod hostname. The
// overrides must be referenced literally (Next inlines NEXT_PUBLIC_* at build time).
//
// Deep links are built from here only: no free-form return URL (open redirect) and never a token in
// the URL. Each app keeps its own login until Cognito SSO (ADR 0006).

export const DEV_BOOKING_ADMIN_URL = "https://aa-booking-admin.vercel.app";
export const DEV_TRIPPLANNER_URL = "https://aa-tripplanner.vercel.app";

function trimSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

/** AA-Booking staff admin (separate Vercel project, signs in with the CIS staff account). */
export const BOOKING_ADMIN_URL = trimSlash(process.env.NEXT_PUBLIC_BOOKING_ADMIN_URL || DEV_BOOKING_ADMIN_URL);

/** TripPlanner public site (no admin of its own, design §6.4). */
export const TRIPPLANNER_URL = trimSlash(process.env.NEXT_PUBLIC_TRIPPLANNER_URL || DEV_TRIPPLANNER_URL);

// No "Tenant portal" entry: the portal needs a tenant session (cis_tenant_token), so an admin opening
// it sees an empty shell. It comes back with a real support view (tenant picker / impersonation);
// until then tenant support is Tenant 360 (/admin/tenants).

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
