// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/Buttons.tsx
// AA-994 — every kind of button the apps draw, so page code never hand-styles a <button> (lint rule
// `no-hand-styled-button`, core eslint-kit.mjs). `primitives.tsx` re-exports this file, so the barrels that
// `export * from "./core/primitives"` (CIS, portal, CRM web, Booking) get every name without a barrel change.
//
//   Button          the brand button: primary (gold pill), secondary (gold outline), danger, ghost, dashed ("add …")
//   LinkButton      an action that reads as a text link ("Show all", "Change", "Show changes (3)"); navigation stays a
//                   real link (next/link)
//   IconButton      an icon-only button; `label` is required (aria-label, and the tooltip unless `title` is given)
//   SegmentedControl one choice out of a few, as a radio group: a track (like Tabs) or separate chips
//   ChipButton      one toggle chip (a filter chip, one of several categories); several can be on at once
//   Switch          an on / off setting that applies at once (role="switch")
//   ButtonGroup     a labelled group of buttons (role="group"), spaced or attached edge to edge
//   ListButton      a full-width row in a popup list or menu (role="option" / "menuitem"): picker results, menu items
//   Pressable       a whole clickable surface with no look of its own (a photo tile, a card, a select-like trigger);
//                   never a way to rebuild one of the looks above
//
// Shared by all of them:
// - `type="button"` unless set, so a button inside a <form> never submits by accident.
// - ARIA and native passthrough: `aria-*`, `data-*`, `role`, `id`, `name`, `value`, `form`, `tabIndex`, `autoFocus`
//   and focus / key / mouse-down handlers reach the DOM <button>, so a menu trigger keeps `aria-haspopup` /
//   `aria-expanded` and a toggle keeps `aria-pressed`. `testId` sets `data-testid` (lesson S252: a kit component used
//   to drop it). `ariaLabel` stays for the existing call sites; `aria-label` works too.
// - `onClick` receives the mouse event (`e.stopPropagation()` inside a clickable row).
// - `ref` reaches the <button> (React 19 ref prop): a popup returns focus to its trigger.
// - `style` merges last; `className` is passed on for Tailwind apps (layout utilities such as `mt-3`, `w-full`; colours
//   come from the tokens). The CIS inline preset forbids `className` in pages anyway (rule `no-class-name`).
// - Look: inline, from K tokens (both themes). Hover, focus-visible and disabled states come from one hoisted
//   stylesheet (React 19 <style href precedence>), like the form controls; the states use !important over the
//   inline base where they change a colour the base sets.

import {
  useRef,
  type AriaAttributes,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type Ref,
} from "react";
import { K, RADIUS, sans, BTN_RADIUS, BTN_PRIMARY_TEXT } from "./tokens";

// ── shared ──────────────────────────────────────────────────────────────────────────────────────────────────────

/** Native attributes that pass through to the DOM button. */
type NativePass = Pick<
  ButtonHTMLAttributes<HTMLButtonElement>,
  | "id" | "name" | "value" | "form" | "tabIndex" | "autoFocus" | "role"
  | "onMouseDown" | "onPointerDown" | "onKeyDown" | "onFocus" | "onBlur" | "onMouseEnter" | "onMouseLeave"
> &
  AriaAttributes & { [data: `data-${string}`]: string | number | boolean | undefined };

/** Props every kit button takes. */
export type BaseButtonProps = NativePass & {
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  type?: "button" | "submit" | "reset";
  title?: string;
  /** Same as `aria-label` (kept for the call sites written before the passthrough). */
  ariaLabel?: string;
  /** Sets `data-testid` on the <button>. */
  testId?: string;
  style?: CSSProperties;
  /** Layout utilities for Tailwind apps; never colours. */
  className?: string;
  ref?: Ref<HTMLButtonElement>;
};

export const BUTTON_CLASS = "aa-kit-btn";

