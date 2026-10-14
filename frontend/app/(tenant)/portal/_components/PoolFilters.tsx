"use client";
import { T, sans } from "./ui";

// AA-796 — the "Best quality first" sort is gone (no A1 quality score on the tenant side); the
// pool is always newest first, so there is no sort control.
export interface PoolFiltersState {
  duration: string;  // "all" | "1-3" | "4-7" | "8+"
}

interface PoolFiltersProps {
  filters: PoolFiltersState;
  onChange: (update: Partial<PoolFiltersState>) => void;
}

const selectStyle = {
  padding: "9px 12px", background: T.card,
  border: `1px solid ${T.line}`, borderRadius: 8,
  color: T.body, fontSize: 13, fontFamily: sans, cursor: "pointer",
} as const;

export function PoolFilters({ filters, onChange }: PoolFiltersProps) {
  return (
    <select value={filters.duration} onChange={e => onChange({ duration: e.target.value })} style={selectStyle}>
      <option value="all">All durations</option>
      <option value="1-3">1–3 days</option>
      <option value="4-7">4–7 days</option>
      <option value="8+">8+ days</option>
    </select>
  );
}
