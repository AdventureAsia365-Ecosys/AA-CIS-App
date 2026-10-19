"use client";
// portal/app/portal/_components/FacebookConnect.tsx — AA-815
//
// Facebook Page connection for T11 (the backend side is AA-462: /v1/integrations/facebook).
// Same shape as WordPressConnect.tsx — a status hook, a connect form that saves then tests, and a
// status card with "Test connection" + Disconnect — used on /portal/t11-publish and
// /portal/t11-publish/connection.
//
// The backend takes a pasted Page ID + Page Access Token (no OAuth login flow): the tenant
// creates the token in Meta's tools. The token never comes back to the browser; the API returns
// only page_id + verification state. Token expiry is not readable without a Meta app token, so
// an expired token shows up as the classified test error ("Invalid or expired Page Access Token").
//
// API: /api/tenant/v1/integrations/facebook (GET status, POST save, DELETE disconnect) +
//      /api/tenant/v1/integrations/facebook/test (POST), through the /api/tenant/[...path] proxy.
//
// AA-831: the connect form's help text names all three permissions and the Live-mode rule, and
// links to the step-by-step guide at /portal/help/facebook (portal/help/facebook/page.tsx).

import Link from "next/link";
import { useState, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, XCircle, Loader2, ArrowRight } from "lucide-react";
import { T, Card, CardHead, Btn, mono } from "./ui";
import { fmtDateTime, inputStyle, labelStyle } from "./WordPressConnect";
import { DisconnectConnection } from "./DisconnectConnection";

/** AA-831 — the tenant guide for getting a Page ID + long-lived Page Access Token. */
export const FACEBOOK_GUIDE_HREF = "/portal/help/facebook";

/** AA-831 — every permission the Page token needs: list the Pages (pages_show_list), read the
 * Page and its feed (pages_read_engagement — the connection test and the read-before-retry feed
 * lookup), create and delete posts (pages_manage_posts — publish and unpublish). */
export const FACEBOOK_PERMISSIONS = ["pages_show_list", "pages_read_engagement", "pages_manage_posts"] as const;

const permStyle: React.CSSProperties = {
  fontFamily: mono, fontSize: 11.5, color: T.ink3, background: T.bg,
  border: `1px solid ${T.line}`, borderRadius: 4, padding: "0 5px", whiteSpace: "nowrap",
};

/** The three permission names as "a, b and c", one inline chip each. */
export function PermissionList() {
  return (
    <>
      {FACEBOOK_PERMISSIONS.map((name, i) => (
        <span key={name}>
          {i > 0 && (i === FACEBOOK_PERMISSIONS.length - 1 ? " and " : ", ")}
          <code style={permStyle}>{name}</code>
        </span>
      ))}
    </>
  );
}

export interface FacebookStatus {
  connected: boolean;
  page_id: string | null;
  connected_at: string | null;
  last_verified_at: string | null;
  last_verify_error: string | null;
}

export function useFacebookStatus() {
  // AA-815 — react-query instead of setState-in-effect; `refresh` refetches in place (the page
  // stays up), `loading` is true only until the first answer.
  const q = useQuery({
    queryKey: ["integration", "facebook"],
    queryFn: () => fetch("/api/tenant/v1/integrations/facebook").then(r => (r.ok ? r.json() : null)) as Promise<FacebookStatus | null>,
  });
  const { refetch } = q;
  const refresh = useCallback(() => { void refetch(); }, [refetch]);
  return { status: q.data ?? null, loading: q.isLoading, refresh };
}

/** Connect / change-credentials form. Save, then test — a token that was never verified would
 * only fail later at publish time, so "Test & Connect" confirms it works first. */
