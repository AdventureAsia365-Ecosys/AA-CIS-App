"use client";

import React from "react";
import { Search, X } from "lucide-react";
import { useT } from "../../_kit/core/useI18n";
import { M } from "./components.messages";
import { K, sans } from "../../_kit";

interface FilterBarProps {
  search: string;
  onSearch: (v: string) => void;
  placeholder?: string;
  filters?: FilterOption[];
  extra?: React.ReactNode;
}

interface FilterOption {
  label: string;
  value: string;
  current: string;
  options: { label: string; value: string }[];
  onChange: (v: string) => void;
  /** Text for the default (value="") option — defaults to "All". */
  allLabel?: string;
}

export function FilterBar({ search, onSearch, placeholder, filters, extra }: FilterBarProps) {
  const t = useT(M);
  return (
    <div style={{
      display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap",
      padding: "10px 0", fontFamily: sans,
    }}>
      <div style={{ position: "relative", minWidth: 220, flex: "0 0 auto" }}>
        <Search size={13} style={{
          position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)",
          color: K.muted2, pointerEvents: "none",
        }} />
        <input
          value={search}
          onChange={e => onSearch(e.target.value)}
          placeholder={placeholder || t("search")}
          style={{
            width: "100%", padding: "7px 32px 7px 30px",
            border: `1px solid ${K.line}`, borderRadius: 7,
            background: K.card, color: K.ink, fontSize: 13,
            fontFamily: sans, outline: "none", boxSizing: "border-box",
          }}
        />
        {search && (
          <button
            onClick={() => onSearch("")}
            aria-label={t("clearSearch")}
            style={{
              position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)",
              background: "none", border: "none", cursor: "pointer", color: K.muted2, padding: 0,
            }}
          >
            <X size={12} />
          </button>
        )}
      </div>

      {filters?.map(f => (
        <select
          key={f.label}
          value={f.current}
          onChange={e => f.onChange(e.target.value)}
          style={{
            padding: "7px 10px", border: `1px solid ${K.line}`, borderRadius: 7,
            background: K.card, color: K.ink, fontSize: 13, fontFamily: sans,
            outline: "none", cursor: "pointer",
          }}
        >
          <option value="">{f.label}: {f.allLabel ?? t("all")}</option>
          {f.options.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      ))}

      {extra && <div style={{ marginLeft: "auto" }}>{extra}</div>}
    </div>
  );
}