// The focus ring is accent-deep, not accent: a focus indicator needs 3:1 on the surfaces it sits on (WCAG 1.4.11 /
// 2.4.13) and light --aa-accent is about 2.5:1 on card. lib/tokensContrast.test.mjs checks the ring pairs.
const FOCUS_RING = `outline:2px solid ${K.accentDeep}`;
const FOCUS = `${FOCUS_RING};outline-offset:2px`;
// Inset variant for buttons that fill their row (ListButton): an outside ring would be clipped by the popup.
const FOCUS_INSET = `${FOCUS_RING};outline-offset:-2px`;

// One stylesheet for every button state. Inline styles win over a class, so a state that changes a colour the inline
// base sets uses !important.
const BUTTONS_CSS =
  `.${BUTTON_CLASS}:focus-visible{${FOCUS}}` +
  `.${BUTTON_CLASS}:disabled{cursor:not-allowed !important;opacity:.5}` +
  // Button
  `.aa-kit-btn--primary:not(:disabled):hover{filter:brightness(.96)}` +
  `.aa-kit-btn--secondary:not(:disabled):hover,.aa-kit-btn--dashed:not(:disabled):hover{background:${K.accentTint} !important}` +
  `.aa-kit-btn--danger:not(:disabled):hover{border-color:${K.danger} !important}` +
  `.aa-kit-btn--ghost:not(:disabled):hover{color:${K.ink} !important;border-color:${K.accentBorder} !important}` +
  // LinkButton
  `.aa-kit-link:not(:disabled):hover{text-decoration:underline;text-underline-offset:2px}` +
  `.aa-kit-link--muted:not(:disabled):hover{color:${K.ink} !important}` +
  // IconButton
  `.aa-kit-icon--outline:not(:disabled):hover,.aa-kit-icon--outline[aria-expanded="true"],` +
  `.aa-kit-icon--outline[aria-pressed="true"]{color:${K.ink} !important;border-color:${K.accentBorder} !important}` +
  `.aa-kit-icon--plain:not(:disabled):hover,.aa-kit-icon--plain[aria-expanded="true"],` +
  `.aa-kit-icon--plain[aria-pressed="true"]{color:${K.ink} !important;background:${K.line2} !important}` +
  `.aa-kit-icon--overlay:not(:disabled):hover{background:var(--aa-hero-bg) !important}` +
  `.aa-kit-icon--danger:not(:disabled):hover{color:${K.danger} !important;background:${K.dangerSoft} !important;` +
  `border-color:${K.dangerBorder} !important}` +
  // SegmentedControl / Tabs-like choices
  `.aa-kit-seg[aria-checked="false"]:not(:disabled):hover{color:${K.ink} !important}` +
  `.aa-kit-chip:not([data-on]):not(:disabled):hover{color:${K.ink} !important;border-color:${K.accentBorder} !important}` +
  // Tabs variant="underline" (primitives.tsx)
  `.aa-kit-tab[aria-selected="false"]:not(:disabled):hover{color:${K.ink} !important}` +
  // The ring goes round the label, not the whole tab: the tablist scrolls (overflow clips) and the tab has no top
  // padding, so a ring round the tab would lose its top and bottom. The tablist keeps 4px of room for it.
  `.aa-kit-tab:focus-visible{outline:none}` +
  `.aa-kit-tab:focus-visible>.aa-kit-tab__label{${FOCUS};border-radius:4px}` +
  // ListButton
  // Keyboard focus keeps a visible inset ring; the tint alone is about 1.1:1 on card and would not show.
  `.aa-kit-listbtn:not(:disabled):hover,.aa-kit-listbtn:not(:disabled):focus-visible{background:${K.accentTint} !important}` +
  `.aa-kit-listbtn:focus-visible{${FOCUS_INSET}}` +
  `.aa-kit-listbtn[data-tone="danger"]:not(:disabled):hover,` +
  `.aa-kit-listbtn[data-tone="danger"]:not(:disabled):focus-visible{background:${K.dangerSoft} !important}` +
  // ButtonGroup attached: neighbours share one border and only the outer corners are round.
  `.aa-kit-btngroup[data-attached]>*+*{margin-left:-1px}` +
  `.aa-kit-btngroup[data-attached]>*:not(:last-child){border-top-right-radius:0 !important;border-bottom-right-radius:0 !important}` +
  `.aa-kit-btngroup[data-attached]>*:not(:first-child){border-top-left-radius:0 !important;border-bottom-left-radius:0 !important}` +
  `.aa-kit-btngroup[data-attached]>*:focus-visible{position:relative;z-index:1}`;

