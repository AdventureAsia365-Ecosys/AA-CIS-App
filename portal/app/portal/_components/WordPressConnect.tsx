"use client";
// portal/app/portal/_components/WordPressConnect.tsx — AA-457 [T11 PR1]
//
// Shared between /portal/t11-publish (inline first-run form when not connected) and
// /portal/t11-publish/connection (manage/change credentials + re-test) — Option 3+2 from
// AA-456's STEP0 report §9: an inline connect flow for the common first-time case, backed by a
// real standalone page for later edits, sharing one form component rather than building the
// credentials UI twice.
//
// AA-815: FacebookConnect.tsx sits next to this one (same shape: status hook, connect form,
// status card) and both cards carry a Disconnect action (DisconnectConnection.tsx).
//
// API: /api/tenant/v1/integrations/wordpress (GET status, POST save, DELETE disconnect) +
//      /api/tenant/v1/integrations/wordpress/test (POST) — proxied through the generic
//      /api/tenant/[...path] route (Bearer cis_tenant_token attached server-side), same
//      convention every other real tenant-portal fetch already uses.

import { useState, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, XCircle, Loader2 } from "lucide-react";
import { T, sans, Card, CardHead, Btn } from "./ui";
import { DisconnectConnection } from "./DisconnectConnection";

export interface WordPressStatus {
  connected: boolean;
  site_url: string | null;
  connected_at: string | null;
  last_verified_at: string | null;
  last_verify_error: string | null;
}

export function useWordPressStatus() {
  // AA-815 — react-query instead of setState-in-effect; `refresh` refetches in place (the page
  // stays up), `loading` is true only until the first answer.
  const q = useQuery({
    queryKey: ["integration", "wordpress"],
    queryFn: () => fetch("/api/tenant/v1/integrations/wordpress").then(r => (r.ok ? r.json() : null)) as Promise<WordPressStatus | null>,
  });
  const { refetch } = q;
  const refresh = useCallback(() => { void refetch(); }, [refetch]);
  return { status: q.data ?? null, loading: q.isLoading, refresh };
}

