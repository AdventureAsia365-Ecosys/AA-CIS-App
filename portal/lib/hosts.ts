// portal/lib/hosts.ts
// AA-885 — cross-app URLs the portal links to. Code constants, no env var (same pattern as
// frontend/lib/apps.ts). The portal itself is https://portal.lumiguides.it.com.

/** The CIS admin console (staff sign in there, never on the portal). */
export const ADMIN_BASE_URL = "https://aa-cis.lumiguides.it.com";

/** Staff login on the admin console — the "← Staff login" link on /tenant-login. */
export const STAFF_LOGIN_URL = `${ADMIN_BASE_URL}/login`;

/** The portal's legal pages (AA-886: app/privacy and app/terms, drafts pending legal review). */
export const PRIVACY_PATH = "/privacy";
export const TERMS_PATH = "/terms";

/** Adventure Asia's company-wide privacy policy, linked from the portal notice (200 on 20/10/2026). */
export const COMPANY_PRIVACY_URL = "https://adventure.asia/privacy-policy";
