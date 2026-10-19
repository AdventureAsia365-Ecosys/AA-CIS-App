// portal/lib/hosts.ts
// AA-885 — cross-app URLs the portal links to. Code constants, no env var (same pattern as
// frontend/lib/apps.ts). The portal itself is https://portal.lumiguides.it.com.

/** The CIS admin console (staff sign in there, never on the portal). */
export const ADMIN_BASE_URL = "https://aa-cis.lumiguides.it.com";

/** Staff login on the admin console — the "← Staff login" link on /tenant-login. */
export const STAFF_LOGIN_URL = `${ADMIN_BASE_URL}/login`;

/** The portal's legal links (next.config.ts redirects /privacy; /terms is app/terms/page.tsx). */
export const PRIVACY_PATH = "/privacy";
export const TERMS_PATH = "/terms";
