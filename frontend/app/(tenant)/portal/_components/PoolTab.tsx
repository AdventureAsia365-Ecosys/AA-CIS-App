"use client";
// app/(tenant)/portal/_components/PoolTab.tsx
// API: GET /api/tenant/v1/tours/pool?page=N&page_size=20&search=X&country=Y
//      POST /api/tenant/v1/tours/pool/{id}/rewrite

import { useState, useEffect, useCallback } from "react";
import { ListSkeleton } from "./Skeleton";
import { Search, ChevronRight, X, RotateCcw, Globe2, MapPin, Clock, Wallet, PenLine } from "lucide-react";
import {
  T, serif, mono, sans,
  Card, Badge, Btn, LoadingScreen, EmptyState,
  parseHighlights, fmtDate, statusVariant,
} from "./ui";
import { PoolFilters, type PoolFiltersState } from "./PoolFilters";

// AA-566 Phần D — the Rewrite Config tab (language/SEO mode/brand rules) is removed per
// Nghiệp's decision (b): the external bulk "Rewrite N" bar already covers triggering a rewrite,
// duplicating that inside a per-tour tab added nothing. These 3 knobs had no other UI anywhere
// in the tenant portal to move into, so they're hardcoded here at RewritePanel's own prior
// defaults (unchanged behavior for the common case) — WHERE a tenant should be able to change
// them again, if ever, is an open product question (a Settings/Brand page?), not decided here.
const REWRITE_LANGUAGE = "en-US";
const REWRITE_SEO_MODE = "standard";
const REWRITE_USE_BRAND_RULES = true;

interface PoolTour {
  id: string; tour_id: string; aa_name: string; aa_subtitle: string;
  aa_summary: string; aa_highlights: string; aa_itineraries: string | null;
  seo_title: string; seo_meta: string; seo_keywords_used: string;
  quality_score: number; published_at: string;
  country: string | null; duration: string | null; price_raw: string | null;
  already_rewritten: boolean;
}

const PAGE_SIZE = 20;

