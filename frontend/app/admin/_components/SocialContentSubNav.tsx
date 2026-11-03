"use client";
// app/admin/_components/SocialContentSubNav.tsx — AA-575; split in two by AA-1039 (S266).
//
// The in-page sub-nav of two areas that used to share one "01–08" list (the file keeps its old name):
//   - AA Catalogue › Atoms & routes (/admin/atom-curation): 01 Atomize · 02 Segment · 03 Score · 04 Route/Hub. These
//     are client-side tab state (`SectionKey`) inside that ONE page: rendered there, pass `onSelectSection` so they are
//     same-page tab switches (no navigation — AA-554's instant switch). Rendered without it they are real links to
//     `/admin/atom-curation?section=<key>`, which the page reads on mount.
//   - Tenant content QA (/admin/tenant-qa/*): Content quality · Content Trace · Review log · Slate, real routes, always
//     links (lib/adminIa.ts holds the same hrefs for the sidebar; root ADR 0019 d13 separates the AA store from
//     tenant-written content).
// `active` picks the area: a catalogue section key shows the catalogue list, a QA key the QA list.
import Link from "next/link";
import { Puzzle, Layers, TrendingUp, GitBranch, FileStack, Radio, BarChart3, ShieldAlert, ShieldCheck } from "lucide-react";
import { useT } from "../../_kit/core/useI18n";
import { M } from "./components.messages";
import { K, ListButton, sans, useMediaQuery } from "../../_kit";

export type SectionKey = "atomize" | "segment" | "score" | "route_hub";
export type QaKey = "content_quality" | "content_trace" | "review_log" | "slate" | "compliance";
export type SubNavKey = SectionKey | QaKey;

const SECTIONS: { key: SectionKey; label: keyof typeof M.en; icon: React.ReactNode }[] = [
  { key: "atomize",    label: "navAtomize",   icon: <Puzzle size={15} /> },
  { key: "segment",    label: "navSegment",   icon: <Layers size={15} /> },
  { key: "score",      label: "navScore",     icon: <TrendingUp size={15} /> },
  { key: "route_hub",  label: "navRouteHub", icon: <GitBranch size={15} /> },
];

export const QA_PAGES: { key: QaKey; href: string; label: keyof typeof M.en; icon: React.ReactNode }[] = [
  { key: "content_quality", href: "/admin/tenant-qa/quality",    label: "navQuality",      icon: <ShieldCheck size={15} /> },
  { key: "content_trace",   href: "/admin/tenant-qa/trace",      label: "navContentTrace", icon: <Radio size={15} /> },
  { key: "review_log",      href: "/admin/tenant-qa/review-log", label: "navReviewLog",    icon: <BarChart3 size={15} /> },
  { key: "slate",           href: "/admin/tenant-qa/slate",      label: "navSlate",        icon: <FileStack size={15} /> },
  { key: "compliance",      href: "/admin/tenant-qa/compliance", label: "navCompliance",   icon: <ShieldAlert size={15} /> },
];

function isSection(key: SubNavKey): key is SectionKey {
  return SECTIONS.some((s) => s.key === key);
}

export default function SocialContentSubNav({ active, onSelectSection }: {
  active: SubNavKey;
  onSelectSection?: (key: SectionKey) => void;
}) {
  const t = useT(M);
  // < 980px the sub-nav is a sideways-scrolling row above the content (it was a `<style>` media query on a class,
  // AA-1006: the kit inline preset has no className, so the breakpoint is a hook).
  const narrow = useMediaQuery(SPLIT_QUERY);
  const catalogue = isSection(active);
  const itemStyle = (on: boolean): React.CSSProperties => ({
    display: "flex", alignItems: "center", gap: 8, padding: "9px 12px", borderRadius: 7,
    background: on ? K.accentTint : "transparent",
    color: on ? K.accent : K.body, cursor: "pointer", fontFamily: sans,
    fontSize: 12.5, fontWeight: on ? 700 : 500, textAlign: "left",
    textDecoration: "none", whiteSpace: narrow ? "nowrap" : undefined, flexShrink: 0, width: narrow ? "auto" : undefined,
  });
  return (
    <nav data-testid="social-content-subnav" aria-label={t(catalogue ? "subNavLabel" : "subNavLabelQa")} style={{
      width: narrow ? "100%" : 200, flexShrink: 0, display: "flex", flexDirection: narrow ? "row" : "column", gap: 2,
      background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, padding: 6, boxSizing: "border-box",
      position: narrow ? "static" : "sticky", top: 0, overflowX: narrow ? "auto" : undefined,
    }}>
      {catalogue ? SECTIONS.map(s => {
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
      }) : QA_PAGES.map(p => (
        <Link key={p.key} href={p.href} aria-current={active === p.key ? "page" : undefined}
          style={itemStyle(active === p.key)}>
          {p.icon}
          <span style={{ flex: 1 }}>{t(p.label)}</span>
        </Link>
      ))}
    </nav>
  );
}

/** Where the Atoms & routes and Tenant content QA pages stack the sub-nav above the content. */
const SPLIT_QUERY = "(max-width: 980px)";

/** The sub-nav + content split of the Atoms & routes and Tenant content QA pages: a row on desktop; < 980px a column
 *  whose children take the full width (S241 AA-863: a child sized to its fit-content width let a table's min-content
 *  push the column wider than the page). */
export function SocialContentSplit({ gap = 20, children }: { gap?: number; children: React.ReactNode }) {
  const narrow = useMediaQuery(SPLIT_QUERY);
  return (
    <div style={{ display: "flex", gap, flexDirection: narrow ? "column" : "row", alignItems: narrow ? "stretch" : "flex-start" }}>
      {children}
    </div>
  );
}
