"use client";
// app/_kit/AdminShell.tsx
// AA-752 — the single admin shell. The admin layout (app/admin/layout.tsx) renders it once around
// every admin page; no page renders a sidebar itself any more.
//
// Layout: a flex row of [Sidebar | (Topbar over the page content)]. The page supplies its own
// <main className="aa-admin-main"> (kept from before the shell), which is the scroll region. On
// phones (< 768px, globals.css media query) the sidebar becomes an off-canvas drawer opened by the
// topbar ☰ or the BottomNav "More", and the fixed BottomNav appears.
//
// The shell owns three bits of interaction state: the ⌘K / Ctrl K palette, the mobile drawer, and
// the backdrop. All are driven by explicit handlers (no setState-in-effect); the only effect is the
// global keydown listener for ⌘K.

import { useEffect, useState } from "react";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { BottomNav } from "./BottomNav";
import { CommandPalette } from "./CommandPalette";
import { K } from "./tokens";

export function AdminShell({ children }: { children: React.ReactNode }) {
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  // ⌘K / Ctrl K toggles the palette (ignored while the user is mid-typing is unnecessary — the
  // shortcut is modifier-gated). Esc on the palette is handled inside CommandPalette.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
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

  return (
    <div style={{ display: "flex", minHeight: "100vh", background: K.bg }}>
      <Sidebar drawerOpen={drawerOpen} onNavigate={() => setDrawerOpen(false)} />

      {/* Backdrop behind the open drawer (< 768px only; CSS shows it). */}
      {drawerOpen && (
        <div className="aa-admin-backdrop" onClick={() => setDrawerOpen(false)} aria-hidden="true" />
      )}

      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", height: "100vh" }}>
        <Topbar
          onOpenSearch={() => setPaletteOpen(true)}
          onOpenMenu={() => setDrawerOpen(true)}
        />
        {/* The page renders its own <main className="aa-admin-main"> as the scroll region. */}
        {children}
      </div>

      <BottomNav onOpenMore={() => setDrawerOpen(true)} />

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
