"use client";
// app/(tenant)/portal/layout.tsx — AA-430 route migration.
//
// Was app/(tenant)/portal/page.tsx: a single client component that owned all shell
// state (tenantName/planTier/poolTotal/catTotal/billing/toast/globalSearch) AND
// conditionally rendered one of 8 Tab components inline based on a `tab` useState.
//
// Now each former "tab" is its own route under /portal/* (own page.tsx — see that
// directory listing), so the shell (Sidebar + topbar + toast) lives here instead: a
// real layout.tsx persists across navigations between sibling routes in the same
// segment, exactly like the old page.tsx persisted across `setTab()` calls, so moving
// the state up here (and exposing it to route pages via PortalShellContext, since a
// layout can't pass props into `children`) preserves the same "fetch/state survives
// switching sections" behavior — nothing is refetched or reset on navigation that
// wasn't refetched/reset on a tab switch before.
import { useState, useEffect, useRef } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Search, Bell, Menu, CheckCircle2 } from "lucide-react";
import Sidebar from "./_components/Sidebar";
import { PortalShellContext } from "./_components/PortalShellContext";
import { T, sans, countUniqueTours } from "./_components/ui";

const BREADCRUMBS: Record<string, string> = {
  "/portal/dashboard":  "Dashboard",
  "/portal/t1-rewrite": "Browse Tours", // AA-576 Phần 3 (was "Browse Pool")
  "/portal/t4-pool":    "My Catalog Tours", // AA-576 Phần 3 (was "My Catalog")
  "/portal/t0-brand":   "Brand Identity",
  // AA-526 — /portal/t6-atoms removed along with tenant atom visibility.
  "/portal/t7-planning": "Social Content", // AA-448, relabeled AA-519 Việc 3, renamed again AA-564 4.1
  "/portal/t8-angle-gate": "Write Content", // AA-449/AA-450 — one wizard, goal->angle->write; no
  // longer a Sidebar entry as of AA-576 Phần 3, but the route/breadcrumb still exist for the
  // deep-link case.
  "/portal/t10-review": "My Content", // AA-576 Phần 3 (was unset — page had no breadcrumb entry
  // before; now matches the page's own <h1> and the Sidebar label)
  "/portal/t11-publish": "Publish", // AA-457 — connect-flow only this PR, no Sidebar entry yet
  "/portal/t11-publish/connection": "Publish", // manage-connection sub-route, same breadcrumb label
  "/portal/marketplace": "Marketplace", // AA-444
  "/portal/api":        "API Access",
  "/portal/activity":   "Activity Log",
  "/portal/billing":    "Billing",
  "/portal/settings":   "Settings",
};

