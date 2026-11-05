// tests/e2e/visual/changed-pages.mjs — AA-1008 (root ADR 0016 decision 9): which pages of pages.ts a change touches,
// so the visual run captures "every changed page". Prints one line: `all`, a comma list of slugs, or nothing.
//   node --experimental-strip-types tests/e2e/visual/changed-pages.mjs <base> <head>
//   node --experimental-strip-types tests/e2e/visual/changed-pages.mjs --files <path>...   (the unit test)
// Rules (paths from the repo root):
//   - a file under frontend/app/: the page folders at or under its folder, cut at the first private folder (`_x`)
//     and without route groups (`(x)`): app/admin/tenants/_components/X.tsx touches every page under
//     app/admin/tenants; app/_kit/**, app/layout.tsx and app/globals.css touch every page;
//   - any other file of frontend/ (lib, components, the kit outside app/, proxy.ts, package.json…), the visual run's
//     own files and its workflow: every page;
//   - anything else (api/, docs/…): no page.
import { execFileSync } from "node:child_process";
import { PAGES } from "./pages.ts";

const APP = "frontend/app/";
const EVERYTHING = ["frontend/", "tests/e2e/visual/", ".github/workflows/visual.yml"];

/** The folder of a route under app/ ("/admin/tenants" -> "admin/tenants"); null for a page without a page.tsx. */
function routeFolder(route) {
  if (!route.startsWith("/")) return null; // "(not-found)"
  return route === "/" ? "" : route.slice(1);
}

/** The slugs a list of changed paths touches: "all", or a sorted list (maybe empty). */
export function changedSlugs(files, pages = PAGES) {
  const slugs = new Set();
  for (const f of files) {
    if (f.startsWith(APP)) {
      const segs = f.slice(APP.length).split("/").slice(0, -1);
      // A private folder (`_x`) and everything under it belong to the folder that holds it.
      const priv = segs.findIndex((s) => s.startsWith("_"));
      if (priv >= 0) segs.length = priv;
      const folder = segs.filter((s) => !/^\(.*\)$/.test(s)).join("/");
      if (folder === "") return "all";
      for (const p of pages) {
        const rf = routeFolder(p.route);
        if (rf !== null && (rf === folder || rf.startsWith(`${folder}/`))) slugs.add(p.slug);
      }
      continue;
    }
    if (EVERYTHING.some((e) => f === e || f.startsWith(e))) return "all";
  }
  return [...slugs].sort();
}

const isMain = process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop());
if (isMain) {
  const args = process.argv.slice(2);
  let files;
  if (args[0] === "--files") files = args.slice(1);
  else {
    const [base, head] = args;
    if (!base || !head) {
      console.error("usage: changed-pages.mjs <base> <head> | --files <path>...");
      process.exit(2);
    }
    files = execFileSync("git", ["diff", "--name-only", `${base}...${head}`], { encoding: "utf8" }).split("\n").filter(Boolean);
  }
  const out = changedSlugs(files);
  console.log(out === "all" ? "all" : out.join(","));
}
