// portal/app/portal/_shell/portalNav.tsx
// AA-759 — the single portal navigation model, shared by the shell sidebar, the topbar breadcrumb,
// the ⌘K palette (Pages group) and the phone bottom bar. Routes carry no T-number slug any more;
// the old slugs answer 308 in next.config.ts (OLD_SLUG_REDIRECTS there mirrors ROUTES here).
//
// Groups follow the tenant's workflow: Tours → Brand → Create → Content → Account.

import {
  LayoutDashboard, Globe2, BookOpen, Store, Sparkles, Share2, FileText, Eye, CreditCard,
  Activity, Settings, Code2, Plug,
} from "lucide-react";

/** Every portal page path, in one place (links in components use these, not string literals). */
export const ROUTES = {
  dashboard: "/portal/dashboard",
  tours: "/portal/tours",
  myTours: "/portal/tours/mine",
  marketplace: "/portal/tours/marketplace",
  brand: "/portal/brand",
  createSocial: "/portal/create/social",
  createArticle: "/portal/create/article",
  createWrite: "/portal/create/write",
  content: "/portal/content",
  publish: "/portal/content/publish",
  billing: "/portal/account/billing",
  activity: "/portal/account/activity",
  settings: "/portal/account/settings",
  api: "/portal/account/api",
  integrations: "/portal/account/integrations",
  helpFacebook: "/portal/help/facebook",
} as const;

export interface NavEntry {
  href: string;
  label: string;
  icon: React.ReactNode;
  /** Extra words the ⌘K palette matches on. */
  keywords?: string;
  /** Other routes that belong to this entry (sub-pages), for the active state. */
  also?: string[];
  /** Which shell count to show next to the label. */
  count?: "pool" | "catalog";
  /** Titled in the topbar but not offered by ⌘K (a page that needs a deep-link parameter). */
  paletteHidden?: boolean;
}

export interface NavGroup {
  label: string;
  items: NavEntry[];
}

export const NAV_GROUPS: NavGroup[] = [
  { label: "Overview", items: [
    { href: ROUTES.dashboard, label: "Dashboard", icon: <LayoutDashboard size={16} />, keywords: "home overview usage quota" },
  ] },
  { label: "Tours", items: [
    { href: ROUTES.tours, label: "Browse Tours", icon: <Globe2 size={16} />, keywords: "pool rewrite published search", count: "pool" },
    { href: ROUTES.myTours, label: "My Tours", icon: <BookOpen size={16} />, keywords: "catalog versions rewritten export", count: "catalog" },
    // AA-760 — "Marketplace" read as a place to find or buy tours; the page is an overview of the
    // tenant's own written tours (price, status). Route unchanged (bookmarks keep working).
    { href: ROUTES.marketplace, label: "Tour overview", icon: <Store size={16} />, keywords: "marketplace overview portfolio price summary" },
  ] },
  { label: "Brand", items: [
    { href: ROUTES.brand, label: "Brand Identity", icon: <Sparkles size={16} />, keywords: "brand voice tone cta rules" },
  ] },
  { label: "Create", items: [
    { href: ROUTES.createSocial, label: "Social Posts", icon: <Share2 size={16} />, keywords: "social slate linkedin facebook instagram tiktok email ads write", also: [ROUTES.createWrite] },
    { href: ROUTES.createArticle, label: "Articles", icon: <FileText size={16} />, keywords: "blog article write seo" },
  ] },
  { label: "Content", items: [
    { href: ROUTES.content, label: "My Content", icon: <Eye size={16} />, keywords: "written posts review publish wordpress", also: [ROUTES.publish] },
  ] },
  { label: "Account", items: [
    { href: ROUTES.billing, label: "Billing", icon: <CreditCard size={16} />, keywords: "plan invoice upgrade quota" },
    { href: ROUTES.activity, label: "Activity", icon: <Activity size={16} />, keywords: "history log" },
    { href: ROUTES.settings, label: "Account settings", icon: <Settings size={16} />, keywords: "account team sign out profile" },
    { href: ROUTES.api, label: "API", icon: <Code2 size={16} />, keywords: "api key docs developer playground" },
    { href: ROUTES.integrations, label: "Integrations", icon: <Plug size={16} />, keywords: "wordpress facebook connect channel connection" },
  ] },
];

/** Pages reachable by link only (not in the sidebar) — still titled and searchable. */
const EXTRA_PAGES: NavEntry[] = [
  { href: ROUTES.publish, label: "Publish", icon: <Eye size={16} />, keywords: "publish wordpress facebook unpublish" },
  { href: ROUTES.createWrite, label: "Write Content", icon: <Share2 size={16} />, paletteHidden: true },
  { href: ROUTES.helpFacebook, label: "Connect your Facebook Page", icon: <Plug size={16} />, keywords: "help facebook guide token" },
];

export function allEntries(): NavEntry[] {
  return [...NAV_GROUPS.flatMap((g) => g.items), ...EXTRA_PAGES];
}

/** Active when the path is the entry, one of its `also` routes, or below one of them. */
export function navActive(entry: Pick<NavEntry, "href" | "also">, pathname: string): boolean {
  const targets = [entry.href, ...(entry.also ?? [])];
  return targets.some((t) => pathname === t || (t !== ROUTES.tours && pathname.startsWith(t + "/")));
}

/** Topbar title for a path: the most specific entry wins (Publish over My Content). */
export function pageTitle(pathname: string): string {
  const exact = allEntries().find((e) => e.href === pathname);
  if (exact) return exact.label;
  const owner = NAV_GROUPS.flatMap((g) => g.items).find((e) => navActive(e, pathname));
  return owner?.label ?? "";
}

/** Topbar crumb root: the nav group of the page ("Help" for the help pages, "Workspace" otherwise
 *  outside the nav). */
export function pageGroup(pathname: string): string {
  const g = NAV_GROUPS.find((grp) => grp.items.some((e) => navActive(e, pathname)));
  if (g && g.label !== "Overview") return g.label;
  return pathname.startsWith("/portal/help/") ? "Help" : "Workspace";
}

/** Phone bottom bar: the 4 daily destinations; "More" opens the drawer with everything. */
export const BOTTOM_ITEMS: NavEntry[] = [
  NAV_GROUPS[0].items[0],
  { ...NAV_GROUPS[1].items[0], label: "Tours", also: [ROUTES.myTours, ROUTES.marketplace] },
  { ...NAV_GROUPS[3].items[0], label: "Create", also: [ROUTES.createArticle, ROUTES.createWrite] },
  { ...NAV_GROUPS[4].items[0], label: "Content" },
];
