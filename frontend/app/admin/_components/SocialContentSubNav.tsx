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
import { useT } from "../../_kit/core/useI18n";
import { M } from "./components.messages";
import { K, ListButton, sans, useMediaQuery } from "../../_kit";

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
  // < 980px the sub-nav is a sideways-scrolling row above the content (it was a `<style>` media query on a class,
  // AA-1006: the kit inline preset has no className, so the breakpoint is a hook).
  const narrow = useMediaQuery(SPLIT_QUERY);
  const itemStyle = (on: boolean): React.CSSProperties => ({
    display: "flex", alignItems: "center", gap: 8, padding: "9px 12px", borderRadius: 7,
    background: on ? K.accentTint : "transparent",
    color: on ? K.accent : K.body, cursor: "pointer", fontFamily: sans,
    fontSize: 12.5, fontWeight: on ? 700 : 500, textAlign: "left",
    textDecoration: "none", whiteSpace: narrow ? "nowrap" : undefined, flexShrink: 0, width: narrow ? "auto" : undefined,
  });
  return (
    <nav data-testid="social-content-subnav" aria-label={t("subNavLabel")} style={{
      width: narrow ? "100%" : 200, flexShrink: 0, display: "flex", flexDirection: narrow ? "row" : "column", gap: 2,
      background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, padding: 6, boxSizing: "border-box",
      position: narrow ? "static" : "sticky", top: 0, overflowX: narrow ? "auto" : undefined,
    }}>
        {SECTIONS.map(s => {
          const isActive = active === s.key;
          return onSelectSection ? (
            <ListButton key={s.key} onClick={() => onSelectSection(s.key)} aria-current={isActive ? "page" : undefined}
              style={itemStyle(isActive)}>
              {s.icon}
              <span style={{ flex: 1 }}>{t(s.label)}</span>
            </ListButton>
          ) : (
            <Link key={s.key} href={`/admin/atom-curation?section=${s.key}`} style={itemStyle(isActive)}>
              {s.icon}
              <span style={{ flex: 1 }}>{t(s.label)}</span>
            </Link>
          );
        })}
        <Link href="/admin/tenant-activity" aria-current={active === "content_trace" ? "page" : undefined}
          style={itemStyle(active === "content_trace")}>
          <Radio size={15} />
          <span style={{ flex: 1 }}>{t("navContentTrace")}</span>
        </Link>
        <Link href="/admin/platform-stats" aria-current={active === "platform_stats" ? "page" : undefined}
          style={itemStyle(active === "platform_stats")}>
          <BarChart3 size={15} />
          <span style={{ flex: 1 }}>{t("navA4Quality")}</span>
        </Link>
        {/* AA-769 — every quality signal tenants no longer see (AA-770), for every tenant. */}
        <Link href="/admin/content-quality" aria-current={active === "content_quality" ? "page" : undefined}
          style={itemStyle(active === "content_quality")}>
          <ShieldCheck size={15} />
          <span style={{ flex: 1 }}>{t("navContentQuality")}</span>
        </Link>
    </nav>
  );
}

/** Where the Social Content pages stack the sub-nav above the content. */
const SPLIT_QUERY = "(max-width: 980px)";

/** The sub-nav + content split of the Social Content pages (atom-curation, Content Trace, platform stats, content
 *  quality): a row on desktop; < 980px a column whose children take the full width (S241 AA-863: a child sized to its
 *  fit-content width let a table's min-content push the column wider than the page). */
export function SocialContentSplit({ gap = 20, children }: { gap?: number; children: React.ReactNode }) {
  const narrow = useMediaQuery(SPLIT_QUERY);
  return (
    <div style={{ display: "flex", gap, flexDirection: narrow ? "column" : "row", alignItems: narrow ? "stretch" : "flex-start" }}>
      {children}
    </div>
  );
}