/** The hoisted stylesheet (rendered once per page by React, whatever the number of buttons). */
export function ButtonStyles() {
  return <style href="aa-kit-buttons" precedence="default">{BUTTONS_CSS}</style>;
}

function joinClass(...names: (string | false | undefined)[]): string {
  return names.filter(Boolean).join(" ");
}

/**
 * Roving focus for a radio group or a tab list: the index the key moves to, skipping disabled items and wrapping
 * around; -1 when every item is disabled; null when the key is not a move key (let it through). ArrowRight / ArrowDown
 * = next, ArrowLeft / ArrowUp = previous, Home = first, End = last enabled item.
 */
const ROVING_KEYS: Record<string, 1 | -1 | "first" | "last" | undefined> = {
  ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1, Home: "first", End: "last",
};

export function rovingTarget(items: readonly { disabled?: boolean }[], from: number, key: string): number | null {
  const step = Object.prototype.hasOwnProperty.call(ROVING_KEYS, key) ? ROVING_KEYS[key] : undefined;
  if (step === undefined) return null;
  const n = items.length;
  let i = step === "first" ? -1 : step === "last" ? n : from;
  const dir = step === "last" || step === -1 ? -1 : 1;
  for (let k = 0; k < n; k++) {
    i = (i + dir + n) % n;
    if (!items[i].disabled) return i;
  }
  return -1;
}

/** Split the kit props from what goes straight to the DOM button. */
function domProps(p: BaseButtonProps) {
  const { onClick, disabled, type, title, ariaLabel, testId, ref, style: _s, className: _c, ...native } = p;
  void _s;
  void _c;
  return {
    ...native,
    ref,
    type: type ?? "button",
    onClick,
    disabled,
    title,
    "aria-label": native["aria-label"] ?? ariaLabel,
    "data-testid": testId,
  };
}

const RESET: CSSProperties = {
  margin: 0,
  fontFamily: sans,
  WebkitTapHighlightColor: "transparent",
};

// ── Button (AA-752) ───────────────────────────────────────────────────────────────────────────────────────────
// The canonical brand button. Variants follow adventure.asia: primary = gold pill (uppercase on md/lg), secondary =
// gold outline, danger = red soft, ghost = line outline, dashed = an "add another …" slot (dashed gold outline, never
// uppercase). `size="sm"` stays non-uppercase for dense table rows (AA-605). The admin legacy `Btn` re-points here.
export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost" | "dashed";
export type ButtonSize = "sm" | "md" | "lg";

export type ButtonProps = BaseButtonProps & {
  children: ReactNode;
  variant?: ButtonVariant;
  size?: ButtonSize;
};

export function Button(props: ButtonProps) {
  const { children, variant = "secondary", size = "md", style = {}, className, ...rest } = props;
  const pad = { sm: "5px 12px", md: "8px 16px", lg: "10px 22px" }[size];
  const fz = { sm: 11, md: 13, lg: 14 }[size];
  // secondary is the adventure.asia gold outline (EXPLORE); primary is the gold pill (ENQUIRE).
  const base: Record<ButtonVariant, CSSProperties> = {
    primary: { background: K.accent, color: "var(--aa-on-accent)", border: `1px solid ${K.accent}` },
    secondary: { background: "transparent", color: K.accentDeep, border: `1px solid ${K.accentBorder}` },
    danger: { background: K.dangerSoft, color: K.danger, border: `1px solid ${K.dangerBorder}` },
    ghost: { background: "transparent", color: K.muted, border: `1px solid ${K.line}` },
    dashed: { background: "transparent", color: K.accentDeep, border: `1px dashed ${K.accentBorder}` },
  };
  return (
    <>
      <ButtonStyles />
      <button
        {...domProps(rest)}
        className={joinClass(BUTTON_CLASS, `aa-kit-btn--${variant}`, className)}
        style={{
          ...RESET,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 6,
          padding: pad,
          borderRadius: BTN_RADIUS,
          fontSize: fz,
          fontWeight: 600,
          cursor: rest.disabled ? "not-allowed" : "pointer",
          opacity: rest.disabled ? 0.5 : 1,
          transition: "opacity .15s",
          // Brand uppercase only on md/lg primary + secondary; "sm" sits in dense table rows where the wider
          // uppercase label overflowed (AA-605).
          ...base[variant],
          ...((variant === "primary" || variant === "secondary") && size !== "sm" ? BTN_PRIMARY_TEXT : {}),
          ...style,
        }}
      >
        {children}
      </button>
    </>
  );
}

