// portal/app/layout.tsx
// AA-885 — root layout of the tenant portal app. Same brand fonts as the admin (AA-605: Fahkwang
// display, Poppins body, JetBrains Mono for IDs) and the same react-query provider (AA-662).
// AA-759 — the portal is no longer pinned light: the same two restore scripts as the admin root layout
// run before paint (theme: localStorage `cis_theme` light | dark | absent = system; sidebar rail:
// `cis_sidebar`), on the portal origin's own localStorage. `suppressHydrationWarning` covers only the
// <html> attributes those scripts set before React hydrates.
import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./_kit/Providers";

export const metadata: Metadata = {
  title: "Adventure Asia — Partner Portal",
  description: "Adventure Asia B2B partner portal",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="light" data-sidebar="expanded" suppressHydrationWarning>
      <head>
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
            try {
              var s = localStorage.getItem('cis_sidebar');
              document.documentElement.setAttribute('data-sidebar', s === 'collapsed' ? 'collapsed' : 'expanded');
            } catch (e) {
              document.documentElement.setAttribute('data-sidebar', 'expanded');
            }
          })();
        `}} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Fahkwang:wght@500;600;700&family=Poppins:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body style={{ margin: 0 }}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
