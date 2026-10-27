// app/layout.tsx
// AA-605: brand fonts (Fahkwang display, Poppins body, JetBrains Mono for IDs) are loaded once
// here via Google Fonts; globals.css sets Poppins as the body default. Inter is no longer used.

import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./_kit/core/Providers";
import { themeStore } from "./_kit/theme";

export const metadata: Metadata = {
  title: "AA-CIS",
  description: "Adventure Asia Content Intelligence System",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="light">
      <head>
        {/* AA-601 part B — theme restore (runs before paint, no wrong-theme flash).
            localStorage.cis_theme holds the CHOICE: 'light' | 'dark' | (absent === system).
            For 'system' we resolve via prefers-color-scheme; an explicit choice wins. The RESOLVED
            value ('light'|'dark') is written to <html data-theme>, which drives the --aa-* vars. The script is the
            kit core's (app/_kit/core/theme.ts createThemeStore, AA-994 S1a) for the CIS key (app/_kit/theme.ts). */}
        <script dangerouslySetInnerHTML={{ __html: themeStore.restoreScript }} />

        {/* AA-752 — sidebar collapsed-state restore (runs before paint, no wrong-width flash).
            localStorage.cis_sidebar === 'collapsed' means the rail is collapsed; absent === expanded.
            Written to <html data-sidebar>, matching app/_kit/core/sidebarStore.ts. */}
        <script dangerouslySetInnerHTML={{ __html: `
          (function() {
            try {
              var s = localStorage.getItem('cis_sidebar');
              document.documentElement.setAttribute('data-sidebar', s === 'collapsed' ? 'collapsed' : 'expanded');
            } catch (e) {
              document.documentElement.setAttribute('data-sidebar', 'expanded');
            }
          })();
        `}} />

        {/* AA-605 — Fahkwang (display) + Poppins (UI) are the Adventure Asia brand faces
            (app/_kit/core/brand.ts), used by admin and the tenant portal;
            JetBrains Mono for IDs/codes. */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Fahkwang:wght@500;600;700&family=Poppins:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body style={{ margin: 0 }}>
        {/* AA-662: app-wide react-query provider (client entry point) around all routes. */}
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
