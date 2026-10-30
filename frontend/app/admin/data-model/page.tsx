"use client";
// app/admin/data-model/page.tsx — AA-894 scope 2: Platform › Developer › Data model, the live ERD.
//
// Data: GET /api/admin/data-model (BFF → backend api/routers/admin_data_model.py): erd.json of the last nightly DB
// Schema Dump (every schema of the shared RDS: tables, columns with full types, PK, unique keys, FKs, DM-6.1 PII /
// confidentiality tags, owner app) plus a diff summary against the model before the last structural change. Admin
// only (lib/rbac.ts PAGE_RULES + API_READ_RULES "data-model"; the backend re-checks, capability ops.read).
//
// What the page does: search (table and column names), schema filter, "changed only" filter, a table list; click a
// table to see its columns (type, NOT NULL, PK / FK / UK, tag, change since the previous dump) and its +1 neighbours
// (FKs out and in), each neighbour clickable; "Neighbours only" narrows the list to the selected table and its +1.
// This repo is public: the page is a renderer only, no table name is written here.
// One fetch on open (react-query, staleTime 5 min, the server caches for 5 min too); Reload asks the server to
// re-read storage (?refresh=1, at most every 30 s server side). No polling.
// AA-994: text in the UI language (messages.ts); names from the model stay as they are.

import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Network, RefreshCw, Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import {
  ApiError,
  Badge,
  Button,
  Drawer,
  EmptyState,
  ErrorState,
  K,
  PageHeader,
  RADIUS,
  SkeletonStyle,
  Spinner,
  TableSkeleton,
  apiGet,
  mono,
  sans,
  useFormat,
  useIsPhone,
  useT,
  PageFrame,
  type Tone,
} from "../../_kit";
import {
  changeOf,
  colList,
  columnChange,
  columnHits,
  countChanges,
  flattenTables,
  matchesQuery,
  neighbours,
  piiColumns,
  type ChangeKind,
  type DataModelResp,
  type ErdDiff,
  type ErdEdge,
  type ErdModel,
  type TableRef,
} from "./erd";
import { DM } from "./messages";

const DATA_PATH = "/api/admin/data-model";
const DATA_KEY = ["admin", "data-model"] as const;
const ALL = "__all__";
const LIST_LIMIT = 400; // rows rendered at once; the search narrows the rest

const CHANGE_TONE: Record<ChangeKind, Tone> = { added: "success", changed: "warning" };
const CHANGE_KEY = { added: "changeAdded", changed: "changeChanged" } as const satisfies Record<ChangeKind, string>;

function tagTone(tag: string): Tone {
  if (tag.startsWith("pii:")) return "danger";
  if (tag.startsWith("conf:")) return "warning";
  return "neutral";
}

// ── small pieces ────────────────────────────────────────────────────────────────────────────────

function Card({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <section
      style={{
        background: K.card,
        border: `1px solid ${K.line}`,
        borderRadius: RADIUS.lg,
        minWidth: 0,
        ...style,
      }}
    >
      {children}
    </section>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        color: K.muted,
        margin: "16px 0 8px",
      }}
    >
      {children}
    </div>
  );
}

function Code({ children }: { children: React.ReactNode }) {
  return (
    <code
      style={{
        fontFamily: mono,
        fontSize: 12,
        color: K.ink,
        background: K.neutralBg,
        padding: "1px 6px",
        borderRadius: RADIUS.sm,
        overflowWrap: "anywhere",
      }}
    >
      {children}
    </code>
  );
}

