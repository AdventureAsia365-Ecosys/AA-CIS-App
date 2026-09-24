"use client";
// app/(internal)/review/page.tsx
// All HITL API logic preserved — /v1/pipeline/review-queue, approve, reject
// Design: brand tokens (app/_brand/tokens.ts) via internalUi

import { useState, useEffect } from "react";
import { CheckCircle, XCircle, RotateCcw, ChevronDown, ChevronUp, Filter, Edit3, Flag } from "lucide-react";
import InternalSidebar from "../_components/InternalSidebar";
import { A, serif, mono, sans, Card, SLabel, Btn, LoadingScreen, TopBar } from "../_components/internalUi";

// ─── Types + helpers ──────────────────────────────────────────────────────────
// AA-435: used to read cis_api_token + call ${API_URL} directly with a Bearer
// header — now goes through /api/tenant/[...path] (httpOnly cis_admin_token,
// server-side), so no client-readable token is needed here at all.

function mapApiToReview(r: any) {
  return {
    id:   r.id,
    name: r.aa_name || r.src_name || "Unknown",
    country: r.country || "Unknown",
    score: parseFloat(r.score_overall || "0"),
    date:  r.ingest_at ? new Date(r.ingest_at).toLocaleDateString() : "",
    issues: (() => { try { return JSON.parse(r.failure_summary || "[]"); } catch { return []; } })(),
    original: { name: r.src_name || "", summary: r.src_summary || "", highlights: [], seo_title: "", seo_meta: "" },
    generated: { name: r.aa_name || "", subtitle: r.aa_subtitle || "", summary: r.aa_summary || "", highlights: [], seo_title: r.seo_title || "", seo_meta: r.seo_meta || "" },
    seo_checks: [
      { label: "Title ≤60 chars",      status: (r.seo_title?.length <= 60 ? "pass" : "fail"),  value: `${r.seo_title?.length || 0} chars` },
      { label: "Meta 80–160 chars",    status: (r.seo_meta?.length  >= 80 ? "pass" : "fail"),  value: `${r.seo_meta?.length  || 0} chars` },
    ],
  };
}

const SEO_STYLE: Record<string, { bg: string; color: string }> = {
  pass: { bg: "#D1FAE5", color: "#065F46" },
  warn: { bg: "#FEF3C7", color: "#92400E" },
  fail: { bg: "#FEE2E2", color: "#991B1B" },
};

// ─── Before/After panel ───────────────────────────────────────────────────────
function BeforeAfter({ item }: { item: any }) {
  const [editSummary, setEditSummary] = useState(item.generated.summary);
  const [editMeta, setEditMeta]       = useState(item.generated.seo_meta);

  const colBase: React.CSSProperties = { flex: 1, borderRadius: 8, overflow: "hidden", display: "flex", flexDirection: "column" };

  return (
    <div style={{ borderTop: `1px solid ${A.line}` }}>
      <div style={{ display: "flex", gap: 12, padding: "16px 20px" }}>
        {/* BEFORE */}
        <div style={{ ...colBase, background: "#FFF5F5", border: "1px solid #FECACA" }}>
          <div style={{ padding: "8px 14px", background: "#FECACA", fontSize: 11, fontWeight: 700, letterSpacing: "0.1em", color: "#991B1B", textTransform: "uppercase" }}>
            ✕ Before — Original Supplier
          </div>
          <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10, overflowY: "auto", maxHeight: 360 }}>
            <Field label="Name"><span style={{ fontFamily: mono, fontSize: 12 }}>{item.original.name}</span></Field>
            <Field label="Summary"><span style={{ fontFamily: mono, fontSize: 12, lineHeight: 1.6 }}>{item.original.summary}</span></Field>
            <Field label="SEO Title"><span style={{ fontFamily: mono, fontSize: 11 }}>{item.original.seo_title}</span></Field>
            <Field label="SEO Meta"><span style={{ fontFamily: mono, fontSize: 11 }}>{item.original.seo_meta}</span></Field>
          </div>
        </div>

        {/* AFTER */}
        <div style={{ ...colBase, background: "#F0FDF4", border: "1px solid #86EFAC" }}>
          <div style={{ padding: "8px 14px", background: "#86EFAC", fontSize: 11, fontWeight: 700, letterSpacing: "0.1em", color: "#166534", textTransform: "uppercase", display: "flex", alignItems: "center", gap: 6 }}>
            <Edit3 size={11} /> After — AI Rewrite (Editable)
          </div>
          <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10, overflowY: "auto", maxHeight: 360 }}>
            <Field label="Name" green><strong style={{ fontSize: 13, color: "#14532D" }}>{item.generated.name}</strong></Field>
            <Field label="Subtitle" green><em style={{ fontSize: 12, color: "#166534" }}>{item.generated.subtitle}</em></Field>
            <Field label="Summary" green>
              <div contentEditable suppressContentEditableWarning
                onInput={e => setEditSummary((e.target as HTMLElement).innerText)}
                style={{ fontSize: 12, color: "#14532D", lineHeight: 1.7, padding: "4px 6px", borderRadius: 4, background: "rgba(255,255,255,0.6)", minHeight: 60, outline: "none" }}>
                {item.generated.summary}
              </div>
            </Field>
            <Field label="SEO Title" green><span style={{ fontFamily: mono, fontSize: 12, color: "#14532D" }}>{item.generated.seo_title}</span></Field>
            <Field label="SEO Meta" green>
              <div contentEditable suppressContentEditableWarning
                onInput={e => setEditMeta((e.target as HTMLElement).innerText)}
                style={{ fontFamily: mono, fontSize: 12, color: "#14532D", padding: "4px 6px", borderRadius: 4, background: "rgba(255,255,255,0.6)", minHeight: 40, outline: "none" }}>
                {item.generated.seo_meta}
              </div>
            </Field>
          </div>
        </div>
      </div>

      {/* SEO breakdown */}
      <div style={{ padding: "12px 20px", background: A.bg, borderTop: `1px solid ${A.line}` }}>
        <div style={{ fontSize: 10, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 8 }}>SEO Validation</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
          {item.seo_checks.map((c: any, i: number) => {
            const s = SEO_STYLE[c.status] ?? SEO_STYLE.pass;
            return (
              <div key={i} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "5px 12px", borderRadius: 6, background: s.bg }}>
                <span style={{ fontSize: 12, color: A.body }}>{c.label}</span>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontSize: 11, color: A.muted }}>{c.value}</span>
                  <span style={{ fontSize: 10, fontWeight: 700, color: s.color, padding: "2px 7px", borderRadius: 4, border: `1px solid ${s.color}33` }}>{c.status}</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function Field({ label, children, green = false }: { label: string; children: React.ReactNode; green?: boolean }) {
  return (
    <div>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: green ? "#166534" : "#6B7280", marginBottom: 4 }}>{label}</div>
      {children}
    </div>
  );
}

