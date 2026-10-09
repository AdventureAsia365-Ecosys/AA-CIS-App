"use client";
// app/admin/_components/AdminSidebar.tsx
// AA-752 — RETIRED. The per-page sidebar was replaced by the one shared admin shell rendered once
// in app/admin/layout.tsx (app/_kit/AdminShell.tsx → Sidebar + Topbar + CommandPalette + BottomNav).
// No page renders a per-page sidebar any more. This thin re-export is kept only so any stray import
// still resolves to the kit Sidebar; delete it once nothing imports it.
export { Sidebar as default } from "../../_kit/Sidebar";