export default function PoolTab({ onRewriteDone, externalSearch = "" }: { onRewriteDone: () => void; externalSearch?: string }) {
  const [tours, setTours]     = useState<PoolTour[]>([]);
  const [total, setTotal]     = useState(0);
  const [countries, setCountries] = useState<string[]>([]);
  const [page, setPage]       = useState(1);
  const [search, setSearch]   = useState("");

  // Sync external search (from topbar)
  useEffect(() => { if (externalSearch !== undefined) { setSearch(externalSearch); setPage(1); } }, [externalSearch]);
  const [country, setCountry] = useState("");
  const [poolFilters, setPoolFilters] = useState<PoolFiltersState>({ duration: "all", sort: "newest" });
  const [inCatalogSet, setInCatalogSet] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<PoolTour | null>(null);
  const [checked, setChecked]   = useState<Set<string>>(new Set());
  const [expandItin, setExpandItin] = useState(false);
  const [rewrit, setRewrit]   = useState(false);

  const fetchPool = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
      if (search)  params.set("search", search);
      if (country) params.set("country", country);
      params.set("sort", poolFilters.sort);
      if (poolFilters.duration !== "all") {
        if (poolFilters.duration === "8+") {
          params.set("duration_min", "8");
        } else {
          const [min, max] = poolFilters.duration.split("-");
          params.set("duration_min", min);
          params.set("duration_max", max);
        }
      }

      const [poolRes, versionsRes] = await Promise.allSettled([
        fetch(`/api/tenant/v1/tours/pool?${params}`),
        fetch("/api/tenant/v1/tours/my-versions?page_size=200"),
      ]);

      if (poolRes.status === "fulfilled" && poolRes.value.ok) {
        const d = await poolRes.value.json();
        setTours(d.data ?? []);
        setTotal(d.pagination?.total ?? 0);
        if (d.countries?.length) setCountries(d.countries);
      }

      if (versionsRes.status === "fulfilled" && versionsRes.value.ok) {
        const vd = await versionsRes.value.json();
        const versions: { published_tour_id?: string; status: string; edit_source: string }[] = vd.data ?? [];
        // AA-565 — My Catalog no longer has a manual "Add to Catalog" approval step, so
        // status never reaches 'approved' anymore. "In My Catalog" now means "has a finished
        // rewrite" — any version that isn't still being AI-written. (Kept edit_source==
        // 'tenant_edit' rows in this set too: those never leave 'pending' by design, see
        // CatalogTab.tsx's isAiWriting() comment, but they ARE finished content.)
        setInCatalogSet(new Set(
          versions
            .filter(v => v.published_tour_id && !(v.status === "pending" && v.edit_source === "ai_generated"))
            .map(v => v.published_tour_id as string)
        ));
      }
    } finally { setLoading(false); }
  }, [page, search, country, poolFilters]);

  useEffect(() => { fetchPool(); }, [fetchPool]);

  async function doRewrite(ids: string[]) {
    setRewrit(true);
    try {
      for (const id of ids) {
        await fetch(`/api/tenant/v1/tours/pool/${id}/rewrite`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rewrite_language: REWRITE_LANGUAGE, seo_mode: REWRITE_SEO_MODE, use_brand_rules: REWRITE_USE_BRAND_RULES }),
        });
      }
      setSelected(null);
      setChecked(new Set());
      onRewriteDone();
    } finally { setRewrit(false); }
  }

  const rewriteTargets = checked.size > 0 ? Array.from(checked) : selected ? [selected.id] : [];

  return (
    <div style={{ display: "grid", gridTemplateColumns: selected ? "minmax(0,36%) minmax(0,64%)" : "1fr", gap: 20, alignItems: "start" }}>

      {/* LEFT — list */}
      <div>
        {/* Filter bar */}
        <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
          <div style={{ position: "relative", flex: 1 }}>
            <Search size={13} style={{ position: "absolute", left: 11, top: "50%", transform: "translateY(-50%)", color: T.muted2 }} />
            <input value={search}
              onChange={e => { setSearch(e.target.value); setPage(1); }}
              placeholder="Search tours by name…"
              style={{ width: "100%", padding: "9px 12px 9px 32px", background: T.card, border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, outline: "none", fontFamily: sans, boxSizing: "border-box" }} />
          </div>
          <select value={country} onChange={e => { setCountry(e.target.value); setPage(1); }}
            style={{ padding: "9px 12px", background: T.card, border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, fontFamily: sans, cursor: "pointer" }}>
            <option value="">All Countries</option>
            {countries.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <PoolFilters
            filters={poolFilters}
            onChange={update => { setPoolFilters(prev => ({ ...prev, ...update })); setPage(1); }}
          />
        </div>

        {/* Batch bar — AA-566 Phần D: this is now the ONLY way to trigger a rewrite (the
            per-tour "Rewrite Config" tab was removed), so it must also cover the single-tour
            case (a row clicked open in the detail panel, not necessarily checkbox-checked) —
            not just the multi-checkbox case it originally handled. */}
        {rewriteTargets.length > 0 && (
          <div style={{ marginBottom: 12, padding: "10px 16px", background: T.goldTint, border: `1px solid ${T.goldSoft}`, borderRadius: 8, display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 13, color: T.amber, fontWeight: 600 }}>
              {rewriteTargets.length} tour{rewriteTargets.length > 1 ? "s" : ""} selected
            </span>
            <Btn variant="primary" size="sm" disabled={rewrit} onClick={() => doRewrite(rewriteTargets)}>
              {rewrit ? "Starting rewrite…" : rewriteTargets.length > 1 ? `Rewrite ${rewriteTargets.length} tours` : "Rewrite this tour"}
            </Btn>
            <Btn variant="ghost" size="sm" onClick={() => { setChecked(new Set()); setSelected(null); }}>Clear</Btn>
          </div>
        )}

        {/* Stats */}
        {!loading && (
          <div style={{ fontSize: 12, color: T.muted2, marginBottom: 12 }}>
            {total.toLocaleString()} tours available
          </div>
        )}

        {/* Tour list */}
        {loading ? <ListSkeleton rows={8} label="Loading tours" /> : tours.length === 0 ? (
          <EmptyState icon={<Globe2 size={32} strokeWidth={1.5} color={T.gold} />} title="No tours found" sub="Try adjusting your search or filters" />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {tours.map((t, i) => {
              const isActive  = selected?.id === t.id;
              const isChecked = checked.has(t.id);
              return (
                <TourRow key={t.id} tour={t} index={(page - 1) * PAGE_SIZE + i + 1} isActive={isActive} isChecked={isChecked}
                  inCatalogSet={inCatalogSet}
                  onSelect={() => { setSelected(isActive ? null : t); setExpandItin(false); }}
                  onCheck={() => setChecked(prev => { const s = new Set(prev); s.has(t.id) ? s.delete(t.id) : s.add(t.id); return s; })}
                />
              );
            })}
          </div>
        )}

        {/* Pagination */}
        {total > PAGE_SIZE && (
          <div style={{ display: "flex", gap: 8, justifyContent: "center", marginTop: 16 }}>
            <Btn variant="secondary" size="sm" disabled={page === 1} onClick={() => setPage(p => p - 1)}>← Prev</Btn>
            <span style={{ padding: "5px 14px", fontSize: 12, color: T.muted, alignSelf: "center" }}>
              {page} / {Math.ceil(total / PAGE_SIZE)}
            </span>
            <Btn variant="secondary" size="sm" disabled={page * PAGE_SIZE >= total} onClick={() => setPage(p => p + 1)}>Next →</Btn>
          </div>
        )}
      </div>

      {/* RIGHT — detail panel */}
      {selected && (
        <div style={{ background: T.card, border: `1px solid ${T.line}`, borderRadius: 12, overflow: "hidden", position: "sticky", top: 20 }}>
          {/* Header */}
          <div style={{ borderBottom: `1px solid ${T.line}` }}>
            <div style={{ padding: "16px 20px 12px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 15, fontWeight: 700, color: T.ink, marginBottom: 4, lineHeight: 1.3 }}>{selected.aa_name}</div>
                  <div style={{ fontSize: 12, color: T.muted, display: "flex", gap: 10, flexWrap: "wrap" }}>
                    {selected.country && <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><MapPin size={12} /> {selected.country}</span>}
                    {selected.duration && <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><Clock size={12} /> {selected.duration}</span>}
                    {selected.price_raw && <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><Wallet size={12} /> {selected.price_raw}</span>}
                  </div>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <button onClick={() => setSelected(null)} style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2, padding: 2 }}>
                    <X size={16} />
                  </button>
                </div>
              </div>
              {inCatalogSet.has(selected.id) ? (
                <span style={{ marginTop: 8, display: "inline-block", fontSize: 11, padding: "2px 8px", background: T.greenSoft, color: T.green, borderRadius: 20, fontWeight: 600 }}>
                  ✓ In My Catalog Tours
                </span>
              ) : selected.already_rewritten ? (
                <span style={{ marginTop: 8, display: "inline-block", fontSize: 11, padding: "2px 8px", background: T.goldTint, color: T.amber, borderRadius: 20, fontWeight: 600 }}>
                  <PenLine size={11} style={{ verticalAlign: -1 }} /> Writing…
                </span>
              ) : null}
            </div>
          </div>

          {/* Body — AA-566 Phần D: single panel now, no more Tour Details/Rewrite Config tabs
              (Rewrite Config removed entirely; triggering a rewrite happens via the batch bar
              above the list, which now also covers the single-tour case — see its own comment). */}
          <div style={{ padding: 20, maxHeight: "70vh", overflowY: "auto" }}>
            <DetailPanel tour={selected} expandItin={expandItin} setExpandItin={setExpandItin} />
          </div>
        </div>
      )}
    </div>
  );
}