// ── LinkButton ──────────────────────────────────────────────────────────────────────────────────────────────────
// An action that reads as a text link: no box, the label underlines on hover. For an action in running text or a
// card ("Show all", "Change", "Only this record", "Show changes (3)", "Download bill.pdf"). It is a <button>: going to
// another page stays a real link (next/link), so open-in-new-tab and the status bar keep working.
export type LinkButtonTone = "accent" | "muted" | "danger" | "inherit";

export type LinkButtonProps = BaseButtonProps & {
  children: ReactNode;
  /** accent (default) = accent-deep text; muted = secondary text that turns ink on hover; danger; inherit = the
   * surrounding colour (a link inside a coloured notice). */
  tone?: LinkButtonTone;
  /** sm = 12px, md = 13px (default), inherit = the surrounding text size (inside a sentence). */
  size?: "sm" | "md" | "inherit";
  /** semibold (default) for actions; regular for a list of file names. */
  weight?: "regular" | "semibold";
  /** One line with an ellipsis (a long file name); the full text should be in `title`. */
  truncate?: boolean;
};

export function LinkButton(props: LinkButtonProps) {
  const { children, tone = "accent", size = "md", weight = "semibold", truncate = false, style = {}, className, ...rest } =
    props;
  const color = { accent: K.accentDeep, muted: K.muted, danger: K.danger, inherit: "inherit" }[tone];
  return (
    <>
      <ButtonStyles />
      <button
        {...domProps(rest)}
        className={joinClass(BUTTON_CLASS, "aa-kit-link", `aa-kit-link--${tone}`, className)}
        style={{
          ...RESET,
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: 0,
          border: "none",
          background: "transparent",
          color,
          fontSize: size === "inherit" ? "inherit" : size === "sm" ? 12 : 13,
          fontWeight: weight === "regular" ? 400 : 600,
          lineHeight: "inherit",
          textAlign: "left",
          borderRadius: 2,
          cursor: rest.disabled ? "not-allowed" : "pointer",
          ...(truncate
            ? { maxWidth: "100%", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }
            : {}),
          ...style,
        }}
      >
        {truncate ? <TruncatedChildren>{children}</TruncatedChildren> : children}
      </button>
    </>
  );
}

/** Icons keep their size; only text nodes shrink (an inline-flex button cannot ellipsize a bare text child). */
function TruncatedChildren({ children }: { children: ReactNode }) {
  const parts = Array.isArray(children) ? children : [children];
  return (
    <>
      {parts.map((c, i) =>
        typeof c === "string" || typeof c === "number" ? (
          <span key={i} style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {c}
          </span>
        ) : (
          <span key={i} style={{ display: "inline-flex", flexShrink: 0 }}>{c}</span>
        ),
      )}
    </>
  );
}

// ── IconButton ──────────────────────────────────────────────────────────────────────────────────────────────────
// An icon-only button. `label` is required: it is the accessible name (aria-label) and, unless `title` is given (or
// `title=""` to switch it off), the hover tooltip, since an icon alone does not say what it does.
export type IconButtonVariant = "outline" | "plain" | "overlay";
export type IconButtonSize = "xs" | "sm" | "md" | "lg";

