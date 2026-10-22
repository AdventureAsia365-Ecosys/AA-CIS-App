"use client";
// portal/app/portal/_shell/PortalCommandPalette.tsx
// AA-759 — the real ⌘K / Ctrl K palette (was: page jump + "search Browse Tours for …"). Built on the
// kit Modal. Groups:
//   - Pages      — client match over the nav model (label + keywords), shown before any typing;
//   - Tours      — GET /v1/tours/pool?search=<q>&page_size=5 (debounced ~200 ms) → Browse Tours
//                  filtered to that tour; plus a "Search all tours for …" row;
//   - My tours   — client match over the cached ["my-versions"] query (same key as My Tours) →
//                  /portal/tours/mine?tour_id=<tour_id>;
//   - My content — client match over the cached ["content-reviews"] query (same key as My Content)
//                  → /portal/content?piece=<piece_id> (opens that piece).
// The two cached lists load only once the palette has ≥ 2 characters (no request on open).
// ↑ ↓ move, Enter opens, Esc closes (kit Modal); a fresh mount per open (no reset effect).

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, CornerDownLeft, FileText, Globe2, Search } from "lucide-react";
import { K, Modal, RADIUS, alpha, apiGet, sans } from "@/app/_kit";
import { fetchMyVersions } from "../_components/catalogApi";
import { fetchReviews } from "../_components/reviewListApi";
import { channelName } from "../_components/channelLabels";
import { ROUTES, allEntries } from "./portalNav";
import { MY_VERSIONS_KEY } from "./useShellData";

interface PoolHit { id: string; aa_name: string; country: string | null; duration: string | null }

type Row =
  | { kind: "page"; key: string; label: string; sub?: string; icon: React.ReactNode; href: string }
  | { kind: "search"; key: string; label: string; sub?: string; icon: React.ReactNode; q: string }
  | { kind: "link"; key: string; label: string; sub?: string; icon: React.ReactNode; href: string };

export function PortalCommandPalette({ open, onClose, onSearchTours }: {
  open: boolean; onClose: () => void; onSearchTours: (q: string) => void;
}) {
  if (!open) return null;
  return <Palette onClose={onClose} onSearchTours={onSearchTours} />;
}

