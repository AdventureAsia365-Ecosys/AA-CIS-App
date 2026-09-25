"use client";
// AA-638 — getting-started checklist on the Dashboard, driven by the tenant's real state (no
// stored "onboarding" flags): brand set up, first tour rewritten, first post written, WordPress
// connected. Hides itself once everything is done; can be dismissed per browser.
import { useEffect, useState } from "react";
import Link from "next/link";
import { Check, ArrowRight, X } from "lucide-react";
import { T, Card, CardHead } from "./ui";

type Item = { key: string; title: string; sub: string; href: string; cta: string; done: boolean };

const DISMISS_KEY = "cis_portal_getting_started_dismissed";

type Json = Record<string, unknown> | null;

async function json(url: string): Promise<Json> {
  try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch { return null; }
}

export default function GettingStarted({ catalogCount }: { catalogCount: number }) {
  const [items, setItems] = useState<Item[] | null>(null);
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try { return typeof window !== "undefined" && localStorage.getItem(DISMISS_KEY) === "1"; } catch { return false; }
  });

  useEffect(() => {
    Promise.all([
      json("/api/tenant/admin/brand-identity"),
      json("/api/tenant/v1/content-writing/reviews"),
      json("/api/tenant/v1/integrations/wordpress"),
    ]).then(([brand, reviews, wp]) => {
      const brandDone = !!(brand && (String(brand.system_prompt ?? "").trim() || String(brand.core_idea ?? "").trim()));
      const reviewList = reviews?.data;
      const postsDone = Array.isArray(reviewList) && reviewList.length > 0;
      setItems([
        { key: "brand", title: "Set up your brand voice", sub: "Tell us how you sound so every rewrite matches it.", href: "/portal/t0-brand", cta: "Open Brand Identity", done: brandDone },
        { key: "tour", title: "Rewrite your first tour", sub: "Pick a published tour and make it yours.", href: "/portal/t1-rewrite", cta: "Browse tours", done: catalogCount > 0 },
        { key: "post", title: "Write your first social post", sub: "Turn a tour into a ready-to-publish post.", href: "/portal/t7-planning", cta: "Open Social Content", done: postsDone },
        { key: "wp", title: "Connect WordPress", sub: "Publish blog posts to your site in one click.", href: "/portal/t11-publish", cta: "Connect", done: !!wp?.connected },
      ]);
    });
  }, [catalogCount]);

  if (!items || dismissed) return null;
  const doneCount = items.filter(i => i.done).length;
  if (doneCount === items.length) return null;
  const next = items.find(i => !i.done);

  function dismiss() {
    setDismissed(true);
    try { localStorage.setItem(DISMISS_KEY, "1"); } catch { /* ignore */ }
  }

  return (
    <Card>
      <CardHead title={`Get started · ${doneCount} of ${items.length}`} action={
        <button onClick={dismiss} aria-label="Hide getting started" style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2, display: "flex", padding: 0 }}>
          <X size={15} />
        </button>
      } />
      <div style={{ height: 6, borderRadius: 999, background: T.line2, overflow: "hidden", marginBottom: 14 }}>
        <div style={{ width: `${(doneCount / items.length) * 100}%`, height: "100%", background: T.gold, borderRadius: 999, transition: "width .4s ease" }} />
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {items.map(i => {
          const isNext = i.key === next?.key;
          return (
            <Link key={i.key} href={i.href} style={{
              display: "grid", gridTemplateColumns: "22px minmax(0,1fr) auto", alignItems: "center", gap: 10,
              padding: "9px 10px", margin: "0 -10px", borderRadius: 8, textDecoration: "none",
              background: isNext ? T.goldTint : "transparent",
            }}>
              <span style={{
                width: 20, height: 20, borderRadius: "50%", display: "grid", placeItems: "center",
                background: i.done ? T.green : "transparent", border: i.done ? "none" : `1.5px solid ${isNext ? T.gold : T.line}`,
              }}>
                {i.done && <Check size={12} color="#fff" strokeWidth={3} />}
              </span>
              <span style={{ minWidth: 0 }}>
                <span style={{ display: "block", fontSize: 13, fontWeight: 600, color: i.done ? T.muted : T.ink, textDecoration: i.done ? "line-through" : "none" }}>{i.title}</span>
                {!i.done && <span style={{ display: "block", fontSize: 11.5, color: T.muted, marginTop: 1 }}>{i.sub}</span>}
              </span>
              {!i.done && isNext && (
                <span style={{ fontSize: 12, fontWeight: 600, color: T.goldDeep, display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}>
                  {i.cta} <ArrowRight size={13} />
                </span>
              )}
            </Link>
          );
        })}
      </div>
    </Card>
  );
}
