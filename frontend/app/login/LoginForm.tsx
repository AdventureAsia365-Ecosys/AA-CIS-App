"use client";
// app/login/LoginForm.tsx — the staff login form, rendered by app/login/page.tsx (server component).
// AA-816 (C4): "Sign in with company email" (Cognito, cis-admin client) first; the legacy
// username/password form only while the server flag LEGACY_ADMIN_LOGIN_ENABLED is on.
// S242: a one-line note under the button says the code arrives by email and names the sign-in host
// (our custom domain staff-auth.lumiguides.it.com, branded like this page).
import { useState } from "react";
import { BRAND, BTN_PRIMARY_TEXT, BTN_RADIUS, FONT_DISPLAY, FONT_SANS, LOGO_SRC } from "../_brand/tokens";
import { User, Lock, Mail } from "lucide-react";

interface LoginFormProps {
  legacyEnabled: boolean;
  cognitoAvailable: boolean;
  /** Host of the Adventure Asia managed-login page (lib/cognito.ts), shown under the button. */
  signInHost: string;
  initialError: string;
}

export default function LoginForm({ legacyEnabled, cognitoAvailable, signInHost, initialError }: LoginFormProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError]       = useState("");
  const [loading, setLoading]   = useState(false);

  const login = async () => {
    // AA-573: guard against a second concurrent submit (rapid double-Enter,
    // or Enter racing the button click) — previously nothing here checked
    // `loading`, so a second call could fire its own /api/auth/login +
    // router.push("/admin/dashboard") while the first was still in flight.
    // Two concurrent client-side navigations to the same route is what the
    // bug report's Network tab captured (2x /login, a stray 307). Mirrors
    // the guard tenant-login/page.tsx already had.
    if (loading) return;
    if (!username || !password) { setError("Enter username and password"); return; }
    setLoading(true);
    setError("");

    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });

      const data = await res.json();

      if (!res.ok) {
        setError(data.detail || "Invalid username or password");
        setLoading(false);
        return;
      }

      // AA-656: no login returns a token any more (the JWT lives only in the httpOnly
      // cis_admin_token cookie). AA-816: the cis_role / cis_user display cookies are set by the
      // server (api/auth/login/route.ts); access is decided from the verified token.

      // AA-573: a full browser navigation instead of router.push(). The
      // client-side App Router transition depends on middleware.ts NOT
      // intervening on the very next request — but middleware's own
      // verifyAdminToken() re-checks the JWT it was just issued via an
      // uncached /auth/verify-admin round trip (the same uncached-round-trip
      // shape AA-551 already fixed for /api/admin/* proxy calls, just a
      // separate instance of it here) and can race/redirect back to /login.
      // router.push() doesn't await/settle on that outcome, so `loading`
      // never got reset — the button was stuck on "Connecting..." forever
      // with no recovery but a manual refresh. window.location.href makes
      // this a real navigation: whatever the server ultimately serves is
      // what renders, and a fresh page load always leaves the old stuck
      // state behind either way.
      // AA-663 — every staff role lands on the one Dashboard (the legacy /upload page is gone).
      // AA-722 — /admin/dashboard is retired as a page but kept as the login landing target: the
      // middleware now redirects it role-aware (admin → /admin/overview, reviewer/content →
      // /admin/review). Keeping the single target here means the login page needs no role branch —
      // whatever role the session has, middleware picks the right landing page on the next request.
      window.location.href = "/admin/dashboard";
    } catch {
      setError("Network error — check connection");
      setLoading(false);
    }
  };

  return (
    <div data-theme="light" style={{ minHeight:"100vh", display:"flex", alignItems:"center", justifyContent:"center", background:"var(--bg-primary)", fontFamily: FONT_SANS }}>
      <div style={{ background:"var(--bg-card)", border:"1px solid var(--border)", borderRadius:16, padding:40, width:380 }}>
        <div style={{ display:"flex", alignItems:"center", gap:12, marginBottom:32 }}>
          {/* AA-605 — real Adventure Asia logo (was a gold "AA" tile). */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={LOGO_SRC} alt="Adventure Asia" width={52} height={33} style={{ width:52, height:"auto", display:"block" }} />
          <div>
            <div style={{ fontFamily: FONT_DISPLAY, fontWeight:600, color:"var(--text-primary)", fontSize:18 }}>CIS Internal</div>
            <div style={{ fontSize:11, color:"var(--text-muted)" }}>Staff sign-in</div>
          </div>
        </div>

        {/* AA-816: a plain link (full navigation) to the BFF route that starts auth code + PKCE. */}
        {cognitoAvailable ? (
          <a href="/api/auth/signin/cognito"
            style={{ display:"flex", alignItems:"center", justifyContent:"center", gap:8, width:"100%", padding:12, background:"var(--brand-gold)", border:"none", borderRadius:BTN_RADIUS, color:"white", fontSize:13, fontWeight:600, fontFamily: FONT_SANS, ...BTN_PRIMARY_TEXT, textDecoration:"none", cursor:"pointer" }}>
            <Mail size={14} /> Sign in with company email
          </a>
        ) : (
          <div>
            <button type="button" disabled
              style={{ display:"flex", alignItems:"center", justifyContent:"center", gap:8, width:"100%", padding:12, background:"var(--bg-primary)", border:"1px solid var(--border)", borderRadius:BTN_RADIUS, color:"var(--text-muted)", fontSize:13, fontWeight:600, fontFamily: FONT_SANS, cursor:"not-allowed" }}>
              <Mail size={14} /> Sign in with company email
            </button>
            <div style={{ fontSize:11, color:"var(--text-muted)", marginTop:6, textAlign:"center" as const }}>
              Company sign-in works on aa-cis.lumiguides.it.com only.
            </div>
          </div>
        )}
        {/* AA-816 (S242): say up front what happens next, so the hand-off to the sign-in page is expected. */}
        {cognitoAvailable && (
          <div style={{ fontSize:11, color:"var(--text-muted)", marginTop:8, textAlign:"center" as const, lineHeight:1.5 }}>
            No password needed: a one-time sign-in code arrives by email. Sign-in opens on {signInHost}.
          </div>
        )}
        {initialError && !error && <div role="alert" style={{ fontSize:12, color:BRAND.danger, marginTop:10 }}>{initialError}</div>}

        {legacyEnabled && (
          <>
            <div style={{ display:"flex", alignItems:"center", gap:10, margin:"22px 0 16px" }}>
              <div style={{ flex:1, height:1, background:"var(--border)" }} />
              <div style={{ fontSize:11, color:"var(--text-muted)", textTransform:"uppercase" as const, letterSpacing:1 }}>or username</div>
              <div style={{ flex:1, height:1, background:"var(--border)" }} />
            </div>
            <div style={{ marginBottom:14 }}>
              <label style={{ fontSize:11, fontWeight:600, color:"var(--text-muted)", textTransform:"uppercase" as const, letterSpacing:1, display:"block", marginBottom:8 }}>Username</label>
              <div style={{ position:"relative" }}>
                <User size={13} style={{ position:"absolute", left:12, top:"50%", transform:"translateY(-50%)", color:"var(--text-muted)" }} />
                <input type="text" value={username} onChange={e => { setUsername(e.target.value); setError(""); }}
                  placeholder="Username"
                  onKeyDown={e => e.key === "Enter" && !loading && login()}
                  style={{ width:"100%", padding:"10px 12px 10px 34px", background:"var(--bg-primary)", border:`1px solid ${error ? BRAND.danger : "var(--border)"}`, borderRadius:8, color:"var(--text-primary)", fontSize:13, outline:"none" }} />
              </div>
            </div>

            <div style={{ marginBottom:20 }}>
              <label style={{ fontSize:11, fontWeight:600, color:"var(--text-muted)", textTransform:"uppercase" as const, letterSpacing:1, display:"block", marginBottom:8 }}>Password</label>
              <div style={{ position:"relative" }}>
                <Lock size={13} style={{ position:"absolute", left:12, top:"50%", transform:"translateY(-50%)", color:"var(--text-muted)" }} />
                <input type="password" value={password} onChange={e => { setPassword(e.target.value); setError(""); }}
                  placeholder="••••••••"
                  onKeyDown={e => e.key === "Enter" && !loading && login()}
                  style={{ width:"100%", padding:"10px 12px 10px 34px", background:"var(--bg-primary)", border:`1px solid ${error ? BRAND.danger : "var(--border)"}`, borderRadius:8, color:"var(--text-primary)", fontSize:13, outline:"none" }} />
              </div>
              {error && <div style={{ fontSize:12, color:BRAND.danger, marginTop:6 }}>{error}</div>}
            </div>

            <button onClick={login} disabled={loading}
              style={{ width:"100%", padding:12, background:"var(--brand-gold)", border:"none", borderRadius:BTN_RADIUS, color:"white", fontSize:13, fontWeight:600, fontFamily: FONT_SANS, ...BTN_PRIMARY_TEXT, cursor: loading ? "not-allowed" : "pointer", opacity: loading ? 0.7 : 1 }}>
              {loading ? "Connecting..." : "Login"}
            </button>
          </>
        )}

        <div style={{ marginTop:16, textAlign:"center" as const }}>
          <a href="/tenant-login" style={{ fontSize:12, color:"var(--text-muted)", textDecoration:"none" }}>B2B Tenant login →</a>
        </div>
      </div>
    </div>
  );
}