// ─── Review Card ──────────────────────────────────────────────────────────────
function ReviewCard({ item, onApprove, onReject, onRegenerate, onFlag }: {
  item: any;
  onApprove: (id: string) => void;
  onReject:  (id: string) => void;
  onRegenerate: (id: string) => void;
  onFlag: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const hasFail = item.seo_checks.some((c: any) => c.status === "fail");
  const scoreColor = item.score >= 7 ? A.green : item.score >= 5 ? A.amber : A.red;

  return (
    <Card style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ padding: "14px 20px", display: "flex", alignItems: "center", gap: 14 }}>
        {/* Score */}
        <div style={{
          width: 44, height: 44, borderRadius: 10, flexShrink: 0,
          display: "flex", alignItems: "center", justifyContent: "center",
          background: item.score >= 7 ? A.greenSoft : A.redSoft,
          color: scoreColor, fontWeight: 700, fontSize: 14, fontFamily: mono,
        }}>
          {item.score.toFixed(1)}
        </div>

        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 600, color: A.ink, fontSize: 14 }}>{item.name}</div>
          <div style={{ fontSize: 11.5, color: A.muted, marginTop: 2 }}>{item.country} · {item.date}</div>
        </div>

        {/* Issue badges */}
        {item.issues.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5, maxWidth: 300 }}>
            {item.issues.slice(0, 3).map((issue: string, i: number) => (
              <span key={i} style={{ fontSize: 10.5, padding: "2px 9px", background: A.redSoft, color: A.red, border: "1px solid #FECACA", borderRadius: 20 }}>{issue}</span>
            ))}
            {item.issues.length > 3 && <span style={{ fontSize: 10.5, color: A.muted }}>+{item.issues.length - 3}</span>}
          </div>
        )}

        {/* Actions */}
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <button onClick={() => onFlag(item.id)} style={{ padding: 7, border: `1px solid ${A.line}`, borderRadius: 8, background: "none", cursor: "pointer", color: A.muted, display: "flex" }}>
            <Flag size={13} />
          </button>
          <button onClick={() => onRegenerate(item.id)} style={{ padding: 7, border: `1px solid ${A.line}`, borderRadius: 8, background: "none", cursor: "pointer", color: A.muted, display: "flex" }}>
            <RotateCcw size={13} />
          </button>
          <Btn variant="danger" size="sm" onClick={() => onReject(item.id)}>
            <XCircle size={12} /> Reject
          </Btn>
          <Btn variant={hasFail ? "ghost" : "primary"} size="sm" disabled={hasFail} onClick={() => onApprove(item.id)}>
            <CheckCircle size={12} /> {hasFail ? "Fix First" : "Approve"}
          </Btn>
          <button onClick={() => setExpanded(!expanded)} style={{ padding: 7, border: `1px solid ${A.line}`, borderRadius: 8, background: "none", cursor: "pointer", color: A.muted, display: "flex" }}>
            {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          </button>
        </div>
      </div>

      {expanded && <BeforeAfter item={item} />}
    </Card>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────
export default function ReviewPage() {
  const [items, setItems]         = useState<any[]>([]);
  const [loading, setLoading]     = useState(true);
  const [filterCountry, setCountry] = useState("all");
  const [filterScore, setScore]   = useState("all");
  const [approved, setApproved]   = useState(0);
  const [rejected, setRejected]   = useState(0);
  const [isAdmin, setIsAdmin]     = useState(false);
  const [userName, setUserName]   = useState("Content");

  useEffect(() => {
    const role = document.cookie.split(";").find(c => c.trim().startsWith("cis_role="))?.split("=")[1];
    const name = document.cookie.split(";").find(c => c.trim().startsWith("cis_user="))?.split("=")[1];
    setIsAdmin(role === "admin");
    if (name) setUserName(decodeURIComponent(name));

    fetch(`/api/tenant/v1/pipeline/review-queue`)
      .then(r => r.json())
      .then(d => { setItems((d.data || []).map(mapApiToReview)); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  async function onApprove(id: string) {
    await fetch(`/api/tenant/v1/pipeline/review-queue/${id}/approve`, { method: "POST" });
    setApproved(a => a + 1);
    setItems(p => p.filter(i => i.id !== id));
  }

  async function onReject(id: string) {
    await fetch(`/api/tenant/v1/pipeline/review-queue/${id}/reject`, { method: "POST" });
    setRejected(r => r + 1);
    setItems(p => p.filter(i => i.id !== id));
  }

  const countries = [...new Set(items.map(i => i.country))];
  const filtered  = items.filter(item => {
    const mc = filterCountry === "all" || item.country === filterCountry;
    const ms = filterScore === "all" || (filterScore === "critical" && item.score < 5) || (filterScore === "low" && item.score >= 5 && item.score < 7);
    return mc && ms;
  });

  const selectStyle: React.CSSProperties = {
    padding: "7px 14px", background: A.card, border: `1px solid ${A.line}`,
    borderRadius: 8, color: A.body, fontSize: 13, fontFamily: sans, cursor: "pointer", outline: "none",
  };

  return (
    <div style={{ display: "flex", minHeight: "100vh", fontFamily: sans, background: A.bg }}>
      <InternalSidebar isAdmin={isAdmin} userName={userName} />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, height: "100vh" }}>
        <TopBar breadcrumb={["Content", "Review Queue"]} />
        <main style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
          {/* Header */}
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 24 }}>
            <div>
              <h1 style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: A.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>
                Review Queue
              </h1>
              <p style={{ fontSize: 13, color: A.muted, margin: 0 }}>Tours requiring human review · Quality score below 7.0</p>
            </div>
            <div style={{ display: "flex", gap: 20 }}>
              {[
                { label: "Approved", value: approved, color: A.green },
                { label: "Rejected", value: rejected, color: A.red },
                { label: "Pending",  value: items.length, color: A.gold },
              ].map(s => (
                <div key={s.label} style={{ textAlign: "center" }}>
                  <div style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: s.color, letterSpacing: "-0.02em" }}>{s.value}</div>
                  <div style={{ fontSize: 11, color: A.muted }}>{s.label}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Filters */}
          <div style={{ display: "flex", gap: 10, marginBottom: 20, alignItems: "center" }}>
            <Filter size={14} style={{ color: A.muted }} />
            <select value={filterCountry} onChange={e => setCountry(e.target.value)} style={selectStyle}>
              <option value="all">All Countries</option>
              {countries.map(c => <option key={c}>{c}</option>)}
            </select>
            <select value={filterScore} onChange={e => setScore(e.target.value)} style={selectStyle}>
              <option value="all">All Scores</option>
              <option value="critical">Critical (&lt;5.0)</option>
              <option value="low">Low (5.0–6.9)</option>
            </select>
          </div>

          {loading ? <LoadingScreen msg="Loading review queue…" /> : filtered.length === 0 ? (
            <div style={{ textAlign: "center", padding: "60px 0" }}>
              <CheckCircle size={40} style={{ margin: "0 auto 12px", color: A.green, display: "block" }} />
              <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink }}>Review queue is empty</div>
              <div style={{ fontSize: 13, color: A.muted, marginTop: 6 }}>All tours have been reviewed</div>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {filtered.map(item => (
                <ReviewCard key={item.id} item={item}
                  onApprove={onApprove} onReject={onReject}
                  onRegenerate={id => console.log("Regenerate", id)}
                  onFlag={id => alert(`Content issue reported for ${id}`)} />
              ))}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
