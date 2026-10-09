// app/layout.tsx
// AA-605: brand fonts (Fahkwang display, Poppins body, JetBrains Mono for IDs) are loaded once
// here via Google Fonts; globals.css sets Poppins as the body default. Inter is no longer used.

import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./_kit/Providers";

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
            value ('light'|'dark') is written to <html data-theme>, which drives the --aa-* vars. */}
        <script dangerouslySetInnerHTML={{ __html: `
          (function() {
            try {
              var c = localStorage.getItem('cis_theme');
              var resolved = (c === 'light' || c === 'dark')
                ? c
                : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
              document.documentElement.setAttribute('data-theme', resolved);
            } catch (e) {
              document.documentElement.setAttribute('data-theme', 'light');
            }
          })();
        `}} />

        {/* AA-605 — Fahkwang (display) + Poppins (UI) are the Adventure Asia brand faces
            (app/_brand/tokens.ts), used by admin and the tenant portal;
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
