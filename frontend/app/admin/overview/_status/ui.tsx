"use client";
// app/admin/overview/_status/ui.tsx — AA-1140: small shared pieces of the Ecosystem status page (kit only): the card
// frame with its own "as of" time, the grey "not measured yet" chip, level badges, chip filters and a sort picker.

import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Badge, ChipButton, K, LinkButton, Section, Select, useFormat, useT, type Tone } from "../../../_kit";
import { flipChip, levelTone, sectionDown, type Section as Sec } from "@/lib/opsStatus";
import { STATUS } from "../status.messages";

export function useST() {
  return useT(STATUS);
}
export type ST = ReturnType<typeof useST>;
export type SKey = Parameters<ST>[0];

export const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
export const rowWrap = { display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" } as const;
export const num = { fontVariantNumeric: "tabular-nums" } as const;

/** One card of the page: kit Section with the section's own "as of" time at the right of the header. */
export function Card({ title, testId, asOf, actions, children, stale }: {
  title: ReactNode; testId: string; asOf?: string | null; actions?: ReactNode; children: ReactNode; stale?: boolean;
}) {
  const t = useST();
  const { formatDateTime } = useFormat();
  const head = (
    <span style={rowWrap}>
      {asOf && <span style={small} data-testid={`${testId}-asof`}>{t("asOf", formatDateTime(asOf))}</span>}
      {stale && <Badge tone="neutral">{t("stale")}</Badge>}
      {actions}
    </span>
  );
  return (
    <Section title={title} actions={head} testId={testId}
      style={{ minWidth: 0, opacity: stale ? 0.7 : 1 }} bodyStyle={{ display: "grid", gap: 12, minWidth: 0 }}>
      {children}
    </Section>
  );
}

/** The grey chip of a signal with no source yet, with the reason as its title (never green). */
export function NotMeasured({ reason, testId }: { reason?: string | null; testId?: string }) {
  const t = useST();
  return (
    <span data-testid={testId} title={reason ?? undefined} style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
      <Badge tone="neutral" dot={false}>{t("notMeasured")}</Badge>
      {reason && <span style={small}>{reason}</span>}
    </span>
  );
}

/** A section that did not answer: its reason in place of its body. */
export function SectionDown({ section, testId }: { section: Sec<unknown> | null | undefined; testId?: string }) {
  const t = useST();
  if (section?.status === "not_measured") return <NotMeasured reason={section.reason} testId={testId} />;
  return (
    <div data-testid={testId} style={small}>
      <Badge tone="neutral" dot={false}>{t("unavailable")}</Badge>{" "}
      {section?.message ?? section?.reason ?? ""}
    </div>
  );
}

/** Renders the body only for a section that answered "ok". */
export function WhenUp<T>({ section, testId, children }: {
  section: Sec<T> | null | undefined; testId?: string; children: (data: T) => ReactNode;
}) {
  if (sectionDown(section) || section?.data === undefined) return <SectionDown section={section} testId={testId} />;
  return <>{children(section.data)}</>;
}

export function LevelBadge({ level, children }: { level: string | null | undefined; children: ReactNode }) {
  return <Badge tone={levelTone(level) as Tone}>{children}</Badge>;
}

/** A colour for a value next to a level (ink when fine or unknown). */
export function levelColor(level: string | null | undefined): string {
  const tone = levelTone(level);
  if (tone === "danger") return K.danger;
  return tone === "warning" ? K.amber : K.ink;
}

/** A multi-select chip row (nothing chosen = all). `onChange` gets a function of the CURRENT list (read from the URL
 *  when the change is written), not a list built from this render's `value`: two quick clicks then both count. */
export function ChipFilter({ label, options, value, onChange, testId }: {
  label: string; options: { value: string; label: string; count?: number }[]; value: string[];
  onChange: (update: (current: string[]) => string[]) => void; testId: string;
}) {
  const flip = (v: string) => onChange((cur) => flipChip(cur, v));
  return (
    <span role="group" aria-label={label} style={rowWrap} data-testid={testId}>
      <span style={small}>{label}</span>
      {options.map((o) => (
        <ChipButton key={o.value} size="sm" selected={value.includes(o.value)} onClick={() => flip(o.value)}>
          {o.label}{o.count != null ? ` ${o.count}` : ""}
        </ChipButton>
      ))}
    </span>
  );
}

/** "Sort by" select for a list; value "col:dir". */
export function SortSelect({ label, options, value, onChange }: {
  label: string; options: { value: string; label: string }[]; value: string; onChange: (v: string) => void;
}) {
  return (
    <Select size="sm" aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}
      options={options} style={{ width: "auto" }} />
  );
}

/** The sort options of a column list: each column ascending and descending. */
export function sortOptions(cols: { col: string; label: string }[]): { value: string; label: string }[] {
  return cols.flatMap((c) => [{ value: `${c.col}:desc`, label: `${c.label} ↓` }, { value: `${c.col}:asc`,
    label: `${c.label} ↑` }]);
}

/** Long text (an alarm description, an ops check message) clamped to `lines` lines with a "Show more" toggle that
 *  appears only when the text is cut; the server never cuts it mid-word. `label` is a small caption before the text
 *  (outside text: "As written by the CloudWatch alarm"). */
export function ClampText({ text, lines = 3, label, testId }: {
  text: string; lines?: number; label?: string | null; testId?: string;
}) {
  const t = useST();
  const ref = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  const [cut, setCut] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && !open) setCut(el.scrollHeight > el.clientHeight + 1);
  }, [text, open, lines]);
  const clamp = open ? {} : { display: "-webkit-box", WebkitLineClamp: lines, WebkitBoxOrient: "vertical" as const,
    overflow: "hidden" };
  return (
    <span style={{ display: "grid", gap: 2, minWidth: 0 }} data-testid={testId}>
      {label && <span style={{ ...small, fontStyle: "italic" }}>{label}</span>}
      <span ref={ref} style={{ ...small, overflowWrap: "anywhere", ...clamp }}>{text}</span>
      {(cut || open) && (
        <span><LinkButton size="sm" tone="muted" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? t("showLess") : t("showMore")}</LinkButton></span>
      )}
    </span>
  );
}
