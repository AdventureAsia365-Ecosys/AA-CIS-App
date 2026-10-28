"use client";
// app/_kit/adminNav.tsx
// AA-752 — the single admin navigation model, shared by the kit Sidebar, Topbar breadcrumb,
// CommandPalette (Pages group) and BottomNav. Visibility follows lib/rbac.ts PAGE_RULES (AA-656): an
// item renders only when the role may open its page.
//
// AA-883 (S241 admin console, design §4) — groups belong to one of two sections, named after who the
// page serves (not how it runs):
//   - Platform: people, tenants (accounts), machines and money — Overview · People & access ·
//     Apps · Tenants · Jobs · AI & spend · Developer (AA-884 added Audit log, Apps, Cost per app);
//   - CIS Content: what CIS produces, including the QA of tenant output — Pipeline · Assets ·
//     Intelligence · Tenant output.
// No URL changed. Settings stays the standalone footer entry until it is split (design GĐ6). The
// other apps (Booking admin, TripPlanner, tenant portal) are links from lib/apps.ts, not nav entries.

import {
  Gauge, Users, Upload, Wand2, ClipboardList, Library, Settings, Wallet, UserCog, Radio,
  Puzzle, ListChecks, Scale, Search, Image as ImageIcon, BookOpen, BarChart3, ShieldCheck,
  Boxes, ScrollText, ChartPie, BellRing, MapPin, Network, Eraser, Building2, FileText, Hotel, Table2,
} from "lucide-react";
import { canSeePage } from "@/lib/rbac";

export interface NavEntry {
  href: string;
  icon: React.ReactNode;
  label: string;
  /** Other routes that belong to this entry (its sub-nav pages) — used for active-state matching. */
  also?: string[];
}

export type NavSectionId = "platform" | "cis";

export interface NavSection {
  id: NavSectionId;
  label: string;
}

export const NAV_SECTIONS: NavSection[] = [
  { id: "platform", label: "Platform" },
  { id: "cis", label: "CIS Content" },
];

export interface NavGroup {
  label: string;
  section: NavSectionId;
  items: NavEntry[];
}

