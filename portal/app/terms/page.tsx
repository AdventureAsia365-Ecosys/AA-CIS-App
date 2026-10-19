// portal/app/terms/page.tsx
// AA-885 — /terms. adventure.asia has no terms page yet (only /privacy-policy exists), and the
// partner terms are not approved (AA-820). This page states that and gives SUPPORT_EMAIL (the contact
// address adventure.asia itself publishes). It is NOT legal text: replace it with a redirect to the real
// document (next.config.ts, like /privacy) once that document exists.
import type { Metadata } from "next";
import { BRAND, FONT_DISPLAY, FONT_SANS, LOGO_SRC, SUPPORT_EMAIL } from "../_brand/tokens";
import { PRIVACY_PATH } from "@/lib/hosts";

export const metadata: Metadata = { title: "Terms — Adventure Asia Partner Portal" };

export default function TermsPage() {
  return (
    <div data-theme="light" style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--bg-primary)", fontFamily: FONT_SANS, padding: 16, boxSizing: "border-box" }}>
      <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 16, padding: 32, width: "100%", maxWidth: 480, boxSizing: "border-box" }}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={LOGO_SRC} alt="Adventure Asia" width={52} height={33} style={{ width: 52, height: "auto", display: "block", marginBottom: 16 }} />
        <h1 style={{ fontFamily: FONT_DISPLAY, fontWeight: 600, fontSize: 20, color: "var(--text-primary)", margin: "0 0 12px" }}>
          Partner terms
        </h1>
        <p style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.6, margin: "0 0 12px" }}>
          The terms for the Adventure Asia Partner Portal are being finalised and will be published here.
        </p>
        <p style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.6, margin: "0 0 20px" }}>
          Questions in the meantime:{" "}
          <a href={`mailto:${SUPPORT_EMAIL}`} style={{ color: BRAND.accentDeep }}>{SUPPORT_EMAIL}</a>
        </p>
        <div style={{ display: "flex", gap: 16, fontSize: 12 }}>
          <a href={PRIVACY_PATH} style={{ color: "var(--text-muted)", textDecoration: "none" }}>Privacy policy</a>
          <a href="/tenant-login" style={{ color: "var(--text-muted)", textDecoration: "none" }}>Partner login</a>
        </div>
      </div>
    </div>
  );
}
