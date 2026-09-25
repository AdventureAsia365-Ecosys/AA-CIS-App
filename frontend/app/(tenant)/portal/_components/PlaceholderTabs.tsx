"use client";
// app/(tenant)/portal/_components/PlaceholderTabs.tsx
// Activity Log, Billing and Settings.
//
// AA-636: these three pages were largely mock UI — every tenant saw "WanderLux Travel" /
// "sara@wanderlux.com" in Settings, a Growth-is-current plan table with hardcoded "300 RPM" /
// "20,000" API calls, and Settings controls (edit name/email, rotate key, notification toggles,
// default language/SEO mode) that saved nothing. Everything shown now comes from the tenant's own
// data (/api/tenant/me, /v1/billing); account changes go through the Adventure Asia team, since
// no tenant self-service endpoint exists for them.

import { useState } from "react";
import { Check, X, RefreshCw, Mail, ExternalLink, KeyRound, LogOut } from "lucide-react";
import { T, sans, Card, PageHeader, Btn, fmtDateTime, statusLabel } from "./ui";
import { usePortalShell } from "./PortalShellContext";
import { SUPPORT_EMAIL, SITE_URL } from "../../../_brand/tokens";

type Activity = { id: string; status: string; edit_source: string; tour_name: string; country: string | null; created_at: string };

const titleCase = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

// ─── Activity Log ─────────────────────────────────────────────────────────────
export function ActivityLogTab({ activity }: { activity: Activity[] }) {
  const { tenantName } = usePortalShell();
  const sc = (s: string) =>
    s === "approved" ? { bg: T.greenSoft, color: T.green, Icon: Check } :
    s === "rejected" ? { bg: T.redSoft, color: T.red, Icon: X } :
    { bg: T.amberSoft, color: T.amber, Icon: RefreshCw };

  return (
    <div style={{ maxWidth: 760 }}>
      <PageHeader title="Activity Log" sub={`Recent rewrite and catalog actions for ${tenantName}.`} />
      {activity.length === 0 ? (
        <Card><div style={{ textAlign: "center", padding: 40, color: T.muted2, fontSize: 13 }}>No activity yet. Rewrite a tour and it will show up here.</div></Card>
      ) : (
        <Card style={{ padding: 0, overflow: "hidden" }}>
          {activity.map((a, i) => {
            const s = sc(a.status);
            return (
              <div key={a.id} style={{ display: "grid", gridTemplateColumns: "36px minmax(0,1fr) auto", alignItems: "center", gap: 14, padding: "14px 20px", borderBottom: i < activity.length - 1 ? `1px solid ${T.line2}` : "none" }}>
                <div style={{ width: 36, height: 36, borderRadius: 8, background: s.bg, color: s.color, display: "grid", placeItems: "center" }}>
                  <s.Icon size={15} strokeWidth={2.2} />
                </div>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 500, color: T.ink, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.tour_name || "Tour"}</div>
                  <div style={{ fontSize: 11, color: T.muted, marginTop: 2 }}>
                    {a.edit_source === "ai_generated" ? "AI rewrite" : "Edited by you"}
                    {a.country ? ` · ${a.country}` : ""} · {fmtDateTime(a.created_at)}
                  </div>
                </div>
                <span style={{ fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999, textTransform: "uppercase", letterSpacing: "0.04em", background: s.bg, color: s.color, whiteSpace: "nowrap" }}>{statusLabel(a.status)}</span>
              </div>
            );
          })}
        </Card>
      )}
    </div>
  );
}

