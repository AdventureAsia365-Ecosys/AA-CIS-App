// app/_kit/chart.ts
// CIS-local chart styling for recharts (moved from the legacy admin/_components/adminUi.tsx, AA-994 S6b). The chart
// colours themselves come from `useChartColors` (app/_kit/useTheme.ts).

/** Recharts `<Tooltip {...CHART_TOOLTIP} />`: a fixed-dark panel in both themes (a tooltip floats over the chart),
 *  on-dark text. */
export const CHART_TOOLTIP = {
  contentStyle: {
    background: "var(--aa-side-bg)", border: "1px solid var(--aa-side-panel)",
    borderRadius: 8, fontSize: 12, color: "var(--aa-on-dark)",
  },
  labelStyle: { color: "var(--aa-on-dark)" },
};