export function fmtDateTime(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export const inputStyle: React.CSSProperties = {
  width: "100%", padding: "9px 12px", background: T.bg, border: `1px solid ${T.line}`,
  borderRadius: 8, color: T.body, fontSize: 13, outline: "none", fontFamily: sans,
  boxSizing: "border-box",
};

export const labelStyle: React.CSSProperties = {
  display: "block", fontSize: 11, fontWeight: 600, textTransform: "uppercase",
  letterSpacing: "0.1em", color: T.muted, marginBottom: 6,
};

/** The connect / change-credentials form. Save then test run sequentially — a saved credential
 * the tenant never verifies is worse than no credential (silent future publish failures), so
 * "Test & Connect" always confirms it works before declaring success. */
export function WordPressConnectForm({ onConnected }: { onConnected?: () => void }) {
  const [siteUrl, setSiteUrl] = useState("");
  const [username, setUsername] = useState("");
  const [appPassword, setAppPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState<"idle" | "saving" | "testing">("idle");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  async function handleConnect() {
    setError(null);
    setSuccess(false);

    if (!siteUrl.trim() || !username.trim() || !appPassword.trim()) {
      setError("All three fields are required.");
      return;
    }

    setBusy(true);
    setStep("saving");
    try {
      const saveRes = await fetch("/api/tenant/v1/integrations/wordpress", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ wp_url: siteUrl.trim(), username: username.trim(), app_password: appPassword }),
      });
      if (!saveRes.ok) {
        const body = await saveRes.json().catch(() => ({}));
        setError(body.detail || "Could not save credentials.");
        return;
      }

      setStep("testing");
      const testRes = await fetch("/api/tenant/v1/integrations/wordpress/test", { method: "POST" });
      const testBody = await testRes.json().catch(() => ({}));
      if (testRes.ok && testBody.success) {
        setSuccess(true);
        setAppPassword("");
        onConnected?.();
      } else {
        setError(testBody.last_verify_error || testBody.detail || "Connection test failed.");
      }
    } catch {
      setError("Network error — please try again.");
    } finally {
      setBusy(false);
      setStep("idle");
    }
  }

  return (
    <Card>
      <CardHead title="Connect WordPress" />
      <p style={{ fontSize: 13, color: T.muted, marginTop: -8, marginBottom: 18, lineHeight: 1.5 }}>
        Publishing writes directly to your own WordPress site. You&rsquo;ll need your site URL and
        a WordPress <strong>Application Password</strong> — generate one under
        Users&nbsp;→&nbsp;Profile&nbsp;→&nbsp;Application Passwords in your WordPress admin.
      </p>

      <div style={{ marginBottom: 14 }}>
        <label style={labelStyle}>Site URL</label>
        <input style={inputStyle} type="text" placeholder="https://yourblog.com"
          value={siteUrl} onChange={e => setSiteUrl(e.target.value)} disabled={busy} />
      </div>

      <div style={{ marginBottom: 14 }}>
        <label style={labelStyle}>Username</label>
        <input style={inputStyle} type="text" placeholder="your WordPress username"
          value={username} onChange={e => setUsername(e.target.value)} disabled={busy} />
      </div>

      <div style={{ marginBottom: 18 }}>
        <label style={labelStyle}>Application Password</label>
        <input style={inputStyle} type="password" placeholder="xxxx xxxx xxxx xxxx xxxx xxxx"
          value={appPassword} onChange={e => setAppPassword(e.target.value)} disabled={busy} />
      </div>

      {error && (
        <div style={{
          display: "flex", alignItems: "flex-start", gap: 8, padding: "10px 12px", marginBottom: 14,
          borderRadius: 8, background: T.redSoft, color: T.red, fontSize: 12.5, lineHeight: 1.5,
        }}>
          <XCircle size={15} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>{error}</span>
        </div>
      )}
      {success && (
        <div style={{
          display: "flex", alignItems: "center", gap: 8, padding: "10px 12px", marginBottom: 14,
          borderRadius: 8, background: T.greenSoft, color: T.green, fontSize: 12.5,
        }}>
          <CheckCircle2 size={15} /> Connected — WordPress verified successfully.
        </div>
      )}

      <Btn variant="primary" onClick={handleConnect} disabled={busy}>
        {busy ? (
          <><Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} />
            {step === "saving" ? "Saving…" : "Testing connection…"}</>
        ) : "Test & Connect"}
      </Btn>
    </Card>
  );
}

/** Read-only connection summary + re-test action, used on the manage-connection page and
 * (once connected) at the top of /portal/t11-publish. */
export function WordPressStatusCard({ status, onRetest, onDisconnected }: {
  status: WordPressStatus; onRetest: () => void;
  /** AA-815 — when given, the card shows a Disconnect action (confirm step) and calls this after. */
  onDisconnected?: () => void;
}) {
  const [testing, setTesting] = useState(false);

  async function retest() {
    setTesting(true);
    try {
      await fetch("/api/tenant/v1/integrations/wordpress/test", { method: "POST" });
    } finally {
      setTesting(false);
      onRetest();
    }
  }

  const verified = status.last_verified_at && !status.last_verify_error;

  return (
    <Card>
      <CardHead title="WordPress Connection" action={
        <Btn size="sm" variant="secondary" onClick={retest} disabled={testing}>
          {testing ? "Testing…" : "Test connection"}
        </Btn>
      } />
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        {verified ? <CheckCircle2 size={16} color={T.green} /> : <XCircle size={16} color={T.amber} />}
        <span style={{ fontSize: 14, fontWeight: 600, color: T.ink }}>
          {status.site_url}
        </span>
      </div>
      <div style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.9 }}>
        <div>Connected: {fmtDateTime(status.connected_at)}</div>
        <div>Last verified: {fmtDateTime(status.last_verified_at)}</div>
      </div>
      {status.last_verify_error && (
        <div style={{
          display: "flex", alignItems: "flex-start", gap: 8, padding: "10px 12px", marginTop: 12,
          borderRadius: 8, background: T.redSoft, color: T.red, fontSize: 12.5, lineHeight: 1.5,
        }}>
          <XCircle size={15} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>{status.last_verify_error}</span>
        </div>
      )}
      {onDisconnected && (
        <div style={{ marginTop: 14 }}>
          <DisconnectConnection integration="wordpress" label="WordPress" onDisconnected={onDisconnected} />
        </div>
      )}
    </Card>
  );
}
