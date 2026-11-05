"use client";
// app/_kit/adminNav.tsx
// AA-752 — the single admin navigation model, shared by the kit Sidebar, Topbar breadcrumb,
// CommandPalette (Pages group) and BottomNav. Visibility follows lib/rbac.ts PAGE_RULES (AA-656): an
// item renders only when the role may open its page.
//
// AA-1039 R6 slice 1 (S266, root ADR 0019 d13) — the groups, their order and every href come from lib/adminIa.ts
// (plain data, also read by next.config.ts for the redirects of the pages that moved and by lib/adminIa.test.mjs).
// This file only adds the icons. Seven groups: Home · Tenants · AA Catalogue · Tenant content QA · AI & spend ·
// Writing Lab · Platform. The S241 split into two foldable sections (Platform / CIS Content) and the standalone
// Settings footer entry are gone: Settings sits in AA Catalogue, and every group folds on its own (kit NavGroup, S264).
// A `hidden` slot of the model (Tenants › Plans & add-ons) is left out everywhere until its page ships.
//
// AA-1037 (root ADR 0019 d9 "Screens now"): an eighth group, Trade, whose items are `external` pages of Booking admin.
// Their href is the full URL from the app registry (lib/apps.ts appPageUrl, nothing else builds it); the sidebar and
// the ⌘K palette open them in the same tab (followExternal: a full page load, not the Next router); they never match
// the current route (no active state, no breadcrumb); a role sees them through lib/rbac.ts canSeeNavItem.

import {
  Cpu, Gauge, Users, Upload, Wand2, ClipboardList, Library, Settings, Wallet, UserCog, Radio,
  Puzzle, ListChecks, Scale, Search, Image as ImageIcon, BookOpen, BarChart3, ShieldAlert, ShieldCheck,
  Boxes, ScrollText, FlaskConical, BellRing, MapPin, Network, Eraser, Building2, FileText, Hotel, Table2, BookMarked, Quote,
  Newspaper, CreditCard, FileStack, PenLine, UserCheck, ChartColumn, Palette, MapPinned,
  CalendarCheck, Receipt, Layers, Briefcase,
} from "lucide-react";
import { useCallback } from "react";
import { canSeeNavItem, type Capability } from "@/lib/rbac";
import { ADMIN_IA, type NavIcon } from "@/lib/adminIa";
import { appPageUrl } from "@/lib/apps";
import { useLang } from "./core/useI18n";
import { NAV_MESSAGES } from "./nav.messages";

export interface NavEntry {
  href: string;
  icon: React.ReactNode;
  label: string;
  /** Other routes that belong to this entry (its sub-nav pages) — used for active-state matching. */
  also?: string[];
  /** AA-1037: a page of another app (href is its full URL): same-tab full load, never the active entry. */
  external?: boolean;
  /** AA-1037: for an external entry, the capabilities that show it (lib/rbac.ts canSeeNavItem). */
  capabilities?: readonly Capability[];
}

export interface NavGroup {
  label: string;
  items: NavEntry[];
}

const ICONS: Record<NavIcon, React.ReactNode> = {
  overview: <Gauge size={16} />,
  appHealth: <Boxes size={16} />,
  reports: <Newspaper size={16} />,
  dataCoverage: <Table2 size={16} />,
  tenants: <Users size={16} />,
  agencies: <Building2 size={16} />,
  erasure: <Eraser size={16} />,
  plans: <CreditCard size={16} />,
  assignments: <UserCheck size={16} />,
  upload: <Upload size={16} />,
  rewrite: <Wand2 size={16} />,
  review: <ClipboardList size={16} />,
  master: <Library size={16} />,
  atoms: <Puzzle size={16} />,
  places: <MapPin size={16} />,
  placeCards: <MapPinned size={16} />,
  photos: <ImageIcon size={16} />,
  facts: <Quote size={16} />,
  supply: <Hotel size={16} />,
  aaContent: <FileText size={16} />,
  seo: <Search size={16} />,
  settings: <Settings size={16} />,
  quality: <ShieldCheck size={16} />,
  trace: <Radio size={16} />,
  reviewLog: <BarChart3 size={16} />,
  slate: <FileStack size={16} />,
  compliance: <ShieldAlert size={16} />,
  aiGateway: <Cpu size={16} />,
  spend: <Wallet size={16} />,
  usage: <ChartColumn size={16} />,
  lab: <FlaskConical size={16} />,
  writingMemory: <PenLine size={16} />,
  decisions: <Scale size={16} />,
  staff: <UserCog size={16} />,
  audit: <ScrollText size={16} />,
  jobs: <ListChecks size={16} />,
  alerts: <BellRing size={16} />,
  apiDocs: <BookOpen size={16} />,
  dataModel: <Network size={16} />,
  docs: <BookMarked size={16} />,
  uiKit: <Palette size={16} />,
  bookingAdmin: <CalendarCheck size={16} />,
  agencyBilling: <Receipt size={16} />,
  agencyTiers: <Layers size={16} />,
  b2bDesk: <Briefcase size={16} />,
};

