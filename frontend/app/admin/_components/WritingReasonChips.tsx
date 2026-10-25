"use client";
// app/admin/_components/WritingReasonChips.tsx — AA-916 WM-07. The one-click reason chip row an admin sets when
// acting on writer output: the Content Quality sentence actions (T9 drawer, T2 TourVersionDrawer) and the A1 review
// Reject / Dismiss / Regenerate. One chip is always selected (preselected from the flagged gate or the review row's
// failure codes), so the action button stays one click; another chip is one more click. The value goes to the API
// as `reason_code` (shared.writing_signal, CIS 277). Codes and labels: lib/writingReasons.ts.
//
// The chips are a radio group (role="radiogroup" / "radio", arrow keys move the choice) on the kit tokens; the kit
// Button has no pressed / checked state to carry this.

import { K, sans } from "../../_kit";
import { REASON_CODES, REASON_HINT, REASON_LABEL, type ReasonCode } from "@/lib/writingReasons";

export function WritingReasonChips({ value, onChange, disabled = false, suggested, label = "Why" }: {
  value: ReasonCode;
  onChange: (code: ReasonCode) => void;
  disabled?: boolean;
  /** The preselected code (marked "suggested" in its tooltip). */
  suggested?: ReasonCode;
  label?: string;
}) {
  const move = (delta: number) => {
    const i = REASON_CODES.indexOf(value);
    onChange(REASON_CODES[(i + delta + REASON_CODES.length) % REASON_CODES.length]);
  };
  return (
    <div data-testid="wm-reason-chips" style={{ display: "flex", flexWrap: "wrap", gap: 5, alignItems: "center", minWidth: 0 }}>
      <span style={{ fontSize: 11, fontWeight: 600, color: K.muted, marginRight: 2 }}>{label}</span>
      <div role="radiogroup" aria-label={`${label}: reason for this action`}
           style={{ display: "flex", flexWrap: "wrap", gap: 5, minWidth: 0 }}
           onKeyDown={(e) => {
             if (disabled) return;
             if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); move(1); }
             if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); move(-1); }
           }}>
        {REASON_CODES.map((code) => {
          const on = code === value;
          return (
            <button key={code} type="button" role="radio" aria-checked={on} tabIndex={on ? 0 : -1}
                    data-reason={code} data-selected={on ? "true" : undefined} disabled={disabled}
                    title={`${REASON_HINT[code]}${code === suggested ? " (suggested)" : ""}`}
                    onClick={() => onChange(code)}
                    style={{
                      fontFamily: sans, fontSize: 11, fontWeight: on ? 700 : 500, lineHeight: 1.3,
                      padding: "3px 9px", borderRadius: 999, whiteSpace: "nowrap",
                      cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.55 : 1,
                      background: on ? K.accent : "transparent",
                      color: on ? "var(--aa-on-accent)" : K.body,
                      border: `1px solid ${on ? K.accent : K.line}`,
                    }}>
              {REASON_LABEL[code]}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** The stored reason of an admin edit, read-only (history rows). */
export function ReasonTag({ code }: { code: string | null | undefined }) {
  if (!code) return null;
  const known = (REASON_CODES as readonly string[]).includes(code);
  return (
    <span data-testid="wm-reason-tag" title={known ? REASON_HINT[code as ReasonCode] : code}
          style={{ fontSize: 10.5, fontWeight: 600, padding: "1px 8px", borderRadius: 999, color: K.accentDeep,
                   border: `1px solid ${K.accentBorder}`, whiteSpace: "nowrap" }}>
      {known ? REASON_LABEL[code as ReasonCode] : code}
    </span>
  );
}
