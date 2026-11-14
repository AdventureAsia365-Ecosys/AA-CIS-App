"use client";
// app/admin/ai-gateway/AppProfilesCard.tsx — AA-1025 H1a (root ADR 0009 decision 4): the App profiles card of AI
// providers › Providers. One row per app (CIS, TripPlanner, Partner CRM, AA-Booking): its AI switch, bulk pause,
// default daily budget and degrade mode (GET /api/admin/ai-gateway/app-profiles). For CIS the central cap llm/app:cis
// of the Budgets tab is shown next to the default budget, because that cap is the one the gateway enforces.
//
// Edit (a kit Drawer, then a confirm; switching AI off is the destructive confirm and needs a reason) only when GET
// /api/admin/ai-gateway/access says this session may use it (usePageAccess "app_profiles", ai.admin on the API); a
// read-only session sees the values only. The PATCH sends only what changed (lib/aiGateway appProfileBody). A backend
// without the table (migration 345) answers 503: a clean notice instead of an error.
// Built on the kit only (Section, DataTable, Badge, Drawer, Field / Select / Input / Textarea, ConfirmModal, Toast).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import {
  ApiError, Badge, Button, ConfirmModal, DataTable, DateText, Drawer, ErrorState, Field, Input, K, Section, Select,
  Textarea, apiGet, apiSend, mono, useT, useToast, type ColumnDef,
} from "../../_kit";
import {
  APP_PROFILES_API, APP_PROFILES_KEY, DEGRADE_MODES, PROFILE_REASON_MAX, appProfileBody, appProfileDraft,
  appProfilePath, isReadOnlyRefusal, usd, type AppProfile, type AppProfileDraft, type AppProfilesResp,
  type DegradeMode,
} from "@/lib/aiGateway";
import { isTightenOnlyRefusal } from "@/lib/capTighten";
import { DEGRADE, PROFILE_APP, PROFILES } from "./appProfiles.messages";
import { usePageAccess } from "./usePageAccess";
import { useRefusalText } from "./useRefusalText";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
// The table's own minimum width: below it the kit DataTable scrolls inside its card (never the page).
const PROFILE_TABLE_MIN_WIDTH = 900;

function useAppName(): (app: string) => string {
  const ta = useT(PROFILE_APP);
  return (app) => (app in PROFILE_APP.en ? ta(app as keyof typeof PROFILE_APP.en) : app);
}

function useDegradeText(): (mode: string) => string {
  const td = useT(DEGRADE);
  return (mode) => (mode in DEGRADE.en ? td(mode as keyof typeof DEGRADE.en) : mode);
}

function ProfileEditor({ profile, onClose }: { profile: AppProfile; onClose: () => void }) {
  const t = useT(PROFILES);
  const refusal = useRefusalText();
  const toast = useToast();
  const qc = useQueryClient();
  const appName = useAppName();
  const degradeText = useDegradeText();
  const name = appName(profile.app);
  const [draft, setDraft] = useState<AppProfileDraft>(() => appProfileDraft(profile));
  const [confirming, setConfirming] = useState(false);
  const [apiError, setApiError] = useState("");
  const body = appProfileBody(profile, draft);
  const set = (p: Partial<AppProfileDraft>) => setDraft((d) => ({ ...d, ...p }));

  const mut = useMutation({
    mutationFn: () => apiSend(appProfilePath(profile.app), { method: "PATCH", body, admin: true }),
    onSuccess: () => {
      toast.success(t("saved", name));
      qc.invalidateQueries({ queryKey: [...APP_PROFILES_KEY] });
      onClose();
    },
    onError: (e) => {
      setConfirming(false);
      setApiError(isTightenOnlyRefusal(e) ? refusal(e)
        : isReadOnlyRefusal(e) ? t("readOnly") : e instanceof Error ? e.message : String(e));
    },
  });

  const switching = draft.ai_enabled !== profile.ai_enabled;
  const confirmTitle = switching ? (draft.ai_enabled ? t("onTitle", name) : t("offTitle", name)) : t("saveTitle", name);
  const confirmBody = switching ? (draft.ai_enabled ? t("onBody") : t("offBody")) : t("saveBody");
  const confirmLabel = switching ? (draft.ai_enabled ? t("onConfirm") : t("offConfirm")) : t("save");
  const blocked = typeof body === "string" || mut.isPending;

  return (
    <Drawer open onClose={onClose} title={t("editTitle", name)} width={520}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose} disabled={mut.isPending}>{t("cancel")}</Button>
          <Button variant="primary" disabled={blocked} onClick={() => setConfirming(true)} testId="app-profile-save">
            {mut.isPending ? t("saving") : t("save")}
          </Button>
        </div>
      }>
      <div data-testid="app-profile-editor" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Field label={t("fieldAi")}>
          <Select value={draft.ai_enabled ? "on" : "off"} disabled={mut.isPending} data-testid="app-profile-ai"
            onChange={(e) => set({ ai_enabled: e.target.value === "on" })}
            options={[{ value: "on", label: t("aiOnOption") }, { value: "off", label: t("aiOffOption") }]} />
        </Field>
        {!draft.ai_enabled && (
          <Field label={t("fieldReason")} hint={t("reasonHint")} required
            error={body === "reason" && draft.reason.trim() !== "" ? t("reasonBad") : undefined}>
            <Textarea value={draft.reason} rows={3} maxLength={PROFILE_REASON_MAX} disabled={mut.isPending}
              onChange={(e) => set({ reason: e.target.value })} data-testid="app-profile-reason" />
          </Field>
        )}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))", gap: 12 }}>
          <Field label={t("fieldBulk")} hint={t("bulkHint")}>
            <Select value={draft.bulk_paused ? "paused" : "running"} disabled={mut.isPending}
              onChange={(e) => set({ bulk_paused: e.target.value === "paused" })}
              options={[{ value: "running", label: t("bulkRun") }, { value: "paused", label: t("bulkPause") }]} />
          </Field>
          <Field label={t("fieldBudget")} hint={t("budgetHint")} error={body === "budget" ? t("budgetBad") : undefined}>
            <Input inputMode="decimal" value={draft.budget} placeholder={t("noBudget")} disabled={mut.isPending}
              onChange={(e) => set({ budget: e.target.value })} style={{ fontFamily: mono }}
              data-testid="app-profile-budget" />
          </Field>
        </div>
        <Field label={t("fieldDegrade")}>
          <Select value={draft.degrade_mode} disabled={mut.isPending}
            onChange={(e) => set({ degrade_mode: e.target.value as DegradeMode })}
            options={DEGRADE_MODES.map((m) => ({ value: m, label: degradeText(m) }))} />
        </Field>
        {body === "unchanged" && <span style={small}>{t("unchanged")}</span>}
        {apiError && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
      </div>
      <ConfirmModal open={confirming} onClose={() => setConfirming(false)} onConfirm={() => mut.mutate()}
        title={confirmTitle} body={confirmBody} confirmLabel={confirmLabel} busy={mut.isPending}
        destructive={switching && !draft.ai_enabled} testId="app-profile-confirm" />
    </Drawer>
  );
}

