"use client";

import React, { useState, useEffect, useRef } from "react";
import { X } from "lucide-react";
import { A, serif, sans, mono, Badge } from "./adminUi";
import { scoreColor } from "./TourDetailPanelV2";

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
        <span style={{ fontSize: 12, color: A.muted }}>{label}</span>
        <span style={{ fontSize: 12, fontWeight: 700, fontFamily: mono, color: scoreColor(score) }}>
          {score != null ? score.toFixed(1) : "—"}
        </span>
      </div>
      <div style={{ height: 4, background: A.line, borderRadius: 2 }}>
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
  const [details, setDetails] = useState<(TourDetailCompare | null)[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<ContentTab>("summary");
  const scrollRefs = useRef<(HTMLDivElement | null)[]>([]);
  const isSyncing = useRef(false);

  const n = Math.min(tourIds.length, 4);
  const ids = tourIds.slice(0, 4);

  useEffect(() => {
    setLoading(true);
    Promise.all(
      ids.map(id =>
        fetch(`/api/admin/tours/${id}/detail`)
          .then(r => r.ok ? r.json() : null)
          .catch(() => null)
      )
    ).then(setDetails).finally(() => setLoading(false));
  }, [ids.join(",")]);  // eslint-disable-line react-hooks/exhaustive-deps

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
    { key: "summary", label: "Summary" },
    { key: "highlights", label: "Highlights" },
    { key: "itineraries", label: "Itineraries" },
    { key: "seo", label: "SEO" },
  ];

  return (
    <div style={{
      position: "fixed", inset: 0, background: A.card,
      zIndex: 300, display: "flex", flexDirection: "column",
    }}>
      {/* Modal header */}
      <div style={{
        padding: "14px 24px", borderBottom: `1px solid ${A.line}`,
        background: A.bg, flexShrink: 0,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <div style={{ fontFamily: serif, fontSize: 20, fontWeight: 500, color: A.ink }}>
            Comparing {n} Tours
          </div>
          <button
            onClick={onClose}
            style={{ background: "none", border: "none", cursor: "pointer", color: A.muted, padding: 6 }}
          >
            <X size={20} />
          </button>
        </div>
        {/* Content tabs */}
        <div style={{ display: "flex", gap: 0 }}>
          {TAB_LABELS.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setActiveTab(key)}
              style={{
                padding: "7px 14px", fontSize: 13,
                fontWeight: activeTab === key ? 600 : 400,
                color: activeTab === key ? A.ink : A.muted,
                cursor: "pointer", background: "none", border: "none",
                borderBottom: activeTab === key ? `2px solid ${A.gold}` : "2px solid transparent",
                fontFamily: sans,
              }}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: A.muted, fontSize: 14 }}>
          Loading tours…
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
                borderRight: idx < n - 1 ? `1px solid ${A.line}` : undefined,
              }}>
                {/* Column header (fixed) */}
                <div style={{ padding: "14px 16px", borderBottom: `1px solid ${A.line}`, flexShrink: 0, background: A.card }}>
                  {/* Badges */}
                  <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginBottom: 7 }}>
                    {raw?.country && <Badge color="blue">{raw.country}</Badge>}
                    {raw?.pipeline_status && (
                      <Badge color={raw.pipeline_status === "published" ? "green" : "gray"}>
                        {raw.pipeline_status}
                      </Badge>
                    )}
                    {gen?.score_overall != null && (
                      <Badge color={gen.score_overall >= 9 ? "green" : gen.score_overall >= 7 ? "amber" : "red"}>
                        ★ {gen.score_overall.toFixed(1)}
                      </Badge>
                    )}
                  </div>
                  {/* Tour name */}
                  <div style={{ fontFamily: serif, fontSize: 14, fontWeight: 600, color: A.ink, lineHeight: 1.3, marginBottom: 8 }}>
                    {gen?.aa_name || raw?.src_name || tourId.slice(0, 8)}
                  </div>
                  {/* Config */}
                  <div style={{ fontSize: 11, color: A.muted, marginBottom: 10 }}>
                    <span style={{ marginRight: 10 }}>Model: <strong>{modelShort(gen?.model_editorial)}</strong></span>
                    <span>v{gen?.version_num ?? "—"}</span>
                  </div>
                  {/* Quality scores */}
                  <ScoreRow label="Overall" score={gen?.score_overall ?? null} />
                  {judgeCapped && (
                    <div style={{ marginTop: -3, marginBottom: 7 }}>
                      <Badge color="amber">⚠ judge-capped (avg {fmtScore(vAvg)})</Badge>
                    </div>
                  )}
                  <ScoreRow label="Brand" score={gen?.score_brand ?? null} />
                  <ScoreRow label="SEO" score={gen?.score_seo ?? null} />
                  <ScoreRow label="Structure" score={gen?.score_structure ?? null} />
                  <ScoreRow label="Quality" score={gen?.score_quality ?? null} />

                  {/* AA-209: brand judge (GPT-4.1) — guarded for older versions with no judge metadata */}
                  <div style={{ marginTop: 10, paddingTop: 10, borderTop: `1px solid ${A.line}` }}>
                    <div style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", color: A.muted2, marginBottom: 6 }}>
                      Brand Judge
                    </div>
                    <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
                      {gen?.judge ? (
                        <>
                          <Badge color="gold">fit {fmtScore(gen.judge.brand_fit)}</Badge>
                          <Badge color="gold">distinct {fmtScore(gen.judge.distinct)}</Badge>
                          <Badge color={gen.judge.mission_present ? "green" : "red"}>
                            mission {gen.judge.mission_present ? "✓" : "✗"}
                          </Badge>
                        </>
                      ) : (
                        <span style={{ fontSize: 11, color: A.muted2 }}>not run</span>
                      )}
                      {gen?.brand_audit_status && (
                        <Badge color={gen.brand_audit_status === "pass" ? "green" : "amber"}>
                          {gen.brand_audit_status}
                        </Badge>
                      )}
                    </div>
                    {gen?.judge?.feedback && (
                      <div style={{ fontSize: 11, color: A.body, lineHeight: 1.5, marginTop: 6 }}>
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
                      ? <p style={{ fontSize: 13, color: A.body, lineHeight: 1.75, margin: 0 }}>{gen.aa_summary}</p>
                      : <span style={{ color: A.muted, fontSize: 13 }}>—</span>
                  )}

                  {activeTab === "highlights" && (
                    (gen?.aa_highlights?.length ?? 0) > 0 ? (
                      <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 6 }}>
                        {(gen?.aa_highlights || []).map((h, i) => (
                          <li key={i} style={{ fontSize: 13, color: A.body }}>{h}</li>
                        ))}
                      </ul>
                    ) : <span style={{ color: A.muted, fontSize: 13 }}>—</span>
                  )}

                  {activeTab === "itineraries" && (
                    gen?.aa_itineraries
                      ? <div style={{ fontSize: 12, color: A.body, lineHeight: 1.75, whiteSpace: "pre-wrap" }}>{gen.aa_itineraries}</div>
                      : <span style={{ color: A.muted, fontSize: 13 }}>—</span>
                  )}

                  {activeTab === "seo" && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                      {gen?.seo_title && (
                        <div>
                          <div style={{ fontSize: 11, color: A.muted, marginBottom: 3 }}>
                            Title <span style={{ color: (gen.seo_title.length > 60 ? A.amber : A.muted2) }}>({gen.seo_title.length}/70)</span>
                          </div>
                          <div style={{ fontSize: 13, color: A.ink }}>{gen.seo_title}</div>
                        </div>
                      )}
                      {gen?.seo_meta && (
                        <div>
                          <div style={{ fontSize: 11, color: A.muted, marginBottom: 3 }}>
                            Meta <span style={{ color: (gen.seo_meta.length > 155 ? A.amber : A.muted2) }}>({gen.seo_meta.length}/170)</span>
                          </div>
                          <div style={{ fontSize: 13, color: A.body }}>{gen.seo_meta}</div>
                        </div>
                      )}
                      {(gen?.seo_keywords_used?.length ?? 0) > 0 && (
                        <div>
                          <div style={{ fontSize: 11, color: A.muted, marginBottom: 6 }}>Keywords</div>
                          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                            {(gen?.seo_keywords_used || []).map((kw, i) => (
                              <span key={i} style={{
                                fontSize: 11, padding: "2px 8px", borderRadius: 12,
                                background: A.goldTint, color: A.gold, border: `1px solid ${A.line}`,
                              }}>{kw}</span>
                            ))}
                          </div>
                        </div>
                      )}
                      {!gen?.seo_title && !gen?.seo_meta && (
                        <span style={{ color: A.muted, fontSize: 13 }}>No SEO data</span>
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