export default function PortalLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();

  const [tenantName, setName] = useState("Partner");
  const [planTier, setPlan]   = useState("growth");
  const [poolTotal, setPool]  = useState(0);
  const [catTotal, setCat]    = useState(0);
  const [billing, setBilling] = useState<any>(null);
  const [toast, setToast]     = useState<string | null>(null);
  const [globalSearch, setGlobalSearch] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  // AA-605: below 900px the sidebar becomes an off-canvas drawer opened from the top bar.
  const [narrow, setNarrow]   = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 900px)");
    const sync = () => { setNarrow(mq.matches); if (!mq.matches) setNavOpen(false); };
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    // AA-443 (gap left by AA-427): cis_tenant_name / cis_tenant_plan became httpOnly in AA-427
    // (PR #184) — no longer readable from document.cookie. PR #184 moved every other file that
    // read these two cookies over to fetch("/api/tenant/me") but missed this one (this shell
    // moved out of portal/page.tsx into its own layout.tsx via AA-430, around the same time AA-427
    // was in flight), so tenantName/planTier silently stayed at their "Partner"/"growth" defaults
    // for every real tenant. Same fix, same pattern, applied here.
    fetch("/api/tenant/me")
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (d?.tenant_name) setName(d.tenant_name);
        if (d?.plan_tier) setPlan(d.plan_tier);
      })
      .catch(() => {});

    Promise.all([
      fetch("/api/tenant/v1/tours/pool?page_size=1"),
      // AA-566 Phần A — page_size=50 (matches CatalogTab.tsx's own fetch) + client-side dedup
      // by published_tour_id, NOT `pagination.total` (a flat per-VERSION count that includes
      // old versions AA-565 already hides from the UI). The sidebar badge and the My Catalog
      // heading must count the same thing — this was the real bug: two different readings of
      // one endpoint.
      fetch("/api/tenant/v1/tours/my-versions?page_size=50"),
      // AA-496: was /api/admin/billing — that proxy requires an admin JWT (requireAdmin()),
      // so this 401'd on every tenant, every page load, always. /v1/billing is the real
      // tenant-scoped sibling (JWT-derived tenant_id, api/routers/v1_tours.py).
      fetch("/api/tenant/v1/billing"),
    ]).then(async ([pRes, cRes, bRes]) => {
      if (pRes.ok) { const d = await pRes.json(); setPool(d.pagination?.total ?? 0); }
      if (cRes.ok) { const d = await cRes.json(); setCat(countUniqueTours(d.data ?? [])); }
      if (bRes.ok) setBilling(await bRes.json());
    }).catch(() => {});
  }, []);

  // ⌘K focus search
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 4000);
  }

  function refreshCatalogCount() {
    // AA-566 Phần A — same fix as the initial load above: unique-tour count, not the flat
    // per-version pagination.total.
    fetch("/api/tenant/v1/tours/my-versions?page_size=50")
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setCat(countUniqueTours(d.data ?? [])); })
      .catch(() => {});
  }

  // Search box: hitting Enter with text jumps to Browse Pool (T1) to show filtered results.
  // AA-586: navigating on bare focus (not just Enter) was removed — it silently discarded
  // whatever page the user was on elsewhere in the portal just from clicking into the field.
  function goToPool() {
    if (pathname !== "/portal/t1-rewrite") router.push("/portal/t1-rewrite");
  }
  function handleSearchKey(e: React.KeyboardEvent) {
    if (e.key === "Enter" && globalSearch.trim()) goToPool();
  }

  return (
    <PortalShellContext.Provider value={{
      tenantName, planTier, poolTotal, catTotal, billing, globalSearch,
      refreshCatalogCount, showToast,
    }}>
      <div style={{ display: "flex", minHeight: "100vh", fontFamily: sans, background: T.bg }}>

        {/* Toast */}
        {toast && (
          <div style={{
            position: "fixed", top: 20, right: 24, zIndex: 999,
            padding: "12px 20px", background: T.green, borderRadius: 10,
            color: "#fff", fontSize: 13, fontWeight: 600,
            boxShadow: "0 4px 20px rgba(0,0,0,0.18)",
            display: "flex", alignItems: "center", gap: 8, maxWidth: "calc(100vw - 48px)",
          }}>
            <CheckCircle2 size={15} style={{ flexShrink: 0 }} /> {toast}
          </div>
        )}

        <Sidebar
          poolCount={poolTotal}
          catalogCount={catTotal}
          tenantName={tenantName}
          planTier={planTier}
          onNavClick={() => setGlobalSearch("")}
          drawer={narrow ? { open: navOpen, onClose: () => setNavOpen(false) } : undefined}
        />

        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, height: "100vh", overflow: "hidden" }}>

          {/* Top bar */}
          <header style={{
            height: 56, background: "#fff", borderBottom: `1px solid ${T.line}`,
            display: "flex", alignItems: "center", padding: narrow ? "0 16px" : "0 32px", gap: narrow ? 10 : 16,
            position: "sticky", top: 0, zIndex: 10, flexShrink: 0,
          }}>
            {narrow && (
              <button onClick={() => setNavOpen(true)} aria-label="Open menu"
                style={{ width: 36, height: 36, borderRadius: 8, background: "#fff", border: `1px solid ${T.line}`, display: "grid", placeItems: "center", cursor: "pointer", color: T.ink3, flexShrink: 0 }}>
                <Menu size={16} />
              </button>
            )}
            <div style={{ fontSize: 12, color: T.muted2, display: "flex", gap: 6, alignItems: "center", minWidth: 0, whiteSpace: "nowrap", overflow: "hidden" }}>
              <span>Workspace</span>
              <span style={{ color: T.line }}>/</span>
              <span style={{ color: T.body, fontWeight: 500 }}>{BREADCRUMBS[pathname] ?? ""}</span>
            </div>
            <div style={{ flex: 1 }} />

            {/* Functional search */}
            <div style={{ position: "relative", display: narrow ? "none" : "block" }}>
              <Search size={13} style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", color: T.muted2 }} />
              <input
                ref={searchRef}
                value={globalSearch}
                onChange={e => setGlobalSearch(e.target.value)}
                onKeyDown={handleSearchKey}
                placeholder="Search tours, jobs…"
                style={{
                  background: T.bg, border: `1px solid ${T.line}`, padding: "7px 40px 7px 32px",
                  borderRadius: 8, width: 240, fontSize: 13, color: T.body, outline: "none",
                  fontFamily: sans,
                }}
              />
              <span style={{
                position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)",
                fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: T.muted2,
                background: "#fff", border: `1px solid ${T.line}`, padding: "1px 5px", borderRadius: 3,
                pointerEvents: "none",
              }}>⌘K</span>
            </div>

            <button style={{ width: 36, height: 36, borderRadius: 8, background: "#fff", border: `1px solid ${T.line}`, display: "grid", placeItems: "center", cursor: "pointer", color: T.ink3 }}>
              <Bell size={15} />
            </button>
          </header>

          {/* Content */}
          <main style={{ flex: 1, minHeight: 0, overflowY: "auto", overflowX: "hidden", padding: narrow ? "20px 16px 40px" : "28px 36px 56px" }}>
            {children}
          </main>
        </div>
      </div>
    </PortalShellContext.Provider>
  );
}
