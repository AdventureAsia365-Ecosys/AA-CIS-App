"use client";
// app/admin/ai-gateway/RouteAlertsCard.tsx — AA-1042 (S268): the Route alerts card of AI gateway › Trace.
//
// One row for the defaults (the * policy row) and one per stage: the fallback alert threshold and its minimum
// requests, the last-model alert, the cost alert and whether content written by a fallback model is held for review
// or only marked (GET /api/admin/ai-gateway/route-policy; backend shared/llm_client/route_watch.py). An admin edits a
// row in a kit Drawer (PUT /route-policy/{stage}, audited; empty = inherit) or resets it to the defaults (DELETE).
// "Run check now" queues the llm_route_watch job (POST /route-watch/run). "Route test" sends 1-6 tiny real requests on
// one stage, optionally with its first model refused for those requests only (POST /route-trace/test): the live
// check of the Trace tab and of the fallback / primary-back alerts. Writes show only when GET
// /api/admin/ai-gateway/access allows them (usePageAccess "route_policy" / "route_test").
// Built on the kit only (Section, DataTable, Badge, Drawer, Field / Input / Select / Textarea, Checkbox, Toast).
// AA-1052 W6: a save or reset that moves the hold switch of a writing-path stage (or of the defaults) asks for the
// evidence (ChangeEvidenceFields: an eval run id or a break-glass reason, lib/editGate.ts); the API refuses it
// without one (422) and while a rerun wave is open (409, shown as the error).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Pencil, Play } from "lucide-react";
import {
  Badge, Button, Checkbox, ConfirmModal, DataTable, Drawer, Field, Input, K, LinkButton, Section, Select, Spinner,
  Textarea, apiGet, apiSend, mono, useT, useToast, type ColumnDef, type Translator,
} from "../../_kit";
import { holdChangeNeedsEvidence } from "@/lib/editGate";
import { isReadOnlyRefusal, usd } from "@/lib/aiGateway";
import {
  DEFAULT_STAGE, ROUTE_KEY, ROUTE_POLICY_API, ROUTE_TEST_API, ROUTE_WATCH_API, TEST_MAX_REQUESTS, policyBody,
  policyDraft, policyPath, rungLabel, type Inherit, type PolicyDraft, type PolicyValues, type RoutePolicyResp,
  type RouteTestResp, type StagePolicy,
} from "@/lib/routeTrace";
import {
  BREAK_GLASS_MIN_CHARS, ChangeEvidenceFields, EMPTY_EVIDENCE, evidenceBody, evidenceReady, type ChangeEvidence,
} from "./ChangeEvidenceFields";
import { RT } from "./routeTrace.messages";
import { usePageAccess } from "./usePageAccess";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const POLICY_KEY = [...ROUTE_KEY, "policy"] as const;

type Row = StagePolicy & { isDefault: boolean };

