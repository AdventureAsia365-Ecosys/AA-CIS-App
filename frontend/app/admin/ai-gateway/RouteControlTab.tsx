"use client";
// app/admin/ai-gateway/RouteControlTab.tsx — AA-1042 (S268, route control center): the Route control tab of AI
// providers. Everything about routing an admin may change with no deploy, on one tab:
//   - the order mode (same model first, or every AWS model first) and the two presets ("Prefer Anthropic API for
//     Claude (sponsored)", "Prefer AWS"), each applied in one click as a new version;
//   - the chain of every stage (drag and drop, or the arrow buttons on a phone / keyboard; add or remove a model);
//   - the provider order of every model with more than one provider (which connection of the same model goes first);
//   - a live preview from the server before saving (POST /api/admin/ai-gateway/route-control/preview, $0): the model
//     that would serve each stage first, the rule errors that block the save and the warnings that do not;
//   - the versions (who, when, what changed), each one viewable and revertible in one click (audited).
// The rules are the server's (api/services/llm_route_control.py); this page only shows them. Writes need the
// ai.admin capability: GET /api/admin/ai-gateway/access answers `route_control`, and a reader sees the routes, the
// presets and the versions without any control. A writer / judge chain change asks for the eval run that backs it, or
// a break-glass reason (ChangeEvidenceFields, the Routing tab's own block). Kit only; English UI copy.

import { useMemo, useRef, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, GripVertical, X } from "lucide-react";
import {
  ApiError, Badge, Button, DataTable, DateText, Drawer, ErrorState, Field, IconButton, K, Modal, Section, Select,
  Spinner, Textarea, apiGet, apiSend, mono, useLocalized, useT, useToast, type ColumnDef,
} from "../../_kit";
import {
  NOTE_MAX, ROUTE_CONTROL_API, ROUTE_CONTROL_KEY, ROUTE_VERSIONS_KEY, addableOptions, draftOf, groupStages,
  isEmptyProposal, moveItem, presetPath, problemsOf, proposalBody, skippedWhere, versionPath, type ControlStage,
  type DiffChange, type OrderMode, type PresetKey, type Problem, type ProviderOrderRow, type RouteControlState, type RouteDraft,
  type RoutePreview, type RouteVersion, type RouteVersionsResp,
} from "@/lib/routeControl";
import {
  ChangeEvidenceFields, EMPTY_EVIDENCE, evidenceBody, evidenceReady, type ChangeEvidence,
} from "./ChangeEvidenceFields";
import { STAGE_GROUPS, STAGE_LABELS } from "./llmShared";
import { STAGE_GROUP_LABELS_VI, STAGE_LABELS_VI } from "./llmShared.vi";
import { RC } from "./routeControl.messages";
import { usePageAccess } from "./usePageAccess";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.5 } as const;
const STAGE_GROUP_LABELS: Record<string, string> = Object.fromEntries(STAGE_GROUPS.map((g) => [g.key, g.label]));

function useRc() {
  return useT(RC);
}

// ── a reorderable list (native drag and drop, plus arrow buttons for touch and keyboard) ───────────────────────────

interface OrderItem {
  key: string;
  label: React.ReactNode;
  caption?: React.ReactNode;
  meta?: React.ReactNode;
  name: string;
}

