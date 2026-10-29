// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/FilterBar.tsx
// AA-994 S4a — list filters, upstreamed from AA-Booking `ui/booking/FilterBar.tsx` (inline styles instead of Tailwind):
// a search box, selects, a date range and "Reset filters". State lives in the URL query (`useUrlFilters`), so a
// filtered list can be bookmarked and shared; changing a filter drops the `page` key.
//
// One fixed shape on every list. Row 1: the search box, then the `tools` slot at the right. Row 2: every filter with
// its label inline ("Status [All statuses]"), all 36px high, and "Reset filters" at the right. "Reset filters" is
// always drawn (disabled while every filter is at its default), so the bar never changes width when a filter is set.
//
// `layout="toolbar"`: the bar sits inside a DataTable toolbar (`toolbarExtra`); its root is then `display: contents`,
// so row 1 shares the toolbar line with the table's own buttons and row 2 (`order: 2`, full width) drops below them.
// The toolbar must be a wrapping flex row for that (the DataTable toolbar is).
//
// next/navigation's useSearchParams: a statically rendered page wraps the bar in <Suspense>.

import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Search } from "lucide-react";
import { DateField } from "./DateField";
import { K, RADIUS, sans } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

export interface FilterSelect {
  key: string;
  label: string;
  /** Label of the empty value (no filter). */
  allLabel: string;
  options: { value: string; label: string }[];
  /** Fixed width in px. Set it when the options load after the page (staff names, countries, facets): a native select
   *  sizes itself to its longest option, so it would grow when they arrive and shift the bar and the list below it. */
  width?: number;
}

/** Read / write list filters in the URL query. `set` / `setMany` remove empty values and the `page` key. */
export function useUrlFilters() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const get = (key: string) => params.get(key) ?? "";
  function setMany(changes: Record<string, string | null>) {
    const next = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (!("page" in changes)) next.delete("page");
    const qs = next.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }
  return { get, set: (key: string, value: string | null) => setMany({ [key]: value }), setMany, params };
}

/** The look of every filter control (36px high); give page-specific controls in `children` the same style and the
 *  `FILTER_CONTROL_CLASS` class (focus ring). */
export const filterControlStyle: CSSProperties = {
  height: 36,
  boxSizing: "border-box",
  borderRadius: RADIUS.md,
  border: `1px solid ${K.line}`,
  background: K.card,
  padding: "0 12px",
  fontSize: 13,
  fontFamily: sans,
  color: K.ink,
  outline: "none",
};
export const FILTER_CONTROL_CLASS = "aa-kit-filter-control";

const FILTER_CSS =
  `.${FILTER_CONTROL_CLASS}:focus{border-color:${K.accentBorder} !important}` +
  `.${FILTER_CONTROL_CLASS}[aria-invalid="true"]{border-color:${K.danger} !important}` +
  `.aa-kit-filter-reset{color:${K.danger}}` +
  `.aa-kit-filter-reset:disabled{color:${K.muted2}}` +
  `.aa-kit-filter-reset:not(:disabled):hover{text-decoration:underline}`;

const SR_ONLY: CSSProperties = {
  position: "absolute", width: 1, height: 1, padding: 0, margin: -1, overflow: "hidden",
  clip: "rect(0, 0, 0, 0)", whiteSpace: "nowrap", border: 0,
};

/** An inline label + control pair, the one shape of every filter (use it for page-specific controls too). */
export function FilterField({ label, children, as = "label" }: { label: string; children: ReactNode; as?: "label" | "div" }) {
  const Tag = as;
  return (
    <Tag
      data-kit-filter=""
      style={{ display: "flex", minWidth: 0, maxWidth: "100%", alignItems: "center", gap: 6, fontFamily: sans }}
    >
      <span style={{ flexShrink: 0, whiteSpace: "nowrap", fontSize: 12, fontWeight: 500, color: K.muted }}>{label}</span>
      {children}
    </Tag>
  );
}