// ── Tour row ──────────────────────────────────────────────────────────────────

function TourRow({ tour, index, isActive, isChecked, inCatalogSet, onSelect, onCheck }: {
  tour: PoolTour; index: number; isActive: boolean; isChecked: boolean;
  inCatalogSet: Set<string>;
  onSelect: () => void; onCheck: () => void;
}) {
  const kws = (() => { try { const v = JSON.parse(JSON.parse(tour.seo_keywords_used)); return Array.isArray(v) ? v.slice(0, 3) : []; } catch { return []; } })();
  return (
    <div data-testid="pool-tour-row" style={{
      background: isActive ? "rgba(219,150,40,0.04)" : T.card,
      border: `1px solid ${isChecked ? T.gold : isActive ? "rgba(219,150,40,0.3)" : T.line}`,
      borderRadius: 10, padding: "12px 14px", cursor: "pointer",
      transition: "all .15s",
    }} onClick={onSelect}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <span style={{ fontSize: 11.5, color: T.muted2, marginTop: 4, flexShrink: 0, minWidth: 18, fontFamily: mono }}>{index}</span>
        <input type="checkbox" checked={isChecked} onChange={() => {}} onClick={e => { e.stopPropagation(); onCheck(); }}
          style={{ marginTop: 3, flexShrink: 0, accentColor: T.gold, cursor: "pointer" }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: T.ink }}>{tour.aa_name}</span>
            {inCatalogSet.has(tour.id) ? (
              <span style={{ fontSize: 10, padding: "1px 6px", background: T.greenSoft, color: T.green, borderRadius: 20, fontWeight: 600, flexShrink: 0 }}>✓ In My Catalog Tours</span>
            ) : tour.already_rewritten ? (
              <span style={{ fontSize: 10, padding: "1px 6px", background: T.goldTint, color: T.amber, borderRadius: 20, fontWeight: 600, flexShrink: 0 }}><PenLine size={11} style={{ verticalAlign: -1 }} /> Writing…</span>
            ) : null}
          </div>
          {tour.aa_subtitle && (
            <div style={{ fontSize: 12, color: T.muted, lineHeight: 1.4, marginBottom: 6 }}>
              {tour.aa_subtitle.slice(0, 100)}{tour.aa_subtitle.length > 100 ? "…" : ""}
            </div>
          )}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {tour.country && <Tag icon={<Globe2 size={10} />}>{tour.country}</Tag>}
            {tour.duration && <Tag>{tour.duration}</Tag>}
            {kws.map((k: string) => <Tag key={k} gold>{k}</Tag>)}
          </div>
        </div>
        <ChevronRight size={14} color={T.muted2} style={{ flexShrink: 0, marginTop: 3 }} />
      </div>
    </div>
  );
}

