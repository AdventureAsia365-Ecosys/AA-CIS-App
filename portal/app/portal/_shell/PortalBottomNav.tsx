"use client";
// portal/app/portal/_shell/PortalBottomNav.tsx
// AA-759 — phone (< 768 px) bottom bar: Dashboard · Tours · Create · Content · More. Fixed to the
// bottom, hidden ≥ 768 px (globals.css `data-pshell="bottom-nav"`). Each target is ≥ 44 px tall; the
// active one is gold. "More" opens the off-canvas drawer with the whole nav.

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu } from "lucide-react";
import { K, sans } from "@/app/_kit";
import { BOTTOM_ITEMS, navActive } from "./portalNav";

export function PortalBottomNav({ onOpenMore }: { onOpenMore: () => void }) {
  const pathname = usePathname();
  return (
    <nav data-pshell="bottom-nav" data-testid="portal-bottom-nav" aria-label="Primary (mobile)">
      {BOTTOM_ITEMS.map((it) => {
        const active = navActive(it, pathname);
        return (
          <Link key={it.href} href={it.href} aria-current={active ? "page" : undefined} style={btn(active)}>
            <span aria-hidden style={{ display: "flex" }}>{it.icon}</span>
            <span style={{ fontSize: 10.5, fontWeight: active ? 600 : 500 }}>{it.label}</span>
          </Link>
        );
      })}
      <button onClick={onOpenMore} aria-label="More navigation" style={btn(false)}>
        <Menu size={18} aria-hidden />
        <span style={{ fontSize: 10.5, fontWeight: 500 }}>More</span>
      </button>
    </nav>
  );
}

function btn(active: boolean): React.CSSProperties {
  return {
    flex: 1, minHeight: 44, display: "flex", flexDirection: "column", alignItems: "center",
    justifyContent: "center", gap: 2, border: "none", background: "transparent", textDecoration: "none",
    cursor: "pointer", color: active ? K.accent : K.muted, padding: "6px 2px", fontFamily: sans,
  };
}