function PolicyEditor({ row, onClose }: { row: Row; onClose: () => void }) {
  const t = useT(RT);
  const toast = useToast();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<PolicyDraft>(() => policyDraft(row.row));
  const [apiError, setApiError] = useState("");
  const [evidence, setEvidence] = useState<ChangeEvidence>(EMPTY_EVIDENCE);
  const body = policyBody(draft);
  const set = (p: Partial<PolicyDraft>) => setDraft((d) => ({ ...d, ...p }));
  const name = row.isDefault ? t("defaultsRow") : row.stage;
  // AA-1052 W6: the hold switch of a writing-path stage moves → the save carries its evidence.
  const askEvidence = typeof body !== "string"
    && holdChangeNeedsEvidence(row.stage, row.row?.hold_fallback_writes, body.hold_fallback_writes);
  const mut = useMutation({
    mutationFn: () => apiSend(policyPath(row.stage), {
      method: "PUT", body: { ...(body as object), ...(askEvidence ? evidenceBody(evidence) : {}) }, admin: true,
    }),
    onSuccess: () => {
      toast.success(t("saved", name));
      qc.invalidateQueries({ queryKey: [...POLICY_KEY] });
      onClose();
    },
    onError: (e) => setApiError(isReadOnlyRefusal(e) ? t("readOnly") : e instanceof Error ? e.message : String(e)),
  });
  const bad = typeof body === "string" ? body : null;
  const tri = (on: string, off: string) => [
    { value: "inherit", label: t("inherit") }, { value: "on", label: on }, { value: "off", label: off },
  ];
  return (
    <Drawer open onClose={onClose} width={520}
      title={row.isDefault ? t("editDefaultsTitle") : t("editTitle", row.stage)}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose} disabled={mut.isPending}>{t("cancel")}</Button>
          <Button variant="primary" disabled={bad != null || mut.isPending} testId="route-policy-save"
            onClick={() => {
              if (askEvidence && !evidenceReady(evidence)) {
                setApiError(t("evidenceMissing", BREAK_GLASS_MIN_CHARS));
                return;
              }
              mut.mutate();
            }}>{mut.isPending ? t("saving") : t("save")}</Button>
        </div>
      }>
      <div data-testid="route-policy-editor" style={{ display: "grid", gap: 14 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))",
          gap: 12 }}>
          <Field label={t("fieldPct")} hint={t("fieldPctHint")}
            error={bad === "fallback_alert_pct" ? t("invalid") : undefined}>
            <Input inputMode="numeric" value={draft.fallback_alert_pct} disabled={mut.isPending}
              placeholder={String(row.effective.fallback_alert_pct)} style={{ fontFamily: mono }}
              onChange={(e) => set({ fallback_alert_pct: e.target.value })} data-testid="route-policy-pct" />
          </Field>
          <Field label={t("fieldMin")} hint={t("fieldMinHint")}
            error={bad === "min_requests" ? t("invalid") : undefined}>
            <Input inputMode="numeric" value={draft.min_requests} disabled={mut.isPending}
              placeholder={String(row.effective.min_requests)} style={{ fontFamily: mono }}
              onChange={(e) => set({ min_requests: e.target.value })} />
          </Field>
        </div>
        <Field label={t("fieldCost")} hint={t("fieldCostHint")}
          error={bad === "cost_shift_usd" ? t("invalid") : undefined}>
          <Input inputMode="decimal" value={draft.cost_shift_usd} disabled={mut.isPending}
            placeholder={row.effective.cost_shift_usd.toFixed(2)} style={{ fontFamily: mono }}
            onChange={(e) => set({ cost_shift_usd: e.target.value })} />
        </Field>
        <Field label={t("fieldLast")}>
          <Select value={draft.alert_last_rung} disabled={mut.isPending}
            onChange={(e) => set({ alert_last_rung: e.target.value as Inherit })}
            options={tri(t("onOption"), t("offOption"))} />
        </Field>
        <Field label={t("fieldHold")} hint={t("fieldHoldHint")}>
          <Select value={draft.hold_fallback_writes} disabled={mut.isPending} data-testid="route-policy-hold"
            onChange={(e) => set({ hold_fallback_writes: e.target.value as Inherit })}
            options={tri(t("holdOption"), t("markOption"))} />
        </Field>
        <Field label={t("fieldNotes")} error={bad === "notes" ? t("invalid") : undefined}>
          <Textarea rows={2} maxLength={500} value={draft.notes} disabled={mut.isPending}
            onChange={(e) => set({ notes: e.target.value })} />
        </Field>
        {askEvidence && (
          <div>
            <p style={{ ...small, margin: 0 }}>{t("holdEvidenceHint")}</p>
            <ChangeEvidenceFields stage={`policy-${row.stage}`} value={evidence}
              onChange={(next) => { setEvidence(next); setApiError(""); }} />
          </div>
        )}
        {apiError && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
      </div>
    </Drawer>
  );
}

