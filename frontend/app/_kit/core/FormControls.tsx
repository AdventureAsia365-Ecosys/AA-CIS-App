// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/FormControls.tsx
// AA-994 S6a — the kit form controls: one labelled `Field` and the native controls it wraps (`Input`, `Select`,
// `Textarea`, `Checkbox`, `Radio`). Every app that vendors the full core draws its forms with these, so a form looks
// and behaves the same in CIS admin, the portal, CRM web and Booking (lint rule `no-raw-form-control`, core
// eslint-kit.mjs). The date field stays `DateField` (same Field wrapper around it).
//
//   <Field label="Supplier's bill number" required hint="As printed on the bill." error={errorFor(errs, "number")}>
//     <Input value={num} onChange={(e) => setNum(e.target.value)} />
//   </Field>
//
// - Look: label above (13px 500), control 40px high (`size="sm"`: 36px, the FilterBar height), radius 8, card
//   background with a 1px line; focus = accent border, invalid = danger border, disabled = panel background. The
//   base look is inline (K tokens, both themes), the states come from one small stylesheet so they win over it.
// - The controls stay native: every native prop (value, onChange, name, placeholder, min, data-*, ref…) passes
//   through, so a page swaps `<input>` for `<Input>` without touching its state. `style` merges last; `className`
//   is passed on for Tailwind apps (layout utilities such as width; colours come from the tokens).
// - Inside a Field the control is labelled by it (the Field is a <label>), described by its hint / error
//   (aria-describedby), `aria-invalid` when the Field has an error and `aria-required` when it is required. The
//   native `required` attribute is never set for the page: browser validation stays the page's choice.
// - `Field as="div"` for a group (several checkboxes or radios): a role="group" labelled by the Field label.

import {
  createContext,
  useContext,
  useId,
  type CSSProperties,
  type InputHTMLAttributes,
  type ReactNode,
  type Ref,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { Lock } from "lucide-react";
import { K, RADIUS, sans } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

export type ControlSize = "sm" | "md";

/** What a Field tells the control inside it. */
interface FieldState {
  describedBy?: string;
  invalid: boolean;
  required: boolean;
}

const FieldContext = createContext<FieldState | null>(null);

export const CONTROL_CLASS = "aa-kit-control";

// States in the stylesheet (inline styles always win over a class, so the states use !important over the inline base).
const CONTROL_CSS =
  `.${CONTROL_CLASS}:focus{border-color:${K.accentBorder} !important}` +
  `.${CONTROL_CLASS}[aria-invalid="true"]{border-color:${K.danger} !important}` +
  `.${CONTROL_CLASS}:disabled{background:${K.panel} !important;color:${K.muted} !important;cursor:not-allowed}` +
  `.${CONTROL_CLASS}::placeholder{color:${K.muted2}}` +
  `.aa-kit-choice input:disabled+span{color:${K.muted}}`;

function ControlStyle() {
  return <style href="aa-kit-form-controls" precedence="default">{CONTROL_CSS}</style>;
}

/** The look of every text-like control (Input, Select); Textarea adds its own padding. */
export function controlStyle(size: ControlSize = "md"): CSSProperties {
  return {
    height: size === "sm" ? 36 : 40,
    boxSizing: "border-box",
    minWidth: 0,
    maxWidth: "100%",
    borderRadius: RADIUS.md,
    border: `1px solid ${K.line}`,
    background: K.card,
    padding: "0 12px",
    fontSize: size === "sm" ? 13 : 14,
    fontFamily: sans,
    color: K.ink,
    outline: "none",
  };
}

function joinIds(...ids: (string | undefined)[]): string | undefined {
  const s = ids.filter(Boolean).join(" ");
  return s || undefined;
}

function joinClass(...names: (string | undefined)[]): string {
  return names.filter(Boolean).join(" ");
}

/** The ARIA wiring a control takes from its Field (its own props win). */
function useFieldAria(props: {
  invalid?: boolean;
  "aria-describedby"?: string;
  "aria-invalid"?: InputHTMLAttributes<HTMLInputElement>["aria-invalid"];
  "aria-required"?: InputHTMLAttributes<HTMLInputElement>["aria-required"];
}) {
  const field = useContext(FieldContext);
  const invalid = props.invalid ?? (props["aria-invalid"] === true || props["aria-invalid"] === "true" || !!field?.invalid);
  return {
    "aria-describedby": joinIds(field?.describedBy, props["aria-describedby"]),
    "aria-invalid": invalid || undefined,
    "aria-required": props["aria-required"] ?? (field?.required || undefined),
  };
}

// ── Field ────────────────────────────────────────────────────────────────────────────────────────────────────────

export function Field({
  label,
  children,
  required = false,
  hint,
  error,
  locked = false,
  as = "label",
  style,
  className,
}: {
  label: ReactNode;
  children: ReactNode;
  /** Shows the required mark and sets aria-required on the control. */
  required?: boolean;
  hint?: ReactNode;
  /** Shown instead of the hint, in the danger colour; marks the control invalid. */
  error?: ReactNode;
  /** Read-only for a reason the user should see (lock icon; give the reason as the hint). */
  locked?: boolean;
  /** "div" for a group of controls (checkboxes, radios): role="group" labelled by the label. */
  as?: "label" | "div";
  style?: CSSProperties;
  className?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const id = useId();
  const labelId = `${id}-label`;
  const noteId = `${id}-note`;
  const hasError = error != null && error !== false && error !== "";
  const note = hasError ? error : hint;
  const state: FieldState = { describedBy: note ? noteId : undefined, invalid: hasError, required };
  const isGroup = as === "div";
  const Tag = as;
  return (
    <Tag
      data-kit-field=""
      className={className}
      {...(isGroup ? { role: "group", "aria-labelledby": labelId, "aria-describedby": state.describedBy } : null)}
      style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 0, fontFamily: sans, ...style }}
    >
      <span id={labelId} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 500, color: K.ink }}>
        {label}
        {required && <span aria-hidden="true" style={{ color: K.danger }}>*</span>}
        {locked && <Lock size={12} color={K.muted2} aria-label={t("locked")} />}
      </span>
      <FieldContext.Provider value={state}>{children}</FieldContext.Provider>
      {note ? (
        <span id={noteId} style={{ fontSize: 12, color: hasError ? K.danger : K.muted, overflowWrap: "anywhere" }}>
          {note}
        </span>
      ) : null}
    </Tag>
  );
}