export type IconButtonProps = Omit<BaseButtonProps, "ariaLabel"> & {
  /** The icon (lucide-react, 14–16px). */
  children: ReactNode;
  /** What the button does, in words ("Edit internal notes", "Remove Vietnam"). Required. */
  label: string;
  /** outline (default) = card with a line border, like the row menu trigger; plain = no border (inside a chip or a
   * toolbar); overlay = round dark scrim over a photo. */
  variant?: IconButtonVariant;
  /** xs 20px (inside a chip), sm 28px, md 32px (default, the row menu size), lg 36px (a FilterBar row). */
  size?: IconButtonSize;
  /** danger: the hover turns red (remove, discard). */
  tone?: "neutral" | "danger";
  /** round instead of the 8px corner (overlay is always round). */
  round?: boolean;
  /** A toggle's state (aria-pressed). */
  pressed?: boolean;
};

const ICON_PX: Record<IconButtonSize, number> = { xs: 20, sm: 28, md: 32, lg: 36 };

export function IconButton(props: IconButtonProps) {
  const {
    children, label, variant = "outline", size = "md", tone = "neutral", round = false, pressed, title,
    style = {}, className, ...rest
  } = props;
  if (process.env.NODE_ENV !== "production" && !label?.trim()) {
    console.error("IconButton: `label` is required (it is the button's only accessible name).");
  }
  const px = ICON_PX[size];
  const look: Record<IconButtonVariant, CSSProperties> = {
    outline: { background: K.card, color: K.muted, border: `1px solid ${K.line}` },
    plain: { background: "transparent", color: K.muted, border: "1px solid transparent" },
    overlay: { background: "var(--aa-scrim)", color: "var(--aa-on-solid)", border: "1px solid transparent" },
  };
  return (
    <>
      <ButtonStyles />
      <button
        {...domProps({ ...rest, title: title === "" ? undefined : (title ?? label) })}
        aria-label={rest["aria-label"] ?? label}
        aria-pressed={pressed ?? rest["aria-pressed"]}
        className={joinClass(
          BUTTON_CLASS, "aa-kit-icon", `aa-kit-icon--${variant}`, tone === "danger" && "aa-kit-icon--danger", className,
        )}
        style={{
          ...RESET,
          display: "inline-grid",
          placeItems: "center",
          flexShrink: 0,
          width: px,
          height: px,
          padding: 0,
          borderRadius: round || variant === "overlay" ? 999 : size === "xs" ? RADIUS.sm : RADIUS.md,
          cursor: rest.disabled ? "not-allowed" : "pointer",
          transition: "color .15s, background .15s, border-color .15s",
          ...look[variant],
          ...style,
        }}
      >
        {children}
      </button>
    </>
  );
}

// ── ChipButton ─────────────────────────────────────────────────────────────────────────────────────────────────
// One rounded toggle chip: a quick filter ("Waiting for an answer"), one of several categories that can all be on
// (role="checkbox"), a single choice inside a page-made radio group. The state is ARIA: aria-pressed by default,
// aria-checked when the caller gives role="checkbox" / "radio". On = gold (accent border, tint, accent-deep text).
// For one choice out of a few prefer SegmentedControl (it brings the radio group and the arrow keys).

/** The chip look, shared with SegmentedControl appearance="chips". */
function chipStyle(on: boolean, size: "sm" | "md"): CSSProperties {
  return {
    padding: size === "sm" ? "4px 12px" : "6px 16px",
    borderRadius: 999,
    border: `1px solid ${on ? K.accent : K.line}`,
    background: on ? K.accentTint : "transparent",
    color: on ? K.accentDeep : K.muted,
    fontSize: size === "sm" ? 12 : 13,
    fontWeight: on ? 600 : 500,
  };
}

export type ChipButtonProps = BaseButtonProps & {
  children: ReactNode;
  /** On or off. */
  selected: boolean;
  /** sm = 12px (a filter row), md = 13px (default). */
  size?: "sm" | "md";
};

