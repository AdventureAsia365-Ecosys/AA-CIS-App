"use client";
// app/(tenant)/portal/_components/DashboardTab.tsx
// API: GET /api/tenant/v1/billing (AA-496 — was /api/admin/billing, guaranteed 401 for every
//      real tenant since that proxy requires an admin JWT; see api/routers/v1_tours.py's
//      GET /v1/billing for the real tenant-scoped sibling)
//      GET /api/tenant/v1/tours/pool?page_size=1 (for total count)

import { useState, useEffect } from "react";
import { ArrowRight, FileText, Code2, RotateCcw, Globe2, BookOpen, Sparkles } from "lucide-react";
import {
  T, serif, mono, sans,
  Card, CardHead, Badge, ProgressBar, Spinner, LoadingScreen,
  fmtDateTime, statusVariant,
} from "./ui";

interface BillingData {
  tenant_name: string; plan_tier: string; tours_quota_monthly: number;
  api_calls_quota_monthly: number; price_usd_monthly: number;
  tours_rewritten: number; api_calls_used: number;
  quota_tours_pct: number; quota_calls_pct: number;
  llm_cost_usd: number; billing_month: string;
  overage_usd: number; overage_rate_usd_per_tour: number;
  activity: { id: string; status: string; edit_source: string; tour_name: string; country: string | null; created_at: string }[];
}

