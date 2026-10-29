// app/_kit/index.ts
// AA-662 — barrel for the shared UI kit. UI-v2 pages import from "@/app/_kit" (or a relative path
// to this file). The kit is the single source for DataTable, Drawer, Modal, PageHeader, Toast,
// Tabs, Badge/StatusBadge, EmptyState/ErrorState/Skeleton, plus the react-query fetch helpers.
//
// AA-994 S1a (root ADR 0016): the portable part of the kit lives in ./core, the canonical copy that AA-Partner,
// AA-Booking and AA-TripPlanner-Web vendor byte for byte. This barrel re-exports it next to the CIS-local parts
// (API helpers, the admin shell and nav, the guide checks), so pages keep importing "@/app/_kit" and never ./core.

// ── kit core (portable) ────────────────────────────────────────────────────────────────────────────────────
export * from "./core/tokens";
export { BRAND, FONT_DISPLAY, FONT_SANS, FONT_MONO, LOGO_SRC, SUPPORT_EMAIL, SITE_URL } from "./core/brand";
export * from "./core/primitives";
export * from "./core/Modal";
export * from "./core/Drawer";
export * from "./core/Toast";
export * from "./core/DataTable";
export * from "./core/csv";
export * from "./core/savedViews";
export { Providers } from "./core/Providers";
// S252 page frame; `PageFrame*` is the AA-994 name, `AdminMain` / `AdminPane*` the aliases pages use today.
export {
  PageFrame, PageFrameHeader, PageFrameBody, AdminMain, AdminPaneHeader, AdminPaneBody, type PageFrameWidth,
} from "./core/PageFrame";
// AA-919 — the Guide tab pattern: every admin page with a Guide tab keeps a GuideContent in
// app/admin/<page>/guide.ts and renders it with GuidePanel; the shape is core/guide.ts, the CIS checks are
// guideContent.ts (which re-exports the shape).
export * from "./guideContent";
export { GuidePanel } from "./core/GuidePanel";
export { useMediaQuery, useIsPhone, PHONE_QUERY } from "./core/useMediaQuery";
// AA-994 S4a — upstreamed from AA-Booking: card region, KPI tile, URL-backed filter bar, row action menu, date field,
// money / date text and their pure formatters, and the gallery that shows them (admin Settings → UI kit).
export { Section } from "./core/Section";
export { KpiTile } from "./core/KpiTile";
export {
  FilterBar, FilterField, useUrlFilters, filterControlStyle, FILTER_CONTROL_CLASS, type FilterSelect,
} from "./core/FilterBar";
export { RowMenu, type RowMenuItem } from "./core/RowMenu";
export { DateField } from "./core/DateField";
export { Money } from "./core/Money";
export { DateText, relativeTime } from "./core/DateText";
export {
  formatMoney, minorUnits, formatDay, isIsoDay, parseDayInput, relativeParts, type RelativeParts,
} from "./core/format";
export { KitGallery } from "./core/KitGallery";
export { useSidebarCollapsed } from "./core/useSidebar";
// AA-994 — UI language (EN default, VI second): one typed catalogue per area (`messages.ts`), `useT(M)` in components.
export {
  defineMessages, localized, missingTranslations, parseLang, translator, DEFAULT_LANG, LANGS, LANG_LABEL,
  type Lang, type Messages, type Translator,
} from "./core/i18n";
export { I18nProvider, useLang, useSetLang, useT, useLocalized, useFormat } from "./core/useI18n";
export { KIT_MESSAGES } from "./core/kit.messages";
export { LangSwitch } from "./core/LangSwitch";

// ── CIS-local ──────────────────────────────────────────────────────────────────────────────────────────────
export * from "./api";
export { langStore, LANG_COOKIE } from "./i18n";
export { SHELL_MESSAGES } from "./shell.messages";
export { NAV_MESSAGES } from "./nav.messages";

// AA-752 — admin shell (collapsible sidebar, topbar, ⌘K command palette, phone bottom nav).
export { AdminShell } from "./AdminShell";
export { Sidebar } from "./Sidebar";
export { Topbar } from "./Topbar";
export { CommandPalette } from "./CommandPalette";
export { BottomNav } from "./BottomNav";
export * from "./adminNav";
export { useAdminIdentity } from "./useAdminIdentity";
export { useReviewPendingCount, REVIEW_PENDING_KEY } from "./useReviewPendingCount";