export function FilterBar({
  search,
  selects = [],
  dateRange,
  extraKeys = [],
  tools,
  children,
  layout = "block",
  testId = "kit-filterbar",
}: {
  search?: { key?: string; placeholder: string };
  selects?: FilterSelect[];
  dateRange?: { fromKey: string; toKey: string; label: string };
  /** URL keys owned by `children` controls: they count as active filters and "Reset filters" clears them. */
  extraKeys?: string[];
  /** Tools at the right of row 1, after the search box (e.g. Export CSV on a list without a DataTable toolbar). */
  tools?: ReactNode;
  /** Page-specific controls in row 2 (wrap each in FilterField). */
  children?: ReactNode;
  /** "toolbar": inside a DataTable toolbar (root `display: contents`). */
  layout?: "block" | "toolbar";
  /** Root test id; the reset button is `<testId>-reset`. */
  testId?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const { get, set, setMany } = useUrlFilters();
  const searchKey = search?.key ?? "q";
  const urlQ = get(searchKey);
  const [q, setQ] = useState(urlQ);

  // Debounce the search box into the URL (~300 ms). setState only in the timer callback.
  useEffect(() => {
    if (!search) return;
    if (q === urlQ) return;
    const id = window.setTimeout(() => set(searchKey, q.trim() || null), 300);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const keys = [
    ...(search ? [searchKey] : []),
    ...selects.map((s) => s.key),
    ...(dateRange ? [dateRange.fromKey, dateRange.toKey] : []),
    ...extraKeys,
  ];
  const active = keys.some((k) => get(k)) || q.trim() !== "";
  const hasRow2 = selects.length > 0 || !!dateRange || !!children || keys.length > 0;

  return (
    <div
      className="aa-kit-filterbar"
      role="search"
      data-testid={testId}
      style={
        layout === "toolbar"
          ? { display: "contents" }
          : { display: "flex", flexWrap: "wrap", alignItems: "center", columnGap: 12, rowGap: 12, minWidth: 0,
            fontFamily: sans }
      }
    >
      <style href="aa-kit-filterbar" precedence="default">{FILTER_CSS}</style>
      {search && (
        <label
          data-kit-filter-row="1"
          style={{ position: "relative", minWidth: 200, maxWidth: 420, flex: "1 1 240px" }}
        >
          <span style={SR_ONLY}>{search.placeholder}</span>
          <Search
            size={15}
            aria-hidden
            style={{
              position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", color: K.muted2,
              pointerEvents: "none",
            }}
          />
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={search.placeholder}
            className={FILTER_CONTROL_CLASS}
            style={{ ...filterControlStyle, width: "100%", paddingLeft: 36 }}
          />
        </label>
      )}
      {tools && (
        <span data-kit-filter-row="1" style={{ order: 1, marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
          {tools}
        </span>
      )}
      {hasRow2 && (
        <div
          data-kit-filter-row="2"
          style={{
            order: 2, display: "flex", minWidth: 0, flexBasis: "100%", flexWrap: "wrap", alignItems: "center",
            columnGap: 10, rowGap: 8, fontFamily: sans,
          }}
        >
          {selects.map((s) => (
            <FilterField key={s.key} label={s.label}>
              <select
                value={get(s.key)}
                onChange={(e) => set(s.key, e.target.value || null)}
                className={FILTER_CONTROL_CLASS}
                style={{ ...filterControlStyle, minWidth: 0, maxWidth: "100%", ...(s.width ? { width: s.width } : {}) }}
              >
                <option value="">{s.allLabel}</option>
                {s.options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </FilterField>
          ))}
          {dateRange && (
            <FilterField label={dateRange.label} as="div">
              <span style={{ display: "flex", minWidth: 0, alignItems: "center", gap: 4 }}>
                <DateField
                  aria-label={t("rangeFrom", dateRange.label)}
                  placeholder={t("dateFrom")}
                  value={get(dateRange.fromKey)}
                  max={get(dateRange.toKey) || undefined}
                  onChange={(v) => set(dateRange.fromKey, v || null)}
                  className={FILTER_CONTROL_CLASS}
                  style={{ ...filterControlStyle, width: 132 }}
                />
                <span aria-hidden style={{ color: K.muted2 }}>–</span>
                <DateField
                  aria-label={t("rangeTo", dateRange.label)}
                  placeholder={t("dateTo")}
                  value={get(dateRange.toKey)}
                  min={get(dateRange.fromKey) || undefined}
                  onChange={(v) => set(dateRange.toKey, v || null)}
                  className={FILTER_CONTROL_CLASS}
                  style={{ ...filterControlStyle, width: 132 }}
                />
              </span>
            </FilterField>
          )}
          {children}
          {keys.length > 0 && (
            <button
              type="button"
              disabled={!active}
              data-testid={`${testId}-reset`}
              onClick={() => {
                setQ("");
                setMany(Object.fromEntries(keys.map((k) => [k, null])));
              }}
              className="aa-kit-filter-reset"
              style={{
                marginLeft: "auto", display: "inline-flex", height: 36, alignItems: "center", gap: 4,
                whiteSpace: "nowrap", borderRadius: RADIUS.md, padding: "0 4px", border: "none",
                background: "transparent", fontSize: 13, fontWeight: 500, fontFamily: sans,
                cursor: active ? "pointer" : "default",
              }}
            >
              {t("resetFilters")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
