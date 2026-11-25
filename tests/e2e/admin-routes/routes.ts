// tests/e2e/admin-routes/routes.ts
// S292 (follow-up of the S291 hotfix, release safety) — the routes of the admin route check: EVERY page of the CIS
// admin that the administrator may open, derived from the code (each frontend/app/admin/**/page.tsx, gated by
// lib/rbac.ts PAGE_RULES), never a hand list, so a new page is covered the day it lands. Pure data and node:fs only
// (no Playwright import), so frontend/lib/adminRoutes.test.mjs checks it with node --test too.
//
// Why every administrator page and not only the admin-only ones: the per-PR visual run has no session, and the signed-in
// smoke / visual runs after a deploy sign in as the E2E viewer, which never opens an admin-only page and sees the shared
// pages without their write controls. Both gaps let a crash that only the administrator meets reach Dev (S291: the
// Ecosystem status page's ack and saved-view controls called useToast() without a ToastProvider). `adminOnly` marks
// the pages the viewer cannot open at all, for the report.
//
// Dynamic segments ([id], [...slug]) are opened with SAMPLE_ID: the pages read the id client-side and ask the API,
// which the stub answers "unavailable", so any value renders the page's own error state.

import { readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { canSeePage } from "../../../frontend/lib/rbac.ts";

export const ADMIN_ROLE = "admin";
/** The role of the CI smoke / visual E2E account (lib/rbac.ts READ_ONLY_ROLES). */
export const VIEWER_ROLE = "viewer";
/** An invented id for a dynamic segment (UUID-shaped, so a page that checks the shape still asks the API). */
export const SAMPLE_ID = "00000000-0000-4000-8000-000000000001";

export interface AdminRoute {
  /** The app route (frontend/app/<route>/page.tsx), dynamic segments in brackets. */
  route: string;
  /** The path the check opens. */
  path: string;
  /** The viewer (the E2E account of the signed-in smoke) cannot open it: no other CI run renders it. */
  adminOnly: boolean;
}

/** Every route under app/admin with a page.tsx: route groups "(x)" dropped, private folders "_x" skipped. */
export function adminPageRoutes(appDir: string): string[] {
  const root = join(appDir, "admin");
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir).sort()) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) {
        if (name.startsWith("_") || name === "node_modules") continue;
        walk(p);
      } else if (name === "page.tsx") {
        const rel = relative(appDir, dir).split(sep).filter((s) => !/^\(.*\)$/.test(s)).join("/");
        out.push(`/${rel}`);
      }
    }
  };
  walk(root);
  return out;
}

/** The path to open for a route: each [x], [...x] and [[...x]] segment becomes SAMPLE_ID. */
export function samplePath(route: string): string {
  return route
    .split("/")
    .map((s) => (/^\[{1,2}(\.\.\.)?[^\]]+\]{1,2}$/.test(s) ? SAMPLE_ID : s))
    .join("/");
}

/**
 * The routes of one run: every admin page the administrator may open, then the extra paths (query variants of those
 * pages, e.g. a tab, from the visual page list) whose route is one of them. Sorted by path, no duplicates.
 */
export function adminRoutes(appDir: string, extra: readonly { route: string; path: string }[] = []): AdminRoute[] {
  const routes = adminPageRoutes(appDir).filter((r) => canSeePage(ADMIN_ROLE, samplePath(r)));
  const known = new Set(routes);
  const seen = new Set<string>();
  const out: AdminRoute[] = [];
  const add = (route: string, path: string) => {
    if (seen.has(path)) return;
    seen.add(path);
    out.push({ route, path, adminOnly: !canSeePage(VIEWER_ROLE, path.split("?")[0]) });
  };
  for (const r of routes) add(r, samplePath(r));
  for (const e of extra) if (known.has(e.route) && !e.route.includes("[")) add(e.route, e.path);
  return out.sort((a, b) => a.path.localeCompare(b.path));
}

// --- what counts as a failure (pure, so the unit test pins it) -------------------------------------------------------

export interface PageSignals {
  /** The path the browser ended on (a redirect to /login means the stand-in session was refused). */
  finalPath: string;
  /** Uncaught exceptions (Playwright `pageerror`). */
  pageErrors: string[];
  /** console.error messages, each with the URL it points at (`location().url`; "" when none). */
  consoleErrors: { text: string; url: string }[];
  /** The page rendered the kit frame ([data-kit-frame], PageFrame) inside the admin shell. */
  frame: boolean;
  /** The text of the document body (to name Next's error page when the frame is gone). */
  bodyText: string;
}

// app/global-error.tsx, and Next's own page for a client exception with no boundary.
const BOUNDARY_TEXTS = ["Something went wrong", "Application error: a client-side exception has occurred"];

/**
 * Problems the check found on its first run that are already on main, by path: each tolerated only while it still
 * happens, with its reason and the follow-up that removes it. An entry that no longer matches fails the run (stale),
 * so the fix PR deletes the entry with the fix. Never add an entry for a crash that takes the page down.
 */
export const KNOWN_PROBLEMS: Record<string, { match: RegExp; reason: string }[]> = {
  "/admin/s1-rewrite": [{
    match: /^uncaught error: \{"detail":"admin_route_check_stub"/,
    reason: "the tour list read (fetchTours) rethrows a failed answer and nothing catches it: an unhandled rejection "
      + "while the API is unavailable; the page itself still renders. Follow-up: catch it and show the kit ErrorState.",
  }],
};

/** Split the problems of `path` into new ones (fail the run), tolerated known ones, and stale known entries. */
export function triage(path: string, problems: string[]): { fresh: string[]; known: string[]; stale: string[] } {
  const entries = KNOWN_PROBLEMS[path] ?? [];
  const fresh = problems.filter((p) => !entries.some((e) => e.match.test(p)));
  const known = problems.filter((p) => entries.some((e) => e.match.test(p)));
  const stale = entries.filter((e) => !problems.some((p) => e.match.test(p))).map((e) => e.reason);
  return { fresh, known, stale };
}

/** A console error the stub causes on purpose: the browser logs every non-2xx BFF answer as a failed resource. */
export function isStubbedResourceError(text: string, url: string, origin: string): boolean {
  if (!text.startsWith("Failed to load resource")) return false;
  if (!url.startsWith(origin)) return false;
  return new URL(url).pathname.startsWith("/api/");
}

/** The problems of one page visit; empty = the page rendered cleanly for the administrator. */
export function problemsOf(s: PageSignals, origin: string): string[] {
  const out: string[] = [];
  if (!s.finalPath.startsWith("/admin")) out.push(`left the admin for ${s.finalPath} (stand-in session refused?)`);
  for (const e of s.pageErrors) out.push(`uncaught error: ${e}`);
  for (const c of s.consoleErrors) {
    if (!isStubbedResourceError(c.text, c.url, origin)) out.push(`console error: ${c.text}`);
  }
  // A boundary replaces the whole admin tree (the admin has no segment error.tsx), so it shows as "no kit frame"; the
  // same words inside a rendered frame are page text (an error toast says "Something went wrong" too).
  if (!s.frame) {
    const boundary = BOUNDARY_TEXTS.find((t) => s.bodyText.includes(t));
    out.push(boundary
      ? `error boundary on screen: "${boundary}"`
      : "no kit frame ([data-kit-frame]): the page did not render inside the admin shell");
  }
  return out;
}
