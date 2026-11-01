"use client";

import React, { useState, useEffect, useRef } from "react";
import { X } from "lucide-react";
import { scoreColor } from "./TourDetailPanelV2";
import { SCORE_BANDS } from "@/lib/quality-registry.generated";
import { K, Badge, IconButton, Tabs, mono, serif, useT } from "../../_kit";
import { M } from "./tour.messages";

// ── Types ──────────────────────────────────────────────────────────────────────

interface JudgeMeta {
  brand_fit: number | null;
  distinct: number | null;
  mission_present: boolean | null;
  feedback: string | null;
  judge_score: number | null;
}

interface TourDetailCompare {
  raw: {
    src_name: string;
    country: string | null;
    pipeline_status: string;
  } | null;
  generated: {
    aa_name: string;
    aa_summary: string | null;
    aa_highlights: string[] | null;
    aa_itineraries: string | null;
    seo_title: string | null;
    seo_meta: string | null;
    seo_keywords_used: string[] | null;
    model_editorial: string | null;
    version_num: number;
    score_overall: number | null;
    score_brand: number | null;
    score_seo: number | null;
    score_structure: number | null;
    score_quality: number | null;
    brand_audit_status: string | null;
    judge: JudgeMeta | null;
  } | null;
}

type ContentTab = "summary" | "highlights" | "itineraries" | "seo";

// ── Helpers ────────────────────────────────────────────────────────────────────

function modelShort(m: string | null | undefined): string {
  if (!m) return "—";
  return m.split(".").pop()?.replace(/-v\d+:\d+$/, "") ?? m;
}

function fmtScore(s: number | null | undefined): string {
  return s != null ? s.toFixed(1) : "—";
}

// AA-209: score_overall is min(validate_avg, judge_score). When the judge pulled it below the mean
// of the 4 sub-scores, flag it so the displayed Overall doesn't look inconsistent with its bars.
function validateAvg(g: TourDetailCompare["generated"] | undefined): number | null {
  if (!g) return null;
  const subs = [g.score_brand, g.score_seo, g.score_structure, g.score_quality];
  if (subs.some(s => s == null)) return null;
  return (subs as number[]).reduce((a, s) => a + s, 0) / 4;
}

// ── ScoreRow ──────────────────────────────────────────────────────────────────

function ScoreRow({ label, score }: { label: string; score: number | null }) {
  return (
    <div style={{ marginBottom: 7 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3 }}>
        <span style={{ fontSize: 12, color: K.muted }}>{label}</span>
        <span style={{ fontSize: 12, fontWeight: 700, fontFamily: mono, color: scoreColor(score) }}>
          {score != null ? score.toFixed(1) : "—"}
        </span>
      </div>
      <div style={{ height: 4, background: K.line, borderRadius: 2 }}>
        <div style={{
          height: "100%", borderRadius: 2,
          width: score != null ? `${Math.min(score / 10 * 100, 100)}%` : "0%",
          background: scoreColor(score), transition: "width .3s",
        }} />
      </div>
    </div>
  );
}

// ── CompareModal ──────────────────────────────────────────────────────────────

