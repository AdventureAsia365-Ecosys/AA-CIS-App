"use client";
import { useState } from "react";
import { BRAND, BTN_PRIMARY_TEXT, BTN_RADIUS, FONT_DISPLAY, FONT_SANS, LOGO_SRC } from "../_brand/tokens";
import { useRouter } from "next/navigation";
import { Key, Loader2 } from "lucide-react";

export default function TenantLoginPage() {
  const [apiKey, setApiKey]     = useState("");
  const [error, setError]       = useState("");
  const [loading, setLoading]   = useState(false);
  const router = useRouter();

  const login = async () => {
    if (!apiKey.trim()) {
      setError("Please enter your API key");
      return;
    }
    setLoading(true);
    setError("");

    try {
      // AA-427: goes through the same-origin BFF route (not the ECS API
      // directly) so the JWT cookies can be minted httpOnly by the response
      // Set-Cookie header — nothing left for client JS to store itself.
      const res = await fetch(`/api/auth/tenant-login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey.trim() }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail ?? "Invalid API key");
        return;
      }

      router.push("/portal");
    } catch {
      setError("Connection error — please try again");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ minHeight:"100vh", display:"flex", alignItems:"center", justifyContent:"center", background:"var(--bg-primary)", fontFamily: FONT_SANS }}>
      <div style={{ background:"var(--bg-card)", border:"1px solid var(--border)", borderRadius:16, padding:40, width:380 }}>
        <div style={{ display:"flex", alignItems:"center", gap:12, marginBottom:8 }}>
          {/* AA-605 — real Adventure Asia logo (was a gold "AA" tile). */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={LOGO_SRC} alt="Adventure Asia" width={52} height={33} style={{ width:52, height:"auto", display:"block" }} />
          <div>
            <div style={{ fontFamily: FONT_DISPLAY, fontWeight:600, color:"var(--text-primary)", fontSize:18 }}>Partner Portal</div>
            <div style={{ fontSize:11, color:"var(--text-muted)" }}>Adventure Asia B2B</div>
          </div>
        </div>
        <p style={{ fontSize:13, color:"var(--text-secondary)", marginBottom:24 }}>
          Enter your API key to access the tenant portal.
        </p>

        <div style={{ marginBottom:16 }}>
          <label style={{ fontSize:11, fontWeight:600, color:"var(--text-muted)", textTransform:"uppercase" as const, letterSpacing:1, display:"block", marginBottom:8 }}>
            API Key
          </label>
          <div style={{ position:"relative" }}>
            <Key size={13} style={{ position:"absolute", left:12, top:"50%", transform:"translateY(-50%)", color:"var(--text-muted)" }} />
            <input
              type="password"
              value={apiKey}
              onChange={e => { setApiKey(e.target.value); setError(""); }}
              placeholder="wl_live_sk_..."
              onKeyDown={e => e.key === "Enter" && !loading && login()}
              disabled={loading}
              style={{
                width:"100%", padding:"10px 12px 10px 34px",
                background:"var(--bg-primary)",
                border:`1px solid ${error ? BRAND.danger : "var(--border)"}`,
                borderRadius:8, color:"var(--text-primary)", fontSize:13, outline:"none",
                opacity: loading ? 0.6 : 1,
              }}
            />
          </div>
          {error && <div style={{ fontSize:12, color:BRAND.danger, marginTop:6 }}>{error}</div>}
        </div>

        <button
          onClick={login}
          disabled={loading}
          style={{
            width:"100%", padding:12,
            background: loading ? "var(--border)" : "var(--brand-gold)",
            border:"none", borderRadius:BTN_RADIUS, color:"white", fontFamily: FONT_SANS, ...BTN_PRIMARY_TEXT,
            fontSize:13, fontWeight:600, cursor: loading ? "not-allowed" : "pointer",
            display:"flex", alignItems:"center", justifyContent:"center", gap:8,
          }}
        >
          {loading ? (
            <>
              <Loader2 size={14} style={{ animation:"spin 1s linear infinite" }} />
              Verifying...
            </>
          ) : "Access Portal"}
        </button>



        <div style={{ marginTop:16, textAlign:"center" as const }}>
          <a href="/login" style={{ fontSize:12, color:"var(--text-muted)", textDecoration:"none" }}>
            ← Staff login
          </a>
        </div>
      </div>
    </div>
  );
}