function RouteTestDrawer({ stages, onClose, onOpenRequest }: {
  stages: string[]; onClose: () => void; onOpenRequest: (traceId: string, at: string | null) => void;
}) {
  const t = useT(RT);
  const qc = useQueryClient();
  const [stage, setStage] = useState(stages[0] ?? "");
  const [count, setCount] = useState("1");
  const [refuse, setRefuse] = useState(true);
  const [apiError, setApiError] = useState("");
  const n = Number(count);
  const okCount = Number.isInteger(n) && n >= 1 && n <= TEST_MAX_REQUESTS;
  const mut = useMutation({
    mutationFn: () => apiSend<RouteTestResp>(ROUTE_TEST_API, {
      method: "POST", body: { stage, refuse_primary: refuse, count: n }, admin: true,
    }),
    onSuccess: () => {
      setApiError("");
      qc.invalidateQueries({ queryKey: [...ROUTE_KEY] });
    },
    onError: (e) => setApiError(isReadOnlyRefusal(e) ? t("readOnly") : e instanceof Error ? e.message : String(e)),
  });
  const res = mut.data;
  return (
    <Drawer open onClose={onClose} width={560} title={t("testTitle")}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose} disabled={mut.isPending}>{t("cancel")}</Button>
          <Button variant="primary" disabled={!stage || !okCount || mut.isPending} onClick={() => mut.mutate()}
            testId="route-test-run">{mut.isPending ? t("testRunning") : t("testRun")}</Button>
        </div>
      }>
      <div data-testid="route-test" style={{ display: "grid", gap: 14 }}>
        <p style={{ ...small, margin: 0 }}>{t("testIntro")}</p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))",
          gap: 12 }}>
          <Field label={t("testStage")}>
            <Select value={stage} onChange={(e) => setStage(e.target.value)} disabled={mut.isPending}
              options={stages.map((s) => ({ value: s, label: s }))} style={{ maxWidth: "100%", minWidth: 0 }}
              data-testid="route-test-stage" />
          </Field>
          <Field label={t("testCount")} error={okCount ? undefined : t("invalid")}>
            <Input inputMode="numeric" value={count} onChange={(e) => setCount(e.target.value)}
              disabled={mut.isPending} style={{ fontFamily: mono }} />
          </Field>
        </div>
        <Checkbox checked={refuse} onChange={(e) => setRefuse(e.target.checked)} disabled={mut.isPending}
          label={t("testRefuse")} />
        {apiError && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
        {res && (
          <div data-testid="route-test-result" style={{ display: "grid", gap: 6 }}>
            <strong style={{ fontSize: 13, color: K.ink }}>
              {t("testResult", res.requests.filter((r) => r.ok).length, res.requests.length,
                res.cost_usd.toFixed(4))}</strong>
            {res.requests.map((r) => (
              <div key={r.n} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap",
                fontSize: 12.5 }}>
                <span style={{ fontFamily: mono, color: K.muted }}>#{r.n}</span>
                {r.ok ? (
                  <>
                    <Badge tone={r.fallback_used ? "warning" : "success"} dot={false}>
                      {rungLabel(r.model, r.connection)}</Badge>
                    <span style={{ fontFamily: mono }}>{usd(r.cost_usd)}</span>
                    {r.trace_id && <LinkButton onClick={() => onOpenRequest(r.trace_id as string, null)}>
                      {t("open")}</LinkButton>}
                  </>
                ) : (
                  <span style={{ color: K.danger, overflowWrap: "anywhere" }}>{t("testFailed")}: {r.error}</span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </Drawer>
  );
}

type T = Translator<typeof RT.en>;

function valuesCells(t: T, v: PolicyValues) {
  return {
    threshold: <span style={{ fontFamily: mono, whiteSpace: "nowrap" }}>{t("thresholdValue", v.fallback_alert_pct)}
      <span style={{ color: K.muted }}> · {t("minValue", v.min_requests)}</span></span>,
    last: <Badge tone={v.alert_last_rung ? "info" : "neutral"} dot={false}>{v.alert_last_rung ? t("on") : t("off")}
    </Badge>,
    cost: <span style={{ fontFamily: mono, whiteSpace: "nowrap" }}>{t("costValue", v.cost_shift_usd.toFixed(2))}</span>,
    hold: <Badge tone={v.hold_fallback_writes ? "warning" : "neutral"} dot={false}>
      {v.hold_fallback_writes ? t("hold") : t("mark")}</Badge>,
  };
}

export default function RouteAlertsCard({ onOpenRequest }: {
  onOpenRequest: (traceId: string, at: string | null) => void;
}) {
  const t = useT(RT);
  const toast = useToast();
  const qc = useQueryClient();
  const access = usePageAccess();
  const canEdit = access.can("route_policy");
  const canTest = access.can("route_test");
  const [editing, setEditing] = useState<Row | null>(null);
  const [testing, setTesting] = useState(false);
  // AA-1052 W6: a reset that clears the hold switch of a writing-path stage waits here for its evidence.
  const [resetting, setResetting] = useState<Row | null>(null);
  const [resetEvidence, setResetEvidence] = useState<ChangeEvidence>(EMPTY_EVIDENCE);
  const [resetError, setResetError] = useState<string | null>(null);
  const q = useQuery({
    queryKey: [...POLICY_KEY],
    queryFn: () => apiGet<RoutePolicyResp>(ROUTE_POLICY_API, { admin: true }),
    retry: false,
  });
  const reset = useMutation({
    mutationFn: ({ stage, proof }: { stage: string; proof?: ChangeEvidence }) => {
      const q = new URLSearchParams(proof ? evidenceBody(proof) : {}).toString();
      return apiSend(`${policyPath(stage)}${q ? `?${q}` : ""}`, { method: "DELETE", admin: true });
    },
    onSuccess: (_d, { stage }) => {
      toast.success(t("resetDone", stage === DEFAULT_STAGE ? t("defaultsRow") : stage));
      setResetEvidence(EMPTY_EVIDENCE);
      qc.invalidateQueries({ queryKey: [...POLICY_KEY] });
    },
    onError: (e) => toast.error(isReadOnlyRefusal(e) ? t("readOnly") : e instanceof Error ? e.message : String(e)),
  });
  const run = useMutation({
    mutationFn: () => apiSend(ROUTE_WATCH_API, { method: "POST", body: { mode: "watch" }, admin: true }),
    onSuccess: () => toast.success(t("runCheckDone")),
    onError: (e) => toast.error(isReadOnlyRefusal(e) ? t("readOnly") : e instanceof Error ? e.message : String(e)),
  });
  const data = q.data;
  const rows = useMemo<Row[]>(() => {
    if (!data) return [];
    const head: Row = { stage: DEFAULT_STAGE, row: data.default_row, isDefault: true,
      effective: { ...data.defaults, ...Object.fromEntries(Object.entries(data.default_row ?? {})
        .filter(([k, v]) => v != null && k in data.defaults)) } as Row["effective"] };
    return [head, ...data.stages.map((s) => ({ ...s, isDefault: false }))];
  }, [data]);
  const stages = useMemo(() => (data?.stages ?? []).map((s) => s.stage), [data]);

  const columns = useMemo<ColumnDef<Row, unknown>[]>(() => {
    const cols: ColumnDef<Row, unknown>[] = [
      { id: "stage", header: t("colStage"), accessorFn: (r) => r.stage,
        cell: ({ row: { original: r } }) => r.isDefault
          ? <strong style={{ color: K.ink }}>{t("defaultsRow")}</strong>
          : <span style={{ fontFamily: mono, fontSize: 12.5, overflowWrap: "anywhere" }}>{r.stage}</span> },
      { id: "threshold", header: t("colThreshold"), enableSorting: false,
        cell: ({ row: { original: r } }) => valuesCells(t, r.effective).threshold },
      { id: "last", header: t("colLastRung"), enableSorting: false,
        cell: ({ row: { original: r } }) => valuesCells(t, r.effective).last },
      { id: "cost", header: t("colCostShift"), enableSorting: false,
        cell: ({ row: { original: r } }) => valuesCells(t, r.effective).cost },
      { id: "hold", header: t("colHold"), accessorFn: (r) => (r.effective.hold_fallback_writes ? 1 : 0),
        cell: ({ row: { original: r } }) => valuesCells(t, r.effective).hold },
      { id: "own", header: t("colOwn"), accessorFn: (r) => (r.row ? 1 : 0),
        cell: ({ row: { original: r } }) => <Badge tone={r.row ? "accent" : "neutral"} dot={false}>
          {r.row ? t("own") : t("inherited")}</Badge> },
    ];
    if (canEdit) {
      cols.push({ id: "actions", header: "", enableSorting: false,
        cell: ({ row: { original: r } }) => (
          <span style={{ display: "inline-flex", gap: 6, whiteSpace: "nowrap" }}>
            <LinkButton onClick={() => setEditing(r)}><Pencil size={12} /> {t("edit")}</LinkButton>
            {r.row && <LinkButton tone="muted" disabled={reset.isPending} onClick={() => (
              holdChangeNeedsEvidence(r.stage, r.row?.hold_fallback_writes, null)
                ? setResetting(r) : reset.mutate({ stage: r.stage }))}>
              {t("reset")}</LinkButton>}
          </span>
        ) });
    }
    return cols;
  }, [t, canEdit, reset]);

  return (
    <Section testId="route-alerts" title={t("alertsTitle")} description={t("alertsIntro")}
      actions={(canEdit || canTest) && (
        <span style={{ display: "inline-flex", gap: 8, flexWrap: "wrap" }}>
          {canTest && <Button variant="ghost" size="sm" onClick={() => setTesting(true)} testId="route-test-open">
            <FlaskConical size={14} /> {t("testOpen")}</Button>}
          {canTest && <Button variant="ghost" size="sm" disabled={run.isPending} onClick={() => run.mutate()}
            testId="route-watch-run"><Play size={14} /> {t("runCheck")}</Button>}
        </span>
      )}>
      {q.isLoading && <Spinner />}
      {q.isError && <div style={{ fontSize: 12.5, color: K.danger }}>{t("loadError")}</div>}
      {data && !data.ready && <div style={{ ...small, marginBottom: 8 }}>{t("alertsNotReady")}</div>}
      {data && (
        <DataTable tableId="route-alerts" data={rows} columns={columns} getRowId={(r) => r.stage} compact
          tableMinWidth={820} paginate={rows.length > 25} />
      )}
      {editing && <PolicyEditor row={editing} onClose={() => setEditing(null)} />}
      <ConfirmModal
        open={resetting !== null}
        onClose={() => { setResetting(null); setResetError(null); }}
        onConfirm={() => {
          if (!resetting) return;
          if (!evidenceReady(resetEvidence)) {
            setResetError(t("evidenceMissing", BREAK_GLASS_MIN_CHARS));
            return;
          }
          reset.mutate({ stage: resetting.stage, proof: resetEvidence });
          setResetting(null);
          setResetError(null);
        }}
        title={resetting ? t("resetTitle", resetting.isDefault ? t("defaultsRow") : resetting.stage) : ""}
        confirmLabel={t("resetConfirm")}
        body={resetting ? (
          <div data-testid="route-policy-reset-confirm">
            <p style={{ ...small, marginTop: 0 }}>{t("holdEvidenceHint")}</p>
            <ChangeEvidenceFields stage={`policy-reset-${resetting.stage}`} value={resetEvidence} error={resetError}
              onChange={(next) => { setResetEvidence(next); setResetError(null); }} />
          </div>
        ) : null}
      />
      {testing && <RouteTestDrawer stages={stages} onClose={() => setTesting(false)}
        onOpenRequest={(id, at) => { setTesting(false); onOpenRequest(id, at); }} />}
    </Section>
  );
}