export function CompareModal({ tourIds, onClose }: {
  tourIds: string[];
  onClose: () => void;
}) {
  const t = useT(M);
  // The details of the ids they were loaded for: a new id list shows the loader until its own answer arrives.
  const [loaded, setLoaded] = useState<{ key: string; details: (TourDetailCompare | null)[] } | null>(null);
  const [activeTab, setActiveTab] = useState<ContentTab>("summary");
  const scrollRefs = useRef<(HTMLDivElement | null)[]>([]);
  const isSyncing = useRef(false);

  const n = Math.min(tourIds.length, 4);
  const ids = tourIds.slice(0, 4);
  const idsKey = ids.join(",");
  const loading = loaded?.key !== idsKey;
  const details = loaded && loaded.key === idsKey ? loaded.details : [];

  useEffect(() => {
    let alive = true;
    Promise.all(
      idsKey.split(",").filter(Boolean).map(id =>
        fetch(`/api/admin/tours/${id}/detail`)
          .then(r => r.ok ? r.json() : null)
          .catch(() => null)
      )
    ).then(d => { if (alive) setLoaded({ key: idsKey, details: d }); });
    return () => { alive = false; };
  }, [idsKey]);

  function handleScroll(idx: number, e: React.UIEvent<HTMLDivElement>) {
    if (isSyncing.current) return;
    isSyncing.current = true;
    const scrollTop = (e.target as HTMLDivElement).scrollTop;
    scrollRefs.current.forEach((ref, i) => {
      if (i !== idx && ref) ref.scrollTop = scrollTop;
    });
    isSyncing.current = false;
  }

  const TAB_LABELS: { key: ContentTab; label: string }[] = [
    { key: "summary", label: t("tabSummary") },
    { key: "highlights", label: t("tabHighlights") },
    { key: "itineraries", label: t("tabItineraries") },
    { key: "seo", label: t("tabSeoShort") },
  ];

  return (
    <div style={{
      position: "fixed", inset: 0, background: K.card,
      zIndex: 300, display: "flex", flexDirection: "column",
    }}>
      {/* Modal header */}
      <div style={{
        padding: "14px 24px", borderBottom: `1px solid ${K.line}`,
        background: K.bg, flexShrink: 0,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <div style={{ fontFamily: serif, fontSize: 20, fontWeight: 500, color: K.ink }}>
            {t("comparing", n)}
          </div>
          <IconButton variant="plain" size="lg" onClick={onClose} label={t("close")}>
            <X size={20} />
          </IconButton>
        </div>
        {/* Content tabs */}
        <Tabs
          variant="underline"
          ariaLabel={t("comparing", n)}
          tabs={TAB_LABELS}
          active={activeTab}
          onChange={(k) => setActiveTab(k as ContentTab)}
        />
      </div>

      {loading ? (
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: K.muted, fontSize: 14 }}>
          {t("loadingTours")}
        </div>
      ) : (
        <div style={{
          flex: 1, display: "grid",
          gridTemplateColumns: `repeat(${n}, 1fr)`,
          overflow: "hidden",
        }}>
          {ids.map((tourId, idx) => {
            const detail = details[idx];
            const gen = detail?.generated;
            const raw = detail?.raw;
            const vAvg = validateAvg(gen);
            const judgeCapped = gen?.score_overall != null && vAvg != null && gen.score_overall < vAvg - 0.01;

            return (
              <div key={tourId} style={{
                display: "flex", flexDirection: "column",
                borderRight: idx < n - 1 ? `1px solid ${K.line}` : undefined,
              }}>
                {/* Column header (fixed) */}
                <div style={{ padding: "14px 16px", borderBottom: `1px solid ${K.line}`, flexShrink: 0, background: K.card }}>
                  {/* Badges */}
                  <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginBottom: 7 }}>
                    {raw?.country && <Badge tone="info">{raw.country}</Badge>}
                    {raw?.pipeline_status && (
                      <Badge tone={raw.pipeline_status === "published" ? "success" : "neutral"}>
                        {raw.pipeline_status}
                      </Badge>
                    )}
                    {gen?.score_overall != null && (
                      <Badge tone={gen.score_overall >= SCORE_BANDS.top ? "success" : gen.score_overall >= SCORE_BANDS.pass ? "warning" : "danger"}>
                        ★ {gen.score_overall.toFixed(1)}
                      </Badge>
                    )}
                  </div>
                  {/* Tour name */}
                  <div style={{ fontFamily: serif, fontSize: 14, fontWeight: 600, color: K.ink, lineHeight: 1.3, marginBottom: 8 }}>
                    {gen?.aa_name || raw?.src_name || tourId.slice(0, 8)}
                  </div>
                  {/* Config */}
                  <div style={{ fontSize: 11, color: K.muted, marginBottom: 10 }}>
                    <span style={{ marginRight: 10 }}>{t("model")}: <strong>{modelShort(gen?.model_editorial)}</strong></span>
                    <span>v{gen?.version_num ?? "—"}</span>
                  </div>
                  {/* Quality scores */}
                  <ScoreRow label={t("sOverall")} score={gen?.score_overall ?? null} />
                  {judgeCapped && (
                    <div style={{ marginTop: -3, marginBottom: 7 }}>
                      <Badge tone="warning">{t("judgeCapped", fmtScore(vAvg))}</Badge>
                    </div>
                  )}
                  <ScoreRow label={t("sBrand")} score={gen?.score_brand ?? null} />
                  <ScoreRow label={t("sSeo")} score={gen?.score_seo ?? null} />
                  <ScoreRow label={t("sStructure")} score={gen?.score_structure ?? null} />
                  <ScoreRow label={t("sQuality")} score={gen?.score_quality ?? null} />

                  {/* AA-209: brand judge (GPT-4.1) — guarded for older versions with no judge metadata */}
                  <div style={{ marginTop: 10, paddingTop: 10, borderTop: `1px solid ${K.line}` }}>
                    <div style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", color: K.muted2, marginBottom: 6 }}>
                      {t("brandJudge")}
                    </div>
                    <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
                      {gen?.judge ? (
                        <>
                          <Badge tone="accent">{t("fit", fmtScore(gen.judge.brand_fit))}</Badge>
                          <Badge tone="accent">{t("distinct", fmtScore(gen.judge.distinct))}</Badge>
                          <Badge tone={gen.judge.mission_present ? "success" : "danger"}>
                            {t("mission", !!gen.judge.mission_present)}
                          </Badge>
                        </>
                      ) : (
                        <span style={{ fontSize: 11, color: K.muted2 }}>{t("notRun")}</span>
                      )}
                      {gen?.brand_audit_status && (
                        <Badge tone={gen.brand_audit_status === "pass" ? "success" : "warning"}>
                          {gen.brand_audit_status}
                        </Badge>
                      )}
                    </div>
                    {gen?.judge?.feedback && (
                      <div style={{ fontSize: 11, color: K.body, lineHeight: 1.5, marginTop: 6 }}>
                        {gen.judge.feedback}
                      </div>
                    )}
                  </div>
                </div>

                {/* Scrollable content (synced) */}
                <div
                  ref={el => { scrollRefs.current[idx] = el; }}
                  onScroll={e => handleScroll(idx, e)}
                  style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: 16 }}
                >
                  {activeTab === "summary" && (
                    gen?.aa_summary
                      ? <p style={{ fontSize: 13, color: K.body, lineHeight: 1.75, margin: 0 }}>{gen.aa_summary}</p>
                      : <span style={{ color: K.muted, fontSize: 13 }}>—</span>
                  )}

                  {activeTab === "highlights" && (
                    (gen?.aa_highlights?.length ?? 0) > 0 ? (
                      <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 6 }}>
                        {(gen?.aa_highlights || []).map((h, i) => (
                          <li key={i} style={{ fontSize: 13, color: K.body }}>{h}</li>
                        ))}
                      </ul>
                    ) : <span style={{ color: K.muted, fontSize: 13 }}>—</span>
                  )}

                  {activeTab === "itineraries" && (
                    gen?.aa_itineraries
                      ? <div style={{ fontSize: 12, color: K.body, lineHeight: 1.75, whiteSpace: "pre-wrap" }}>{gen.aa_itineraries}</div>
                      : <span style={{ color: K.muted, fontSize: 13 }}>—</span>
                  )}

                  {activeTab === "seo" && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                      {gen?.seo_title && (
                        <div>
                          <div style={{ fontSize: 11, color: K.muted, marginBottom: 3 }}>
                            {t("title")} <span style={{ color: (gen.seo_title.length > 60 ? K.amber : K.muted2) }}>({gen.seo_title.length}/70)</span>
                          </div>
                          <div style={{ fontSize: 13, color: K.ink }}>{gen.seo_title}</div>
                        </div>
                      )}
                      {gen?.seo_meta && (
                        <div>
                          <div style={{ fontSize: 11, color: K.muted, marginBottom: 3 }}>
                            {t("meta")} <span style={{ color: (gen.seo_meta.length > 155 ? K.amber : K.muted2) }}>({gen.seo_meta.length}/170)</span>
                          </div>
                          <div style={{ fontSize: 13, color: K.body }}>{gen.seo_meta}</div>
                        </div>
                      )}
                      {(gen?.seo_keywords_used?.length ?? 0) > 0 && (
                        <div>
                          <div style={{ fontSize: 11, color: K.muted, marginBottom: 6 }}>{t("keywords")}</div>
                          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                            {(gen?.seo_keywords_used || []).map((kw, i) => (
                              <span key={i} style={{
                                fontSize: 11, padding: "2px 8px", borderRadius: 12,
                                background: K.accentTint, color: K.accent, border: `1px solid ${K.line}`,
                              }}>{kw}</span>
                            ))}
                          </div>
                        </div>
                      )}
                      {!gen?.seo_title && !gen?.seo_meta && (
                        <span style={{ color: K.muted, fontSize: 13 }}>{t("noSeo")}</span>
                      )}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