function OrderList({ items, onMove, onRemove, disabled, testId }: {
  items: OrderItem[];
  onMove: (from: number, to: number) => void;
  onRemove?: (index: number) => void;
  disabled: boolean;
  testId: string;
}) {
  const t = useRc();
  const dragFrom = useRef<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  return (
    <ol data-testid={testId} style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
      {items.map((item, i) => (
        <li key={item.key} draggable={!disabled}
          onDragStart={(e) => { dragFrom.current = i; e.dataTransfer.effectAllowed = "move"; }}
          onDragOver={(e) => { if (!disabled && dragFrom.current !== null) { e.preventDefault(); setOver(i); } }}
          onDragLeave={() => setOver((o) => (o === i ? null : o))}
          onDrop={(e) => {
            e.preventDefault();
            if (dragFrom.current !== null) onMove(dragFrom.current, i);
            dragFrom.current = null;
            setOver(null);
          }}
          onDragEnd={() => { dragFrom.current = null; setOver(null); }}
          style={{
            display: "flex", alignItems: "center", gap: 8, minWidth: 0, padding: "6px 8px", borderRadius: 8,
            border: `1px solid ${over === i ? K.accent : K.line}`, background: K.card,
            cursor: disabled ? "default" : "grab",
          }}>
          {!disabled && <GripVertical size={14} color={K.muted2} aria-hidden style={{ flexShrink: 0 }} />}
          <span style={{ ...small, minWidth: 64, flexShrink: 0 }}>{item.caption}</span>
          <span style={{ flex: 1, minWidth: 0, display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
            <span style={{ color: K.ink, fontSize: 13, overflowWrap: "anywhere" }}>{item.label}</span>
            {item.meta}
          </span>
          {!disabled && (
            <span style={{ display: "inline-flex", gap: 2, flexShrink: 0 }}>
              <IconButton size="xs" variant="plain" label={t("moveUp", item.name)} disabled={i === 0}
                onClick={() => onMove(i, i - 1)}><ArrowUp size={13} /></IconButton>
              <IconButton size="xs" variant="plain" label={t("moveDown", item.name)} disabled={i === items.length - 1}
                onClick={() => onMove(i, i + 1)}><ArrowDown size={13} /></IconButton>
              {onRemove && (
                <IconButton size="xs" variant="plain" tone="danger" label={t("remove", item.name)}
                  disabled={items.length <= 1} onClick={() => onRemove(i)}><X size={13} /></IconButton>
              )}
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}

// ── problems and diffs ───────────────────────────────────────────────────────────────────────────────────────────

function ProblemList({ problems, tone }: { problems: Problem[]; tone: "danger" | "warning" }) {
  const t = useRc();
  if (!problems.length) return null;
  return (
    <ul style={{ margin: "6px 0 0", paddingLeft: 18, fontSize: 12, lineHeight: 1.5,
      color: tone === "danger" ? K.danger : K.amber }}>
      {problems.map((p, i) => (
        <li key={`${p.rule}-${i}`} style={{ overflowWrap: "anywhere" }}>
          {p.stage ? <span style={{ fontFamily: mono }}>{p.stage}: </span> : null}{p.message}
          {p.existing ? <span style={{ color: K.muted }}> ({t("existingNote")})</span> : null}
        </li>
      ))}
    </ul>
  );
}

function DiffList({ diff, modelLabel }: { diff: DiffChange[]; modelLabel: (k: string) => string }) {
  const t = useRc();
  const chain = (c: string[] | null) => (c ? c.map(modelLabel).join(" → ") : t("none"));
  if (!diff.length) return <div style={small}>{t("nothingChanged")}</div>;
  return (
    <ul data-testid="route-control-diff" style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 6, fontSize: 12.5 }}>
      {diff.map((d, i) => (
        <li key={i} style={{ overflowWrap: "anywhere", color: K.ink }}>
          {d.kind === "order_mode" ? t("diffOrder", d.before ?? t("none"), d.after ?? t("none")) : (
            <>
              <strong>{d.kind === "route" ? t("diffRoute", d.stage) : t("diffProvider", modelLabel(d.model))}</strong>
              <div style={small}>{t("diffBefore")}: {d.kind === "route" ? chain(d.before) : (d.before ?? []).join(" → ")}</div>
              <div style={small}>{t("diffAfter")}: {d.kind === "route" ? chain(d.after) : (d.after ?? []).join(" → ")}</div>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

// ── one stage's chain editor ─────────────────────────────────────────────────────────────────────────────────────

function StageEditor({ stage, chain, onChange, preview, canEdit, modelLabel, stageLabel }: {
  stage: ControlStage;
  chain: string[];
  onChange: (next: string[]) => void;
  preview: RoutePreview | undefined;
  canEdit: boolean;
  modelLabel: (k: string) => string;
  stageLabel: string;
}) {
  const t = useRc();
  const { errors, warnings } = problemsOf(preview, stage.stage);
  const p = preview?.stages.find((s) => s.stage === stage.stage);
  const addable = addableOptions(stage, chain);
  const opt = (k: string) => stage.options.find((o) => o.model_id === k);
  return (
    <div data-testid={`route-control-stage-${stage.stage}`}
      style={{ border: `1px solid ${errors.length ? K.danger : K.line}`, borderRadius: 10, padding: 12, minWidth: 0 }}>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: 8, marginBottom: 8 }}>
        <strong style={{ color: K.ink, fontSize: 13.5 }}>{stageLabel}</strong>
        <span style={{ fontFamily: mono, fontSize: 11.5, color: K.muted }}>{stage.stage}</span>
        <Badge tone="neutral" dot={false}>{stage.role}</Badge>
        {!stage.is_active && <Badge tone="warning" dot={false}>{t("switchedOff")}</Badge>}
      </div>
      {p && stage.is_active && (
        <div style={{ ...small, marginBottom: 8, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
          <span>{t("firstServes")}:</span>
          {p.first ? <span style={{ fontFamily: mono, color: K.ink }}>{p.first}</span>
            : <Badge tone="danger">{t("noLiveModel")}</Badge>}
          {p.first_before !== p.first && (
            <span>({t("firstBefore", p.first_before ?? t("noLiveModel"))})</span>
          )}
        </div>
      )}
      {p && stage.is_active && (p.skipped?.length ?? 0) > 0 && (
        <div data-testid={`route-control-skipped-${stage.stage}`}
          style={{ ...small, marginBottom: 8, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "baseline" }}>
          <span>{t("skippedTitle")}:</span>
          {p.skipped!.map((s) => (
            <span key={skippedWhere(s)} style={{ fontFamily: mono, color: K.muted, overflowWrap: "anywhere" }}>
              {t("skippedItem", skippedWhere(s), s.reason)}
            </span>
          ))}
        </div>
      )}
      <OrderList testId={`route-control-chain-${stage.stage}`} disabled={!canEdit}
        onMove={(from, to) => onChange(moveItem(chain, from, to))}
        onRemove={(i) => onChange(chain.filter((_, n) => n !== i))}
        items={chain.map((k, i) => ({
          key: k, name: modelLabel(k), caption: i === 0 ? t("primary") : t("fallback", i),
          label: modelLabel(k),
          meta: opt(k)?.via ? <span style={{ fontSize: 11.5, color: K.muted }}>{opt(k)?.via}</span> : null,
        }))} />
      {canEdit && addable.length > 0 && (
        <div style={{ marginTop: 8, maxWidth: 360 }}>
          <Select size="sm" aria-label={t("addModel")} value="" placeholder={t("addPlaceholder")}
            data-testid={`route-control-add-${stage.stage}`}
            onChange={(e) => { if (e.target.value) onChange([...chain, e.target.value]); }}
            options={addable.map((o) => ({ value: o.model_id, label: o.via ? `${o.label} · ${o.via}` : o.label }))} />
        </div>
      )}
      <ProblemList problems={errors} tone="danger" />
      <ProblemList problems={warnings} tone="warning" />
    </div>
  );
}

// ── one model's provider order ───────────────────────────────────────────────────────────────────────────────────

function ProviderCard({ row, order, onChange, canEdit }: {
  row: ProviderOrderRow;
  order: string[];
  onChange: (next: string[]) => void;
  canEdit: boolean;
}) {
  const t = useRc();
  const byKey = new Map(row.deployments.map((d) => [d.connection_key, d]));
  return (
    <div data-testid={`route-control-provider-${row.model_key}`}
      style={{ border: `1px solid ${K.line}`, borderRadius: 10, padding: 12, minWidth: 0 }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "baseline", marginBottom: 8 }}>
        <strong style={{ color: K.ink, fontSize: 13.5 }}>{row.label}</strong>
        <span style={{ fontFamily: mono, fontSize: 11.5, color: K.muted }}>{row.model_key}</span>
      </div>
      <OrderList testId={`route-control-provider-list-${row.model_key}`} disabled={!canEdit}
        onMove={(from, to) => onChange(moveItem(order, from, to))}
        items={order.map((c, i) => {
          const d = byKey.get(c);
          return {
            key: c, name: d?.label ?? c, caption: `${i + 1}.`, label: d?.label ?? c,
            meta: (
              <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 4 }}>
                <Badge tone={d?.data_scope === "aws_only" ? "info" : "purple"} dot={false}>
                  {d?.data_scope === "aws_only" ? t("scopeAws") : t("scopeVendor")}</Badge>
                {d && !d.enabled && <Badge tone="neutral" dot={false}>{t("deploymentOff")}</Badge>}
                {d && d.enabled && !d.connection_enabled && <Badge tone="neutral" dot={false}>{t("connectionOff")}</Badge>}
              </span>
            ),
          };
        })} />
    </div>
  );
}

// ── versions ─────────────────────────────────────────────────────────────────────────────────────────────────────

function VersionsSection({ current, canEdit, modelLabel, onRevert }: {
  current: number;
  canEdit: boolean;
  modelLabel: (k: string) => string;
  onRevert: (v: RouteVersion) => void;
}) {
  const t = useRc();
  const [viewing, setViewing] = useState<RouteVersion | null>(null);
  const q = useQuery({
    queryKey: [...ROUTE_VERSIONS_KEY],
    queryFn: () => apiGet<RouteVersionsResp>(`${ROUTE_CONTROL_API}/versions?limit=50`, { admin: true }),
    retry: false,
  });
  const actionText = (v: RouteVersion) => t(`action_${v.action}` as "action_save");
  const columns = useMemo<ColumnDef<RouteVersion>[]>(() => [
    { accessorKey: "version", header: t("colVersion"), enableSorting: false,
      cell: (x) => (
        <span style={{ display: "inline-flex", gap: 6, alignItems: "center", fontFamily: mono }}>
          {x.row.original.version}
          {x.row.original.version === current && <Badge tone="success" dot={false}>{t("current")}</Badge>}
        </span>
      ) },
    { accessorKey: "created_at", header: t("colWhen"), enableSorting: false,
      cell: (x) => <span style={small}><DateText value={x.row.original.created_at} relative /></span> },
    { accessorKey: "created_by", header: t("colBy"), enableSorting: false,
      cell: (x) => <span style={{ ...small, overflowWrap: "anywhere" }}>{x.row.original.created_by}</span> },
    { accessorKey: "action", header: t("colAction"), enableSorting: false,
      cell: (x) => {
        const v = x.row.original;
        return (
          <span style={{ display: "grid", gap: 2 }}>
            <Badge tone={v.action === "external" ? "warning" : "neutral"} dot={false}
              style={{ justifySelf: "start" }}>{actionText(v)}</Badge>
            {v.reverted_from != null && <span style={small}>{t("revertedFrom", v.reverted_from)}</span>}
          </span>
        );
      } },
    { id: "changes", header: t("colChanges"), enableSorting: false,
      cell: (x) => <span style={small}>{t("changesCount", x.row.original.diff.length)}</span> },
    { accessorKey: "note", header: t("colNote"), enableSorting: false,
      cell: (x) => <span style={{ ...small, overflowWrap: "anywhere" }}>{x.row.original.note ?? ""}</span> },
    { id: "actions", header: "", enableSorting: false, meta: { align: "right" },
      cell: (x) => (
        <span style={{ display: "inline-flex", gap: 6 }}>
          <Button size="sm" variant="ghost" onClick={() => setViewing(x.row.original)}
            testId={`route-version-view-${x.row.original.version}`}>{t("view")}</Button>
          {canEdit && x.row.original.version !== current && (
            <Button size="sm" variant="secondary" onClick={() => onRevert(x.row.original)}
              testId={`route-version-revert-${x.row.original.version}`}>{t("revert")}</Button>
          )}
        </span>
      ) },
  ], [t, current, canEdit, onRevert]);  // eslint-disable-line react-hooks/exhaustive-deps -- actionText is t
  return (
    <Section title={t("versionsTitle")} description={t("versionsHelp")} padded={false}
      testId="route-control-versions">
      {q.isError ? (
        <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />
      ) : (
        <DataTable<RouteVersion> tableId="route-control-versions" data={q.data?.versions ?? []} columns={columns}
          getRowId={(r) => String(r.version)} loading={q.isLoading} searchable={false} paginate={false}
          stickyHeader={false} tableMinWidth={760} emptyTitle="—" />
      )}
      {viewing && (
        <Drawer open onClose={() => setViewing(null)} title={t("versionTitle", viewing.version)} width={520}
          footer={<div style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => setViewing(null)}>{t("close")}</Button></div>}>
          <div style={{ display: "grid", gap: 10 }}>
            <div style={small}>{actionText(viewing)} · {viewing.created_by} · <DateText value={viewing.created_at} /></div>
            {viewing.note && <div style={{ fontSize: 13, color: K.ink }}>{viewing.note}</div>}
            <DiffList diff={viewing.diff} modelLabel={modelLabel} />
          </div>
        </Drawer>
      )}
    </Section>
  );
}

// ── the tab ──────────────────────────────────────────────────────────────────────────────────────────────────────

type Pending =
  | { kind: "save" }
  | { kind: "preset"; preset: PresetKey; label: string }
  | { kind: "revert"; version: number };

export default function RouteControlTab() {
  const t = useRc();
  const toast = useToast();
  const qc = useQueryClient();
  const canEdit = usePageAccess().can("route_control");
  const stageLabels = useLocalized({ en: STAGE_LABELS, vi: STAGE_LABELS_VI as Record<string, string> });
  const groupLabels = useLocalized({ en: STAGE_GROUP_LABELS, vi: STAGE_GROUP_LABELS_VI as Record<string, string> });
  const q = useQuery({
    queryKey: [...ROUTE_CONTROL_KEY],
    queryFn: () => apiGet<RouteControlState>(ROUTE_CONTROL_API, { admin: true }),
    retry: false,
  });
  const state = q.data;
  // The draft starts again from the config in force whenever it changes (state adjusted during render, no effect;
  // react-query keeps the same object while the data is unchanged).
  const [draft, setDraft] = useState<RouteDraft | null>(null);
  const [draftBase, setDraftBase] = useState<RouteControlState | undefined>(undefined);
  if (state && draftBase !== state) {
    setDraftBase(state);
    setDraft(draftOf(state));
  }
  const body = useMemo(() => (state && draft ? proposalBody(state, draft) : {}), [state, draft]);
  const changed = !isEmptyProposal(body);

  const preview = useQuery({
    queryKey: [...ROUTE_CONTROL_KEY, "preview", state?.version ?? 0, JSON.stringify(body)],
    queryFn: () => apiSend<RoutePreview>(`${ROUTE_CONTROL_API}/preview`, { method: "POST", body, admin: true }),
    enabled: canEdit && !!state,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
    retry: false,
  });

  const [pending, setPending] = useState<Pending | null>(null);
  const [note, setNote] = useState("");
  const [evidence, setEvidence] = useState<ChangeEvidence>(EMPTY_EVIDENCE);
  const [actionError, setActionError] = useState("");
  const pendingBody = pending?.kind === "preset" ? { preset: pending.preset }
    : pending?.kind === "revert" ? { revert_to: pending.version } : null;
  const pendingPreview = useQuery({
    queryKey: [...ROUTE_CONTROL_KEY, "preview", state?.version ?? 0, JSON.stringify(pendingBody)],
    queryFn: () => apiSend<RoutePreview>(`${ROUTE_CONTROL_API}/preview`, { method: "POST", body: pendingBody,
      admin: true }),
    enabled: canEdit && !!pendingBody,
    retry: false,
  });
  const shown = pending?.kind === "save" ? preview.data : pendingPreview.data;

  const close = () => { setPending(null); setNote(""); setEvidence(EMPTY_EVIDENCE); setActionError(""); };
  const write = useMutation({
    mutationFn: () => {
      const meta = { expected_version: state?.version, note: note.trim() || undefined,
        ...(shown?.needs_evidence ? evidenceBody(evidence) : {}) };
      if (pending?.kind === "preset") {
        return apiSend<{ version: number }>(presetPath(pending.preset), { method: "POST", body: meta, admin: true });
      }
      if (pending?.kind === "revert") {
        return apiSend<{ version: number }>(`${versionPath(pending.version)}/revert`,
          { method: "POST", body: meta, admin: true });
      }
      return apiSend<{ version: number }>(`${ROUTE_CONTROL_API}/save`, { method: "POST", body: { ...body, ...meta },
        admin: true });
    },
    onSuccess: (out) => {
      toast.success(pending?.kind === "preset" ? t("presetDone", out.version)
        : pending?.kind === "revert" ? t("reverted", out.version) : t("saved", out.version));
      close();
      qc.invalidateQueries({ queryKey: [...ROUTE_CONTROL_KEY] });
      qc.invalidateQueries({ queryKey: ["llm-config"] });
      qc.invalidateQueries({ queryKey: ["llm-availability"] });
    },
    onError: (e) => {
      setActionError(e instanceof ApiError && e.status === 409 ? t("conflict")
        : e instanceof Error ? e.message : String(e));
    },
  });

  const modelLabel = (k: string) => state?.models[k]?.label ?? k;
  const stageLabel = (s: string) => stageLabels[s] ?? STAGE_LABELS[s] ?? s;
  const groupLabel = (g: string) => groupLabels[g] ?? STAGE_GROUP_LABELS[g] ?? g;

  const notDeployed = q.isError && q.error instanceof ApiError && [404, 501, 503].includes(q.error.status);
  if (q.isLoading) return <Section title={t("title")}><Spinner /></Section>;
  if (notDeployed) {
    return <Section title={t("title")} testId="route-control"><div role="status" style={small}>{t("notDeployed")}</div></Section>;
  }
  if (q.isError || !state || !draft) {
    return (
      <Section title={t("title")} testId="route-control">
        <ErrorState message={`${t("loadError")}: ${(q.error as Error | null)?.message ?? ""}`} onRetry={() => q.refetch()} />
      </Section>
    );
  }

  const p = preview.data;
  const globalErrors = (p?.errors ?? []).filter((e) => !e.stage);
  const blocked = !p || preview.isFetching || !p.valid || p.unchanged;
  const needsEv = !!shown?.needs_evidence;
  const confirmBlocked = !shown || (pending?.kind !== "save" && pendingPreview.isFetching) || !shown.valid
    || shown.unchanged || (needsEv && !evidenceReady(evidence)) || write.isPending;
  const providerRows = state.provider_order.filter((r) => r.deployments.length > 1);
  const groups = groupStages(state.stages, STAGE_GROUPS);
  const setRoute = (stage: string, chain: string[]) => setDraft((d) => d && { ...d, routes: { ...d.routes, [stage]: chain } });
  const setOrder = (model: string, order: string[]) =>
    setDraft((d) => d && { ...d, provider_order: { ...d.provider_order, [model]: order } });

  return (
    <div data-testid="route-control" style={{ display: "grid", gap: 14, minWidth: 0 }}>
      <Section title={t("title")} description={t("intro")} testId="route-control-summary"
        actions={<Badge tone="accent" dot={false}>{t("version", state.version)}</Badge>}>
        <div style={{ display: "grid", gap: 14 }}>
          {!canEdit && <div role="status" style={small}>{t("readOnly")}</div>}
          <Field label={t("orderTitle")} hint={`${t("orderHelp")} ${t("orderEnvNote", state.env_order_mode)}`}>
            <Select value={draft.order_mode} disabled={!canEdit} data-testid="route-control-order-mode"
              onChange={(e) => setDraft({ ...draft, order_mode: e.target.value as OrderMode })}
              options={[{ value: "model_first", label: t("orderModelFirst") },
                { value: "aws_first", label: t("orderAwsFirst") }]} />
          </Field>
          <div>
            <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink2 }}>{t("presetsTitle")}</div>
            <div style={{ ...small, marginBottom: 8 }}>{t("presetsHelp")}</div>
            <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))" }}>
              {state.presets.map((ps) => {
                const label = t(`presetLabel_${ps.key}` as "presetLabel_prefer_aws");
                return (
                  <div key={ps.key} data-testid={`route-control-preset-${ps.key}`}
                    style={{ border: `1px solid ${K.line}`, borderRadius: 10, padding: 12, display: "grid", gap: 6 }}>
                    <strong style={{ fontSize: 13, color: K.ink }}>{label}</strong>
                    <span style={small}>{t(`presetHelp_${ps.key}` as "presetHelp_prefer_aws")}</span>
                    {canEdit && (
                      <span><Button size="sm" variant="secondary" disabled={changed}
                        testId={`route-control-preset-apply-${ps.key}`}
                        onClick={() => setPending({ kind: "preset", preset: ps.key, label })}>{t("presetApply")}</Button></span>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
          {canEdit && (
            <div data-testid="route-control-savebar" style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center",
              borderTop: `1px solid ${K.line}`, paddingTop: 12 }}>
              <span style={{ ...small, flex: 1, minWidth: 160 }}>
                {preview.isFetching ? t("previewLoading")
                  : changed ? t("changesCount", p?.diff.length ?? 0) : t("previewIdle")}
              </span>
              <Button variant="ghost" disabled={!changed || write.isPending} onClick={() => setDraft(draftOf(state))}
                testId="route-control-reset">{t("reset")}</Button>
              <Button variant="primary" disabled={blocked} onClick={() => setPending({ kind: "save" })}
                testId="route-control-review">{t("review")}</Button>
            </div>
          )}
          {globalErrors.length > 0 && (
            <div role="alert"><strong style={{ fontSize: 12.5, color: K.danger }}>{t("errorsTitle")}</strong>
              <ProblemList problems={globalErrors} tone="danger" /></div>
          )}
          {preview.isError && <div role="alert" style={{ fontSize: 12, color: K.danger }}>
            {(preview.error as Error).message}</div>}
        </div>
      </Section>

      <Section title={t("chainsTitle")} description={t("chainsHelp")} testId="route-control-chains">
        <div style={{ display: "grid", gap: 16 }}>
          {groups.map((g) => (
            <div key={g.key} style={{ display: "grid", gap: 8, minWidth: 0 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: K.muted2, textTransform: "uppercase", letterSpacing: 0.4 }}>
                {g.key === "other" ? "—" : groupLabel(g.key)}</div>
              <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 340px), 1fr))" }}>
                {g.stages.map((s) => (
                  <StageEditor key={s.stage} stage={s} chain={draft.routes[s.stage] ?? s.chain} canEdit={canEdit}
                    onChange={(c) => setRoute(s.stage, c)} preview={p} modelLabel={modelLabel}
                    stageLabel={stageLabel(s.stage)} />
                ))}
              </div>
            </div>
          ))}
        </div>
      </Section>

      <Section title={t("providerTitle")} description={t("providerHelp")} testId="route-control-providers">
        <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 300px), 1fr))" }}>
          {providerRows.map((row) => (
            <ProviderCard key={row.model_key} row={row} canEdit={canEdit}
              order={draft.provider_order[row.model_key] ?? row.deployments.map((d) => d.connection_key)}
              onChange={(o) => setOrder(row.model_key, o)} />
          ))}
        </div>
      </Section>

      <VersionsSection current={state.version} canEdit={canEdit} modelLabel={modelLabel}
        onRevert={(v) => setPending({ kind: "revert", version: v.version })} />

      <Modal open={!!pending} onClose={write.isPending ? () => undefined : close} width={560}
        testId="route-control-confirm"
        title={pending?.kind === "preset" ? t("presetTitle", pending.label)
          : pending?.kind === "revert" ? t("revertTitle", pending.version) : t("saveTitle")}
        footer={
          <>
            <Button variant="ghost" onClick={close} disabled={write.isPending}>{t("cancel")}</Button>
            <Button variant="primary" disabled={confirmBlocked} onClick={() => write.mutate()}
              testId="route-control-confirm-go">
              {write.isPending ? t("saving") : pending?.kind === "preset" ? t("presetApply")
                : pending?.kind === "revert" ? t("revertConfirm") : t("saveConfirm")}
            </Button>
          </>
        }>
        <div style={{ display: "grid", gap: 10 }}>
          <div style={small}>{pending?.kind === "revert" ? t("revertBody") : t("saveBody")}</div>
          {!shown ? <Spinner /> : (
            <>
              {shown.unchanged && pending?.kind !== "save" ? <div style={small}>{t("presetNothing")}</div>
                : <DiffList diff={shown.diff} modelLabel={modelLabel} />}
              {shown.errors.length > 0 && (
                <div role="alert"><strong style={{ fontSize: 12.5, color: K.danger }}>{t("errorsTitle")}</strong>
                  <ProblemList problems={shown.errors} tone="danger" /></div>
              )}
              {shown.warnings.filter((w) => !w.existing).length > 0 && (
                <div><strong style={{ fontSize: 12.5, color: K.amber }}>{t("warningsTitle")}</strong>
                  <ProblemList problems={shown.warnings.filter((w) => !w.existing)} tone="warning" /></div>
              )}
              {(shown.frozen_by_waves?.length ?? 0) > 0 && (
                <div role="alert" data-testid="route-control-frozen" style={{ fontSize: 12.5, color: K.danger }}>
                  {t("frozenByWaves", (shown.frozen_by_waves ?? []).map((w) => w.name || w.id).join(", "))}
                </div>
              )}
              {needsEv && (
                <>
                  <div style={{ fontSize: 12.5, color: K.ink }}>{t("evidenceNeeded", shown.evidence_stages.join(", "))}</div>
                  <ChangeEvidenceFields stage="route-control" value={evidence} onChange={setEvidence} />
                </>
              )}
            </>
          )}
          <Field label={t("noteLabel")} hint={t("noteHint")}>
            <Textarea value={note} rows={2} maxLength={NOTE_MAX} onChange={(e) => setNote(e.target.value)}
              data-testid="route-control-note" />
          </Field>
          {actionError && <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>{actionError}</p>}
        </div>
      </Modal>
    </div>
  );
}

