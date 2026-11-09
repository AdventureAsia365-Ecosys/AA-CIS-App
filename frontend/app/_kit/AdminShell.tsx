"use client";
// app/_kit/AdminShell.tsx
// AA-752 — the single admin shell. The admin layout (app/admin/layout.tsx) renders it once around
// every admin page; no page renders a sidebar itself any more.
//
// AA-1008 (S6e, root ADR 0016 decision 1): the frame is the kit core ShellFrame; this file is the CIS
// configuration of it. Layout: [Sidebar | (Topbar over the page content)]. Every page renders the kit
// AdminMain / PageFrame (the standard padding, phone rule and its own scroll), which is the scroll region
// (`main="page"`). Below 768px the ShellFrame turns the sidebar into an off-canvas drawer opened by the
// topbar ☰ or the BottomNav "More", with the backdrop and Esc; the fixed BottomNav appears (globals.css).
// The ⌘K / Ctrl K palette state is the ShellFrame's too.
//
// AA-994: the shell renders the one I18nProvider of the admin (language from the `cis_lang` cookie, English by
// default), so the topbar switch changes the text of the shell and of every page below it.

import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { BottomNav } from "./BottomNav";
import { CommandPalette } from "./CommandPalette";
import { SessionKeepAlive } from "./SessionKeepAlive";
import { ShellFrame } from "./core/ShellFrame";
import { I18nProvider } from "./core/useI18n";
import { langStore } from "./i18n";

export function AdminShell({ children }: { children: React.ReactNode }) {
  return (
    <I18nProvider store={langStore}>
      {/* AA-816: refreshes a Cognito session while the page stays open (no-op for legacy). */}
      <SessionKeepAlive />
      <ShellFrame
        drawerBelow={768}
        main="page"
        sidebarId="admin-sidebar-drawer"
        sidebar={(c) => <Sidebar drawerOpen={c.drawerOpen} onNavigate={c.closeDrawer} />}
        topbar={(c) => <Topbar onOpenSearch={c.openPalette} onOpenMenu={c.openDrawer} />}
        bottomNav={(c) => <BottomNav onOpenMore={c.openDrawer} />}
        palette={(c) => <CommandPalette open={c.paletteOpen} onClose={c.closePalette} />}
      >
        {/* The page renders the kit AdminMain as its frame and scroll region (never a hand-written <main>). */}
        {children}
      </ShellFrame>
    </I18nProvider>
  );
}