export function ChipButton(props: ChipButtonProps) {
  const { children, selected, size = "md", style = {}, className, ...rest } = props;
  const checkedRole = rest.role === "checkbox" || rest.role === "radio" || rest.role === "menuitemcheckbox"
    || rest.role === "menuitemradio";
  const state = checkedRole ? { "aria-checked": selected } : { "aria-pressed": selected };
  return (
    <>
      <ButtonStyles />
      <button
        {...domProps(rest)}
        {...state}
        className={joinClass(BUTTON_CLASS, "aa-kit-chip", className)}
        data-on={selected ? "" : undefined}
        style={{
          ...RESET,
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          whiteSpace: "nowrap",
          cursor: rest.disabled ? "not-allowed" : "pointer",
          transition: "color .15s, background .15s, border-color .15s",
          ...chipStyle(selected, size),
          ...style,
        }}
      >
        {children}
      </button>
    </>
  );
}

// ── Switch ─────────────────────────────────────────────────────────────────────────────────────────────────────
// An on / off setting that takes effect at once ("Email me when a payment arrives"): role="switch" with aria-checked,
// 44 × 24 track (success green when on, muted2 grey when off: both 3:1+ on card / bg, WCAG 1.4.11), 20px knob in the
// card colour (5:1+ on either track in both themes; lib/tokensContrast.test.mjs checks the pairs). `label` is required (the switch has no visible text of its own;
// put the visible label next to it). For a choice that is saved with a form, use the kit Checkbox.
export type SwitchProps = Omit<BaseButtonProps, "ariaLabel" | "role" | "onClick"> & {
  checked: boolean;
  onChange: (next: boolean) => void;
  /** The setting's name for screen readers. Required. */
  label: string;
};

export function Switch(props: SwitchProps) {
  const { checked, onChange, label, style = {}, className, ...rest } = props;
  return (
    <>
      <ButtonStyles />
      <button
        {...domProps({ ...rest, onClick: () => onChange(!checked) })}
        role="switch"
        aria-checked={checked}
        aria-label={rest["aria-label"] ?? label}
        className={joinClass(BUTTON_CLASS, "aa-kit-switch", className)}
        style={{
          ...RESET,
          position: "relative",
          flexShrink: 0,
          width: 44,
          height: 24,
          padding: 0,
          border: "none",
          borderRadius: 999,
          background: checked ? K.success : K.muted2,
          cursor: rest.disabled ? "not-allowed" : "pointer",
          transition: "background .15s",
          ...style,
        }}
      >
        <span
          aria-hidden="true"
          style={{
            position: "absolute",
            top: 2,
            left: checked ? 22 : 2,
            width: 20,
            height: 20,
            borderRadius: 999,
            background: K.card,
            boxShadow: "0 1px 2px rgba(0,0,0,0.2)",
            transition: "left .15s",
          }}
        />
      </button>
    </>
  );
}

// ── SegmentedControl ───────────────────────────────────────────────────────────────────────────────────────────
// One choice out of a few (2–6), shown all at once: "Existing customer | New lead", "Waiting | Answered | All". A radio
// group (WAI-ARIA radio pattern): role="radiogroup" labelled by `label`, each option role="radio" with aria-checked;
// Tab enters on the chosen option, the arrow keys (and Home / End) move and choose, skipping disabled options.
// appearance "track" = the Tabs look (a grey track, the chosen option raised); "chips" = separate rounded chips, the
// chosen one gold. For switching the view of a page use Tabs; for a filter that is also in the URL, either works.
export type SegmentedOption<V extends string> = {
  value: V;
  label: ReactNode;
  disabled?: boolean;
  title?: string;
  /** A small count after the label ("Answered 3"). */
  count?: ReactNode;
};

