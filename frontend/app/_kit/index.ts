// app/_kit/index.ts
// AA-662 — barrel for the shared UI kit. UI-v2 pages import from "@/app/_kit" (or a relative path
// to this file). The kit is the single source for DataTable, Drawer, Modal, PageHeader, Toast,
// Tabs, Badge/StatusBadge, EmptyState/ErrorState/Skeleton, plus the react-query fetch helpers.

export * from "./tokens";
export * from "./primitives";
export * from "./Modal";
export * from "./Drawer";
export * from "./Toast";
export * from "./DataTable";
export * from "./csv";
export * from "./savedViews";
export * from "./api";
export { Providers } from "./Providers";

// AA-919 — the Guide tab pattern: every admin page with a Guide tab keeps a GuideContent in
// app/admin/<page>/guide.ts and renders it with GuidePanel (see guideContent.ts for the rules).
export { AdminMain, AdminPaneHeader, AdminPaneBody } from "./AdminMain";
export * from "./guideContent";
export { GuidePanel } from "./GuidePanel";

// AA-752 — admin shell (collapsible sidebar, topbar, ⌘K command palette, phone bottom nav).
export { AdminShell } from "./AdminShell";
export { Sidebar } from "./Sidebar";
export { Topbar } from "./Topbar";
export { CommandPalette } from "./CommandPalette";
export { BottomNav } from "./BottomNav";
export * from "./adminNav";
export { useSidebarCollapsed } from "./useSidebar";
export { useAdminIdentity } from "./useAdminIdentity";
export { useMediaQuery, useIsPhone, PHONE_QUERY } from "./useMediaQuery";
export { useReviewPendingCount, REVIEW_PENDING_KEY } from "./useReviewPendingCount";