function Tag({ children, icon, gold = false }: { children: React.ReactNode; icon?: React.ReactNode; gold?: boolean }) {
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 3,
      fontSize: 11, padding: "1px 8px", borderRadius: 20,
      background: gold ? "rgba(219,150,40,0.09)" : T.line2,
      color: gold ? T.amber : T.muted,
    }}>
      {icon}{children}
    </span>
  );
}

// ── Detail panel ──────────────────────────────────────────────────────────────

function DetailPanel({ tour, expandItin, setExpandItin }: {
  tour: PoolTour; expandItin: boolean; setExpandItin: (v: boolean) => void;
}) {
  const highlights = parseHighlights(tour.aa_highlights);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      {/* Summary */}
      <Section label="Summary">
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.65, margin: 0 }}>{tour.aa_summary}</p>
      </Section>
      {/* Highlights */}
      {highlights.length > 0 && (
        <Section label="Highlights">
          <ul style={{ margin: 0, paddingLeft: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 5 }}>
            {highlights.map((h, i) => (
              <li key={i} style={{ display: "flex", gap: 8, fontSize: 12.5, color: T.body, lineHeight: 1.5 }}>
                <span style={{ color: T.gold, fontWeight: 700, flexShrink: 0 }}>•</span>{h}
              </li>
            ))}
          </ul>
        </Section>
      )}
      {/* Itinerary */}
      {tour.aa_itineraries && (
        <Section label="Itinerary">
          <div style={{ position: "relative" }}>
            <div style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.7, whiteSpace: "pre-wrap", maxHeight: expandItin ? "none" : 120, overflow: expandItin ? "visible" : "hidden" }}>
              {tour.aa_itineraries}
            </div>
            {!expandItin && <div style={{ position: "absolute", bottom: 0, left: 0, right: 0, height: 40, background: "linear-gradient(transparent, #fff)" }} />}
          </div>
          <button onClick={() => setExpandItin(!expandItin)}
            style={{ marginTop: 6, width: "100%", padding: "6px 0", fontSize: 12, color: T.gold, background: T.goldTint, border: `1px solid ${T.goldSoft}`, borderRadius: 6, cursor: "pointer", fontWeight: 600, fontFamily: sans }}>
            {expandItin ? "▲ Collapse" : "▼ Show full itinerary"}
          </button>
        </Section>
      )}
      {/* SEO */}
      <Section label="SEO">
        <div style={{ background: T.bg, borderRadius: 8, padding: "10px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
          {tour.seo_title && <SeoLine label="Title" value={tour.seo_title} />}
          {tour.seo_meta  && <SeoLine label="Meta"  value={tour.seo_meta}  />}
        </div>
      </Section>
      <div style={{ fontSize: 11, color: T.muted2, fontFamily: mono }}>
        Published {fmtDate(tour.published_at)}
      </div>
    </div>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 8 }}>{label}</div>
      {children}
    </div>
  );
}

function SeoLine({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ fontSize: 10, color: T.muted2, marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 12, color: T.ink, fontWeight: 500, lineHeight: 1.4 }}>{value}</div>
    </div>
  );
}