// AA-430: was onTabChange(t: Tab) — portal is real routes now, so this just takes the
// destination href directly (router.push in the page.tsx that renders this component).
export default function DashboardTab({ onNavigate }: { onNavigate: (href: string) => void }) {
  const [billing, setBilling] = useState<BillingData | null>(null);
  const [pool, setPool]       = useState(0);
  const [loading, setLoading] = useState(true);
  const [dismissAlert, setDismissAlert] = useState(false);

  useEffect(() => {
    Promise.all([
      fetch("/api/tenant/v1/billing"),
      fetch("/api/tenant/v1/tours/pool?page_size=1"),
    ]).then(async ([bRes, pRes]) => {
      if (bRes.ok) setBilling(await bRes.json());
      if (pRes.ok) { const d = await pRes.json(); setPool(d.pagination?.total ?? 0); }
    }).finally(() => setLoading(false));
  }, []);

  if (loading) return <LoadingScreen message="Loading dashboard…" />;

  const b = billing;
  const toursUsed  = b?.tours_rewritten ?? 0;
  const toursTotal = b?.tours_quota_monthly ?? 200;
  const toursPct   = b?.quota_tours_pct ?? 0;
  const apiUsed    = b?.api_calls_used ?? 0;
  const apiTotal   = b?.api_calls_quota_monthly ?? 20000;
  const apiPct     = b?.quota_calls_pct ?? 0;
  const llmCost    = b?.llm_cost_usd ?? 0;
  const price      = b?.price_usd_monthly ?? 0;
  const planName   = (b?.plan_tier ?? "growth");
  const planLabel  = planName.charAt(0).toUpperCase() + planName.slice(1);
  const month      = b?.billing_month ?? "—";
  const activity   = b?.activity ?? [];

  return (
    <div style={{ fontFamily: sans }}>

      {/* Rate limit alert */}
      {!dismissAlert && apiPct > 30 && (
        <div style={{ background: T.amberSoft, border: `1px solid #F1DDB4`, borderLeft: `4px solid ${T.amber}`, borderRadius: "0 8px 8px 0", padding: "11px 16px", marginBottom: 20, display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ color: T.amber, flexShrink: 0 }}>⚠</span>
          <div style={{ flex: 1, fontSize: 13, color: "#78350F", lineHeight: 1.5 }}>
            <strong>API usage at {apiPct.toFixed(0)}%</strong> — approaching limit. Growth plan = 300 RPM. Consider upgrading to Business (1,000 RPM).
          </div>
          <button onClick={() => setDismissAlert(true)} style={{ background: "none", border: "none", color: T.amber, cursor: "pointer", fontSize: 18, lineHeight: 1, padding: 0 }}>×</button>
        </div>
      )}

      {/* Row 1: Membership · Quota · Re-rewrite */}
      <div style={{ display: "grid", gridTemplateColumns: "1.15fr 1fr 1fr", gap: 18 }}>

        {/* Membership — dark */}
        <Card dark>
          <CardHead title="Membership Status" light action={
            <button onClick={() => onNavigate("/portal/billing")} style={{ background: T.gold, color: T.ink, border: 0, fontWeight: 700, fontSize: 12, padding: "7px 14px", borderRadius: 6, cursor: "pointer", fontFamily: sans }}>
              Upgrade →
            </button>
          } />
          {/* Active pill — in normal flow under the title (was position:absolute on top of it) */}
          <div style={{ marginTop: 2 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "3px 9px", background: "rgba(219,150,40,0.15)", color: T.gold, borderRadius: 999, fontSize: 11, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase" }}>
              <span style={{ width: 5, height: 5, background: T.gold, borderRadius: "50%", display: "block" }} />Active
            </span>
          </div>
          <div style={{ marginTop: 16 }}>
            <div style={{ fontFamily: serif, fontSize: 36, fontWeight: 500, letterSpacing: "-0.02em", color: "#fff", lineHeight: 1 }}>
              {planLabel}
            </div>
            <div style={{ display: "flex", alignItems: "baseline", gap: 4, marginTop: 8, color: "rgba(255,255,255,0.65)" }}>
              <span style={{ fontSize: 22, fontWeight: 600, color: "#fff", letterSpacing: "-0.01em" }}>${price.toLocaleString()}</span>
              <span style={{ fontSize: 12 }}>/ month · billed monthly</span>
            </div>
          </div>
          <div style={{ display: "flex", gap: 22, marginTop: 22, paddingTop: 18, borderTop: "1px solid rgba(255,255,255,0.08)" }}>
            {[["Tours/Mo", toursTotal.toLocaleString()], ["API Calls", apiTotal.toLocaleString()], ["Billing", month]].map(([l, v]) => (
              <div key={l}>
                <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.12em", color: "rgba(255,255,255,0.45)", marginBottom: 4 }}>{l}</div>
                <div style={{ fontSize: 12.5, fontWeight: 600, color: "#fff", fontVariantNumeric: "tabular-nums" }}>{v}</div>
              </div>
            ))}
          </div>
        </Card>

        {/* Quota */}
        <Card>
          <CardHead title="Quota Usage" action={<span style={{ cursor: "pointer" }}>Details →</span>} />
          <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            <QuotaRow icon={<FileText size={13} color={T.gold} />} label="Tours rewritten" used={toursUsed} total={toursTotal} pct={toursPct} />
            <QuotaRow icon={<Code2 size={13} color={T.gold} />} label="API calls" used={apiUsed} total={apiTotal} pct={apiPct} warn={apiPct > 30} />
          </div>
          <div style={{ marginTop: 18, paddingTop: 14, borderTop: `1px dashed ${T.line}`, display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: T.muted }}>
            🕐 Resets in <strong style={{ color: T.ink }}>22 days</strong> · {month}
          </div>
        </Card>

        {/* Re-rewrite balance */}
        <Card>
          <CardHead title="Re-rewrite Balance" action={<span style={{ cursor: "pointer" }}>How it works →</span>} />
          <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
            <div style={{ width: 44, height: 44, borderRadius: 10, background: T.goldTint, color: T.gold, display: "grid", placeItems: "center", flexShrink: 0, border: `1px solid ${T.goldSoft}` }}>
              <RotateCcw size={20} />
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 28, fontWeight: 600, color: T.ink, letterSpacing: "-0.02em", lineHeight: 1 }}>
                2 <span style={{ color: T.muted2, fontSize: 18 }}>/ 3</span>
              </div>
              <div style={{ color: T.muted, fontSize: 12.5, marginTop: 6, lineHeight: 1.4 }}>
                free re-rewrites remaining <strong style={{ color: T.ink }}>per tour</strong> this month.
              </div>
              <div style={{ display: "flex", gap: 4, marginTop: 12 }}>
                {[1, 2, 3].map(i => (
                  <div key={i} style={{ flex: 1, height: 6, borderRadius: 3, background: i <= 2 ? T.gold : T.line2 }} />
                ))}
              </div>
            </div>
          </div>
        </Card>
      </div>

      {/* Row 2: Spend + Activity */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, marginTop: 18 }}>

        {/* Spend */}
        <Card>
          <CardHead title="Spend Summary" action={<span style={{ cursor: "pointer" }}>View invoice →</span>} />
          <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 16 }}>
            <div>
              <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 6, fontWeight: 600 }}>LLM Cost · {month}</div>
              <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 36, fontWeight: 600, color: T.ink, letterSpacing: "-0.02em", lineHeight: 1 }}>
                ${Math.floor(llmCost)}<span style={{ fontSize: 18, color: T.muted2 }}>.{String(Math.round((llmCost % 1) * 10000)).padStart(4, "0")}</span>
              </div>
            </div>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: 12, fontWeight: 600, color: T.green, background: T.greenSoft, padding: "3px 8px", borderRadius: 5 }}>
              ↑ Low LLM spend
            </span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 18 }}>
            <SpendTile label="Platform Fee" value={`$${price.toLocaleString()}`} sub="billed monthly" />
            <SpendTile label="Rewrites" value={`${toursUsed} tours`} sub={`~$0.018/tour · Bedrock`} warn />
          </div>
        </Card>

        {/* Activity */}
        <div style={{ background: T.card, border: `1px solid ${T.line}`, borderRadius: 12, padding: "22px 22px 8px" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
            <span style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted }}>Recent Activity</span>
            <span style={{ fontSize: 12, color: T.muted, cursor: "pointer" }}>View all →</span>
          </div>
          {activity.length === 0 ? (
            <div style={{ padding: "32px 0", textAlign: "center", color: T.muted2, fontSize: 13 }}>
              No activity yet — browse the pool to start
            </div>
          ) : activity.slice(0, 5).map((a, i) => {
            const v = statusVariant(a.status);
            const iconBg   = a.status === "approved" ? T.greenSoft : a.status === "rejected" ? T.redSoft : T.goldTint;
            const iconColor = a.status === "approved" ? T.green    : a.status === "rejected" ? T.red     : T.amber;
            const emoji     = a.status === "approved" ? "✓" : a.status === "rejected" ? "✗" : "↻";
            return (
              <div key={a.id} style={{ display: "grid", gridTemplateColumns: "36px 1fr auto auto", alignItems: "center", gap: 12, padding: "13px 0", borderTop: i === 0 ? "none" : `1px solid ${T.line2}` }}>
                <div style={{ width: 36, height: 36, borderRadius: 8, background: iconBg, color: iconColor, display: "grid", placeItems: "center", fontSize: 14, fontWeight: 700 }}>
                  {emoji}
                </div>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 500, color: T.ink, lineHeight: 1.3 }}>
                    <strong>{a.tour_name || "Tour"}</strong>
                    <span style={{ color: T.muted2, fontSize: 11, marginLeft: 6, fontWeight: 400 }}>
                      · {a.edit_source === "ai_generated" ? "AI" : "Edited"}
                    </span>
                  </div>
                  <div style={{ fontSize: 11, color: T.muted, marginTop: 2 }}>{fmtDateTime(a.created_at)}</div>
                </div>
                <Badge variant={v}>{a.status}</Badge>
                <div style={{ fontSize: 11, color: T.muted2, fontFamily: mono, whiteSpace: "nowrap" }}>
                  {a.created_at ? new Date(a.created_at).toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) : "—"}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Row 3: Quick Actions */}
      <div style={{ marginTop: 18 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 12 }}>
          Quick Actions
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 14 }}>
          {[
            { icon: <Globe2 size={18} />, title: "Browse Tours",      sub: `${pool.toLocaleString()} published tours available`, href: "/portal/t1-rewrite" }, // AA-576 Phần 3 (was "Browse Pool")
            { icon: <BookOpen size={18} />, title: "My Catalog Tours",  sub: `${toursUsed} rewrites · approve, edit, export`,     href: "/portal/t4-pool" }, // AA-576 Phần 3 (was "My Catalog")
            { icon: <Sparkles size={18} />, title: "Brand Identity", sub: "Configure your content voice & style",               href: "/portal/t0-brand" },
          ].map(a => (
            <ActionCard key={a.title} icon={a.icon} title={a.title} sub={a.sub} onClick={() => onNavigate(a.href)} />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function QuotaRow({ icon, label, used, total, pct, warn = false }: {
  icon: React.ReactNode; label: string; used: number; total: number; pct: number; warn?: boolean;
}) {
  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 8 }}>
        <div style={{ fontSize: 13, color: T.ink, fontWeight: 500, display: "flex", alignItems: "center", gap: 7 }}>
          {icon} {label}
        </div>
        <div style={{ fontFamily: mono, fontSize: 12, color: T.ink, fontVariantNumeric: "tabular-nums" }}>
          {used.toLocaleString()} <span style={{ color: T.muted2 }}>/ {total.toLocaleString()}</span>
        </div>
      </div>
      <ProgressBar pct={pct} warn={warn} />
      <div style={{ fontSize: 11, color: warn ? T.amber : T.muted, marginTop: 6, display: "flex", justifyContent: "space-between" }}>
        <span>{pct.toFixed(0)}% used</span>
        <span style={{ fontWeight: warn ? 600 : 400 }}>
          {warn ? "⚠ Rate limit risk" : `${(total - used).toLocaleString()} remaining`}
        </span>
      </div>
    </div>
  );
}