// ── Input / Select / Textarea ────────────────────────────────────────────────────────────────────────────────────

type OwnProps = {
  /** 40px (forms, default) or 36px (toolbars and filter rows, the FilterBar height). */
  size?: ControlSize;
  /** Danger border + aria-invalid; inside a Field with an error it is set already. */
  invalid?: boolean;
};

export type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "size"> & OwnProps & { ref?: Ref<HTMLInputElement> };

/** A text-like native input (text, email, number, search, password, url, tel, time, month…). Checkboxes and radios
 *  are `Checkbox` / `Radio`; a calendar day is `DateField`. */
export function Input({ size = "md", invalid, style, className, ref, ...rest }: InputProps) {
  const aria = useFieldAria({ invalid, ...rest });
  return (
    <>
      <ControlStyle />
      <input
        ref={ref}
        {...rest}
        {...aria}
        className={joinClass(CONTROL_CLASS, className)}
        style={{ ...controlStyle(size), ...style }}
      />
    </>
  );
}

export type SelectOption = { value: string; label: ReactNode; disabled?: boolean };

export type SelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, "size"> &
  OwnProps & {
    /** The options; or pass <option> children. */
    options?: SelectOption[];
    /** A first option with the empty value ("All statuses", "Choose a supplier…"). */
    placeholder?: string;
    ref?: Ref<HTMLSelectElement>;
  };

/** A native select (the browser's own list on every device). */
export function Select({ size = "md", invalid, options, placeholder, children, style, className, ref, ...rest }: SelectProps) {
  const aria = useFieldAria({ invalid, ...rest });
  return (
    <>
      <ControlStyle />
      <select
        ref={ref}
        {...rest}
        {...aria}
        className={joinClass(CONTROL_CLASS, className)}
        style={{ ...controlStyle(size), paddingRight: 8, cursor: rest.disabled ? undefined : "pointer", ...style }}
      >
        {placeholder != null && <option value="">{placeholder}</option>}
        {options?.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
        {children}
      </select>
    </>
  );
}

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement> &
  Omit<OwnProps, "size"> & { ref?: Ref<HTMLTextAreaElement> };

/** A native multi-line text box, 3 rows by default, resizable in height only. */
export function Textarea({ invalid, rows = 3, style, className, ref, ...rest }: TextareaProps) {
  const aria = useFieldAria({ invalid, ...rest });
  return (
    <>
      <ControlStyle />
      <textarea
        ref={ref}
        rows={rows}
        {...rest}
        {...aria}
        className={joinClass(CONTROL_CLASS, className)}
        style={{
          ...controlStyle("md"), height: "auto", padding: "8px 12px", lineHeight: 1.5, resize: "vertical", ...style,
        }}
      />
    </>
  );
}

// ── Checkbox / Radio ─────────────────────────────────────────────────────────────────────────────────────────────

export type ChoiceProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size"> & {
  /** The text next to the box; without it, give the control an aria-label. */
  label?: ReactNode;
  /** Checkbox only: the mixed state ("some rows selected"). */
  indeterminate?: boolean;
  invalid?: boolean;
  ref?: Ref<HTMLInputElement>;
};

function Choice({
  type,
  label,
  indeterminate,
  invalid,
  style,
  className,
  ref,
  ...rest
}: ChoiceProps & { type: "checkbox" | "radio" }) {
  const aria = useFieldAria({ invalid, ...rest });
  // The mixed state is a DOM property only (no attribute); set it on every render through the callback ref.
  const setRef = (el: HTMLInputElement | null) => {
    if (el && type === "checkbox") el.indeterminate = !!indeterminate;
    if (typeof ref === "function") ref(el);
    else if (ref) ref.current = el;
  };
  const box = (
    <input
      ref={setRef}
      type={type}
      {...rest}
      {...aria}
      style={{
        width: 16, height: 16, margin: 0, flexShrink: 0, accentColor: K.accent,
        cursor: rest.disabled ? "not-allowed" : "pointer",
        ...(label == null ? style : null),
      }}
      className={label == null ? className : undefined}
    />
  );
  if (label == null) return box;
  return (
    <label
      className={joinClass("aa-kit-choice", className)}
      style={{
        display: "inline-flex", alignItems: "center", gap: 8, minWidth: 0, fontSize: 14, fontFamily: sans, color: K.ink,
        cursor: rest.disabled ? "not-allowed" : "pointer", ...style,
      }}
    >
      <ControlStyle />
      {box}
      <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>{label}</span>
    </label>
  );
}

/** A native checkbox in the accent colour, with its label to the right. */
export function Checkbox(props: ChoiceProps) {
  return <Choice type="checkbox" {...props} />;
}

/** A native radio in the accent colour, with its label to the right (group them in `Field as="div"`). */
export function Radio(props: Omit<ChoiceProps, "indeterminate">) {
  return <Choice type="radio" {...props} />;
}
