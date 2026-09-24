"use client";
// app/(tenant)/portal/_components/PlaceholderTabs.tsx — v2
// Settings: functional toggles + inline edit
// Billing: Upgrade Plan opens contact modal

import { useState } from "react";
import { T, serif, sans, Card } from "./ui";

// ─── Activity Log ─────────────────────────────────────────────────────────────
export function ActivityLogTab({ activity }: {
  activity: { id: string; status: string; edit_source: string; tour_name: string; country: string | null; created_at: string }[]
}) {
  const fmtTime = (iso: string | null | undefined): string => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  };
  const sc = (s: string) =>
    s === "approved" ? { bg: T.greenSoft, color: T.green } :
    s === "rejected" ? { bg: T.redSoft, color: T.red } :
    { bg: T.amberSoft, color: T.amber };

  return (
    <div style={{ maxWidth: 760 }}>
      <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>Activity Log</h2>
      <p style={{ fontSize: 13, color: T.muted, marginBottom: 24 }}>All rewrite and catalog actions for WanderLux Travel.</p>
      {activity.length === 0 ? (
        <Card><div style={{ textAlign: "center", padding: 40, color: T.muted2 }}>No activity yet.</div></Card>
      ) : (
        <Card style={{ padding: 0, overflow: "hidden" }}>
          {activity.map((a, i) => {
            const s = sc(a.status);
            return (
              <div key={a.id} style={{ display: "grid", gridTemplateColumns: "36px 1fr auto auto", alignItems: "center", gap: 14, padding: "14px 20px", borderBottom: i < activity.length - 1 ? `1px solid ${T.line2}` : "none" }}>
                <div style={{ width: 36, height: 36, borderRadius: 8, background: s.bg, color: s.color, display: "grid", placeItems: "center", fontSize: 14, fontWeight: 700 }}>
                  {a.status === "approved" ? "✓" : a.status === "rejected" ? "✗" : "↻"}
                </div>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 500, color: T.ink }}>{a.tour_name || "Tour"}</div>
                  <div style={{ fontSize: 11, color: T.muted, marginTop: 2 }}>
                    {a.edit_source === "ai_generated" ? "AI Generated" : "Manually Edited"}
                    {a.country ? ` · ${a.country}` : ""}
                  </div>
                </div>
                <span style={{ fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999, textTransform: "uppercase", letterSpacing: "0.04em", background: s.bg, color: s.color }}>{a.status}</span>
                <span style={{ fontSize: 11, color: T.muted2, whiteSpace: "nowrap" }}>{fmtTime(a.created_at)}</span>
              </div>
            );
          })}
        </Card>
      )}
    </div>
  );
}

// ─── Upgrade Modal ────────────────────────────────────────────────────────────
function UpgradeModal({ plan, onClose }: { plan: string; onClose: () => void }) {
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)", zIndex: 999, display: "flex", alignItems: "center", justifyContent: "center" }}
      onClick={onClose}>
      <div style={{ background: T.card, borderRadius: 16, padding: 36, maxWidth: 460, width: "90%", boxShadow: "0 20px 60px rgba(0,0,0,0.25)" }}
        onClick={e => e.stopPropagation()}>
        <div style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: T.ink, marginBottom: 10, letterSpacing: "-0.01em" }}>
          Upgrade to {plan}
        </div>
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, marginBottom: 24 }}>
          Plan changes are handled by the Adventure Asia team. Contact us and we'll get you upgraded within 24 hours.
        </p>
        <div style={{ display: "flex", gap: 12, flexDirection: "column" }}>
          <a href="mailto:admin@adventureasia.com?subject=Upgrade to Business Plan — WanderLux Travel"
            style={{ display: "block", padding: "13px 20px", background: T.gold, color: T.ink, borderRadius: 8, fontSize: 14, fontWeight: 700, textDecoration: "none", textAlign: "center" }}>
            ✉ Email us to upgrade
          </a>
          <a href="https://adventureasia.com/contact" target="_blank" rel="noreferrer"
            style={{ display: "block", padding: "11px 20px", background: T.bg, border: `1px solid ${T.line}`, color: T.ink3, borderRadius: 8, fontSize: 13, fontWeight: 500, textDecoration: "none", textAlign: "center" }}>
            Book a call with our team →
          </a>
        </div>
        <button onClick={onClose} style={{ marginTop: 16, width: "100%", padding: "8px 0", background: "none", border: "none", color: T.muted2, cursor: "pointer", fontSize: 12, fontFamily: sans }}>
          Cancel
        </button>
      </div>
    </div>
  );
}

