"use client";

import React from "react";
import { A, sans } from "./adminUi";
import { ChevronLeft, ChevronRight } from "lucide-react";

interface PaginationProps {
  page: number;
  total: number;
  pageSize: number;
  onPage: (p: number) => void;
}

export function Pagination({ page, total, pageSize, onPage }: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  if (totalPages <= 1) return null;

  const btnStyle = (active: boolean, disabled: boolean): React.CSSProperties => ({
    minWidth: 32, height: 32, padding: "0 8px",
    border: `1px solid ${active ? A.gold : A.line}`,
    borderRadius: 6, background: active ? A.gold : A.card,
    color: active ? "var(--aa-on-solid)" : disabled ? A.muted2 : A.ink,
    cursor: disabled ? "not-allowed" : "pointer",
    fontSize: 12, fontFamily: sans, fontWeight: active ? 600 : 400,
    display: "inline-flex", alignItems: "center", justifyContent: "center",
    opacity: disabled ? 0.5 : 1,
  });

  const pages: (number | "…")[] = [];
  if (totalPages <= 7) {
    for (let i = 1; i <= totalPages; i++) pages.push(i);
  } else {
    pages.push(1);
    if (page > 3) pages.push("…");
    for (let i = Math.max(2, page - 1); i <= Math.min(totalPages - 1, page + 1); i++) pages.push(i);
    if (page < totalPages - 2) pages.push("…");
    pages.push(totalPages);
  }

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4, fontFamily: sans }}>
      <button
        style={btnStyle(false, page === 1)}
        disabled={page === 1}
        onClick={() => onPage(page - 1)}
      >
        <ChevronLeft size={13} />
      </button>
      {pages.map((p, i) =>
        p === "…" ? (
          <span key={`ellipsis-${i}`} style={{ fontSize: 12, color: A.muted2, padding: "0 4px" }}>…</span>
        ) : (
          <button
            key={p}
            style={btnStyle(p === page, false)}
            onClick={() => p !== page && onPage(p as number)}
          >
            {p}
          </button>
        ),
      )}
      <button
        style={btnStyle(false, page === totalPages)}
        disabled={page === totalPages}
        onClick={() => onPage(page + 1)}
      >
        <ChevronRight size={13} />
      </button>
      <span style={{ fontSize: 11, color: A.muted, marginLeft: 6 }}>
        {((page - 1) * pageSize) + 1}–{Math.min(page * pageSize, total)} of {total}
      </span>
    </div>
  );
}