// ─── Contact modal (plan / account changes) ───────────────────────────────────
function ContactModal({ heading, body, subject, onClose }: {
  heading: string; body: string; subject: string; onClose: () => void;
}) {
  return (
    <div role="dialog" aria-modal="true" aria-label={heading}
      style={{ position: "fixed", inset: 0, background: "rgba(31,41,51,0.45)", zIndex: 999, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}
      onClick={onClose}>
      <div style={{ background: T.card, borderRadius: 16, padding: 32, maxWidth: 460, width: "100%", boxShadow: "0 20px 60px rgba(0,0,0,0.25)" }}
        onClick={e => e.stopPropagation()}>
        <div style={{ fontFamily: "inherit", fontSize: 22, fontWeight: 600, color: T.ink, marginBottom: 10, letterSpacing: "-0.01em" }}>{heading}</div>
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, margin: "0 0 18px" }}>{body}</p>
        <div style={{ padding: "12px 14px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 10, marginBottom: 16 }}>
          <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, fontWeight: 600, marginBottom: 4 }}>Email</div>
          <div style={{ fontSize: 14, fontWeight: 600, color: T.ink, userSelect: "all" }}>{SUPPORT_EMAIL}</div>
          <div style={{ fontSize: 11.5, color: T.muted2, marginTop: 4 }}>Subject: {subject}</div>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <a href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}`}
            style={{ flex: 1, display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "11px 18px", background: T.gold, color: T.ink, borderRadius: 999, fontSize: 13, fontWeight: 700, textDecoration: "none", minWidth: 160 }}>
            <Mail size={15} /> Email us
          </a>
          <a href={SITE_URL} target="_blank" rel="noreferrer"
            style={{ flex: 1, display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "10px 18px", background: T.card, border: `1px solid ${T.line}`, color: T.ink3, borderRadius: 999, fontSize: 13, fontWeight: 500, textDecoration: "none", minWidth: 160 }}>
            adventure.asia <ExternalLink size={13} />
          </a>
        </div>
        <button onClick={onClose} style={{ marginTop: 14, width: "100%", padding: "8px 0", background: "none", border: "none", color: T.muted2, cursor: "pointer", fontSize: 12, fontFamily: sans }}>
          Close
        </button>
      </div>
    </div>
  );
}

// ─── Billing ──────────────────────────────────────────────────────────────────
type Plan = { plan_name: string; tours_quota_monthly: number; api_calls_quota_monthly: number; price_usd_monthly: number | null; rate_limit_rpm: number | null };

const fmtQuota = (n: number | null | undefined) => (n == null ? "—" : n >= 999999 ? "Unlimited" : n.toLocaleString());

export function BillingTab({ billing }: { billing: any }) {
  const { tenantName } = usePortalShell();
  const [contactPlan, setContactPlan] = useState<string | null>(null);

  const plan       = String(billing?.plan_tier ?? "starter");
  const price      = Number(billing?.price_usd_monthly ?? 0);
  const toursUsed  = Number(billing?.tours_rewritten ?? 0);
  const toursTotal = Number(billing?.tours_quota_monthly ?? 0);
  const apiUsed    = Number(billing?.api_calls_used ?? 0);
  const apiTotal   = Number(billing?.api_calls_quota_monthly ?? 0);
  const overage    = Number(billing?.overage_usd ?? 0);
  const rpm        = billing?.rate_limit_rpm as number | null | undefined;
  const month      = billing?.billing_month ?? "—";
  const plans: Plan[] = billing?.plans ?? [];
  const pct = (u: number, t: number) => (t > 0 ? Math.min(100, (u / t) * 100) : 0);

  return (
    <div style={{ maxWidth: 760 }}>
      {contactPlan && (
        <ContactModal
          heading={`Switch to ${titleCase(contactPlan)}`}
          body="Plan changes are handled by the Adventure Asia team. Email us and we will switch your plan, usually within one business day."
          subject={`Plan change to ${titleCase(contactPlan)} — ${tenantName}`}
          onClose={() => setContactPlan(null)}
        />
      )}

      <PageHeader title="Billing" sub={`Your plan and usage for the ${month} billing period.`} />

      {/* Current plan */}
      <Card dark style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.14em", color: "rgba(255,255,255,0.5)", marginBottom: 8 }}>Current plan</div>
            <div style={{ fontSize: 32, fontWeight: 600, color: "#fff", letterSpacing: "-0.02em" }}>{titleCase(plan)}</div>
            <div style={{ fontSize: 14, color: "rgba(255,255,255,0.65)", marginTop: 4 }}>
              {price > 0 ? `$${price.toLocaleString()} / month · billed monthly` : "Custom pricing"}
            </div>
          </div>
          <Btn variant="primary" onClick={() => setContactPlan(plans.find(p => p.plan_name !== plan && (p.price_usd_monthly ?? Infinity) > price)?.plan_name ?? "enterprise")}>
            Change plan
          </Btn>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 16, marginTop: 20, paddingTop: 16, borderTop: "1px solid rgba(255,255,255,0.08)" }}>
          {[["Tours / month", fmtQuota(toursTotal)], ["API calls / month", fmtQuota(apiTotal)], ["Rate limit", rpm ? `${rpm.toLocaleString()} RPM` : "—"]].map(([l, v]) => (
            <div key={l}>
              <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: "rgba(255,255,255,0.45)", marginBottom: 4 }}>{l}</div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "#fff", fontVariantNumeric: "tabular-nums" }}>{v}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* This period */}
      <Card style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 16 }}>This period — {month}</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16 }}>
          {[
            { l: "Tours rewritten", v: `${toursUsed.toLocaleString()} / ${fmtQuota(toursTotal)}`, sub: `${pct(toursUsed, toursTotal).toFixed(0)}% used` },
            { l: "API calls",       v: `${apiUsed.toLocaleString()} / ${fmtQuota(apiTotal)}`,     sub: `${pct(apiUsed, apiTotal).toFixed(0)}% used` },
            { l: "Amount due",      v: `$${(price + overage).toLocaleString(undefined, { maximumFractionDigits: 2 })}`, sub: overage > 0 ? `Includes $${overage.toLocaleString()} overage` : "Plan fee, no overage" },
          ].map(({ l, v, sub }) => (
            <div key={l} style={{ padding: "14px 16px", background: T.bg, borderRadius: 8, border: `1px solid ${T.line}` }}>
              <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, fontWeight: 600, marginBottom: 6 }}>{l}</div>
              <div style={{ fontSize: 18, fontWeight: 700, color: T.ink, fontVariantNumeric: "tabular-nums" }}>{v}</div>
              <div style={{ fontSize: 11, color: T.muted2, marginTop: 4 }}>{sub}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* Plan comparison — from shared.membership_plans via /v1/billing */}
      {plans.length > 0 && (
        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 16 }}>Compare plans</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
            {plans.map(p => {
              const active = p.plan_name === plan;
              return (
                <div key={p.plan_name} style={{ padding: 14, borderRadius: 10, border: `${active ? 2 : 1}px solid ${active ? T.gold : T.line}`, background: active ? "#FFFDF7" : T.bg, display: "flex", flexDirection: "column" }}>
                  <div style={{ fontSize: 13, fontWeight: 700, color: active ? T.amber : T.ink, marginBottom: 4 }}>{titleCase(p.plan_name)}</div>
                  <div style={{ fontSize: 20, fontWeight: 600, color: T.ink, marginBottom: 10, fontVariantNumeric: "tabular-nums" }}>
                    {p.price_usd_monthly != null ? <>${p.price_usd_monthly.toLocaleString()}<span style={{ fontSize: 11, color: T.muted, fontWeight: 400 }}>/mo</span></> : "Custom"}
                  </div>
                  {[`${fmtQuota(p.tours_quota_monthly)} tours / month`, `${fmtQuota(p.api_calls_quota_monthly)} API calls`, p.rate_limit_rpm ? `${p.rate_limit_rpm.toLocaleString()} RPM` : "Custom rate limit"].map(feat => (
                    <div key={feat} style={{ fontSize: 11.5, color: T.muted, marginBottom: 4, display: "flex", gap: 6, alignItems: "center" }}>
                      <Check size={12} color={T.green} style={{ flexShrink: 0 }} />{feat}
                    </div>
                  ))}
                  <div style={{ marginTop: "auto", paddingTop: 10 }}>
                    {active
                      ? <div style={{ fontSize: 11, fontWeight: 700, color: T.green, textAlign: "center", display: "flex", alignItems: "center", justifyContent: "center", gap: 4 }}><Check size={12} /> Current plan</div>
                      : <Btn size="sm" onClick={() => setContactPlan(p.plan_name)} style={{ width: "100%" }}>Switch</Btn>}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      )}
    </div>
  );
}

// ─── Settings ─────────────────────────────────────────────────────────────────
export function SettingsTab() {
  const { tenantName, planTier } = usePortalShell();
  const [contact, setContact] = useState(false);

  const row: React.CSSProperties = {
    display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16,
    padding: "13px 0", borderBottom: `1px solid ${T.line2}`,
  };
  const last: React.CSSProperties = { ...row, borderBottom: "none" };
  const label = (t: string, sub?: string) => (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 13, fontWeight: 500, color: T.ink }}>{t}</div>
      {sub && <div style={{ fontSize: 11.5, color: T.muted2, marginTop: 2 }}>{sub}</div>}
    </div>
  );

  async function signOut() {
    // AA-427: tenant cookies are httpOnly — cleared server-side.
    try { await fetch("/api/auth/tenant-logout", { method: "POST" }); } catch { /* redirect regardless */ }
    window.location.href = "/tenant-login";
  }

  return (
    <div style={{ maxWidth: 600, fontFamily: sans }}>
      {contact && (
        <ContactModal
          heading="Update account details"
          body="Company name, contacts and API keys are managed by the Adventure Asia team for now. Email us with the change and we will update it."
          subject={`Account change — ${tenantName}`}
          onClose={() => setContact(false)}
        />
      )}

      <PageHeader title="Settings" sub={`Account details for ${tenantName}.`} />

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 6 }}>Account</div>
          <div style={row}>{label("Company", tenantName)}</div>
          <div style={row}>{label("Plan", titleCase(planTier))}<a href="/portal/billing" style={{ fontSize: 12, color: T.ink3, fontWeight: 500, textDecoration: "none" }}>View billing →</a></div>
          <div style={last}>
            {label("Need to change something?", "Company name, contacts or a new API key")}
            <Btn size="sm" onClick={() => setContact(true)}><Mail size={13} /> Contact us</Btn>
          </div>
        </Card>

        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 6 }}>API access</div>
          <div style={last}>
            {label("API key", "Shown once when it was issued. Use it to call the Adventure Asia API.")}
            <a href="/portal/api" style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: T.ink3, fontWeight: 500, textDecoration: "none", whiteSpace: "nowrap" }}>
              <KeyRound size={13} /> API docs →
            </a>
          </div>
        </Card>

        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 6 }}>Session</div>
          <div style={last}>
            {label("Sign out", "Ends your session on this browser.")}
            <Btn size="sm" variant="danger" onClick={signOut}><LogOut size={13} /> Sign out</Btn>
          </div>
        </Card>
      </div>
    </div>
  );
}
