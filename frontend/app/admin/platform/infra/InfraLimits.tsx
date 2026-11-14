"use client";
// app/admin/platform/infra/InfraLimits.tsx — Platform › Infra limits (read-only).
//
// The limits that stay in Terraform on purpose (root thresholds audit S279, section 4): WAF rate rules and the country
// allow-list, API Gateway throttles, Lambda reserved concurrency, the AWS budgets of the main account and the alarm
// thresholds. No app role may write them, so a change is an infrastructure pull request; the admin-editable layer
// above them is Platform › Settings and the Spend page. One section per group: what the group does, why it is not a
// setting, the editable layer above it (with a link), then a table of its values with each value's plain label, its
// readable value and unit, and the Terraform source that sets it.
//
// API (backend GET /admin/platform/infra, api/routers/admin_platform_ops.py, ops.read; BFF
// /api/admin/platform/infra, admin only — lib/rbac.ts API_READ_RULES "platform"). The values come from the generated
// alarm catalog page (OPS-07) of the Docs bundle; the provenance block names it, the infrastructure commit and the
// publish time. Labels and explanations come from the API in English and Vietnamese. Pure helpers:
// lib/platformInfra.ts. Kit only.

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { ExternalLink, Server } from "lucide-react";
import { useMemo, useState } from "react";
import {
  DataTable, DateText, ErrorState, Input, K, PageFrame, PageHeader, Section, TableSkeleton, apiGet, mono, useLang,
  useT, type ColumnDef,
} from "../../../_kit";
import {
  INFRA_PATH, docsLink, filterGroups, pick, prettyValue, shortSha, type InfraGroup, type InfraItem, type InfraLang,
  type InfraResp,
} from "@/lib/platformInfra";
import { INFRA } from "./messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.5 } as const;
const linkStyle = { color: K.accentDeep, fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 6 } as const;

function useInfraT() {
  return useT(INFRA);
}
type IT = ReturnType<typeof useInfraT>;

function unitText(item: InfraItem, t: IT): string {
  // Every InfraUnit has a unit_<code> message (messages.ts); "text" is empty.
  return String(t(`unit_${item.unit}` as "unit_text"));
}

function GroupSection({ group, lang }: { group: InfraGroup; lang: InfraLang }) {
  const t = useInfraT();
  const columns = useMemo<ColumnDef<InfraItem, unknown>[]>(() => [
    { id: "limit", header: t("colLimit"), accessorFn: (i) => pick(i.label, lang),
      cell: ({ row: { original: i } }) => (
        <div style={{ display: "grid", gap: 3, minWidth: 0 }}>
          <span style={{ fontSize: 13, color: K.ink, lineHeight: 1.45 }}>
            {i.known ? pick(i.label, lang) : t("noExplanation")}
          </span>
          <span style={{ ...small, fontFamily: mono, wordBreak: "break-all" }}>{i.key}</span>
        </div>
      ) },
    { id: "value", header: t("colValue"), accessorKey: "value",
      cell: ({ row: { original: i } }) => {
        const pretty = prettyValue(i, lang);
        const unit = unitText(i, t);
        return (
          <div style={{ display: "grid", gap: 2 }}>
            <strong style={{ fontFamily: mono, fontSize: 13, wordBreak: "break-word" }}>{pretty ?? i.value}</strong>
            <span style={small}>{pretty ? `${i.value}${unit ? ` ${unit}` : ""}` : unit}</span>
          </div>
        );
      } },
    { id: "source", header: t("colSource"), accessorFn: (i) => i.source ?? "",
      cell: ({ row: { original: i } }) => (
        <span style={{ ...small, fontFamily: mono, wordBreak: "break-word" }}>{i.source ?? "—"}</span>
      ) },
  ], [t, lang]);
  const editableLink = group.editable_href
    ? <Link href={group.editable_href} style={linkStyle}>
      {group.editable_href === "/admin/spend" ? t("spendLink") : t("settingsLink")}</Link>
    : null;
  return (
    <Section title={pick(group.title, lang)} description={pick(group.what, lang)} padded={false}
      testId={`infra-group-${group.group}`}>
      <div style={{ display: "grid", gap: 6, padding: "14px 20px" }}>
        <div style={small}><strong style={{ color: K.ink }}>{t("whyLabel")}:</strong> {pick(group.why, lang)}</div>
        {group.editable && (
          <div style={small}>
            <strong style={{ color: K.ink }}>{t("editableLabel")}:</strong> {pick(group.editable, lang)}{" "}
            {editableLink}
          </div>
        )}
      </div>
      <DataTable<InfraItem> tableId={`infra-${group.group}`} data={group.items} columns={columns}
        getRowId={(i) => i.key} searchable={false} paginate={false} compact tableMinWidth={760} />
    </Section>
  );
}

function Provenance({ data, lang }: { data: InfraResp; lang: InfraLang }) {
  const t = useInfraT();
  const doc = data.doc;
  const link = docsLink(doc);
  const tf = shortSha(doc.terraform_sha);
  const change = data.groups[0]?.change;
  return (
    <Section title={t("sourceTitle")} testId="infra-source" bodyStyle={{ display: "grid", gap: 8 }}>
      <div style={small}>{t("sourceText")}</div>
      <div style={{ ...small, display: "flex", flexWrap: "wrap", gap: "4px 18px" }}>
        <span>{t("sourceDoc")}:{" "}
          {link ? <Link href={link} style={linkStyle}>{doc.title ?? doc.path} <ExternalLink size={12} /></Link>
            : t("unknown")}</span>
        <span>{t("terraformCommit")}: <span style={{ fontFamily: mono }}>{tf || t("unknown")}</span></span>
        <span>{t("bundleTime")}: {doc.generated_at ? <DateText value={doc.generated_at} withTime /> : t("unknown")}</span>
      </div>
      {change && <div style={small}><strong style={{ color: K.ink }}>{t("howToChange")}:</strong> {pick(change, lang)}</div>}
      {data.missing_keys.length > 0 && (
        <div role="status" style={{ ...small, color: K.amber }} data-testid="infra-stale-keys">
          {t("staleKeys", data.missing_keys.join(", "))}
        </div>
      )}
    </Section>
  );
}

export default function InfraLimits() {
  const t = useInfraT();
  const lang: InfraLang = useLang() === "vi" ? "vi" : "en";
  const q = useQuery({ queryKey: ["platform-infra"], queryFn: () => apiGet<InfraResp>(INFRA_PATH) });
  const [query, setQuery] = useState("");
  const groups = useMemo(() => filterGroups(q.data?.groups ?? [], query, lang), [q.data, query, lang]);
  const missing = q.data?.doc.missing ? q.data.doc.reason ?? "not_published" : null;
  return (
    <PageFrame>
      <PageHeader
        title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><Server size={18} /> {t("title")}</span>}
        description={t("intro")}
      />
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
        {q.isLoading && <TableSkeleton rows={8} cols={3} />}
        {q.isError && <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`}
          onRetry={() => void q.refetch()} />}
        {missing && (
          <Section testId="infra-missing">
            <div style={small}>{t(`missing_${missing}` as "missing_not_published")}</div>
          </Section>
        )}
        {q.data && !missing && (
          <>
            <Provenance data={q.data} lang={lang} />
            <div style={{ maxWidth: 360 }}>
              <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("search")}
                aria-label={t("search")} data-testid="infra-filter" />
            </div>
            {groups.length === 0 && <Section><div style={small}>{t("noMatch")}</div></Section>}
            {groups.map((g) => <GroupSection key={g.group} group={g} lang={lang} />)}
          </>
        )}
      </div>
    </PageFrame>
  );
}