function Palette({ onClose, onSearchTours }: { onClose: () => void; onSearchTours: (q: string) => void }) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const id = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, []);

  // Debounce (~200 ms); the setState runs in the timer callback, not in the effect body.
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(query.trim()), 200);
    return () => window.clearTimeout(id);
  }, [query]);

  const enabled = debounced.length >= 2;
  const needle = debounced.toLowerCase();

  const { data: pool } = useQuery({
    queryKey: ["palette-pool", debounced],
    queryFn: () => apiGet<{ data?: PoolHit[] }>(
      `/api/tenant/v1/tours/pool?page_size=5&search=${encodeURIComponent(debounced)}`,
    ),
    enabled,
    staleTime: 30_000,
    retry: false,
  });
  const { data: versions } = useQuery({ queryKey: MY_VERSIONS_KEY, queryFn: fetchMyVersions, enabled });
  const { data: reviews } = useQuery({ queryKey: ["content-reviews"], queryFn: fetchReviews, enabled });

  const groups = useMemo(() => {
    const pages: Row[] = allEntries()
      .filter((e) => !needle || e.label.toLowerCase().includes(needle) || (e.keywords ?? "").includes(needle))
      .map((e) => ({ kind: "page", key: `page:${e.href}`, label: e.label, icon: e.icon, href: e.href }));

    if (!enabled) return [{ title: "Pages", rows: pages }];

    const tours: Row[] = (pool?.data ?? []).map((t) => ({
      kind: "search", key: `pool:${t.id}`, label: t.aa_name,
      sub: [t.country, t.duration].filter(Boolean).join(" · ") || "Browse Tours",
      icon: <Globe2 size={16} />, q: t.aa_name,
    }));
    tours.push({
      kind: "search", key: "pool:all", label: `Search all tours for “${debounced}”`, sub: "Browse Tours",
      icon: <Search size={16} />, q: debounced,
    });

    const seen = new Set<string>();
    const mine: Row[] = [];
    for (const v of versions ?? []) {
      if (!v.published_tour_id || seen.has(v.published_tour_id)) continue;
      if (!(v.aa_name ?? "").toLowerCase().includes(needle)) continue;
      seen.add(v.published_tour_id);
      mine.push({
        kind: "link", key: `mine:${v.published_tour_id}`, label: v.aa_name || "Tour",
        sub: [v.country, "My Tours"].filter(Boolean).join(" · "), icon: <BookOpen size={16} />,
        href: v.tour_id ? `${ROUTES.myTours}?tour_id=${encodeURIComponent(v.tour_id)}` : ROUTES.myTours,
      });
      if (mine.length >= 5) break;
    }

    const content: Row[] = (reviews ?? [])
      .filter((r) => [r.angle?.name, r.tour?.name, r.content_text?.slice(0, 400)]
        .some((s) => (s ?? "").toLowerCase().includes(needle)))
      .slice(0, 5)
      .map((r) => ({
        kind: "link", key: `piece:${r.piece_id}`, label: r.angle?.name || r.tour?.name || "Untitled piece",
        sub: [channelName(r.channel), r.tour?.name].filter(Boolean).join(" · "), icon: <FileText size={16} />,
        href: `${ROUTES.content}?piece=${encodeURIComponent(r.piece_id)}`,
      }));

    return [
      { title: "Pages", rows: pages },
      { title: "My tours", rows: mine },
      { title: "My content", rows: content },
      { title: "Tours", rows: tours },
    ].filter((g) => g.rows.length > 0);
  }, [needle, enabled, debounced, pool, versions, reviews]);

  const flat = useMemo(() => groups.flatMap((g) => g.rows), [groups]);
  const safeSelected = flat.length === 0 ? 0 : Math.min(selected, flat.length - 1);

  function activate(row: Row | undefined) {
    if (!row) return;
    onClose();
    if (row.kind === "search") onSearchTours(row.q);
    else router.push(row.href);
  }

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") { e.preventDefault(); setSelected((s) => Math.min(s + 1, Math.max(0, flat.length - 1))); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSelected((s) => Math.max(s - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); activate(flat[safeSelected]); }
  }

  let runningIndex = -1;

  return (
    <Modal open onClose={onClose} width={580}>
      <div style={{ margin: "-18px -20px", fontFamily: sans }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 16px", borderBottom: `1px solid ${K.line}` }}>
          <Search size={18} style={{ color: K.muted2, flexShrink: 0 }} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSelected(0); }}
            onKeyDown={onKey}
            placeholder="Search tours, your content, or jump to a page…"
            aria-label="Search tours, your content, or jump to a page"
            aria-controls="portal-palette-results"
            style={{ flex: 1, minWidth: 0, border: "none", outline: "none", background: "transparent", fontSize: 15, color: K.ink, fontFamily: sans }}
          />
          <kbd style={{ fontSize: 11, color: K.muted, border: `1px solid ${K.line}`, borderRadius: 6, padding: "2px 6px", background: K.bg, fontFamily: sans }}>Esc</kbd>
        </div>

        <div id="portal-palette-results" role="listbox" aria-label="Results" style={{ maxHeight: "min(440px, 60vh)", overflowY: "auto", padding: 6 }}>
          {flat.length === 0 ? (
            <div style={{ padding: "22px 14px", fontSize: 13, color: K.muted, textAlign: "center" }}>
              No results for “{debounced}”.
            </div>
          ) : groups.map((g) => (
            <div key={g.title} role="group" aria-label={g.title} style={{ marginBottom: 4 }}>
              <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted2, padding: "8px 10px 4px" }}>
                {g.title}
              </div>
              {g.rows.map((row) => {
                runningIndex += 1;
                const idx = runningIndex;
                const isSel = idx === safeSelected;
                return (
                  <button
                    key={row.key}
                    role="option"
                    aria-selected={isSel}
                    onClick={() => activate(row)}
                    onMouseEnter={() => setSelected(idx)}
                    style={{
                      display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "9px 10px",
                      borderRadius: RADIUS.md, border: "none", cursor: "pointer", textAlign: "left", fontFamily: sans,
                      background: isSel ? alpha(K.accent, 12) : "transparent", color: K.ink,
                    }}
                  >
                    <span aria-hidden style={{ color: isSel ? K.accentDeep : K.muted2, display: "flex", flexShrink: 0 }}>{row.icon}</span>
                    <span style={{ flex: 1, minWidth: 0 }}>
                      <span style={{ display: "block", fontSize: 13.5, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{row.label}</span>
                      {row.sub && (
                        <span style={{ display: "block", fontSize: 11.5, color: K.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{row.sub}</span>
                      )}
                    </span>
                    {isSel && <CornerDownLeft size={14} aria-hidden style={{ color: K.muted2, flexShrink: 0 }} />}
                  </button>
                );
              })}
            </div>
          ))}
          {enabled && !pool && (
            <div role="status" style={{ padding: "6px 10px 10px", fontSize: 11.5, color: K.muted2 }}>Searching tours…</div>
          )}
        </div>

        <div style={{ display: "flex", gap: 14, padding: "8px 14px", borderTop: `1px solid ${K.line}`, fontSize: 11, color: K.muted2 }}>
          <span>↑ ↓ navigate</span>
          <span>↵ open</span>
          <span>esc close</span>
        </div>
      </div>
    </Modal>
  );
}
