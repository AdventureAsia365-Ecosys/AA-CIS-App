"use client";
// app/_kit/adminNav.tsx
// AA-752 — the single admin navigation model, shared by the kit Sidebar, Topbar breadcrumb,
// CommandPalette (Pages group) and BottomNav. Groups/items/roles are identical to the AA-663
// information architecture that lived in AdminSidebar before the shell: Overview · Content ·
// Intelligence · Tenants · Operations, plus the standalone Settings entry. Visibility follows
// middleware.ts PROTECTED_ROUTES (adminOnly items render for role "admin" only).

import {
  Gauge, Users, Upload, Wand2, ClipboardList, Library, Settings, Wallet,
  Puzzle, ListChecks, Scale, Search, Image as ImageIcon,
} from "lucide-react";

export interface NavEntry {
  href: string;
  icon: React.ReactNode;
  label: string;
  adminOnly?: boolean;
  /** Other routes that belong to this entry (its sub-nav pages) — used for active-state matching. */
  also?: string[];
}

export interface NavGroup {
  label: string;
  items: NavEntry[];
}

export const NAV_GROUPS: NavGroup[] = [
  { label: "Overview", items: [
    { href: "/admin/overview", icon: <Gauge size={16} />, label: "Overview", adminOnly: true },
  ] },
  { label: "Content", items: [
    { href: "/admin/upload",         icon: <Upload size={16} />,        label: "Upload (S0)" },
    { href: "/admin/s1-rewrite",     icon: <Wand2 size={16} />,         label: "Rewrite (S1)" },
    { href: "/admin/review",         icon: <ClipboardList size={16} />, label: "Review Queue" },
    { href: "/admin/master-content", icon: <Library size={16} />,       label: "Master Content" },
    { href: "/admin/photos",         icon: <ImageIcon size={16} />,     label: "Photos", adminOnly: true },
  ] },
  { label: "Intelligence", items: [
    { href: "/admin/atom-curation", icon: <Puzzle size={16} />, label: "Social Content", adminOnly: true,
      also: ["/admin/tenant-activity", "/admin/platform-stats"] },
    { href: "/admin/seo-intelligence", icon: <Search size={16} />, label: "SEO Intelligence", adminOnly: true },
  ] },
  { label: "Tenants", items: [
    { href: "/admin/tenants", icon: <Users size={16} />, label: "Tenants", adminOnly: true },
  ] },
  { label: "Operations", items: [
    { href: "/admin/llm-usage", icon: <Wallet size={16} />,     label: "External Spend", adminOnly: true },
    { href: "/admin/jobs",      icon: <ListChecks size={16} />, label: "Jobs",           adminOnly: true },
    { href: "/admin/decisions", icon: <Scale size={16} />,      label: "Jev Decisions",  adminOnly: true },
  ] },
];

/** The standalone Settings entry (all staff roles, same as middleware.ts). */
export const SETTINGS_ENTRY: NavEntry = {
  href: "/admin/settings", icon: <Settings size={16} />, label: "Settings",
};

/** Does `href` (or any of its `also` routes) match the current pathname? */
export function navActive(entry: Pick<NavEntry, "href" | "also">, pathname: string): boolean {
  const all = [entry.href, ...(entry.also ?? [])];
  return all.some((h) => pathname === h || pathname.startsWith(h + "/"));
}

/** Flat list of every entry (incl. Settings), filtered by role. */
export function allEntries(isAdmin: boolean): NavEntry[] {
  const out: NavEntry[] = [];
  for (const g of NAV_GROUPS) {
    for (const it of g.items) {
      if (isAdmin || !it.adminOnly) out.push(it);
    }
  }
  out.push(SETTINGS_ENTRY);
  return out;
}

/** The human page title for the breadcrumb (Admin / <title>). Falls back to a prettified slug. */
export function pageTitle(pathname: string): string {
  for (const g of NAV_GROUPS) {
    for (const it of g.items) {
      if (navActive(it, pathname)) return it.label;
    }
  }
  if (navActive(SETTINGS_ENTRY, pathname)) return SETTINGS_ENTRY.label;
  const slug = pathname.replace(/^\/admin\/?/, "").split("/")[0] || "";
  if (!slug) return "Admin";
  return slug.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
