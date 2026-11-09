"use client";

import React from "react";
import { Search, X } from "lucide-react";
import { useT } from "../../_kit/core/useI18n";
import { M } from "./components.messages";
import { IconButton, Input, K, Select, sans } from "../../_kit";

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
        <Input
          value={search}
          onChange={e => onSearch(e.target.value)}
          placeholder={placeholder || t("search")}
          size="sm"
          style={{ width: "100%", padding: "0 32px 0 30px" }}
        />
        {search && (
          <IconButton
            variant="plain"
            size="xs"
            onClick={() => onSearch("")}
            label={t("clearSearch")}
            title=""
            style={{ position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)", color: K.muted2 }}
          >
            <X size={12} />
          </IconButton>
        )}
      </div>

      {filters?.map(f => (
        <Select
          key={f.label}
          value={f.current}
          onChange={e => f.onChange(e.target.value)}
          size="sm"
          aria-label={f.label}
        >
          <option value="">{f.label}: {f.allLabel ?? t("all")}</option>
          {f.options.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </Select>
      ))}

      {extra && <div style={{ marginLeft: "auto" }}>{extra}</div>}
    </div>
  );
}
