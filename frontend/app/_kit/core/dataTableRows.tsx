// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/dataTableRows.tsx
// AA-1131 (S283 UI audit, batch K1) — the keyboard half of a DataTable row that opens something (`onRowClick`): Tab
// reaches the row (tabIndex 0), Enter or Space on the row itself opens it (a key on a button, link or field inside the
// row stays that control's), and the row shows the kit focus ring on :focus-visible (DATATABLE_CSS, hoisted once).
// A table row keeps its row role (no role="button": that would hide the table semantics, and a row with controls in it
// would be a nested interactive element). Same props on the phone card.
import type { KeyboardEvent } from "react";
import { K } from "./tokens";

/** True when a key on a clickable row should open it: Enter or Space pressed on the row element itself (the target is
 *  the row), so a key on a control inside the row (a checkbox, a link, a row menu) stays that control's. */
export function rowKeyOpens(key: string, targetIsRow: boolean): boolean {
  return targetIsRow && (key === "Enter" || key === " " || key === "Spacebar");
}

// The focus ring of a clickable row: inset (a table row has no room outside, the scroll box would clip it), accent-deep
// like every kit focus ring (3:1 on card, lib/tokensContrast.test.mjs).
export const DATATABLE_CSS =
  `[data-kit-row-open]:focus-visible{outline:2px solid ${K.accentDeep};outline-offset:-2px}`;

export function DataTableStyles() {
  return <style href="aa-kit-datatable" precedence="default">{DATATABLE_CSS}</style>;
}

/** The props of one row: none without `onRowClick`; with it, the click, the keys, the tab stop and the ring hook. */
export function rowOpenProps<T>(onRowClick: ((row: T) => void) | undefined, original: T) {
  if (!onRowClick) return {};
  return {
    tabIndex: 0,
    "data-kit-row-open": "",
    onClick: () => onRowClick(original),
    onKeyDown: (e: KeyboardEvent<HTMLElement>) => {
      if (!rowKeyOpens(e.key, e.target === e.currentTarget)) return;
      e.preventDefault();
      onRowClick(original);
    },
  };
}
