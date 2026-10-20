// portal/lib/hosts.ts
// AA-885 — URLs the portal links to. It never links to the staff console (separate app, separate host). Code constants, no env var (same pattern as
// frontend/lib/apps.ts). The portal itself is https://portal.lumiguides.it.com.

/** The portal's legal pages (AA-886: app/privacy and app/terms, drafts pending legal review). */
export const PRIVACY_PATH = "/privacy";
export const TERMS_PATH = "/terms";

/** Adventure Asia's company-wide privacy policy, linked from the portal notice (200 on 20/10/2026). */
export const COMPANY_PRIVACY_URL = "https://adventure.asia/privacy-policy";
