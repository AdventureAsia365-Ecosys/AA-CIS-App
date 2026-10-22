// portal/app/_kit/index.ts
// AA-885 — the portal's barrel for its VENDORED copy of the admin UI kit. Every other file in this
// folder is a byte copy of frontend/app/_kit/<same name> (guarded by
// tests/unit/test_aa885_portal_split.py); this barrel is portal-owned and exports only the files the
// portal has, so no admin shell code (AdminShell, Sidebar, adminNav, SessionKeepAlive …) can be
// imported here. To change a kit component, edit frontend/app/_kit and copy it here in the same PR.
// AA-759 — the theme + sidebar-collapse stores (and useHydrated) are vendored too: generic, no admin
// identity in them; the portal shell (app/portal/_shell) uses them.

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
export { useMediaQuery, useIsPhone, PHONE_QUERY } from "./useMediaQuery";
export { useSidebarCollapsed } from "./useSidebar";
export { useThemeChoice, useResolvedTheme } from "./useTheme";
export type { ThemeChoice, ResolvedTheme } from "./theme";
export { useHydrated } from "./useHydrated";
