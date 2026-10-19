// portal/app/layout.tsx
// AA-885 — root layout of the tenant portal app. Same brand fonts as the admin (AA-605: Fahkwang
// display, Poppins body, JetBrains Mono for IDs) and the same react-query provider (AA-662), but no
// admin theme or sidebar restore scripts: the portal stays light (AA-601 part B), so <html> is pinned
// to data-theme="light".
import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./_kit/Providers";

export const metadata: Metadata = {
  title: "Adventure Asia — Partner Portal",
  description: "Adventure Asia B2B partner portal",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="light">
      <head>
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
