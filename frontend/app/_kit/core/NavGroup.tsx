// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/NavGroup.tsx
// S264 — a collapsible group of a sidebar nav (Pipeline, Assets, CRM, Money…), the same in every app's sidebar.
//
// NavGroupHeader is the group title as a disclosure button: uppercase caption, chevron, aria-expanded and
// aria-controls. Folded, it carries what its items would show: the sum of their counts as a pill (`badge`, e.g. the
// Review Queue's pending count) and the accent colour when it holds the current page (`active`).
// NavGroup wraps a header and its items. The items stay in the DOM (`hidden` when folded) so aria-controls always
// points at an element. In the icon rail (`rail`: the sidebar collapsed to icons, which has no title to unfold with)
// the header is not rendered and every item shows, folded or not. A group without a label (a one-item group named
// like its item) has no header and is always open.
//
// State comes from the caller (useNavGroups in useSidebar.ts); this file only renders. data-* props pass through to
// the header button (headerProps) and the wrapper (rest), so an app's CSS hooks and test ids reach the DOM.

import { useId } from "react";
import { ChevronRight } from "lucide-react";
import { K, sans } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

/** "99+" above 99, like the item badges. */
export function navBadgeText(n: number): string {
  return n > 99 ? "99+" : String(n);
}

export interface NavGroupHeaderProps {
  label: string;
  open: boolean;
  onToggle: () => void;
  /** id of the items element the button shows / hides. */
  controls?: string;
  /** The group holds the current page (shown in the accent colour while folded). */
  active?: boolean;
  /** Count shown on the folded header (sum of the item counts); hidden when open, null or 0. */
  badge?: number | null;
  /** Accessible name of the count, default "<n> in this group". */
  badgeLabel?: string;
  className?: string;
  style?: React.CSSProperties;
  testId?: string;
  /** Extra attributes for the button (data-* hooks of the app's CSS). */
  headerProps?: Record<string, string | undefined>;
}

export function NavGroupHeader({
  label, open, onToggle, controls, active = false, badge, badgeLabel, className, style, testId, headerProps,
}: NavGroupHeaderProps) {
  const t = useT(KIT_MESSAGES);
  const count = !open && badge != null && badge > 0 ? badge : 0;
  const color = !open && active ? K.accent : "var(--aa-side-text3)";
  return (
    <button
      type="button"
      {...headerProps}
      className={className}
      onClick={onToggle}
      aria-expanded={open}
      aria-controls={controls}
      title={open ? t("hideGroup", label) : t("showGroup", label)}
      data-testid={testId}
      data-active={active ? "true" : undefined}
      style={{
        display: "flex", alignItems: "center", gap: 6, width: "100%",
        padding: "0 10px 8px", border: "none", background: "transparent", cursor: "pointer",
        fontFamily: sans, fontSize: 9.5, fontWeight: 600, letterSpacing: "0.16em",
        textTransform: "uppercase", textAlign: "left", color,
        ...style,
      }}
    >
      <span style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {label}
      </span>
      {count > 0 && (
        <span
          data-nav-group-badge="true"
          aria-label={badgeLabel ?? t("groupBadge", count)}
          style={{
            background: K.accent, color: "var(--aa-on-accent)", borderRadius: 999,
            fontSize: 9.5, fontWeight: 700, letterSpacing: 0, minWidth: 16, height: 15,
            display: "grid", placeItems: "center", padding: "0 4px", flexShrink: 0,
          }}
        >
          {navBadgeText(count)}
        </span>
      )}
      <ChevronRight
        size={12}
        aria-hidden="true"
        style={{ flexShrink: 0, opacity: 0.8, transform: open ? "rotate(90deg)" : "none", transition: "transform .15s" }}
      />
    </button>
  );
}

export interface NavGroupProps extends Omit<NavGroupHeaderProps, "label" | "controls" | "style" | "className" | "testId"> {
  /** Stable id (navGroupId(label)), used for the test ids. */
  id: string;
  /** The group title; null or empty = no header, always open. */
  label?: string | null;
  /** The sidebar is the icon rail: no header, every item shown. */
  rail?: boolean;
  children: React.ReactNode;
  /** Wrapper style (spacing between groups). */
  style?: React.CSSProperties;
  headerClassName?: string;
  headerStyle?: React.CSSProperties;
  /** Style of the items container while shown (gap between items). */
  itemsStyle?: React.CSSProperties;
  /** Test id prefix: `<testId>` on the wrapper, `<testId>-toggle` on the header. Default `nav-group-<id>`. */
  testId?: string;
}

export function NavGroup({
  id, label, open, onToggle, active, badge, badgeLabel, rail = false, children,
  style, headerClassName, headerStyle, headerProps, itemsStyle, testId,
}: NavGroupProps) {
  const regionId = useId();
  const tid = testId ?? `nav-group-${id}`;
  const titled = !!label;
  const shown = !titled || rail || open;
  return (
    <div data-testid={tid} data-nav-group={id} data-open={shown ? "true" : "false"} style={style}>
      {titled && !rail && (
        <NavGroupHeader
          label={label}
          open={open}
          onToggle={onToggle}
          controls={regionId}
          active={active}
          badge={badge}
          badgeLabel={badgeLabel}
          className={headerClassName}
          style={headerStyle}
          testId={`${tid}-toggle`}
          headerProps={headerProps}
        />
      )}
      <div
        id={regionId}
        role="group"
        aria-label={label || undefined}
        hidden={!shown}
        style={shown ? { display: "flex", flexDirection: "column", gap: 1, ...itemsStyle } : undefined}
      >
        {children}
      </div>
    </div>
  );
}
