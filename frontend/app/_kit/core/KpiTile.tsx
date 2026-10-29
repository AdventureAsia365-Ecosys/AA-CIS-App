// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/KpiTile.tsx
// AA-994 S4a — the dashboard / list KPI tile, upstreamed from AA-Booking `ui/booking/KpiTile.tsx`: a label, a big
// tabular value in the display font, and an optional hint line with a tone. `value === undefined` draws a skeleton
// bar (loading); `null` draws "—".
//
// Additions over the Booking tile, so the CIS-local `Kpi` helpers can switch to it: `size="sm"` (a dense tile for a
// row of six), `valueTone` (a margin in red / green), `info` (an InfoTip next to the label saying where the number
// comes from) and `testId`.

import type { CSSProperties, ReactNode } from "react";
import { InfoTip, SkeletonBar, SkeletonStyle, TONE, type Tone } from "./primitives";
import { K, RADIUS, sans, serif } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

/** Hint / value text colour per tone (the TONE foregrounds; neutral is the muted grey, not the pill grey). */
function toneColour(tone: Tone): string {
  return tone === "neutral" ? K.muted : TONE[tone].fg;
}

export function KpiTile({
  label,
  value,
  hint,
  hintTone = "neutral",
  valueTone,
  icon,
  info,
  size = "md",
  testId,
  style,
  className,
}: {
  label: string;
  /** undefined: loading (skeleton); null: no value ("—"). */
  value: ReactNode | undefined | null;
  hint?: ReactNode;
  hintTone?: Tone;
  /** Colour of the value; default ink. */
  valueTone?: Tone;
  /** A small icon at the top right, in the accent colour. */
  icon?: ReactNode;
  /** Where the number comes from: an InfoTip next to the label. */
  info?: string;
  /** "md" (default) 28px value, 20px padding; "sm" 20px value for dense rows. */
  size?: "md" | "sm";
  testId?: string;
  style?: CSSProperties;
  /** For Tailwind apps (Booking); inline-style apps leave it out. */
  className?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const sm = size === "sm";
  return (
    <div
      data-testid={testId}
      data-kit-kpi=""
      className={className}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: sm ? 4 : 8,
        minWidth: 0,
        padding: sm ? "10px 14px" : 20,
        borderRadius: sm ? RADIUS.md : RADIUS.lg,
        border: `1px solid ${K.line}`,
        background: K.card,
        fontFamily: sans,
        ...style,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, minWidth: 0 }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4, minWidth: 0 }}>
          <span
            title={label}
            style={{
              fontSize: sm ? 12 : 13,
              color: K.muted,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              minWidth: 0,
            }}
          >
            {label}
          </span>
          {info && <InfoTip text={info} testId={testId ? `${testId}-info` : "kpi-info"} />}
        </span>
        {icon && <span style={{ color: K.accent, display: "inline-flex", flexShrink: 0 }}>{icon}</span>}
      </div>
      <div
        style={{
          fontFamily: serif,
          fontSize: sm ? 20 : 28,
          fontWeight: 600,
          lineHeight: 1,
          color: valueTone ? toneColour(valueTone) : K.ink,
          fontVariantNumeric: "tabular-nums",
          minWidth: 0,
          overflowWrap: "anywhere",
        }}
      >
        {value === undefined ? (
          <div role="status" aria-label={t("loading")}>
            <SkeletonStyle />
            <SkeletonBar w={64} h={sm ? 20 : 28} />
          </div>
        ) : value === null ? (
          "—"
        ) : (
          value
        )}
      </div>
      {hint != null && <div style={{ fontSize: 12, color: toneColour(hintTone) }}>{hint}</div>}
    </div>
  );
}
