"use client";
// AA-638 — daily usage for the current month (from /v1/billing `daily`), drawn as a small area
// sparkline with the latest day emphasised. Scale is 0..max of the series; the axis labels name
// only values the series actually reaches (first/last day, peak).
import { useId } from "react";
import { T } from "./ui";

export type DailyPoint = { day: string; api_calls: number; rewrites: number };

export default function UsageSparkline({ data, metric, label }: {
  data: DailyPoint[]; metric: "api_calls" | "rewrites"; label: string;
}) {
  const gid = useId().replace(/:/g, "");
  if (!data || data.length < 2) return null;
  const W = 280, H = 56, PAD_T = 6, PAD_B = 4;
  const vals = data.map(d => Number(d[metric]) || 0);
  const max = Math.max(...vals);
  const total = vals.reduce((a, b) => a + b, 0);
  const x = (i: number) => (i / (vals.length - 1)) * W;
  const y = (v: number) => PAD_T + (max > 0 ? (1 - v / max) : 1) * (H - PAD_T - PAD_B);
  const line = vals.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${W},${H} L0,${H} Z`;
  const last = vals.length - 1;
  const fmtDay = (iso: string) => new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });

  return (
    <figure style={{ margin: 0 }} aria-label={`${label}: ${total.toLocaleString()} this month`}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
        <figcaption style={{ fontSize: 11, color: T.muted }}>{label} per day</figcaption>
        <span style={{ fontSize: 11, color: T.muted2, fontVariantNumeric: "tabular-nums" }}>peak {max.toLocaleString()}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none" role="img" style={{ display: "block", overflow: "visible" }}>
        <defs>
          <linearGradient id={`g${gid}`} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={T.gold} stopOpacity="0.28" />
            <stop offset="100%" stopColor={T.gold} stopOpacity="0" />
          </linearGradient>
        </defs>
        <line x1="0" x2={W} y1={H - PAD_B} y2={H - PAD_B} stroke={T.line2} strokeWidth="1" vectorEffect="non-scaling-stroke" />
        <path d={area} fill={`url(#g${gid})`} stroke="none" />
        <path d={line} fill="none" stroke={T.gold} strokeWidth="1.75" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        <circle cx={x(last)} cy={y(vals[last])} r="3.2" fill={T.gold} stroke="#fff" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10.5, color: T.muted2, marginTop: 4 }}>
        <span>{fmtDay(data[0].day)}</span>
        <span>today · {vals[last].toLocaleString()}</span>
      </div>
    </figure>
  );
}
