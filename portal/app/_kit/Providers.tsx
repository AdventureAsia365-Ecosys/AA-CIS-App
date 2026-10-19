"use client";
// app/_kit/Providers.tsx
// AA-662 — adopt @tanstack/react-query as the standard data-fetching layer for UI v2.
//
// This is the single app-wide QueryClientProvider. It is a client component (react-query needs
// client-side state) mounted from the server root layout (app/layout.tsx) around {children}, which
// is the React-standard way to use a context provider in the App Router: the server layout stays a
// server component, the provider is its own "use client" entry point.
//
// The QueryClient is created once per browser session and held in useState so it is not recreated
// on re-render (a fresh client on every render would drop the cache). Defaults are conservative so
// adopting react-query on a page does not change its observable behaviour versus the old hand-rolled
// fetch: no refetch-on-focus, one retry, a short stale window. Pages that poll (e.g. Review Queue,
// My Content) opt in per-query with `refetchInterval`.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            gcTime: 5 * 60_000,
            refetchOnWindowFocus: false,
            retry: 1,
          },
          mutations: {
            retry: 0,
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
