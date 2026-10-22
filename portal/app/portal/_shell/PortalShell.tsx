"use client";
// portal/app/portal/_shell/PortalShell.tsx
// AA-759 — the single tenant-portal shell, on the UI kit (same shape as the admin AdminShell,
// AA-752): [sidebar | topbar over <main>], a ⌘K / Ctrl K command palette, a phone bottom bar, the
// kit toast. app/portal/layout.tsx renders it once around every portal page.
//
// Responsive behaviour is CSS (portal/app/globals.css, the `data-pshell` hooks): below 900 px the
// sidebar is an off-canvas drawer opened from the topbar ☰ or the bottom bar "More"; below 768 px
// the bottom bar shows and the search pill becomes an icon. No JS media query, so the server HTML is
// already right on a phone. The shell owns three bits of interaction state: palette, drawer, and
// the topbar search text Browse Tours reads (`globalSearch`).

import { useCallback, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { K, ToastProvider, useToast } from "@/app/_kit";
import { PortalShellContext, type PortalShellValue } from "../_components/PortalShellContext";
import { PortalSidebar } from "./PortalSidebar";
import { PortalTopbar } from "./PortalTopbar";
import { PortalBottomNav } from "./PortalBottomNav";
import { PortalCommandPalette } from "./PortalCommandPalette";
import { ROUTES } from "./portalNav";
import { MY_VERSIONS_KEY, useBilling, useCatalogCount, usePoolTotal, useTenantMe } from "./useShellData";

export function PortalShell({ children }: { children: React.ReactNode }) {
  return (
    <ToastProvider>
      <ShellFrame>{children}</ShellFrame>
    </ToastProvider>
  );
}

function ShellFrame({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const qc = useQueryClient();
  const toast = useToast();

  const [paletteOpen, setPaletteOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [globalSearch, setGlobalSearch] = useState("");

  const { tenantName, planTier } = useTenantMe();
  const poolTotal = usePoolTotal();
  const catTotal = useCatalogCount();
  const billing = useBilling();

  // ⌘K / Ctrl K toggles the palette (modifier-gated, so typing in a field never triggers it).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Esc closes the drawer.
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setDrawerOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  const searchTours = useCallback((q: string) => {
    setGlobalSearch(q);
    if (pathname !== ROUTES.tours) router.push(ROUTES.tours);
  }, [pathname, router]);

  const showToast = useCallback((msg: string) => toast.success(msg), [toast]);
  const refreshCatalogCount = useCallback(() => {
    qc.invalidateQueries({ queryKey: MY_VERSIONS_KEY });
  }, [qc]);

  const value: PortalShellValue = useMemo(() => ({
    tenantName, planTier, poolTotal, catTotal, billing, globalSearch, refreshCatalogCount, showToast,
  }), [tenantName, planTier, poolTotal, catTotal, billing, globalSearch, refreshCatalogCount, showToast]);

  return (
    <PortalShellContext.Provider value={value}>
      <div data-pshell="frame" style={{ display: "flex", minHeight: "100vh", background: K.bg, color: K.body }}>
        <PortalSidebar
          drawerOpen={drawerOpen}
          poolCount={poolTotal}
          catalogCount={catTotal}
          onNavigate={() => { setDrawerOpen(false); setGlobalSearch(""); }}
        />

        {drawerOpen && (
          <div data-pshell="backdrop" onClick={() => setDrawerOpen(false)} aria-hidden="true" />
        )}

        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", height: "100vh" }}>
          <PortalTopbar
            tenantName={tenantName}
            planTier={planTier}
            activity={billing?.activity ?? []}
            onOpenSearch={() => setPaletteOpen(true)}
            onOpenMenu={() => setDrawerOpen(true)}
          />
          <main data-pshell="main" style={{ flex: 1, minHeight: 0, overflowY: "auto", overflowX: "hidden" }}>
            {children}
          </main>
        </div>

        <PortalBottomNav onOpenMore={() => setDrawerOpen(true)} />

        <PortalCommandPalette
          open={paletteOpen}
          onClose={() => setPaletteOpen(false)}
          onSearchTours={searchTours}
        />
      </div>
    </PortalShellContext.Provider>
  );
}
