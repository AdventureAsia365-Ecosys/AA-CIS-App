"use client";
import { useState } from "react";
import { BRAND, BTN_PRIMARY_TEXT, BTN_RADIUS, FONT_DISPLAY, FONT_SANS, LOGO_SRC } from "../_brand/tokens";
import { User, Lock } from "lucide-react";

export default function LoginPage() {
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

      // AA-435: /api/auth/login no longer returns `token` for admin/reviewer
      // logins (the JWT lives only in the httpOnly cis_admin_token cookie
      // set server-side) — this branch now only ever fires for the content
      // role, whose login is a separate, already-accepted-risk path (AA-253)
      // this fix deliberately left untouched. See docs/implementation-notes/
      // AA-435.md.
      if (data.token) {
        document.cookie = `cis_api_token=${encodeURIComponent(data.token)}; path=/; max-age=86400`;
      }
      document.cookie = `cis_role=${data.role}; path=/; max-age=86400`;
      document.cookie = `cis_user=${encodeURIComponent(data.name)}; path=/; max-age=86400`;

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
      window.location.href = data.role === "admin" ? "/admin/dashboard" : "/upload";
    } catch {
      setError("Network error — check connection");
      setLoading(false);
    }
  };

  return (
    <div style={{ minHeight:"100vh", display:"flex", alignItems:"center", justifyContent:"center", background:"var(--bg-primary)", fontFamily: FONT_SANS }}>
      <div style={{ background:"var(--bg-card)", border:"1px solid var(--border)", borderRadius:16, padding:40, width:380 }}>
        <div style={{ display:"flex", alignItems:"center", gap:12, marginBottom:32 }}>
          {/* AA-605 — real Adventure Asia logo (was a gold "AA" tile). */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={LOGO_SRC} alt="Adventure Asia" width={52} height={33} style={{ width:52, height:"auto", display:"block" }} />
          <div>
            <div style={{ fontFamily: FONT_DISPLAY, fontWeight:600, color:"var(--text-primary)", fontSize:18 }}>CIS Internal</div>
            <div style={{ fontSize:11, color:"var(--text-muted)" }}>Staff Login</div>
          </div>
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

        <div style={{ marginTop:16, textAlign:"center" as const }}>
          <a href="/tenant-login" style={{ fontSize:12, color:"var(--text-muted)", textDecoration:"none" }}>B2B Tenant login →</a>
        </div>
      </div>
    </div>
  );
}
