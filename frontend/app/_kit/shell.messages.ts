// app/_kit/shell.messages.ts
// AA-994 — the admin shell's messages (topbar). English is the source; the Vietnamese side must carry every key
// (tsc). Dictionary files (`messages.ts`, `*.messages.ts`) are the only frontend files where Vietnamese may appear
// (tests/unit/test_aa994_admin_i18n.py).

import { defineMessages } from "./core/i18n";

export const SHELL_MESSAGES = defineMessages(
  {
    language: "Language",
    openMenu: "Open navigation menu",
    openSearch: "Open global search",
    searchPlaceholder: "Search tours, tenants, jobs…",
    search: "Search",
    admin: "Admin",
    themeLight: "Light",
    themeDark: "Dark",
    themeSystem: "System",
    themeTitle: (current: string, next: string) => `Theme: ${current} — click for ${next}`,
    themeAria: (current: string, next: string) => `Theme: ${current}. Switch to ${next}`,
    notifications: "Notifications",
    markAllRead: "Mark all read",
    noNotifications: "No notifications",
    accountMenu: "Account menu",
    settings: "Settings",
    signOut: "Sign out",
  },
  {
    language: "Ngôn ngữ",
    openMenu: "Mở menu điều hướng",
    openSearch: "Mở tìm kiếm",
    searchPlaceholder: "Tìm tour, tenant, job…",
    search: "Tìm kiếm",
    admin: "Quản trị",
    themeLight: "Sáng",
    themeDark: "Tối",
    themeSystem: "Theo hệ thống",
    themeTitle: (current: string, next: string) => `Giao diện: ${current} — bấm để chuyển sang ${next}`,
    themeAria: (current: string, next: string) => `Giao diện: ${current}. Chuyển sang ${next}`,
    notifications: "Thông báo",
    markAllRead: "Đánh dấu đã đọc hết",
    noNotifications: "Không có thông báo",
    accountMenu: "Menu tài khoản",
    settings: "Cài đặt",
    signOut: "Đăng xuất",
  },
);
