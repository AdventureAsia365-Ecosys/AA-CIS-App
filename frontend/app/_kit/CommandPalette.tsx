"use client";
// app/_kit/CommandPalette.tsx
// AA-752 — the ⌘K / Ctrl K global command palette. Opened by the keyboard shortcut, by the Topbar
// search pill, or by the phone search icon. A debounced (~200ms) react-query fetch hits
// /admin/search; results are grouped Pages (client-side match over the nav items) · Tours ·
// Tenants · Jobs. ↑ ↓ move the selection, Enter opens it, Esc closes; focus is trapped in the
// input and the dialog closes on navigate.
//
// Deep-link targets:
//   tour   → /admin/master-content?tour=<tour_id>   (opens TourDetailPanelV2)
//   tenant → /admin/tenants?tenant=<tenant_id>       (expands the tenant's inline 360 panel)
//   job    → /admin/jobs?job=<id>                    (already supported)
//
// No external dependency (no cmdk): built on a small modal + the kit tokens. The debounce lives in
// a setTimeout inside an effect (the setState runs in the async callback, not synchronously in the
// effect body — React-Compiler safe).

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Search, FileText, Users, ListChecks, CornerDownLeft } from "lucide-react";
import { K, RADIUS, sans, serif, alpha } from "./tokens";
import { allEntries, type NavEntry } from "./adminNav";
import { useAdminIdentity } from "./useAdminIdentity";
import { apiGet } from "./api";

interface TourHit { tour_id: string; name: string; src_name: string; country: string | null; master: boolean; trashed: boolean; }
interface TenantHit { tenant_id: string; name: string; slug: string; is_active: boolean; }
interface JobHit { id: string; kind: string; status: string; created_at: string | null; }
interface SearchResult { tours: TourHit[]; tenants: TenantHit[]; jobs: JobHit[]; }

type Row =
  | { kind: "page"; label: string; href: string; icon: React.ReactNode }
  | { kind: "tour"; label: string; sub: string; href: string }
  | { kind: "tenant"; label: string; sub: string; href: string }
  | { kind: "job"; label: string; sub: string; href: string };

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  // Returning null when closed unmounts the dialog, so each open starts with fresh state — no
  // reset-on-open setState-in-effect needed (React-Compiler safe).
  if (!open) return null;
  return <PaletteDialog onClose={onClose} />;
}

function PaletteDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const { isAdmin } = useAdminIdentity();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Focus the input on mount (DOM call, not setState — safe).
  useEffect(() => {
    const id = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, []);

  // Debounce the query (~200ms). setState runs in the timer callback, not synchronously.
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(query.trim()), 200);
    return () => window.clearTimeout(id);
  }, [query]);

  const enabled = debounced.length >= 2;
  const { data } = useQuery({
    queryKey: ["command-palette", debounced],
    queryFn: () => apiGet<SearchResult>(`/api/admin/search?q=${encodeURIComponent(debounced)}`, { admin: true }),
    enabled,
    staleTime: 30_000,
    retry: false,
  });

  // Build the flat, grouped row list. Pages are a client-side match over the nav entries.
  const groups = useMemo(() => {
    const q = debounced.toLowerCase();
    const pageEntries: NavEntry[] = q
      ? allEntries(isAdmin).filter((e) => e.label.toLowerCase().includes(q))
      : [];
    const pages: Row[] = pageEntries.map((e) => ({
      kind: "page" as const, label: e.label, href: e.href, icon: e.icon,
    }));
    const tours: Row[] = (data?.tours ?? []).map((t) => ({
      kind: "tour" as const,
      label: t.name || t.src_name,
      sub: [t.country, t.trashed ? "trashed" : t.master ? "master" : "raw"].filter(Boolean).join(" · "),
      href: `/admin/master-content?tour=${encodeURIComponent(t.tour_id)}`,
    }));
    const tenants: Row[] = (data?.tenants ?? []).map((t) => ({
      kind: "tenant" as const,
      label: t.name,
      sub: [t.slug, t.is_active ? "active" : "inactive"].filter(Boolean).join(" · "),
      href: `/admin/tenants?tenant=${encodeURIComponent(t.tenant_id)}`,
    }));
    const jobs: Row[] = (data?.jobs ?? []).map((j) => ({
      kind: "job" as const,
      label: j.kind,
      sub: [j.id.slice(0, 8), j.status].filter(Boolean).join(" · "),
      href: `/admin/jobs?job=${encodeURIComponent(j.id)}`,
    }));
    return [
      { title: "Pages", rows: pages },
      { title: "Tours", rows: tours },
      { title: "Tenants", rows: tenants },
      { title: "Jobs", rows: jobs },
    ].filter((g) => g.rows.length > 0);
  }, [data, debounced, isAdmin]);

  const flat = useMemo(() => groups.flatMap((g) => g.rows), [groups]);

  // Keep the selection in range when the result set changes (clamp in render, not an effect).
  const safeSelected = flat.length === 0 ? 0 : Math.min(selected, flat.length - 1);

  function activate(row: Row | undefined) {
    if (!row) return;
    onClose();
    router.push(row.href);
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); setSelected((s) => Math.min(s + 1, Math.max(0, flat.length - 1))); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setSelected((s) => Math.max(s - 1, 0)); return; }
      if (e.key === "Enter") { e.preventDefault(); activate(flat[safeSelected]); return; }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flat, safeSelected]);

  let runningIndex = -1;

  return (
    <div
      onClick={onClose}
      role="presentation"
      style={{
        position: "fixed", inset: 0, zIndex: 11000,
        background: "rgba(31,41,51,0.45)",
        display: "flex", alignItems: "flex-start", justifyContent: "center",
        padding: "12vh 16px 16px", fontFamily: sans,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Global search"
        style={{
          width: 560, maxWidth: "100%", maxHeight: "70vh", display: "flex", flexDirection: "column",
          background: K.card, borderRadius: RADIUS.lg, border: `1px solid ${K.line}`,
          boxShadow: "0 24px 70px rgba(0,0,0,0.3)", overflow: "hidden",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 16px", borderBottom: `1px solid ${K.line}` }}>
          <Search size={18} style={{ color: K.muted2, flexShrink: 0 }} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSelected(0); }}
            placeholder="Search tours, tenants, jobs…"
            aria-label="Search tours, tenants, jobs"
            style={{
              flex: 1, border: "none", outline: "none", background: "transparent",
              fontSize: 15, color: K.ink, fontFamily: sans,
            }}
          />
          <kbd style={{
            fontSize: 11, color: K.muted, border: `1px solid ${K.line}`, borderRadius: 6,
            padding: "2px 6px", background: K.bg,
          }}>Esc</kbd>
        </div>

        <div style={{ overflowY: "auto", padding: 6 }}>
          {debounced.length < 2 ? (
            <div style={{ padding: "22px 14px", fontSize: 13, color: K.muted, textAlign: "center" }}>
              Type at least 2 characters to search.
            </div>
          ) : flat.length === 0 ? (
            <div style={{ padding: "22px 14px", fontSize: 13, color: K.muted, textAlign: "center" }}>
              No results for “{debounced}”.
            </div>
          ) : (
            groups.map((g) => (
              <div key={g.title} style={{ marginBottom: 4 }}>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted2, padding: "8px 10px 4px" }}>
                  {g.title}
                </div>
                {g.rows.map((row) => {
                  runningIndex += 1;
                  const idx = runningIndex;
                  const isSel = idx === safeSelected;
                  return (
                    <button
                      key={`${row.kind}-${row.href}`}
                      onClick={() => activate(row)}
                      onMouseEnter={() => setSelected(idx)}
                      style={{
                        display: "flex", alignItems: "center", gap: 10, width: "100%",
                        padding: "9px 10px", borderRadius: RADIUS.md, border: "none",
                        background: isSel ? alpha(K.accent, 12) : "transparent",
                        color: K.ink, cursor: "pointer", textAlign: "left", fontFamily: sans,
                      }}
                    >
                      <span style={{ color: K.muted2, display: "flex", flexShrink: 0 }}>
                        {row.kind === "page" ? row.icon
                          : row.kind === "tour" ? <FileText size={16} />
                          : row.kind === "tenant" ? <Users size={16} />
                          : <ListChecks size={16} />}
                      </span>
                      <span style={{ flex: 1, minWidth: 0 }}>
                        <span style={{ display: "block", fontSize: 13.5, fontWeight: 500, color: K.ink, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {row.label}
                        </span>
                        {"sub" in row && row.sub && (
                          <span style={{ display: "block", fontSize: 11.5, color: K.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {row.sub}
                          </span>
                        )}
                      </span>
                      {isSel && <CornerDownLeft size={14} style={{ color: K.muted2, flexShrink: 0 }} />}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>

        <div style={{ display: "flex", gap: 14, padding: "8px 14px", borderTop: `1px solid ${K.line}`, fontSize: 11, color: K.muted2 }}>
          <span style={{ fontFamily: serif }}>↑ ↓ navigate</span>
          <span>↵ open</span>
          <span>esc close</span>
        </div>
      </div>
    </div>
  );
}