/** The navigation groups in display order, reserved (`hidden`) slots left out. */
export const NAV_GROUPS: NavGroup[] = ADMIN_IA.map((g) => ({
  label: g.label,
  items: g.items.filter((it) => !it.hidden).map((it) => ({
    href: it.external ? appPageUrl(it.external, it.href) : it.href, label: it.label, icon: ICONS[it.icon],
    ...(it.also ? { also: it.also } : {}),
    ...(it.external ? { external: true, capabilities: it.capabilities ?? [] } : {}),
  })),
}));

/** The groups `role` sees, each with only the items the role may open; empty groups left out. */
export function visibleGroups(role: string): NavGroup[] {
  return NAV_GROUPS
    .map((g) => ({ ...g, items: g.items.filter((n) => canSeeNavItem(role, n)) }))
    .filter((g) => g.items.length > 0);
}

/** AA-1037: go to an external entry (another app) in the same tab, a full page load; false for a CIS route. */
export function followExternal(entry: Pick<NavEntry, "href" | "external">): boolean {
  if (!entry.external) return false;
  window.location.assign(entry.href);
  return true;
}

/** Does `href` (or any of its `also` routes) match the current pathname? An external entry never does. */
export function navActive(entry: Pick<NavEntry, "href" | "also" | "external">, pathname: string): boolean {
  if (entry.external) return false;
  const all = [entry.href, ...(entry.also ?? [])];
  return all.some((h) => pathname === h || pathname.startsWith(h + "/"));
}

/** The entry of the page at `pathname`: the longest matching href wins (/admin/tenants/agencies is CRM agencies,
 *  not the Directory at /admin/tenants). */
function activeEntry(pathname: string): { group: NavGroup; entry: NavEntry } | null {
  let best: { group: NavGroup; entry: NavEntry; len: number } | null = null;
  for (const group of NAV_GROUPS) {
    for (const entry of group.items) {
      if (entry.external) continue;   // AA-1037: another app's page is never the current one
      for (const h of [entry.href, ...(entry.also ?? [])]) {
        if ((pathname === h || pathname.startsWith(h + "/")) && (!best || h.length > best.len)) {
          best = { group, entry, len: h.length };
        }
      }
    }
  }
  return best ? { group: best.group, entry: best.entry } : null;
}

/** Is `entry` the nav entry of the page at `pathname`? (one entry at a time, even when hrefs nest) */
export function isActiveEntry(entry: Pick<NavEntry, "href" | "external">, pathname: string): boolean {
  if (entry.external) return false;
  return activeEntry(pathname)?.entry.href === entry.href;
}

/** The group of the page at `pathname`, or null outside the nav. */
export function activeGroup(pathname: string): NavGroup | null {
  return activeEntry(pathname)?.group ?? null;
}

/** Flat list of every entry, filtered by role. */
export function allEntries(role: string): NavEntry[] {
  return visibleGroups(role).flatMap((g) => g.items);
}

/** The group label of the page at `pathname` (breadcrumb root), or null outside the nav. */
export function pageSection(pathname: string): string | null {
  return activeGroup(pathname)?.label ?? null;
}

/** The human page title for the breadcrumb (<Group> / <title>). Falls back to a prettified slug. */
export function pageTitle(pathname: string): string {
  const hit = activeEntry(pathname);
  if (hit) return hit.entry.label;
  const slug = pathname.replace(/^\/admin\/?/, "").split("/")[0] || "";
  if (!slug) return "Admin";
  return slug.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * AA-994 — a nav label (group, entry, as this module carries them in English) in the UI language
 * (nav.messages.ts). A label without a translation is returned as is.
 */
export function useNavLabel(): (label: string) => string {
  const lang = useLang();
  return useCallback((label: string) => {
    const table = NAV_MESSAGES[lang] as Readonly<Record<string, string>>;
    return table[label] ?? label;
  }, [lang]);
}
