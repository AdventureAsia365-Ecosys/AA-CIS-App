// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/DateField.tsx
// AA-994 S4a — the kit date field, upstreamed from AA-Booking `ui/booking/DateField.tsx` (the FilterBar date range
// needs it). A native <input type="date"> draws its text in the browser's locale ("10/28/2026" on an en-US machine);
// this field always shows the kit day format ("28 Oct 2026", core/format.ts formatDay, in the UI language) while
// keeping the native value contract, so a page swaps one for the other without touching its state:
//   value / onChange: "YYYY-MM-DD" or "" (empty)
//
// A text box (type "28/10/2026", "28 Oct 2026" or ISO; day first, never US order) plus a calendar button that opens
// the browser's own date picker through a hidden native input (showPicker), so there is no home-made calendar.
// `min` / `max` are honoured by both. What is on screen is always what the page holds:
//   - a real day outside min / max is passed up (as the native input does) and the box is marked invalid, so the
//     page's own range checks and the API see it and block the save;
//   - text that is not a day ("31/11/2026") is marked invalid while the field has focus; on blur it is dropped and
//     the box shows the page's value again;
//   - when the page's value changes from outside (Reset filters, a form reset), any pending text goes.
// The time field stays in Booking until the kit form controls (AA-994 S6a).

import { useRef, useState, type CSSProperties } from "react";
import { CalendarDays } from "lucide-react";
import { formatDay, parseDayInput } from "./format";
import { K } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useLang, useT } from "./useI18n";

// Colour lives in the stylesheet, not the inline style, so the :hover rule can win (an inline style always would).
const PICK_CSS = `.aa-kit-datefield-pick{color:${K.muted}}.aa-kit-datefield-pick:not(:disabled):hover{color:${K.accentDeep}}`;

function inRange(day: string, min?: string, max?: string): boolean {
  return (!min || day >= min) && (!max || day <= max);
}

export function DateField({
  value,
  onChange,
  min,
  max,
  style,
  className,
  wrapperStyle,
  wrapperClassName,
  placeholder,
  disabled,
  id,
  name,
  required,
  "aria-label": ariaLabel,
}: {
  value: string | null | undefined;
  onChange: (value: string) => void;
  min?: string;
  max?: string;
  /** The text box (border, height, width), as for a native input. */
  style?: CSSProperties;
  /** For Tailwind apps (Booking): the text box classes. */
  className?: string;
  /** The wrapper (layout in a flex row or grid). */
  wrapperStyle?: CSSProperties;
  wrapperClassName?: string;
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  name?: string;
  required?: boolean;
  "aria-label"?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const lang = useLang();
  const picker = useRef<HTMLInputElement>(null);
  // What the person is typing; null = show the formatted value.
  const [draft, setDraft] = useState<string | null>(null);
  // A new value from outside drops the pending text (state adjusted during render, no effect).
  const [seen, setSeen] = useState(value);
  if (seen !== value) {
    setSeen(value);
    setDraft(null);
  }
  const shown = draft ?? (value ? formatDay(value, lang) : "");
  const parsed = draft === null ? (value ?? "") : parseDayInput(draft);
  const invalid = parsed === null || (!!parsed && !inRange(parsed, min, max));

  /** Enter keeps unreadable text in the box to fix; blur drops it. A readable day is always passed up. */
  function commit(onBlur: boolean) {
    if (draft === null) return;
    const day = parseDayInput(draft);
    if (day === null) {
      if (onBlur) setDraft(null);
      return;
    }
    setDraft(null);
    if (day !== (value ?? "")) onChange(day);
  }

  function openPicker() {
    const el = picker.current;
    if (!el) return;
    try {
      el.showPicker();
    } catch {
      el.focus();
      el.click();
    }
  }

  return (
    <span
      className={wrapperClassName}
      data-kit-field="date"
      style={{ position: "relative", display: "flex", minWidth: 0, ...wrapperStyle }}
    >
      <style href="aa-kit-datefield" precedence="default">{PICK_CSS}</style>
      <input
        id={id}
        name={name}
        required={required}
        aria-label={ariaLabel}
        type="text"
        inputMode="text"
        autoComplete="off"
        value={shown}
        placeholder={placeholder ?? t("datePlaceholder")}
        disabled={disabled}
        aria-invalid={invalid || undefined}
        title={invalid ? t(min || max ? "invalidDateRange" : "invalidDate") : undefined}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => commit(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit(false);
          } else if (e.key === "Escape" && draft !== null) {
            setDraft(null);
          }
        }}
        className={className}
        style={{ ...style, paddingRight: 34, ...(invalid ? { borderColor: K.danger } : null) }}
      />
      <button
        type="button"
        tabIndex={-1}
        disabled={disabled}
        onClick={openPicker}
        aria-label={ariaLabel ? t("pickDateFor", ariaLabel) : t("pickDate")}
        className="aa-kit-datefield-pick"
        style={{
          position: "absolute",
          right: 4,
          top: "50%",
          transform: "translateY(-50%)",
          display: "grid",
          placeItems: "center",
          width: 28,
          height: 28,
          padding: 0,
          border: "none",
          borderRadius: 4,
          background: "transparent",
          cursor: disabled ? "not-allowed" : "pointer",
          opacity: disabled ? 0.5 : 1,
        }}
      >
        <CalendarDays size={15} />
      </button>
      {/* The browser's picker only: never visible, never focused by Tab; its locale text is never shown. */}
      <input
        ref={picker}
        type="date"
        tabIndex={-1}
        aria-hidden="true"
        value={value ?? ""}
        min={min}
        max={max}
        disabled={disabled}
        onChange={(e) => {
          setDraft(null);
          onChange(e.target.value);
        }}
        style={{
          position: "absolute", bottom: 0, right: 0, width: 1, height: 1, opacity: 0, pointerEvents: "none",
          border: 0, padding: 0,
        }}
      />
    </span>
  );
}