// ─── Billing ──────────────────────────────────────────────────────────────────
export function BillingTab({ billing }: { billing: any }) {
  const [showUpgrade, setShowUpgrade] = useState(false);
  const [targetPlan, setTargetPlan]   = useState("Business");

  const plan       = billing?.plan_tier ?? "growth";
  const price      = billing?.price_usd_monthly ?? 799;
  const toursUsed  = billing?.tours_rewritten ?? 0;
  const toursTotal = billing?.tours_quota_monthly ?? 200;
  const llmCost    = billing?.llm_cost_usd ?? 0;
  const month      = billing?.billing_month ?? "—";

  function upgrade(planName: string) {
    setTargetPlan(planName);
    setShowUpgrade(true);
  }

  return (
    <div style={{ maxWidth: 720 }}>
      {showUpgrade && <UpgradeModal plan={targetPlan} onClose={() => setShowUpgrade(false)} />}

      <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>Billing</h2>
      <p style={{ fontSize: 13, color: T.muted, marginBottom: 24 }}>Current plan and usage for billing period {month}.</p>

      {/* Current plan */}
      <Card dark style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.14em", color: "rgba(255,255,255,0.5)", marginBottom: 8 }}>Current Plan</div>
            <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 32, fontWeight: 600, color: "#fff", letterSpacing: "-0.02em" }}>
              {plan.charAt(0).toUpperCase() + plan.slice(1)}
            </div>
            <div style={{ fontSize: 14, color: "rgba(255,255,255,0.65)", marginTop: 4 }}>${price.toLocaleString()} / month · billed monthly</div>
          </div>
          <button onClick={() => upgrade("Business")} style={{ background: T.gold, color: T.ink, border: 0, fontWeight: 700, fontSize: 13, padding: "9px 18px", borderRadius: 8, cursor: "pointer", fontFamily: sans }}>
            Upgrade Plan →
          </button>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16, marginTop: 20, paddingTop: 16, borderTop: "1px solid rgba(255,255,255,0.08)" }}>
          {[["Tours / Month", toursTotal.toLocaleString()], ["API Calls / Month", "20,000"], ["API Rate Limit", "300 RPM"]].map(([l, v]) => (
            <div key={l}>
              <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: "rgba(255,255,255,0.45)", marginBottom: 4 }}>{l}</div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "#fff" }}>{v}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* This period */}
      <Card style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 16 }}>This Period — {month}</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 16 }}>
          {[
            { l: "Tours Rewritten", v: `${toursUsed} / ${toursTotal}`, sub: `${((toursUsed / toursTotal) * 100).toFixed(0)}% used` },
            { l: "LLM Cost",        v: `$${llmCost.toFixed(4)}`,        sub: "~$0.018 per tour · Bedrock" },
            { l: "Platform Fee",    v: `$${price.toLocaleString()}`,     sub: "Invoiced monthly" },
          ].map(({ l, v, sub }) => (
            <div key={l} style={{ padding: "14px 16px", background: T.bg, borderRadius: 8, border: `1px solid ${T.line}` }}>
              <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, fontWeight: 600, marginBottom: 6 }}>{l}</div>
              <div style={{ fontSize: 18, fontWeight: 700, color: T.ink }}>{v}</div>
              <div style={{ fontSize: 11, color: T.muted2, marginTop: 4 }}>{sub}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* Plan comparison */}
      <Card>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 16 }}>Compare Plans</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
          {[
            { name: "Starter",    price: "$299",   tours: "50",   rpm: "60",    active: false },
            { name: "Growth",     price: "$799",   tours: "200",  rpm: "300",   active: true  },
            { name: "Business",   price: "$1,499", tours: "500",  rpm: "1,000", active: false },
            { name: "Enterprise", price: "Custom", tours: "∞",    rpm: "∞",     active: false },
          ].map(p => (
            <div key={p.name} style={{ padding: "14px", borderRadius: 10, border: `${p.active ? 2 : 1}px solid ${p.active ? T.gold : T.line}`, background: p.active ? "#FFFDF7" : T.bg }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: p.active ? T.amber : T.ink, marginBottom: 4 }}>{p.name}</div>
              <div style={{ fontFamily: serif, fontSize: 20, fontWeight: 500, color: T.ink, marginBottom: 10 }}>
                {p.price}<span style={{ fontSize: 11, color: T.muted }}>/mo</span>
              </div>
              {[`${p.tours} tours/mo`, `${p.rpm} RPM`].map(feat => (
                <div key={feat} style={{ fontSize: 11, color: T.muted, marginBottom: 4, display: "flex", gap: 6 }}>
                  <span style={{ color: T.green }}>✓</span>{feat}
                </div>
              ))}
              {p.active
                ? <div style={{ marginTop: 10, fontSize: 11, fontWeight: 700, color: T.green, textAlign: "center" }}>✓ Current</div>
                : <button onClick={() => upgrade(p.name)} style={{ marginTop: 10, width: "100%", padding: "6px 0", borderRadius: 6, border: `1px solid ${T.line}`, background: T.card, fontSize: 11, fontWeight: 600, color: T.ink3, cursor: "pointer", fontFamily: sans, transition: "border-color .15s" }}
                    onMouseEnter={e => (e.currentTarget as HTMLElement).style.borderColor = T.gold}
                    onMouseLeave={e => (e.currentTarget as HTMLElement).style.borderColor = T.line}>
                    Upgrade →
                  </button>
              }
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

// ─── Settings ─────────────────────────────────────────────────────────────────
export function SettingsTab() {
  // Account state
  const [editingName, setEditingName]   = useState(false);
  const [editingEmail, setEditingEmail] = useState(false);
  const [tenantName, setTenantName]     = useState("WanderLux Travel");
  const [email, setEmail]               = useState("sara@wanderlux.com");
  const [nameVal, setNameVal]           = useState("WanderLux Travel");
  const [emailVal, setEmailVal]         = useState("sara@wanderlux.com");
  const [keyRotated, setKeyRotated]     = useState(false);
  const [saved, setSaved]               = useState<string | null>(null);

  // Notification toggles
  const [notifications, setNotifications] = useState({
    rewrite_complete: true,
    quality_alert: true,
    weekly_digest: false,
  });

  // Defaults
  const [language, setLanguage] = useState("EN-US");
  const [seoMode, setSeoMode]   = useState("Standard");
  const [langOpen, setLangOpen] = useState(false);
  const [seoOpen, setSeoOpen]   = useState(false);

  function saveAndFlash(msg: string) {
    setSaved(msg);
    setTimeout(() => setSaved(null), 2500);
  }

  function rotateKey() {
    if (confirm("Are you sure? Your existing API key will stop working immediately.")) {
      setKeyRotated(true);
      saveAndFlash("API key rotated — check your email for the new key.");
    }
  }

  const inputStyle: React.CSSProperties = {
    padding: "7px 10px", border: `1px solid ${T.gold}`, borderRadius: 6,
    fontSize: 13, fontFamily: sans, outline: "none", color: T.ink,
    background: "#fff", width: 220,
  };

  const rowStyle: React.CSSProperties = {
    display: "flex", alignItems: "center", justifyContent: "space-between",
    padding: "13px 0", borderBottom: `1px solid ${T.line2}`,
  };

  const lastRowStyle: React.CSSProperties = { ...rowStyle, borderBottom: "none" };

  return (
    <div style={{ maxWidth: 560, fontFamily: sans }}>
      {/* Save toast */}
      {saved && (
        <div style={{ position: "fixed", top: 20, right: 24, zIndex: 999, padding: "10px 18px", background: T.green, borderRadius: 8, color: "#fff", fontSize: 13, fontWeight: 600, boxShadow: "0 4px 20px rgba(0,0,0,0.15)" }}>
          ✓ {saved}
        </div>
      )}

      <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>Settings</h2>
      <p style={{ fontSize: 13, color: T.muted, marginBottom: 24 }}>Account preferences for WanderLux Travel.</p>

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>

        {/* Account */}
        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 14 }}>Account</div>

          {/* Tenant Name */}
          <div style={rowStyle}>
            <div>
              <div style={{ fontSize: 13, fontWeight: 500, color: T.ink }}>Tenant Name</div>
              {editingName
                ? <input value={nameVal} onChange={e => setNameVal(e.target.value)} autoFocus style={{ ...inputStyle, marginTop: 4 }} />
                : <div style={{ fontSize: 11.5, color: T.muted2, marginTop: 2 }}>{tenantName}</div>
              }
            </div>
            {editingName
              ? <div style={{ display: "flex", gap: 8 }}>
                  <Pill color="green" onClick={() => { setTenantName(nameVal); setEditingName(false); saveAndFlash("Tenant name updated."); }}>Save</Pill>
                  <Pill color="gray" onClick={() => { setNameVal(tenantName); setEditingName(false); }}>Cancel</Pill>
                </div>
              : <Pill color="gold" onClick={() => setEditingName(true)}>Edit</Pill>
            }
          </div>

          {/* Email */}
          <div style={rowStyle}>
            <div>
              <div style={{ fontSize: 13, fontWeight: 500, color: T.ink }}>Primary Email</div>
              {editingEmail
                ? <input value={emailVal} onChange={e => setEmailVal(e.target.value)} autoFocus style={{ ...inputStyle, marginTop: 4 }} />
                : <div style={{ fontSize: 11.5, color: T.muted2, marginTop: 2 }}>{email}</div>
              }
            </div>
            {editingEmail
              ? <div style={{ display: "flex", gap: 8 }}>
                  <Pill color="green" onClick={() => { setEmail(emailVal); setEditingEmail(false); saveAndFlash("Email updated."); }}>Save</Pill>
                  <Pill color="gray" onClick={() => { setEmailVal(email); setEditingEmail(false); }}>Cancel</Pill>
                </div>
              : <Pill color="gold" onClick={() => setEditingEmail(true)}>Change</Pill>
            }
          </div>

          {/* API Key */}
          <div style={lastRowStyle}>
            <div>
              <div style={{ fontSize: 13, fontWeight: 500, color: T.ink }}>API Key</div>
              <div style={{ fontSize: 11.5, color: keyRotated ? T.green : T.muted2, marginTop: 2 }}>
                {keyRotated ? "✓ New key sent to your email" : "•".repeat(24)}
              </div>
            </div>
            <Pill color={keyRotated ? "gray" : "gold"} onClick={keyRotated ? undefined : rotateKey}>
              {keyRotated ? "Rotated" : "Rotate"}
            </Pill>
          </div>
        </Card>

        {/* Notifications */}
        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 14 }}>Notifications</div>
          {[
            { key: "rewrite_complete" as const, label: "Email on rewrite complete",  sub: "Notified when a batch finishes processing" },
            { key: "quality_alert"    as const, label: "Email on quality score < 7.0", sub: "HITL review trigger notifications" },
            { key: "weekly_digest"    as const, label: "Weekly usage digest",          sub: "Summary of quota, spend, and catalog stats" },
          ].map(({ key, label, sub }, i, arr) => (
            <div key={key} style={i < arr.length - 1 ? rowStyle : lastRowStyle}>
              <div>
                <div style={{ fontSize: 13, fontWeight: 500, color: T.ink }}>{label}</div>
                <div style={{ fontSize: 11.5, color: T.muted2, marginTop: 2 }}>{sub}</div>
              </div>
              <Toggle value={notifications[key]} onChange={v => {
                setNotifications(p => ({ ...p, [key]: v }));
                saveAndFlash(`${label} ${v ? "enabled" : "disabled"}.`);
              }} />
            </div>
          ))}
        </Card>

        {/* Defaults */}
        <Card>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 14 }}>Defaults</div>

          <div style={rowStyle}>
            <div>
              <div style={{ fontSize: 13, fontWeight: 500, color: T.ink }}>Default Rewrite Language</div>
              <div style={{ fontSize: 11.5, color: T.muted2, marginTop: 2 }}>{language}</div>
            </div>
            <div style={{ position: "relative" }}>
              <Pill color="gold" onClick={() => setLangOpen(p => !p)}>Change</Pill>
              {langOpen && (
                <div style={{ position: "absolute", right: 0, top: 36, background: T.card, border: `1px solid ${T.line}`, borderRadius: 8, boxShadow: "0 4px 16px rgba(0,0,0,0.1)", zIndex: 20, overflow: "hidden", minWidth: 120 }}>
                  {["EN-US", "EN-GB"].map(l => (
                    <button key={l} onClick={() => { setLanguage(l); setLangOpen(false); saveAndFlash(`Default language set to ${l}.`); }}
                      style={{ display: "block", width: "100%", padding: "10px 16px", textAlign: "left", background: language === l ? T.goldTint : "none", border: "none", fontSize: 13, color: language === l ? T.amber : T.ink, fontWeight: language === l ? 600 : 400, cursor: "pointer", fontFamily: sans }}>
                      {l} {language === l && "✓"}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div style={lastRowStyle}>
            <div>
              <div style={{ fontSize: 13, fontWeight: 500, color: T.ink }}>Default SEO Mode</div>
              <div style={{ fontSize: 11.5, color: T.muted2, marginTop: 2 }}>{seoMode}</div>
            </div>
            <div style={{ position: "relative" }}>
              <Pill color="gold" onClick={() => setSeoOpen(p => !p)}>Change</Pill>
              {seoOpen && (
                <div style={{ position: "absolute", right: 0, top: 36, background: T.card, border: `1px solid ${T.line}`, borderRadius: 8, boxShadow: "0 4px 16px rgba(0,0,0,0.1)", zIndex: 20, overflow: "hidden", minWidth: 140 }}>
                  {["Standard", "Aggressive", "Minimal"].map(m => (
                    <button key={m} onClick={() => { setSeoMode(m); setSeoOpen(false); saveAndFlash(`Default SEO mode set to ${m}.`); }}
                      style={{ display: "block", width: "100%", padding: "10px 16px", textAlign: "left", background: seoMode === m ? T.goldTint : "none", border: "none", fontSize: 13, color: seoMode === m ? T.amber : T.ink, fontWeight: seoMode === m ? 600 : 400, cursor: "pointer", fontFamily: sans }}>
                      {m} {seoMode === m && "✓"}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </Card>

        <div style={{ padding: "12px 0", textAlign: "center" }}>
          <button
            onClick={() => {
              if (confirm("Sign out of all active sessions?")) {
                // AA-427: tenant cookies are httpOnly now — clear server-side.
                fetch("/api/auth/tenant-logout", { method: "POST" }).finally(() => {
                  window.location.href = "/tenant-login";
                });
              }
            }}
            style={{ fontSize: 13, color: T.red, background: T.redSoft, border: `1px solid ${T.redBorder}`, borderRadius: 8, padding: "9px 20px", cursor: "pointer", fontFamily: sans, fontWeight: 600 }}>
            Sign Out of All Sessions
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Sub-components ───────────────────────────────────────────────────────────
function Pill({ children, color, onClick }: {
  children: React.ReactNode;
  color: "gold" | "green" | "gray";
  onClick?: () => void;
}) {
  const styles = {
    gold:  { bg: T.goldTint, border: T.goldSoft, text: T.amber },
    green: { bg: T.greenSoft, border: "#86EFAC", text: T.green },
    gray:  { bg: T.line2, border: T.line, text: T.muted },
  }[color];
  return (
    <button onClick={onClick} disabled={!onClick}
      style={{ fontSize: 12, fontWeight: 600, padding: "5px 13px", borderRadius: 6, border: `1px solid ${styles.border}`, background: styles.bg, color: styles.text, cursor: onClick ? "pointer" : "default", fontFamily: sans }}>
      {children}
    </button>
  );
}

function Toggle({ value, onChange }: { value: boolean; onChange: (v: boolean) => void }) {
  return (
    <button onClick={() => onChange(!value)} style={{
      width: 42, height: 24, borderRadius: 12, border: "none", cursor: "pointer", padding: 2,
      background: value ? T.gold : "#D1D5DB", transition: "background .2s", position: "relative", flexShrink: 0,
    }}>
      <div style={{
        width: 20, height: 20, borderRadius: "50%", background: "#fff",
        position: "absolute", top: 2, left: value ? 18 : 2, transition: "left .2s",
        boxShadow: "0 1px 3px rgba(0,0,0,0.2)",
      }} />
    </button>
  );
}
