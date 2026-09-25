// app/layout.tsx
// AA-605: brand fonts (Fahkwang display, Poppins body, JetBrains Mono for IDs) are loaded once
// here via Google Fonts; globals.css sets Poppins as the body default. Inter is no longer used.

import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AA-CIS",
  description: "Adventure Asia Content Intelligence System",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="light">
      <head>
        {/* Theme restore — giữ nguyên như cũ */}
        <script dangerouslySetInnerHTML={{ __html: `
          (function() {
            var t = localStorage.getItem('cis_theme') || 'light';
            document.documentElement.setAttribute('data-theme', t);
          })();
        `}} />

        {/* AA-605 — Fahkwang (display) + Poppins (UI) are the Adventure Asia brand faces
            (app/_brand/tokens.ts), used by admin, the tenant portal and the (internal) routes;
            JetBrains Mono for IDs/codes. */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Fahkwang:wght@500;600;700&family=Poppins:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body style={{ margin: 0 }}>
        {children}
      </body>
    </html>
  );
}
