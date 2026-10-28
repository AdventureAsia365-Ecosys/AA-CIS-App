"use client";
// app/admin/_components/SocialContentSubNav.tsx — AA-575
//
// STEP0 (AA-575): 01-05 are NOT separate Next.js routes — they are client-side tab state
// (`SectionKey`) inside ONE page, atom-curation/page.tsx. Its "01-06" inner-sidebar used to be
// JSX embedded directly in that page's own render tree (never a shared component). Content Trace
// (06, /admin/tenant-activity) is a genuinely separate route/page with its own `page.tsx` render
// tree — it never had that inline JSX, so the sub-nav simply didn't exist there. That's the root
// cause of the sub-nav "disappearing": it was never a layout wrapping both routes, just one
// page's local markup.
//
// Fix: this component is the single source of truth for the 01-06 list, used by both pages.
// - Rendered from atom-curation/page.tsx (which owns 01-05's tab state): pass `onSelectSection`
//   so 01-05 are same-page tab switches (no navigation/reload — preserves the instant-switch UX
//   AA-554's own comments call out as load-bearing).
// - Rendered from any other page (Content Trace today): omit `onSelectSection` so 01-05 render as
//   real links to `/admin/atom-curation?section=<key>`, which reads that query param on mount to
//   open the right tab.
// 06 · Content Trace is always a real link/highlight — there's no local tab state for it to hook
// into from atom-curation, and it's a no-op nav when already there.
//
// AA-560 — "07 · Platform Stats" added the same way as 06: a real link/highlight, no local tab
// state (it replaces the deleted `/admin/a4-oversight` page — Review Log + Trust Ramp + a new
// backend gate/error aggregate).
import Link from "next/link";
import { Puzzle, Layers, TrendingUp, GitBranch, FileStack, Radio, BarChart3, ShieldCheck } from "lucide-react";
import { A, sans } from "./adminUi";
import { useT } from "../../_kit/core/useI18n";
import { M } from "./components.messages";

export type SectionKey = "atomize" | "segment" | "score" | "route_hub" | "slate";
export type SubNavKey = SectionKey | "content_trace" | "platform_stats" | "content_quality";

const SECTIONS: { key: SectionKey; label: keyof typeof M.en; icon: React.ReactNode }[] = [
  { key: "atomize",    label: "navAtomize",   icon: <Puzzle size={15} /> },
  { key: "segment",    label: "navSegment",   icon: <Layers size={15} /> },
  { key: "score",      label: "navScore",     icon: <TrendingUp size={15} /> },
  { key: "route_hub",  label: "navRouteHub", icon: <GitBranch size={15} /> },
  { key: "slate",      label: "navSlate",     icon: <FileStack size={15} /> },
];

export default function SocialContentSubNav({ active, onSelectSection }: {
  active: SubNavKey;
  onSelectSection?: (key: SectionKey) => void;
}) {
  const t = useT(M);
  return (
    <>
      {/* Media query shared across every page that mounts this component — <style> tags are
          global (not scoped to this subtree), so defining it once here covers atom-curation's
          own ".a527-dash-body" wrapper too without duplicating the block per-page.
          S241 UI verify (AA-863) — stacked, the wrappers' `align-items: flex-start` sized each
          child to its fit-content width; a table's min-content (the overflow-x wrapper does not
          cap it) then pushed the body column wider than the page. `stretch` gives every stacked
          child exactly the container width on all four pages that use this split. */}
      <style>{`
        @media (max-width: 980px) {
          .a527-inner-sidebar { flex-direction: row !important; overflow-x: auto !important; width: 100% !important; border-right: none !important; border-bottom: 1px solid ${A.line}; position: static !important; }
          .a527-inner-sidebar button, .a527-inner-sidebar a { white-space: nowrap; }
          .a527-dash-body { flex-direction: column !important; align-items: stretch !important; }
        }
      `}</style>
      <div className="a527-inner-sidebar" style={{
        width: 200, flexShrink: 0, display: "flex", flexDirection: "column", gap: 2,
        background: A.card, border: `1px solid ${A.line}`, borderRadius: 10, padding: 6,
        position: "sticky", top: 0,
      }}>
        {SECTIONS.map(s => {
          const isActive = active === s.key;
          const itemStyle: React.CSSProperties = {
            display: "flex", alignItems: "center", gap: 8, padding: "9px 12px", borderRadius: 7,
            border: "none", background: isActive ? A.goldTint : "transparent",
            color: isActive ? A.gold : A.body, cursor: "pointer", fontFamily: sans,
            fontSize: 12.5, fontWeight: isActive ? 700 : 500, textAlign: "left",
            textDecoration: "none",
          };
          return onSelectSection ? (
            <button key={s.key} onClick={() => onSelectSection(s.key)} style={itemStyle}>
              {s.icon}
              <span style={{ flex: 1 }}>{t(s.label)}</span>
            </button>
          ) : (
            <Link key={s.key} href={`/admin/atom-curation?section=${s.key}`} style={itemStyle}>
              {s.icon}
              <span style={{ flex: 1 }}>{t(s.label)}</span>
            </Link>
          );
        })}
        <Link href="/admin/tenant-activity" style={{
          display: "flex", alignItems: "center", gap: 8, padding: "9px 12px", borderRadius: 7,
          background: active === "content_trace" ? A.goldTint : "transparent",
          color: active === "content_trace" ? A.gold : A.body, cursor: "pointer", fontFamily: sans,
          textDecoration: "none", fontSize: 12.5, fontWeight: active === "content_trace" ? 700 : 500,
        }}>
          <Radio size={15} />
          <span style={{ flex: 1 }}>{t("navContentTrace")}</span>
        </Link>
        <Link href="/admin/platform-stats" style={{
          display: "flex", alignItems: "center", gap: 8, padding: "9px 12px", borderRadius: 7,
          background: active === "platform_stats" ? A.goldTint : "transparent",
          color: active === "platform_stats" ? A.gold : A.body, cursor: "pointer", fontFamily: sans,
          textDecoration: "none", fontSize: 12.5, fontWeight: active === "platform_stats" ? 700 : 500,
        }}>
          <BarChart3 size={15} />
          <span style={{ flex: 1 }}>{t("navA4Quality")}</span>
        </Link>
        {/* AA-769 — every quality signal tenants no longer see (AA-770), for every tenant. */}
        <Link href="/admin/content-quality" style={{
          display: "flex", alignItems: "center", gap: 8, padding: "9px 12px", borderRadius: 7,
          background: active === "content_quality" ? A.goldTint : "transparent",
          color: active === "content_quality" ? A.gold : A.body, cursor: "pointer", fontFamily: sans,
          textDecoration: "none", fontSize: 12.5, fontWeight: active === "content_quality" ? 700 : 500,
        }}>
          <ShieldCheck size={15} />
          <span style={{ flex: 1 }}>{t("navContentQuality")}</span>
        </Link>
      </div>
    </>
  );
}