function Muted({ children }: { children: React.ReactNode }) {
  return <span style={{ fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>{children}</span>;
}

function SmallBadge({ tone, children, title }: { tone: Tone; children: React.ReactNode; title?: string }) {
  return (
    <span title={title} style={{ display: "inline-flex" }}>
      <Badge tone={tone} dot={false} style={{ fontSize: 10, padding: "2px 7px" }}>
        {children}
      </Badge>
    </span>
  );
}

// ── diff summary ────────────────────────────────────────────────────────────────────────────────

function DiffSummary({
  diff,
  previousAt,
  changedOnly,
  onToggleChangedOnly,
}: {
  diff: ErdDiff | null;
  previousAt: string | null;
  changedOnly: boolean;
  onToggleChangedOnly: () => void;
}) {
  const t = useT(DM);
  const { formatDateTime } = useFormat();
  if (!diff) {
    return (
      <Card style={{ padding: "12px 16px", marginBottom: 14 }}>
        <Muted>{t("noPrevious")}</Muted>
      </Card>
    );
  }
  const n = countChanges(diff);
  const parts = [
    diff.tables_added.length && t("tablesAdded", diff.tables_added.length),
    diff.tables_removed.length && t("removed", diff.tables_removed.length),
    Object.keys(diff.tables_changed).length && t("changed", Object.keys(diff.tables_changed).length),
    diff.edges_added.length && t("fksAdded", diff.edges_added.length),
    diff.edges_removed.length && t("fksRemoved", diff.edges_removed.length),
  ].filter(Boolean);
  return (
    <Card style={{ padding: "12px 16px", marginBottom: 14 }}>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px 12px" }}>
        <span style={{ fontSize: 13, fontWeight: 700, color: K.ink }}>{t("changedSince")}</span>
        <Muted>{previousAt ? t("previousModelAt", formatDateTime(previousAt)) : t("previousModel")}</Muted>
        <span style={{ flex: "1 1 auto" }} />
        {n > 0 && (
          <Button variant={changedOnly ? "primary" : "ghost"} size="sm" onClick={onToggleChangedOnly}>
            {changedOnly ? t("showingChangedOnly") : t("showChangedOnly")}
          </Button>
        )}
      </div>
      <div style={{ fontSize: 13, color: K.body, marginTop: 6 }}>
        {n === 0 ? t("noStructuralChange") : parts.join(" · ")}
      </div>
      {diff.tables_removed.length > 0 && (
        <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
          <Muted>{t("removedLabel")}</Muted>
          {diff.tables_removed.map((id) => (
            <span key={id} style={{ textDecoration: "line-through" }}>
              <Code>{id}</Code>
            </span>
          ))}
        </div>
      )}
      {diff.edges_removed.length > 0 && (
        <div style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
          <Muted>{t("fksRemovedLabel")}</Muted>
          {diff.edges_removed.map((k) => (
            <Code key={k}>{k}</Code>
          ))}
        </div>
      )}
    </Card>
  );
}

// ── table list ──────────────────────────────────────────────────────────────────────────────────

function TableRow({
  ref_,
  selected,
  change,
  hits,
  onSelect,
}: {
  ref_: TableRef;
  selected: boolean;
  change: ChangeKind | null;
  hits: string[];
  onSelect: () => void;
}) {
  const tr = useT(DM);
  const t = ref_.table;
  const pii = piiColumns(t).length;
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 4,
        width: "100%",
        padding: "9px 14px",
        background: selected ? K.accentTint : "transparent",
        border: "none",
        borderTop: `1px solid ${K.line2}`,
        borderLeft: `3px solid ${selected ? K.accent : "transparent"}`,
        cursor: "pointer",
        textAlign: "left",
        fontFamily: sans,
        minWidth: 0,
      }}
    >
      <span style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "4px 6px", minWidth: 0 }}>
        <span style={{ fontFamily: mono, fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>{ref_.schema}.</span>
        <span style={{ fontFamily: mono, fontSize: 13, fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>
          {t.name}
        </span>
        {change && <SmallBadge tone={CHANGE_TONE[change]}>{tr(CHANGE_KEY[change])}</SmallBadge>}
        {t.kind && t.kind !== "table" && <SmallBadge tone="info">{t.kind}</SmallBadge>}
      </span>
      <span style={{ display: "flex", flexWrap: "wrap", gap: "4px 10px", fontSize: 11, color: K.muted }}>
        <span>{tr("colCount", t.columns.length)}</span>
        {t.foreign_keys.length > 0 && <span>{t.foreign_keys.length} FK</span>}
        {t.rls && <span>RLS{t.rls_forced ? tr("forced") : ""}</span>}
        {pii > 0 && <span style={{ color: K.danger }}>{pii} PII</span>}
        {hits.length > 0 && (
          <span style={{ fontFamily: mono, overflowWrap: "anywhere" }}>{tr("match", hits.slice(0, 3).join(", "))}</span>
        )}
      </span>
    </button>
  );
}

// ── neighbours (+1) ─────────────────────────────────────────────────────────────────────────────

function EdgeChip({
  edge,
  direction,
  onOpen,
  known,
}: {
  edge: ErdEdge;
  direction: "out" | "in";
  onOpen: (id: string) => void;
  known: boolean;
}) {
  const t = useT(DM);
  const target = direction === "out" ? edge.to : edge.from;
  // Always child columns → parent columns, whichever side this table is on.
  const cols = `${colList(edge.from_columns)} → ${colList(edge.to_columns)}`;
  return (
    <button
      type="button"
      onClick={() => known && onOpen(target)}
      disabled={!known}
      title={edge.name}
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "flex-start",
        gap: 3,
        width: "100%",
        padding: "8px 10px",
        background: K.card,
        border: `1px ${edge.validated ? "solid" : "dashed"} ${K.line}`,
        borderRadius: RADIUS.md,
        cursor: known ? "pointer" : "default",
        textAlign: "left",
        fontFamily: sans,
        minWidth: 0,
      }}
    >
      <span style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, minWidth: 0 }}>
        <span style={{ fontFamily: mono, fontSize: 12, fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>
          {target}
        </span>
        {edge.cross_owner && <SmallBadge tone="purple">{t("otherApp")}</SmallBadge>}
        {!edge.cross_owner && edge.cross_schema && <SmallBadge tone="info">{t("otherSchema")}</SmallBadge>}
        {!edge.validated && <SmallBadge tone="warning">{t("notValid")}</SmallBadge>}
      </span>
      <span style={{ fontFamily: mono, fontSize: 11, color: K.muted, overflowWrap: "anywhere" }}>{cols}</span>
    </button>
  );
}