export function FacebookConnectForm({ onConnected }: { onConnected?: () => void }) {
  const [pageId, setPageId] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState<"idle" | "saving" | "testing">("idle");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  async function handleConnect() {
    setError(null);
    setSuccess(false);
    if (!pageId.trim() || !token.trim()) {
      setError("Page ID and Page Access Token are both required.");
      return;
    }

    setBusy(true);
    setStep("saving");
    try {
      const saveRes = await fetch("/api/tenant/v1/integrations/facebook", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ page_id: pageId.trim(), page_access_token: token.trim() }),
      });
      if (!saveRes.ok) {
        const body = await saveRes.json().catch(() => ({}));
        setError(body.detail || "Could not save credentials.");
        return;
      }

      setStep("testing");
      const testRes = await fetch("/api/tenant/v1/integrations/facebook/test", { method: "POST" });
      const testBody = await testRes.json().catch(() => ({}));
      if (testRes.ok && testBody.success) {
        setSuccess(true);
        setToken("");
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
      <CardHead title="Connect Facebook Page" />
      {/* AA-831 — all three permissions, the Live-mode requirement and where each value comes
          from. The names render as separate inline chips with explicit {" "}/{", "} separators:
          the old single <code> run read "pages_manage_postspermission" in the S237 UI check. */}
      <div style={{ fontSize: 13, color: T.muted, marginTop: -8, marginBottom: 18, lineHeight: 1.6 }}>
        <p style={{ margin: "0 0 8px" }}>
          Publish posts to your own Facebook Page. You need two things from Meta:
        </p>
        <ul style={{ margin: "0 0 8px", paddingLeft: 18 }}>
          <li style={{ marginBottom: 4 }}>
            <strong style={{ color: T.ink3 }}>Page ID</strong>{" "}
            — the number under your Page&rsquo;s About &rarr; Page transparency.
          </li>
          <li>
            <strong style={{ color: T.ink3 }}>Page Access Token</strong>{" "}
            — a long-lived Page token with the permissions{" "}
            <PermissionList />.
          </li>
        </ul>
        <p style={{ margin: "0 0 8px" }}>
          Your Meta app must be in <strong style={{ color: T.ink3 }}>Live</strong> mode. While
          it is in Development mode, only you can see the posts.
        </p>
        <Link href={FACEBOOK_GUIDE_HREF} style={{
          display: "inline-flex", alignItems: "center", gap: 5, fontWeight: 600,
          color: T.goldDeep, textDecoration: "none",
        }}>
          Step-by-step guide <ArrowRight size={13} />
        </Link>
      </div>

      <div style={{ marginBottom: 14 }}>
        <label style={labelStyle}>Page ID</label>
        <input style={inputStyle} type="text" inputMode="numeric" placeholder="e.g. 104123456789012"
          value={pageId} onChange={e => setPageId(e.target.value)} disabled={busy} />
      </div>

      <div style={{ marginBottom: 18 }}>
        <label style={labelStyle}>Page Access Token</label>
        <input style={inputStyle} type="password" placeholder="EAAB…" autoComplete="off"
          value={token} onChange={e => setToken(e.target.value)} disabled={busy} />
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
          <CheckCircle2 size={15} /> Connected — Facebook Page verified successfully.
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

/** Connection summary + re-test, and Disconnect when `onDisconnected` is given. */
export function FacebookStatusCard({ status, onRetest, onDisconnected }: {
  status: FacebookStatus; onRetest: () => void; onDisconnected?: () => void;
}) {
  const [testing, setTesting] = useState(false);

  async function retest() {
    setTesting(true);
    try {
      await fetch("/api/tenant/v1/integrations/facebook/test", { method: "POST" });
    } finally {
      setTesting(false);
      onRetest();
    }
  }

  const verified = status.last_verified_at && !status.last_verify_error;

  return (
    <Card>
      <CardHead title="Facebook Page" action={
        <Btn size="sm" variant="secondary" onClick={retest} disabled={testing}>
          {testing ? "Testing…" : "Test connection"}
        </Btn>
      } />
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        {verified ? <CheckCircle2 size={16} color={T.green} /> : <XCircle size={16} color={T.amber} />}
        <span style={{ fontSize: 14, fontWeight: 600, color: T.ink }}>
          Page {status.page_id}
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
          <DisconnectConnection integration="facebook" label="Facebook" onDisconnected={onDisconnected} />
        </div>
      )}
    </Card>
  );
}
