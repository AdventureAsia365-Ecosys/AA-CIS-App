"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useT } from "../../_kit/core/useI18n";
import { M } from "./components.messages";
import { Button, IconButton, K, sans } from "../../_kit";

interface PaginationProps {
  page: number;
  total: number;
  pageSize: number;
  onPage: (p: number) => void;
}

export function Pagination({ page, total, pageSize, onPage }: PaginationProps) {
  const t = useT(M);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  if (totalPages <= 1) return null;

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
      <IconButton
        size="md"
        disabled={page === 1}
        label={t("previousPage")}
        title=""
        onClick={() => onPage(page - 1)}
      >
        <ChevronLeft size={13} />
      </IconButton>
      {pages.map((p, i) =>
        p === "…" ? (
          <span key={`ellipsis-${i}`} style={{ fontSize: 12, color: K.muted2, padding: "0 4px" }}>…</span>
        ) : (
          <Button
            key={p}
            size="sm"
            variant={p === page ? "primary" : "ghost"}
            aria-current={p === page ? "page" : undefined}
            style={{ minWidth: 32, height: 32, padding: "0 8px" }}
            onClick={() => p !== page && onPage(p as number)}
          >
            {p}
          </Button>
        ),
      )}
      <IconButton
        size="md"
        disabled={page === totalPages}
        label={t("nextPage")}
        title=""
        onClick={() => onPage(page + 1)}
      >
        <ChevronRight size={13} />
      </IconButton>
      <span style={{ fontSize: 11, color: K.muted, marginLeft: 6 }}>
        {t("rangeOf", ((page - 1) * pageSize) + 1, Math.min(page * pageSize, total), total)}
      </span>
    </div>
  );
}