function NeighbourMap({
  id,
  model,
  known,
  onOpen,
}: {
  id: string;
  model: ErdModel;
  known: Set<string>;
  onOpen: (id: string) => void;
}) {
  const t = useT(DM);
  const isPhone = useIsPhone();
  const n = useMemo(() => neighbours(model, id), [model, id]);
  const column = (title: string, icon: React.ReactNode, edges: ErdEdge[], direction: "out" | "in") => (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, fontWeight: 600, color: K.muted }}>
        {icon} {title} ({edges.length})
      </div>
      {edges.length ? (
        edges.map((e) => (
          <EdgeChip
            key={`${e.from}:${e.name}`}
            edge={e}
            direction={direction}
            onOpen={onOpen}
            known={known.has(direction === "out" ? e.to : e.from)}
          />
        ))
      ) : (
        <Muted>{t("none")}</Muted>
      )}
    </div>
  );
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: isPhone ? "minmax(0, 1fr)" : "minmax(0, 1fr) auto minmax(0, 1fr)",
        gap: 12,
        alignItems: "start",
      }}
    >
      {column(t("referencedBy"), <ArrowRight size={13} />, n.incoming, "in")}
      {!isPhone && (
        <div
          style={{
            alignSelf: "center",
            padding: "10px 12px",
            border: `2px solid ${K.accent}`,
            borderRadius: RADIUS.md,
            fontFamily: mono,
            fontSize: 12,
            fontWeight: 700,
            color: K.ink,
            maxWidth: 220,
            overflowWrap: "anywhere",
            textAlign: "center",
          }}
        >
          {id}
        </div>
      )}
      {column(t("references"), <ArrowRight size={13} />, n.outgoing, "out")}
    </div>
  );
}

// ── table detail ────────────────────────────────────────────────────────────────────────────────