export function SegmentedControl<V extends string>({
  label,
  options,
  value,
  onChange,
  appearance = "track",
  size = "md",
  testId,
  style,
  className,
}: {
  /** The group's name for screen readers ("Customer", "Request state"). Required. */
  label: string;
  options: SegmentedOption<V>[];
  /** The chosen value; a value no option has leaves nothing chosen (Tab then enters on the first option). */
  value: V | null;
  onChange: (value: V) => void;
  appearance?: "track" | "chips";
  /** sm = 12px (a dense filter row), md = 13px (default). */
  size?: "sm" | "md";
  testId?: string;
  style?: CSSProperties;
  className?: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const chosen = options.findIndex((o) => o.value === value);
  const firstEnabled = options.findIndex((o) => !o.disabled);
  const tabStop = chosen >= 0 && !options[chosen].disabled ? chosen : firstEnabled;

  function onKey(e: KeyboardEvent<HTMLButtonElement>, from: number) {
    const i = rovingTarget(options, from, e.key);
    if (i === null) return;
    e.preventDefault();
    if (i < 0) return;
    refs.current[i]?.focus();
    if (options[i].value !== value) onChange(options[i].value);
  }

  const chips = appearance === "chips";
  const fz = size === "sm" ? 12 : 13;
  return (
    <>
      <ButtonStyles />
      <div
        role="radiogroup"
        aria-label={label}
        data-testid={testId}
        className={className}
        style={{
          display: "flex",
          fontFamily: sans,
          maxWidth: "100%",
          minWidth: 0,
          ...(chips
            ? { gap: 8, flexWrap: "wrap" }
            : { gap: 4, padding: 4, background: K.line2, borderRadius: RADIUS.md, width: "fit-content", overflowX: "auto" }),
          ...style,
        }}
      >
        {options.map((o, i) => {
          const on = i === chosen;
          const look: CSSProperties = chips
            ? chipStyle(on, size)
            : {
                padding: size === "sm" ? "5px 12px" : "7px 16px",
                borderRadius: 7,
                border: "none",
                background: on ? K.card : "transparent",
                color: on ? K.ink : K.muted,
                fontWeight: on ? 600 : 400,
                boxShadow: on ? "0 1px 3px rgba(0,0,0,0.08)" : "none",
              };
          return (
            <button
              key={o.value}
              ref={(el) => {
                refs.current[i] = el;
              }}
              type="button"
              role="radio"
              aria-checked={on}
              disabled={o.disabled}
              title={o.title}
              tabIndex={i === tabStop ? 0 : -1}
              onClick={() => {
                if (!on) onChange(o.value);
              }}
              onKeyDown={(e) => onKey(e, i)}
              className={joinClass(BUTTON_CLASS, "aa-kit-seg")}
              style={{
                ...RESET,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontSize: fz,
                whiteSpace: "nowrap",
                flexShrink: 0,
                cursor: o.disabled ? "not-allowed" : "pointer",
                transition: "color .15s, background .15s",
                ...look,
              }}
            >
              {o.label}
              {o.count != null && (
                <span style={{ fontSize: fz - 1, fontWeight: 400, color: on ? K.accentDeep : K.muted2 }}>{o.count}</span>
              )}
            </button>
          );
        })}
      </div>
    </>
  );
}

// ── ButtonGroup ────────────────────────────────────────────────────────────────────────────────────────────────
// A labelled group of related buttons (role="group"): the row actions of a table line, a toolbar, "Approve | Reject".
// Spaced by default (8px, wraps on a phone); `attached` joins them edge to edge (one shared border, only the outer
// corners round), the classic button group. For one choice out of several use SegmentedControl instead.
export function ButtonGroup({
  label,
  children,
  attached = false,
  gap = "md",
  testId,
  style,
  className,
}: {
  /** The group's name for screen readers ("Actions for BK-1042"). Required. */
  label: string;
  children: ReactNode;
  attached?: boolean;
  /** sm = 4px (icon buttons in a table row), md = 8px (default). Ignored when attached. */
  gap?: "sm" | "md";
  testId?: string;
  style?: CSSProperties;
  className?: string;
}) {
  return (
    <>
      <ButtonStyles />
      <div
        role="group"
        aria-label={label}
        data-testid={testId}
        data-attached={attached ? "" : undefined}
        className={joinClass("aa-kit-btngroup", className)}
        style={{
          display: "inline-flex",
          alignItems: "center",
          flexWrap: attached ? "nowrap" : "wrap",
          gap: attached ? 0 : gap === "sm" ? 4 : 8,
          maxWidth: "100%",
          ...style,
        }}
      >
        {children}
      </div>
    </>
  );
}

// ── ListButton ─────────────────────────────────────────────────────────────────────────────────────────────────
// A full-width, left-aligned row inside a popup list or menu: a picker result (role="option", `selected` →
// aria-selected), a menu item (role="menuitem"), a "Clear filter" row. Hover = the accent tint, like the kit RowMenu
// items; keyboard focus = the tint plus an inset accent-deep ring; tone danger = red text and a red tint ("Cancel case…"). The popup itself (role="listbox" /
// "menu", placement, Escape) stays with the caller; for a row's action menu use RowMenu.
export type ListButtonProps = BaseButtonProps & {
  children: ReactNode;
  /** role="option": aria-selected (false when not given); also the tinted, semibold look. */
  selected?: boolean;
  tone?: "neutral" | "danger";
  /** compact = 12px, 6px 8px padding (a filter popup); normal = 13px, 8px 12px (default). */
  density?: "compact" | "normal";
};

export function ListButton(props: ListButtonProps) {
  const { children, selected, tone = "neutral", density = "normal", style = {}, className, ...rest } = props;
  // aria-selected belongs to role="option" only (a menu item or a plain row has no selected state).
  const ariaSelected = rest.role === "option" ? { "aria-selected": selected ?? rest["aria-selected"] ?? false } : {};
  return (
    <>
      <ButtonStyles />
      <button
        {...domProps(rest)}
        {...ariaSelected}
        data-tone={tone}
        className={joinClass(BUTTON_CLASS, "aa-kit-listbtn", className)}
        style={{
          ...RESET,
          display: "flex",
          width: "100%",
          alignItems: "center",
          gap: 8,
          padding: density === "compact" ? "6px 8px" : "8px 12px",
          border: "none",
          borderRadius: RADIUS.sm,
          background: selected ? K.accentTint : "transparent",
          color: tone === "danger" ? K.danger : K.ink,
          fontSize: density === "compact" ? 12 : 13,
          fontWeight: selected ? 600 : 400,
          textAlign: "left",
          cursor: rest.disabled ? "not-allowed" : "pointer",
          ...style,
        }}
      >
        {children}
      </button>
    </>
  );
}

// ── Pressable ──────────────────────────────────────────────────────────────────────────────────────────────────
// A clickable surface with no look of its own: the button is only there for the click, the keyboard and the focus
// ring. For a photo tile ("Open photo 3"), a whole card, or a trigger that looks like a form control
// (`style={controlStyle("sm")}`, with aria-haspopup="listbox"). Resets the browser button look (no border, no
// background, inherited font and colour), keeps the kit focus ring. Not for text, icon or brand buttons: those have
// their own component above.
export type PressableProps = BaseButtonProps & {
  children: ReactNode;
  /** block (default) = full width of its box (a tile); inline = shrinks to its content. */
  display?: "block" | "inline";
};

export function Pressable(props: PressableProps) {
  const { children, display = "block", style = {}, className, ...rest } = props;
  return (
    <>
      <ButtonStyles />
      <button
        {...domProps(rest)}
        className={joinClass(BUTTON_CLASS, "aa-kit-pressable", className)}
        style={{
          ...RESET,
          display: display === "block" ? "block" : "inline-flex",
          alignItems: display === "inline" ? "center" : undefined,
          width: display === "block" ? "100%" : undefined,
          padding: 0,
          border: "none",
          background: "transparent",
          color: "inherit",
          font: "inherit",
          textAlign: "inherit",
          cursor: rest.disabled ? "not-allowed" : "pointer",
          ...style,
        }}
      >
        {children}
      </button>
    </>
  );
}
