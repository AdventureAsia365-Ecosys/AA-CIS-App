// app/layout.tsx
// AA-605: brand fonts (Fahkwang display, Poppins body, JetBrains Mono for IDs) are loaded once
// here via Google Fonts; globals.css sets Poppins as the body default. Inter is no longer used.

import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";
import { Providers } from "./_kit/core/Providers";
import { themeStore } from "./_kit/theme";
import { langStore } from "./_kit/i18n";
import { NONCE_HEADER } from "@/lib/securityHeaders";

export const metadata: Metadata = {
  title: "AA-CIS",
  description: "Adventure Asia Content Intelligence System",
};

// AA-1111 (S284): the CSP allows a script only with this request's nonce (proxy.ts); reading the request headers also
// renders every page per request, so no prerendered page is served with an inline script the policy would refuse.
export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const nonce = (await headers()).get(NONCE_HEADER) ?? undefined;
  return (
    // The head scripts set data-theme / data-sidebar / lang before paint, so <html> attributes may differ from the
    // server HTML on purpose.
    <html lang="en" data-theme="light" suppressHydrationWarning>
      <head>
        {/* AA-601 part B — theme restore (runs before paint, no wrong-theme flash).
            localStorage.cis_theme holds the CHOICE: 'light' | 'dark' | (absent === system).
            For 'system' we resolve via prefers-color-scheme; an explicit choice wins. The RESOLVED
            value ('light'|'dark') is written to <html data-theme>, which drives the --aa-* vars. The script is the
            kit core's (app/_kit/core/theme.ts createThemeStore, AA-994 S1a) for the CIS key (app/_kit/theme.ts). */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: themeStore.restoreScript }} />

        {/* AA-994 — UI language restore: <html lang> from the `cis_lang` cookie (absent === English) before paint.
            The kit core's script (app/_kit/core/i18n.ts createLangStore) for the CIS cookie (app/_kit/i18n.ts). */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: langStore.restoreScript }} />

        {/* AA-752 — sidebar collapsed-state restore (runs before paint, no wrong-width flash).
            localStorage.cis_sidebar === 'collapsed' means the rail is collapsed; absent === expanded.
            Written to <html data-sidebar>, matching app/_kit/core/sidebarStore.ts. */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: `
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
