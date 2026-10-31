"use client";
// app/admin/ai-gateway/LlmAvailabilityCard.tsx
// AA-900 — "can the platform call its LLMs right now?" at the top of AI providers › Routing (AA-804 UI part 2; was
// Settings › LLM Models › Stages).
// Reads GET /api/admin/llm-availability: the status comes from real outcomes (answered calls in the call log, failed
// jobs classified by error), never from a test call. Accounts are named through lib/awsAccounts (AA-998).
// Built on the kit only (Section, Badge, DataTable, DateText, tokens).

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Badge, DataTable, DateText, K, Section, Spinner, apiGet, mono, useT, type ColumnDef, type Translator,
} from "../../_kit";
import { MODELS } from "./models.messages";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { accountName, type AccountText, type AwsAccounts } from "@/lib/awsAccounts";
import {
  type FailureClass,
  type LlmAvailability,
  type ProviderRow,
  jobLink,
  sortProviders,
  statusTone,
} from "@/lib/llmAvailability";

type T = Translator<typeof MODELS.en>;

const STATUS_KEY = {
  ok: "availOk",
  degraded: "availDegraded",
  blocked: "availBlocked",
  unknown: "availUnknown",
} as const;

const CAUSE_KEY: Record<FailureClass, "availCauseAccessDenied" | "availCauseQuota" | "availCauseThrottled"> = {
  access_denied: "availCauseAccessDenied",
  quota_exhausted: "availCauseQuota",
  throttled: "availCauseThrottled",
};

const CLASS_KEY: Record<FailureClass, "availClassAccessDenied" | "availClassQuota" | "availClassThrottled"> = {
  access_denied: "availClassAccessDenied",
  quota_exhausted: "availClassQuota",
  throttled: "availClassThrottled",
};

const num = { fontFamily: mono, fontSize: 12.5, color: K.ink } as const;

function providerColumns(t: T, dir: AwsAccounts | undefined, text: AccountText): ColumnDef<ProviderRow, unknown>[] {
  return [
    {
      accessorKey: "provider",
      header: t("availProvider"),
      cell: (c) => (
        <span style={num}>
          {c.row.original.provider}
          {c.row.original.embed && <span style={{ color: K.muted2, fontFamily: "inherit" }}> · {t("availEmbed")}</span>}
        </span>
      ),
    },
    {
      id: "account",
      accessorFn: (r) => accountName(r.account, dir, text),
      header: t("availAccount"),
      cell: (c) => <span style={num}>{accountName(c.row.original.account, dir, text) || "—"}</span>,
    },
    { accessorKey: "calls", header: t("availCalls"), cell: (c) => <span style={num}>{c.row.original.calls}</span> },
    {
      accessorKey: "ok_calls",
      header: t("availAnswered"),
      cell: (c) => <span style={num}>{c.row.original.ok_calls}</span>,
    },
    {
      accessorKey: "last_ok_at",
      header: t("availLastCall"),
      cell: (c) => <DateText value={c.row.original.last_ok_at} withTime relative style={{ fontSize: 12.5 }} />,
    },
  ];
}

export function LlmAvailabilityCard() {
  const t = useT(MODELS);
  const awsAccounts = useAwsAccounts();
  const accountText = useAccountText();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["llm-availability", 24],
    queryFn: () => apiGet<LlmAvailability>("/api/admin/llm-availability?hours=24", { admin: true }),
    refetchInterval: 60_000,
  });
  const columns = useMemo(() => providerColumns(t, awsAccounts, accountText), [t, awsAccounts, accountText]);
  const providers = useMemo(() => sortProviders(data?.providers ?? []), [data]);

  if (isLoading && !data) {
    return <Section title={t("availTitle")} testId="llm-availability"><Spinner /></Section>;
  }
  if (isError || !data) {
    return (
      <Section title={t("availTitle")} testId="llm-availability">
        <div style={{ fontSize: 12.5, color: K.danger }}>{t("availLoadError")}</div>
      </Section>
    );
  }

  const causeLink = jobLink(data.cause?.job_id);
  const causeKey = data.cause ? CAUSE_KEY[data.cause.error_class] : undefined;
  return (
    <Section
      testId="llm-availability"
      title={t("availTitle")}
      description={t("availIntro", data.window_hours)}
      actions={<Badge tone={statusTone(data.status)}>{t(STATUS_KEY[data.status] ?? "availUnknown")}</Badge>}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
        <div style={{ fontSize: 12.5, color: K.muted }}>
          {t("availLastOk")}:{" "}
          {data.last_ok_at ? <DateText value={data.last_ok_at} withTime relative /> : t("availNever")}
        </div>

        {data.cause && causeKey && (
          <div style={{
            fontSize: 12.5, padding: "8px 10px", borderRadius: 8, border: `1px solid ${K.line}`,
            background: K.panel, display: "flex", flexDirection: "column", gap: 4, minWidth: 0, color: K.body,
          }}>
            <span>{t(causeKey)}</span>
            <span style={{ color: K.muted }}>
              {data.cause.kind && <>{t("availCauseJob", data.cause.kind)} · </>}
              <DateText value={data.cause.at} withTime relative />
              {causeLink && <> · <a href={causeLink} style={{ color: K.accentDeep }}>{t("availOpenJob")}</a></>}
            </span>
            {data.cause.error && (
              <code style={{ fontFamily: mono, fontSize: 11, color: K.muted, overflowWrap: "anywhere" }}>
                {data.cause.error}
              </code>
            )}
          </div>
        )}

        <DataTable
          tableId="llm-availability-providers"
          data={providers}
          columns={columns}
          getRowId={(r) => `${r.provider}|${r.account ?? ""}|${r.embed}`}
          paginate={false}
          compact
          tableMinWidth={520}
          emptyTitle={t("availNoCalls")}
        />

        <div style={{ fontSize: 12, color: K.muted2, fontWeight: 600 }}>{t("availFailures")}</div>
        {data.failures.length === 0 ? (
          <div style={{ fontSize: 12.5, color: K.muted }}>{t("availNoFailures")}</div>
        ) : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {data.failures.map((f) => (
              <span key={f.error_class} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12 }}>
                <Badge tone={f.error_class === "throttled" ? "warning" : "danger"} dot={false}>
                  {t(CLASS_KEY[f.error_class])}
                </Badge>
                <span style={{ color: K.muted }}>
                  {t("availJobs", f.jobs)} · <DateText value={f.last_at} withTime relative />
                </span>
              </span>
            ))}
          </div>
        )}
      </div>
    </Section>
  );
}
