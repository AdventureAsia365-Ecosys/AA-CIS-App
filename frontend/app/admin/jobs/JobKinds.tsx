"use client";
// app/admin/jobs/JobKinds.tsx — AA-721 "Job kinds" table. Every registered kind from
// /admin/job-runner/summary (today 10) with its concurrency / max_attempts / expected_seconds and
// the per-status job count for the last 30 days (from `counts`). Kinds with 0 jobs are still listed
// so the registry is visible in full. Clicking a count filters the main list by (kind, status).

import { DataTable, Eyebrow, K, LinkButton, StatusBadge, mono, useT, type ColumnDef } from "../../_kit";
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

  const num = (v: React.ReactNode, color?: string) => <span style={{ fontFamily: mono, fontSize: 12, color }}>{v}</span>;
  const columns: ColumnDef<Kind, unknown>[] = [
    { id: "kind", header: t("colKind"), accessorKey: "kind", cell: ({ row }) => num(row.original.kind, K.ink3) },
    { id: "concurrency", header: t("maxAtATime"), accessorKey: "concurrency", meta: { align: "right" },
      cell: ({ row }) => num(row.original.concurrency) },
    { id: "attempts", header: t("attempts"), accessorKey: "max_attempts", meta: { align: "right" },
      cell: ({ row }) => num(row.original.max_attempts) },
    { id: "slow", header: t("slowAfter"), accessorFn: (k) => k.expected_seconds ?? 0, meta: { align: "right" },
      cell: ({ row }) => num(row.original.expected_seconds ? fmtSeconds(row.original.expected_seconds) : "—", K.muted) },
    ...STATUSES.map((s): ColumnDef<Kind, unknown> => ({
      id: `n:${s}`, header: s, accessorFn: (k) => byKey.get(`${k.kind}:${s}`) ?? 0, meta: { align: "right" },
      cell: ({ row }) => {
        const n = byKey.get(`${row.original.kind}:${s}`) ?? 0;
        return n > 0 ? (
          <LinkButton size="sm" weight="regular" onClick={() => onPick(row.original.kind, s)}
            title={t("filterTitle", row.original.kind, s)} style={{ fontFamily: mono }}>
            {n}
          </LinkButton>
        ) : num(0, K.muted2);
      },
    })),
    { id: "total", header: t("totalCol"), accessorFn: (k) => totalFor(k.kind), meta: { align: "right" },
      cell: ({ row }) => num(totalFor(row.original.kind), K.ink) },
  ];

  return (
    <div style={{ marginTop: 16 }}>
      <Eyebrow>{t("tabKinds")}</Eyebrow>
      <div style={{ fontSize: 12, color: K.muted, marginBottom: 8 }}>
        {t("kindsIntro", kinds.length)}
      </div>
      <DataTable<Kind>
        tableId="jobs-kinds"
        data={kinds}
        columns={columns}
        getRowId={(k) => k.kind}
        searchable={false}
        paginate={false}
        compact
        tableMinWidth={760}
      />
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