function SpendTile({ label, value, sub, warn = false }: {
  label: string; value: string; sub?: string; warn?: boolean;
}) {
  return (
    <div style={{ background: warn ? T.amberSoft : "#FBF9F4", border: `1px solid ${warn ? "#F1DDB4" : T.line2}`, borderRadius: 8, padding: "12px 14px" }}>
      <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, fontWeight: 600, marginBottom: 5 }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 600, color: T.ink, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: T.muted2, marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

function ActionCard({ icon, title, sub, onClick }: {
  icon: React.ReactNode; title: string; sub: string; onClick: () => void;
}) {
  const [hov, setHov] = useState(false);
  return (
    <button onClick={onClick}
      onMouseEnter={() => setHov(true)} onMouseLeave={() => setHov(false)}
      style={{
        background: T.card, border: `1px solid ${hov ? T.gold : T.line}`, borderRadius: 12,
        padding: "18px 20px", display: "flex", alignItems: "center", gap: 14,
        cursor: "pointer", textAlign: "left", fontFamily: sans,
        transform: hov ? "translateY(-1px)" : "none",
        boxShadow: hov ? "0 4px 12px -6px rgba(219,150,40,0.35)" : "none",
        transition: "all .15s",
      }}>
      <div style={{ width: 40, height: 40, borderRadius: 9, background: T.ink, color: T.gold, display: "grid", placeItems: "center", flexShrink: 0 }}>
        {icon}
      </div>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: T.ink, lineHeight: 1.2 }}>{title}</div>
        <div style={{ fontSize: 12, color: T.muted, marginTop: 3 }}>{sub}</div>
      </div>
      <ArrowRight size={15} color={T.muted2} />
    </button>
  );
}