function TableDetail({
  ref_,
  model,
  diff,
  known,
  onOpen,
}: {
  ref_: TableRef;
  model: ErdModel;
  diff: ErdDiff | null;
  known: Set<string>;
  onOpen: (id: string) => void;
}) {
  const tr = useT(DM);
  const t = ref_.table;
  const change = changeOf(diff, ref_.id);
  const td = diff?.tables_changed[ref_.id];
  return (
    <div style={{ fontFamily: sans, minWidth: 0 }}>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
        <span style={{ fontFamily: mono, fontSize: 15, fontWeight: 700, color: K.ink, overflowWrap: "anywhere" }}>
          {ref_.id}
        </span>
        {change && <SmallBadge tone={CHANGE_TONE[change]}>{tr("sinceDump", tr(CHANGE_KEY[change]))}</SmallBadge>}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 12px", marginTop: 6 }}>
        <Muted>{tr("app", ref_.ownerApp)}</Muted>
        {t.owner_role && <Muted>{tr("owner", t.owner_role)}</Muted>}
        {t.kind && <Muted>{t.kind}</Muted>}
        <Muted>{t.rls ? tr("rlsOn", t.rls_forced) : tr("noRls")}</Muted>
        {t.is_partition && <Muted>{tr("partition")}</Muted>}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", marginTop: 8 }}>
        <Muted>PK</Muted>
        {t.primary_key.length ? <Code>{colList(t.primary_key)}</Code> : <Muted>{tr("noneLower")}</Muted>}
        {t.unique_keys.map((u) => (
          <span key={u.join(",")} style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
            <Muted>UK</Muted>
            <Code>{colList(u)}</Code>
          </span>
        ))}
      </div>

      {td && (td.columns_removed?.length || td.fks_removed?.length || td.fks_added?.length || td.primary_key || td.rls) && (
        <div
          style={{
            marginTop: 12,
            padding: "8px 12px",
            borderRadius: RADIUS.md,
            background: K.amberSoft,
            fontSize: 12,
            color: K.body,
            display: "flex",
            flexDirection: "column",
            gap: 4,
          }}
        >
          {td.columns_removed?.length ? <span>{tr("columnsRemoved", td.columns_removed.join(", "))}</span> : null}
          {td.primary_key ? (
            <span>
              PK {colList(td.primary_key.from) || tr("noneLower")} → {colList(td.primary_key.to) || tr("noneLower")}
            </span>
          ) : null}
          {td.fks_added?.length ? <span>{tr("fksAddedList", td.fks_added.join(", "))}</span> : null}
          {td.fks_removed?.length ? <span>{tr("fksRemovedList", td.fks_removed.join(", "))}</span> : null}
          {td.rls ? <span>{tr("rlsChanged")}</span> : null}
        </div>
      )}

      <SectionLabel>{tr("neighbours")}</SectionLabel>
      <NeighbourMap id={ref_.id} model={model} known={known} onOpen={onOpen} />

      <SectionLabel>{tr("columns", t.columns.length)}</SectionLabel>
      <div style={{ border: `1px solid ${K.line}`, borderRadius: RADIUS.md, overflow: "hidden" }}>
        {t.columns.map((c, i) => {
          const ch = columnChange(diff, ref_.id, c.name);
          return (
            <div
              key={c.name}
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "baseline",
                gap: "4px 8px",
                padding: "7px 10px",
                borderTop: i ? `1px solid ${K.line2}` : "none",
                minWidth: 0,
              }}
            >
              <span
                style={{
                  fontFamily: mono,
                  fontSize: 12,
                  fontWeight: c.pk ? 700 : 500,
                  color: K.ink,
                  overflowWrap: "anywhere",
                  flex: "0 1 auto",
                }}
              >
                {c.name}
              </span>
              <span style={{ fontFamily: mono, fontSize: 11, color: K.muted, overflowWrap: "anywhere" }}>{c.type}</span>
              {c.not_null && <SmallBadge tone="neutral">{tr("notNull")}</SmallBadge>}
              {c.pk && <SmallBadge tone="accent">PK</SmallBadge>}
              {c.fk && <SmallBadge tone="info">FK</SmallBadge>}
              {c.uk && <SmallBadge tone="purple">UK</SmallBadge>}
              {c.tag && (
                <SmallBadge tone={tagTone(c.tag)} title={tr("dataTag")}>
                  {c.tag}
                </SmallBadge>
              )}
              {ch === "added" && <SmallBadge tone="success">{tr("changeAdded")}</SmallBadge>}
              {Array.isArray(ch) && (
                <SmallBadge tone="warning" title={ch.join("; ")}>
                  {tr("changeChanged")}
                </SmallBadge>
              )}
              {Array.isArray(ch) && (
                <span style={{ flexBasis: "100%", fontSize: 11, color: K.muted, overflowWrap: "anywhere" }}>
                  {ch.join(" · ")}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── page ────────────────────────────────────────────────────────────────────────────────────────

export default function DataModelPage() {
  const t = useT(DM);
  const { formatDateTime } = useFormat();
  const isPhone = useIsPhone();
  const qc = useQueryClient();
  const { data, error, isLoading, isFetching, refetch } = useQuery({
    queryKey: DATA_KEY,
    queryFn: () => apiGet<DataModelResp>(DATA_PATH, { admin: true }),
    staleTime: 5 * 60_000,
    retry: (count, err) => !(err instanceof ApiError && (err.status === 404 || err.status === 403)) && count < 2,
  });
  // Reload = ask the server to re-read storage once; the answer replaces the cached one.
  const reload = useMutation({
    mutationFn: () => apiGet<DataModelResp>(`${DATA_PATH}?refresh=1`, { admin: true }),
    onSuccess: (d) => qc.setQueryData(DATA_KEY, d),
  });

  const [query, setQuery] = useState("");
  const [schema, setSchema] = useState(ALL);
  const [changedOnly, setChangedOnly] = useState(false);
  const [neighboursOnly, setNeighboursOnly] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const model = data?.model;
  const diff = data?.diff ?? null;
  const all = useMemo(() => (model ? flattenTables(model) : []), [model]);
  const byId = useMemo(() => new Map(all.map((r) => [r.id, r])), [all]);
  const known = useMemo(() => new Set(byId.keys()), [byId]);
  const selected = selectedId ? byId.get(selectedId) ?? null : null;
  const near = useMemo(
    () => (model && selected && neighboursOnly ? neighbours(model, selected.id).ids : null),
    [model, selected, neighboursOnly],
  );

  const shown = useMemo(
    () =>
      all.filter(
        (r) =>
          (schema === ALL || r.schema === schema) &&
          (!changedOnly || changeOf(diff, r.id) !== null) &&
          (!near || near.has(r.id)) &&
          matchesQuery(r, query),
      ),
    [all, schema, changedOnly, diff, near, query],
  );

  const open = (id: string) => {
    setSelectedId(id);
    if (isPhone) setDrawerOpen(true);
  };

  const tableCount = all.length;
  const edgeCount = model?.edges.length ?? 0;
  const description = model
    ? t("descriptionLoaded", formatDateTime(model.generated_at), model.fingerprint.slice(0, 12), model.schemas.length,
        tableCount, edgeCount)
    : t("descriptionEmpty");
  const busy = isFetching || reload.isPending;
  const loadError = (reload.error as Error | null) ?? (error as Error | null);
  const notPublished = error instanceof ApiError && error.status === 404;

  const detail = selected && model && (
    <TableDetail ref_={selected} model={model} diff={diff} known={known} onOpen={open} />
  );

  return (
    <PageFrame style={{ fontFamily: sans }}>
      <SkeletonStyle />
      <PageHeader
        title={t("title")}
        description={description}
        actions={
          <Button variant="ghost" size="sm" onClick={() => reload.mutate()} disabled={busy} ariaLabel={t("reloadAria")}>
            {busy ? <Spinner size={13} /> : <RefreshCw size={13} />} {t("reload")}
          </Button>
        }
      />

      {isLoading ? (
        <Card style={{ padding: 16 }}>
          <TableSkeleton rows={8} cols={3} />
        </Card>
      ) : notPublished ? (
        <Card>
          <EmptyState
            icon={<Network size={30} />}
            title={t("notPublishedTitle")}
            description={t("notPublishedBody")}
          />
        </Card>
      ) : loadError && !model ? (
        <Card>
          <ErrorState message={t("loadError", loadError.message)} onRetry={() => refetch()} />
        </Card>
      ) : !model || !tableCount ? (
        <Card>
          <EmptyState icon={<Network size={30} />} title={t("noTables")} />
        </Card>
      ) : (
        <>
          <DiffSummary
            diff={diff}
            previousAt={data?.previous?.generated_at ?? null}
            changedOnly={changedOnly}
            onToggleChangedOnly={() => setChangedOnly((v) => !v)}
          />

          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, marginBottom: 14 }}>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                flex: "1 1 240px",
                minWidth: 0,
                padding: "8px 12px",
                background: K.card,
                border: `1px solid ${K.line}`,
                borderRadius: RADIUS.md,
              }}
            >
              <Search size={14} style={{ color: K.muted2, flexShrink: 0 }} />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setQuery("");
                }}
                placeholder={t("searchPlaceholder")}
                aria-label={t("searchAria")}
                style={{
                  flex: 1,
                  minWidth: 0,
                  border: "none",
                  outline: "none",
                  background: "transparent",
                  color: K.ink,
                  fontSize: 13,
                  fontFamily: sans,
                }}
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery("")}
                  aria-label={t("clearSearch")}
                  style={{ display: "inline-flex", background: "none", border: "none", padding: 0, cursor: "pointer", color: K.muted }}
                >
                  <X size={14} />
                </button>
              )}
            </label>
            <select
              value={schema}
              onChange={(e) => setSchema(e.target.value)}
              aria-label={t("schema")}
              style={{
                padding: "8px 10px",
                background: K.card,
                color: K.ink,
                border: `1px solid ${K.line}`,
                borderRadius: RADIUS.md,
                fontSize: 13,
                fontFamily: sans,
                maxWidth: "100%",
                minWidth: 0,
              }}
            >
              <option value={ALL}>{t("allSchemas")}</option>
              {model.schemas
                .filter((s) => s.tables.length > 0)
                .map((s) => (
                  <option key={s.schema} value={s.schema}>
                    {s.schema} ({s.owner_app}, {s.tables.length})
                  </option>
                ))}
            </select>
            <Button
              variant={neighboursOnly ? "primary" : "ghost"}
              size="sm"
              onClick={() => setNeighboursOnly((v) => !v)}
              disabled={!selected}
              title={selected ? t("onlyNeighbours", selected.id) : t("selectFirst")}
            >
              <Network size={13} /> {neighboursOnly ? t("neighboursOnly") : t("tablePlusOne")}
            </Button>
          </div>

          <div
            style={{
              display: "grid",
              gridTemplateColumns: isPhone ? "minmax(0, 1fr)" : "minmax(260px, 340px) minmax(0, 1fr)",
              gap: 14,
              alignItems: "start",
            }}
          >
            <Card style={{ overflow: "hidden" }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 8,
                  padding: "10px 14px",
                  fontSize: 12,
                  color: K.muted,
                }}
              >
                <span>{t("shownOf", shown.length, tableCount)}</span>
                {neighboursOnly && selected && (
                  <button
                    type="button"
                    onClick={() => setNeighboursOnly(false)}
                    style={{ display: "inline-flex", alignItems: "center", gap: 4, background: "none", border: "none", cursor: "pointer", color: K.accentDeep, fontSize: 12, fontFamily: sans }}
                  >
                    <ArrowLeft size={12} /> {t("allTables")}
                  </button>
                )}
              </div>
              <div style={{ maxHeight: isPhone ? "none" : "calc(100vh - 300px)", overflowY: "auto" }}>
                {shown.length ? (
                  shown.slice(0, LIST_LIMIT).map((r) => (
                    <TableRow
                      key={r.id}
                      ref_={r}
                      selected={r.id === selected?.id}
                      change={changeOf(diff, r.id)}
                      hits={query ? columnHits(r, query) : []}
                      onSelect={() => open(r.id)}
                    />
                  ))
                ) : (
                  <EmptyState title={t("noMatchTitle")} description={t("noMatchBody")} />
                )}
                {shown.length > LIST_LIMIT && (
                  <div style={{ padding: "10px 14px", fontSize: 12, color: K.muted }}>
                    {t("more", shown.length - LIST_LIMIT)}
                  </div>
                )}
              </div>
            </Card>

            {!isPhone && (
              <Card style={{ padding: "16px 18px" }}>
                {detail || (
                  <EmptyState
                    icon={<Network size={30} />}
                    title={t("selectTitle")}
                    description={t("selectBody")}
                  />
                )}
              </Card>
            )}
          </div>

          {isPhone && (
            <Drawer open={drawerOpen && !!selected} onClose={() => setDrawerOpen(false)} title={t("drawerTitle")}>
              {detail}
            </Drawer>
          )}
        </>
      )}
    </PageFrame>
  );
}