export default function AppProfilesCard() {
  const t = useT(PROFILES);
  const appName = useAppName();
  const degradeText = useDegradeText();
  const canEdit = usePageAccess().can("app_profiles");
  const [editing, setEditing] = useState<AppProfile | null>(null);
  const q = useQuery({
    queryKey: [...APP_PROFILES_KEY],
    queryFn: () => apiGet<AppProfilesResp>(APP_PROFILES_API, { admin: true }),
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
    retry: false,
  });
  const notDeployed = q.isError && q.error instanceof ApiError && [404, 501, 503].includes(q.error.status);

  const columns = useMemo<ColumnDef<AppProfile>[]>(() => {
    const cols: ColumnDef<AppProfile>[] = [
      { accessorKey: "app", header: t("colApp"), enableSorting: false,
        cell: (x) => <strong style={{ color: K.ink }}>{appName(x.row.original.app)}</strong> },
      { accessorKey: "ai_enabled", header: t("colAi"), enableSorting: false,
        cell: (x) => {
          const p = x.row.original;
          return (
            <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
              <Badge tone={p.ai_enabled ? "success" : "danger"} style={{ whiteSpace: "nowrap", justifySelf: "start" }}>
                {p.ai_enabled ? t("aiOn") : t("aiOff")}</Badge>
              {!p.ai_enabled && p.disabled_reason && <span style={small}>{p.disabled_reason}</span>}
            </div>
          );
        } },
      { accessorKey: "bulk_paused", header: t("colBulk"), enableSorting: false,
        cell: (x) => (
          <Badge tone={x.row.original.bulk_paused ? "warning" : "neutral"} dot={false} style={{ whiteSpace: "nowrap" }}>
            {x.row.original.bulk_paused ? t("paused") : t("running")}</Badge>
        ) },
      { id: "budget", header: t("colBudget"), enableSorting: false,
        accessorFn: (r) => r.default_daily_budget_usd ?? -1,
        cell: (x) => {
          const p = x.row.original;
          const cap = p.central_cap?.per_day_usd;
          return (
            <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
              <span style={{ fontFamily: mono, color: K.ink }}>
                {p.default_daily_budget_usd == null ? t("noBudget") : usd(p.default_daily_budget_usd, 2)}</span>
              {cap != null && <span style={small}>{t("centralCap", usd(cap, 2))}</span>}
            </div>
          );
        } },
      { accessorKey: "degrade_mode", header: t("colDegrade"), enableSorting: false,
        cell: (x) => <span style={{ color: K.ink }}>{degradeText(x.row.original.degrade_mode)}</span> },
      { accessorKey: "enforced_here", header: t("colApplies"), enableSorting: false,
        cell: (x) => <span style={small}>{x.row.original.enforced_here ? t("appliesHere") : t("appliesLater")}</span> },
      { accessorKey: "updated_at", header: t("colChanged"), enableSorting: false,
        cell: (x) => (x.row.original.updated_at
          ? <span style={small}><DateText value={x.row.original.updated_at} relative /></span>
          : <span style={small}>—</span>) },
    ];
    if (canEdit) {
      cols.push({ id: "actions", header: "", enableSorting: false, enableHiding: false, meta: { align: "right" },
        cell: (x) => (
          <Button variant="ghost" size="sm" onClick={() => setEditing(x.row.original)}
            testId={`app-profile-edit-${x.row.original.app}`}>
            <Pencil size={14} /> {t("edit")}</Button>
        ) });
    }
    return cols;
  }, [t, appName, degradeText, canEdit]);

  return (
    <Section title={t("title")} description={t("help")} padded={false}>
      <div data-testid="ai-gateway-app-profiles">
        {q.isError && !notDeployed ? (
          <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />
        ) : notDeployed ? (
          <div role="status" style={{ ...small, padding: "12px 16px" }}>{t("notDeployed")}</div>
        ) : (
          <DataTable<AppProfile>
            tableId="ai-gateway-app-profiles" data={q.data?.profiles ?? []} columns={columns}
            getRowId={(r) => r.app} loading={q.isLoading} searchable={false} paginate={false} stickyHeader={false}
            tableMinWidth={PROFILE_TABLE_MIN_WIDTH} emptyTitle="—" />
        )}
      </div>
      {editing && <ProfileEditor key={editing.app} profile={editing} onClose={() => setEditing(null)} />}
    </Section>
  );
}
