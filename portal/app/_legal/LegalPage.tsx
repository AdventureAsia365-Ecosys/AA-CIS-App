// portal/app/_legal/LegalPage.tsx
// AA-886 — shell of the portal's legal pages (/privacy, /terms). Public pages (proxy.ts matches only
// /portal/* and /tenant-login), light theme like /tenant-login, and a draft banner that is ALWAYS shown
// while counsel has not approved the text. Portal-owned (not a vendored kit file).
import type { CSSProperties, ReactNode } from "react";
import { BRAND, FONT_DISPLAY, FONT_SANS, LOGO_SRC, SUPPORT_EMAIL } from "../_brand/tokens";
import { PRIVACY_PATH, TERMS_PATH } from "@/lib/hosts";

/** The banner title on every legal page while the text is not approved. */
export const LEGAL_DRAFT_LABEL = "Draft — pending legal review";
export const LEGAL_VERSION = "Draft v0.1";
export const LEGAL_UPDATED = "20 October 2026";

// Ink + underline, not gold: BRAND.accentDeep is 4.47:1 on white and 4.05:1 on the gold tint (below WCAG AA).
const linkStyle: CSSProperties = { color: BRAND.ink, fontWeight: 600, textDecoration: "underline", textUnderlineOffset: 2 };

/** A link in the body text; external links open in a new tab. */
export function A({ href, children }: { href: string; children: ReactNode }) {
  const external = href.startsWith("http");
  return (
    <a href={href} style={linkStyle} {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}>
      {children}
    </a>
  );
}

export function DraftBanner() {
  return (
    <div
      role="note"
      data-testid="legal-draft-banner"
      style={{
        background: BRAND.accentTint,
        border: `1px solid ${BRAND.accentBorder}`,
        borderRadius: 12,
        padding: "12px 16px",
        fontSize: 13,
        lineHeight: 1.6,
        color: BRAND.body,
      }}
    >
      <p style={{ margin: 0, fontWeight: 700, letterSpacing: "0.04em", textTransform: "uppercase", color: BRAND.ink }}>
        {LEGAL_DRAFT_LABEL}
      </p>
      <p style={{ margin: "4px 0 0" }}>
        This is a working draft. Adventure Asia&apos;s legal counsel has not reviewed or approved it, and it may
        change before the Partner Portal leaves its development phase. Parts marked [to be confirmed] are still
        open. Questions: <A href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</A>.
      </p>
    </div>
  );
}

export function LegalSection({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} style={{ paddingTop: 24 }}>
      <h2 id={`${id}-title`} style={{ fontFamily: FONT_DISPLAY, fontWeight: 600, fontSize: 17, color: BRAND.ink, margin: "0 0 8px" }}>
        {title}
      </h2>
      <div className="aa-legal-body" style={{ fontSize: 14, lineHeight: 1.65, color: BRAND.body }}>
        {children}
      </div>
    </section>
  );
}

export default function LegalPage({
  title,
  intro,
  children,
}: {
  title: string;
  intro: ReactNode;
  children: ReactNode;
}) {
  return (
    <div data-theme="light" style={{ minHeight: "100vh", background: "var(--bg-primary)", fontFamily: FONT_SANS, padding: "32px 16px", boxSizing: "border-box" }}>
      {/* Paragraph and list rhythm for the body text (inline styles cannot target descendants). */}
      <style>{`.aa-legal-body p{margin:0 0 10px}.aa-legal-body ul{list-style:disc;margin:0 0 10px;padding-left:20px}.aa-legal-body li{margin:0 0 6px}`}</style>
      <main style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 16, padding: "28px 24px", width: "100%", maxWidth: 760, margin: "0 auto", boxSizing: "border-box" }}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={LOGO_SRC} alt="Adventure Asia" width={52} height={33} style={{ width: 52, height: "auto", display: "block", marginBottom: 16 }} />
        <h1 style={{ fontFamily: FONT_DISPLAY, fontWeight: 600, fontSize: 24, color: BRAND.ink, margin: "0 0 4px" }}>{title}</h1>
        <p style={{ fontSize: 12, color: BRAND.muted, margin: "0 0 16px" }}>
          Adventure Asia Partner Portal · {LEGAL_VERSION} · last updated {LEGAL_UPDATED}
        </p>
        <DraftBanner />
        <div style={{ fontSize: 14, lineHeight: 1.65, color: BRAND.body, marginTop: 16 }}>{intro}</div>
        {children}
        <nav aria-label="Legal" style={{ display: "flex", flexWrap: "wrap", gap: 16, fontSize: 12, marginTop: 28, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
          <a href={PRIVACY_PATH} style={{ color: BRAND.muted, textDecoration: "none" }}>Privacy notice</a>
          <a href={TERMS_PATH} style={{ color: BRAND.muted, textDecoration: "none" }}>Partner terms</a>
          <a href="/tenant-login" style={{ color: BRAND.muted, textDecoration: "none" }}>Partner login</a>
        </nav>
      </main>
    </div>
  );
}
