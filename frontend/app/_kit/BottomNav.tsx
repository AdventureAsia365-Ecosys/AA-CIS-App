"use client";
// app/_kit/BottomNav.tsx
// AA-752 — phone (< 768px) bottom navigation: Overview · Review (badge) · Master · Jobs · More.
// Fixed to the bottom, hidden ≥ 768px (the `.aa-bottom-nav` media query in globals.css). Each
// target is ≥ 44px tall; the active item is gold. "More" opens the existing off-canvas drawer.

import { useRouter, usePathname } from "next/navigation";
import { Gauge, ClipboardList, Library, ListChecks, Menu } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { K } from "./tokens";
import { navActive } from "./adminNav";
import { apiGet } from "./api";

interface ReviewTotal { pagination?: { total?: number }; total?: number; }

const ITEMS = [
  { href: "/admin/overview", label: "Overview", icon: Gauge },
  { href: "/admin/review", label: "Review", icon: ClipboardList, badge: true },
  { href: "/admin/master-content", label: "Master", icon: Library },
  { href: "/admin/jobs", label: "Jobs", icon: ListChecks },
] as const;

export function BottomNav({ onOpenMore }: { onOpenMore: () => void }) {
  const router = useRouter();
  const pathname = usePathname();

  const { data: reviewTotal } = useQuery({
    queryKey: ["bottomnav", "review-pending"],
    queryFn: () =>
      apiGet<ReviewTotal>("/api/admin/review-queue?status=pending&page=1&page_size=1", { admin: true }),
    staleTime: 60_000,
    refetchInterval: 60_000,
    retry: false,
  });
  const pending = reviewTotal?.pagination?.total ?? reviewTotal?.total;

  return (
    <nav className="aa-bottom-nav" aria-label="Primary (mobile)">
      {ITEMS.map((it) => {
        const Icon = it.icon;
        const active = navActive({ href: it.href }, pathname);
        const showBadge = "badge" in it && it.badge && pending;
        return (
          <button
            key={it.href}
            onClick={() => router.push(it.href)}
            aria-current={active ? "page" : undefined}
            aria-label={it.label}
            style={bnBtn(active)}
          >
            <span style={{ position: "relative", display: "flex" }}>
              <Icon size={20} />
              {showBadge ? (
                <span style={bnBadge}>{pending! > 99 ? "99+" : pending}</span>
              ) : null}
            </span>
            <span style={{ fontSize: 10.5, fontWeight: active ? 600 : 500 }}>{it.label}</span>
          </button>
        );
      })}
      <button onClick={onOpenMore} aria-label="More navigation" style={bnBtn(false)}>
        <Menu size={20} />
        <span style={{ fontSize: 10.5, fontWeight: 500 }}>More</span>
      </button>
    </nav>
  );
}

function bnBtn(active: boolean): React.CSSProperties {
  return {
    flex: 1, minHeight: 44, display: "flex", flexDirection: "column", alignItems: "center",
    justifyContent: "center", gap: 2, border: "none", background: "transparent",
    cursor: "pointer", color: active ? K.accent : K.muted, padding: "6px 2px",
  };
}

const bnBadge: React.CSSProperties = {
  position: "absolute", top: -5, right: -8,
  background: K.danger, color: "var(--aa-on-solid)",
  borderRadius: 999, fontSize: 8.5, fontWeight: 700,
  minWidth: 14, height: 14, display: "grid", placeItems: "center", padding: "0 3px",
};
