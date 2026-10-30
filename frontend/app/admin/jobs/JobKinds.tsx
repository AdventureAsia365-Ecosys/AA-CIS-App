"use client";
// app/admin/jobs/JobKinds.tsx — AA-721 "Job kinds" table. Every registered kind from
// /admin/job-runner/summary (today 10) with its concurrency / max_attempts / expected_seconds and
// the per-status job count for the last 30 days (from `counts`). Kinds with 0 jobs are still listed
// so the registry is visible in full. Clicking a count filters the main list by (kind, status).

import { StatusBadge, useT, Eyebrow, K, Section, mono } from "../../_kit";
import { JOBS } from "./messages";
import { STATUSES, fmtSeconds, type Count, type Kind } from "./jobsShared";

export default function JobKinds({
  kinds, counts, onPick,
}: {
  kinds: Kind[];
  counts: Count[];
  onPick: (kind: string, status: string) => void;
}) {
  const t = useT(JOBS);
  if (kinds.length === 0) return null;

  // counts[(kind, status)] -> n, built once for O(1) lookup per cell.
  const byKey = new Map<string, number>();
  for (const c of counts) byKey.set(`${c.kind}:${c.status}`, (byKey.get(`${c.kind}:${c.status}`) ?? 0) + c.n);
  const totalFor = (kind: string) => STATUSES.reduce((a, s) => a + (byKey.get(`${kind}:${s}`) ?? 0), 0);

  const cell: React.CSSProperties = { padding: "7px 10px", fontSize: 12, borderBottom: `1px solid ${K.line2}`, whiteSpace: "nowrap" };
  const head: React.CSSProperties = { padding: "6px 10px", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: K.muted, textAlign: "left", borderBottom: `1px solid ${K.line}` };

  return (
    <div style={{ marginTop: 16 }}>
      <Eyebrow>{t("tabKinds")}</Eyebrow>
      <div style={{ fontSize: 12, color: K.muted, marginBottom: 8 }}>
        {t("kindsIntro", kinds.length)}
      </div>
      <Section padded={false} style={{ overflow: "hidden" }}>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 760 }}>
            <thead>
              <tr>
                <th style={head}>{t("colKind")}</th>
                <th style={{ ...head, textAlign: "right" }}>{t("maxAtATime")}</th>
                <th style={{ ...head, textAlign: "right" }}>{t("attempts")}</th>
                <th style={{ ...head, textAlign: "right" }}>{t("slowAfter")}</th>
                {STATUSES.map(s => <th key={s} style={{ ...head, textAlign: "right" }}>{s}</th>)}
                <th style={{ ...head, textAlign: "right" }}>{t("totalCol")}</th>
              </tr>
            </thead>
            <tbody>
              {kinds.map(k => (
                <tr key={k.kind}>
                  <td style={{ ...cell, fontFamily: mono, color: K.ink3 }}>{k.kind}</td>
                  <td style={{ ...cell, fontFamily: mono, textAlign: "right" }}>{k.concurrency}</td>
                  <td style={{ ...cell, fontFamily: mono, textAlign: "right" }}>{k.max_attempts}</td>
                  <td style={{ ...cell, fontFamily: mono, textAlign: "right", color: K.muted }}>
                    {k.expected_seconds ? fmtSeconds(k.expected_seconds) : "—"}
                  </td>
                  {STATUSES.map(s => {
                    const n = byKey.get(`${k.kind}:${s}`) ?? 0;
                    return (
                      <td key={s} style={{ ...cell, textAlign: "right" }}>
                        {n > 0 ? (
                          <button
                            onClick={() => onPick(k.kind, s)}
                            title={t("filterTitle", k.kind, s)}
                            style={{ border: "none", background: "transparent", cursor: "pointer", fontFamily: mono, fontSize: 12, color: K.accentDeep, padding: 0 }}
                          >
                            {n}
                          </button>
                        ) : (
                          <span style={{ fontFamily: mono, color: K.muted2 }}>0</span>
                        )}
                      </td>
                    );
                  })}
                  <td style={{ ...cell, fontFamily: mono, textAlign: "right", color: K.ink }}>{totalFor(k.kind)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 8, fontSize: 11, color: K.muted }}>
        {STATUSES.map(s => (
          <span key={s} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <StatusBadge status={s} />
          </span>
        ))}
      </div>
    </div>
  );
}
