"use client";
// AA-638 — skeleton placeholders shaped like the content they stand in for, so a page keeps its
// layout while data loads instead of flashing a centred spinner. Shimmer respects
// prefers-reduced-motion.
import { T } from "./ui";

const CSS = `
@keyframes sk-shimmer { 0% { background-position: -400px 0 } 100% { background-position: 400px 0 } }
.sk { background: linear-gradient(90deg, ${T.line2} 0px, #F7F3EA 160px, ${T.line2} 320px); background-size: 800px 100%;
      animation: sk-shimmer 1.3s linear infinite; border-radius: 6px; }
@media (prefers-reduced-motion: reduce) { .sk { animation: none; } }
`;

export function SkeletonStyle() {
  return <style>{CSS}</style>;
}

export function Bar({ w = "100%", h = 12, r = 6, style = {} }: { w?: number | string; h?: number; r?: number; style?: React.CSSProperties }) {
  return <div className="sk" aria-hidden style={{ width: w, height: h, borderRadius: r, ...style }} />;
}

function SkCard({ children, dark = false, minH }: { children: React.ReactNode; dark?: boolean; minH?: number }) {
  return (
    <div style={{
      background: dark ? T.ink : T.card, border: `1px solid ${dark ? T.ink : T.line}`, borderRadius: 12,
      padding: "22px 22px", display: "flex", flexDirection: "column", gap: 12, minHeight: minH,
    }}>{children}</div>
  );
}

// Dashboard: header + two card rows + quick actions — same grid as DashboardTab.
export function DashboardSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading dashboard">
      <SkeletonStyle />
      <Bar w={280} h={26} style={{ marginBottom: 10 }} />
      <Bar w={220} h={12} style={{ marginBottom: 24 }} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 18 }}>
        <SkCard minH={240}><Bar w={120} h={10} /><Bar w={160} h={34} style={{ marginTop: 18 }} /><Bar w={200} h={14} /></SkCard>
        <SkCard minH={240}><Bar w={110} h={10} /><Bar h={8} style={{ marginTop: 20 }} /><Bar w="60%" h={10} /><Bar h={8} style={{ marginTop: 14 }} /><Bar w="60%" h={10} /></SkCard>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 18, marginTop: 18 }}>
        <SkCard minH={220}><Bar w={140} h={10} /><Bar w={120} h={34} style={{ marginTop: 14 }} /><Bar h={60} style={{ marginTop: 10 }} /></SkCard>
        <SkCard minH={220}><Bar w={120} h={10} />{[0, 1, 2].map(i => <Bar key={i} h={32} style={{ marginTop: 8 }} />)}</SkCard>
      </div>
    </div>
  );
}

// A list of rows (tours, versions, posts).
export function ListSkeleton({ rows = 6, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div aria-busy="true" aria-label={label} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <SkeletonStyle />
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} style={{ background: T.card, border: `1px solid ${T.line}`, borderRadius: 10, padding: "14px 16px", display: "grid", gridTemplateColumns: "minmax(0,1fr) 80px", gap: 12, alignItems: "center" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <Bar w={`${55 + ((i * 17) % 35)}%`} h={13} />
            <Bar w={`${30 + ((i * 11) % 25)}%`} h={10} />
          </div>
          <Bar h={22} r={999} />
        </div>
      ))}
    </div>
  );
}