export const NAV_GROUPS: NavGroup[] = [
  // ── Platform ──
  { label: "Overview", section: "platform", items: [
    { href: "/admin/overview", icon: <Gauge size={16} />, label: "Overview" },
    { href: "/admin/data-coverage", icon: <Table2 size={16} />, label: "Data coverage" },  // AA-992
  ] },
  { label: "People & access", section: "platform", items: [
    { href: "/admin/platform/staff", icon: <UserCog size={16} />, label: "Staff" },  // AA-883
    { href: "/admin/platform/audit", icon: <ScrollText size={16} />, label: "Audit log" },  // AA-884
    { href: "/admin/platform/alerts", icon: <BellRing size={16} />, label: "Alert recipients" },  // AA-913
  ] },
  // AA-884 — health and running commit of each ecosystem app (CIS api/worker, Booking, TripPlanner).
  { label: "Apps", section: "platform", items: [
    { href: "/admin/platform/apps", icon: <Boxes size={16} />, label: "Apps" },
  ] },
  { label: "Tenants", section: "platform", items: [
    { href: "/admin/tenants", icon: <Users size={16} />, label: "Tenants" },
    { href: "/admin/platform/agencies", icon: <Building2 size={16} />, label: "Agencies (CRM)" },  // AA-941
  ] },
  { label: "Jobs", section: "platform", items: [
    { href: "/admin/jobs", icon: <ListChecks size={16} />, label: "Jobs" },
    { href: "/admin/data-erasure", icon: <Eraser size={16} />, label: "Data erasure" },  // AA-839
  ] },
  { label: "AI & spend", section: "platform", items: [
    { href: "/admin/cost-ops", icon: <Wallet size={16} />, label: "Cost Operations" },
    { href: "/admin/platform/cost", icon: <ChartPie size={16} />, label: "Cost per app" },  // AA-884
    { href: "/admin/decisions", icon: <Scale size={16} />,  label: "Jev Decisions" },
  ] },
  { label: "Developer", section: "platform", items: [
    { href: "/admin/api-docs", icon: <BookOpen size={16} />, label: "API Docs" },  // AA-577 follow-up
    { href: "/admin/data-model", icon: <Network size={16} />, label: "Data model" },  // AA-894 — live ERD
  ] },
  // ── CIS Content ──
  { label: "Pipeline", section: "cis", items: [
    { href: "/admin/upload",         icon: <Upload size={16} />,        label: "Upload (S0)" },
    { href: "/admin/s1-rewrite",     icon: <Wand2 size={16} />,         label: "Rewrite (S1)" },
    { href: "/admin/review",         icon: <ClipboardList size={16} />, label: "Review Queue" },
    { href: "/admin/master-content", icon: <Library size={16} />,       label: "Master Content" },
  ] },
  { label: "Assets", section: "cis", items: [
    { href: "/admin/photos", icon: <ImageIcon size={16} />, label: "Photos" },
    { href: "/admin/places", icon: <MapPin size={16} />, label: "Places" },  // AA-847
    { href: "/admin/aa-content", icon: <FileText size={16} />, label: "AA content (CRM)" },  // AA-977
    { href: "/admin/supply-content", icon: <Hotel size={16} />, label: "Supply (Booking)" },  // AA-835 (S251: shorter, "Supply content (Booking)" was cut at 1440)
  ] },
  { label: "Intelligence", section: "cis", items: [
    { href: "/admin/atom-curation",    icon: <Puzzle size={16} />, label: "Social Content" },
    { href: "/admin/seo-intelligence", icon: <Search size={16} />, label: "SEO Intelligence" },
  ] },
  { label: "Tenant output", section: "cis", items: [
    { href: "/admin/tenant-activity", icon: <Radio size={16} />,       label: "Content Trace" },
    { href: "/admin/platform-stats",  icon: <BarChart3 size={16} />,   label: "A4 Quality" },
    { href: "/admin/content-quality", icon: <ShieldCheck size={16} />, label: "Content Quality" },
    { href: "/admin/writing-memory", icon: <BookOpen size={16} />, label: "Writing memory" },  // AA-917
  ] },
];

/** The groups of one section. */
export function sectionGroups(section: NavSectionId): NavGroup[] {
  return NAV_GROUPS.filter((g) => g.section === section);
}

/** The sections `role` sees at least one page of, in display order. */
export function visibleSections(role: string): NavSection[] {
  return NAV_SECTIONS.filter((s) =>
    sectionGroups(s.id).some((g) => g.items.some((it) => canSeePage(role, it.href))),
  );
}

/** The standalone Settings entry (all staff roles, same as proxy.ts). */
export const SETTINGS_ENTRY: NavEntry = {
  href: "/admin/settings", icon: <Settings size={16} />, label: "Settings",
};

/** Does `href` (or any of its `also` routes) match the current pathname? */
export function navActive(entry: Pick<NavEntry, "href" | "also">, pathname: string): boolean {
  const all = [entry.href, ...(entry.also ?? [])];
  return all.some((h) => pathname === h || pathname.startsWith(h + "/"));
}

/** Flat list of every entry (incl. Settings), filtered by role. */
export function allEntries(role: string): NavEntry[] {
  const out: NavEntry[] = [];
  for (const g of NAV_GROUPS) {
    for (const it of g.items) {
      if (canSeePage(role, it.href)) out.push(it);
    }
  }
  if (canSeePage(role, SETTINGS_ENTRY.href)) out.push(SETTINGS_ENTRY);
  return out;
}

/** The section label of the page at `pathname` (breadcrumb root), or null outside the nav. */
export function pageSection(pathname: string): string | null {
  for (const g of NAV_GROUPS) {
    if (g.items.some((it) => navActive(it, pathname))) {
      return NAV_SECTIONS.find((s) => s.id === g.section)?.label ?? null;
    }
  }
  return null;
}

/** The human page title for the breadcrumb (<Section> / <title>). Falls back to a prettified slug. */
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
