"use client";
// app/admin/platform/catalogue/ProductMap.tsx — AA-1106: Platform › Product map.
//
// The root repo product catalogue (docs/catalogue/features.yaml + integrations.yaml, checked by
// tools/catalogue/catalogue.py and published by the Docs sync workflow) as five tabs: Apps (one card per app),
// Features (filter by app, status, role and condition; a drawer with the pages, conditions, links and UAT scenarios),
// Links (every link between the apps and to outside providers: contract, auth, conditions, failure, kill switch),
// Journeys (business stories as steps of features and links) and Graph (one arrow per pair of apps). Conditions link to
// Platform › Settings (the app tab and area of a setting) and guides to the Docs reader.
//
// API (backend api/routers/admin_product_catalogue.py; BFF /api/admin/platform/catalogue, admin only — lib/rbac.ts):
//   GET /api/admin/platform/catalogue[?refresh=1]
// Pure helpers: lib/productCatalogue.ts. Kit only. Filters live in the URL (FilterBar), the tab too (?tab=).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, BookOpen, ExternalLink, Map as MapIcon, RefreshCw, SlidersHorizontal } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge, Button, DataTable, Drawer, LinkButton, EmptyState, ErrorState, FilterBar, KpiTile, PageFrame, PageHeader, Section,
  SkeletonStyle, Tabs, apiGet, mono, useFormat, useLang, useT, useUrlFilters, K, type ColumnDef, type Tone,
} from "../../../_kit";
import {
  CATALOGUE_PATH, CONDITION_KINDS, EXTERNAL, STATUSES, conditionKinds, docHref, graphEdges, graphLayout, localName,
  localSummary, matchesFeature, matchesIntegration, roleOptions, settingHref, shortName, summaryCounts,
  type CatalogueFeature, type CatalogueIntegration, type CatalogueResp, type CatalogueStatus, type ConditionKind,
  type Conditions, type IntegrationKind,
} from "@/lib/productCatalogue";
import { CATALOGUE } from "./messages";

const KEY = ["product-catalogue"];
const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const chipRow = { display: "flex", flexWrap: "wrap", gap: 4 } as const;
const STATUS_TONE: Record<CatalogueStatus, Tone> = { live: "success", beta: "info", planned: "neutral", retired: "danger" };
const STATUS_LABEL = {
  live: "statuslive", beta: "statusbeta", planned: "statusplanned", retired: "statusretired",
} as const;
const COND_LABEL = {
  entitlements: "condentitlements", settings: "condsettings", connections: "condconnections", secrets: "condsecrets",
  tenant_kinds: "condtenant_kinds",
} as const;
const KIND_LABEL = {
  db_view: "kinddb_view", http_api: "kindhttp_api", event_outbox: "kindevent_outbox", webhook: "kindwebhook",
  m2m: "kindm2m", provider: "kindprovider",
} as const;
const KINDS = Object.keys(KIND_LABEL) as IntegrationKind[];
type Tab = "apps" | "features" | "links" | "journeys" | "graph";
const TABS: Tab[] = ["apps", "features", "links", "journeys", "graph"];

function useCatalogueT() {
  return useT(CATALOGUE);
}
type CT = ReturnType<typeof useCatalogueT>;

function Code({ children, nowrap = false }: { children: React.ReactNode; nowrap?: boolean }) {
  return <code style={{ fontFamily: mono, fontSize: 11.5, color: K.ink, overflowWrap: "anywhere",
    ...(nowrap ? { whiteSpace: "nowrap", flexShrink: 0 } : {}) }}>{children}</code>;
}

function StatusBadge({ status }: { status: CatalogueStatus }) {
  const t = useCatalogueT();
  return <Badge tone={STATUS_TONE[status] ?? "neutral"}>{t(STATUS_LABEL[status] ?? "statusplanned")}</Badge>;
}

function AppName({ id, data, short = false }: { id: string; data: CatalogueResp; short?: boolean }) {
  const t = useCatalogueT();
  const lang = useLang();
  if (id === EXTERNAL) return <>{t("external")}</>;
  const app = data.apps.find((a) => a.id === id);
  const name = app ? localName(app, lang) : id;
  return <>{short ? shortName(name) : name}</>;
}

function FromTo({ i, data }: { i: CatalogueIntegration; data: CatalogueResp }) {
  return (
    <span style={{ ...small, display: "inline-flex", gap: 4, alignItems: "center", flexWrap: "wrap" }}>
      <AppName id={i.producer} data={data} short /> <ArrowRight size={11} />
      {i.consumers.map((c, n) => <span key={c}><AppName id={c} data={data} short />{n < i.consumers.length - 1 && ","}</span>)}
    </span>
  );
}

function ConditionChips({ c }: { c: Conditions }) {
  const t = useCatalogueT();
  const kinds = conditionKinds(c);
  if (!kinds.length) return <span style={small}>{t("none")}</span>;
  return (
    <span style={chipRow}>
      {kinds.map((k) => <Badge key={k} tone="accent" dot={false}>{t(COND_LABEL[k])} · {(c[k] as string[]).length}</Badge>)}
    </span>
  );
}

function ConditionDetail({ c, data }: { c: Conditions; data: CatalogueResp }) {
  const t = useCatalogueT();
  const kinds = conditionKinds(c);
  if (!kinds.length && !c.note) return <span style={small}>{t("none")}</span>;
  return (
    <div style={{ display: "grid", gap: 8 }}>
      {kinds.map((k) => (
        <div key={k} style={{ display: "grid", gap: 4 }}>
          <span style={{ ...small, fontWeight: 600 }}>{t(COND_LABEL[k])}</span>
          <span style={{ display: "grid", gap: 3 }}>
            {(c[k] as string[]).map((name) => {
              const href = k === "settings" ? settingHref(name, data.settings) : null;
              return (
                <span key={name} style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                  <Code>{name}</Code>
                  {href && <a href={href} style={{ ...small, color: K.accentDeep, display: "inline-flex", gap: 3,
                    alignItems: "center" }}><SlidersHorizontal size={11} /> {t("openSetting")}</a>}
                </span>
              );
            })}
          </span>
        </div>
      ))}
      {c.note && <div style={small}><strong>{t("note")}:</strong> {c.note}</div>}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "grid", gap: 4 }}>
      <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.05em", textTransform: "uppercase",
        color: K.muted }}>{label}</span>
      <div style={{ fontSize: 13, color: K.ink, lineHeight: 1.5 }}>{children}</div>
    </div>
  );
}

function IdChip({ id, onClick }: { id: string; onClick?: () => void }) {
  return onClick
    ? <LinkButton size="sm" onClick={onClick} testId={`catalogue-open-${id}`}
      style={{ border: `1px solid ${K.line}`, borderRadius: 6, padding: "2px 7px", fontFamily: mono }}>{id}</LinkButton>
    : <span style={{ border: `1px solid ${K.line}`, borderRadius: 6, padding: "2px 7px", fontFamily: mono,
      fontSize: 11.5, color: K.ink }}>{id}</span>;
}

// ── Apps ───────────────────────────────────────────────────────────────────────────────────────────────────────────

function AppsTab({ data, go }: { data: CatalogueResp; go: (tab: Tab, app: string) => void }) {
  const t = useCatalogueT();
  const lang = useLang();
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 320px), 1fr))", gap: 12 }}>
      {data.apps.map((a) => (
        <Section key={a.id} testId={`catalogue-app-${a.id}`} title={localName(a, lang)}
          description={a.repo ? <Code>{a.repo}</Code> : undefined}
          bodyStyle={{ display: "grid", gap: 10 }}>
          <div style={{ fontSize: 13, color: K.ink, lineHeight: 1.55 }}>{localSummary(a, lang)}</div>
          <div style={small}>{t("featuresOf", a.features ?? 0, a.live ?? 0)}
            {a.surface ? <> · {t("surfaceOf", a.surface)}</> : null}</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button size="sm" variant="secondary" onClick={() => go("features", a.id)}>{t("showFeatures")}</Button>
            <Button size="sm" variant="ghost" onClick={() => go("links", a.id)}>{t("showLinks")}</Button>
          </div>
        </Section>
      ))}
    </div>
  );
}

// ── Features ───────────────────────────────────────────────────────────────────────────────────────────────────────

function FeatureDrawer({ f, data, onClose, openFeature, openLink }: {
  f: CatalogueFeature; data: CatalogueResp; onClose: () => void; openFeature: (id: string) => void;
  openLink: (id: string) => void;
}) {
  const t = useCatalogueT();
  const lang = useLang();
  return (
    <Drawer open onClose={onClose} width={560} title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center",
      flexWrap: "wrap" }}><Code>{f.id}</Code> {localName(f, lang)}</span>}>
      <div style={{ display: "grid", gap: 16 }} data-testid="catalogue-feature-drawer">
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <StatusBadge status={f.status} />
          <span style={small}><AppName id={f.app} data={data} /> · {t("module")} <Code nowrap>{f.module}</Code> ·{" "}
            {t("owner")} <Code nowrap>{f.owner}</Code></span>
        </div>
        <Field label={t("summary")}>{localSummary(f, lang)}</Field>
        <Field label={t("colRoles")}><span style={chipRow}>{f.roles.map((r) => <IdChip key={r} id={r} />)}</span></Field>
        <Field label={t("needs")}><ConditionDetail c={f.conditions} data={data} /></Field>
        {f.surface.length > 0 && (
          <Field label={t("surface")}>
            <span style={{ display: "grid", gap: 2 }}>{f.surface.map((s) => <Code key={s}>{s}</Code>)}</span>
          </Field>
        )}
        {f.depends_on.length > 0 && (
          <Field label={t("dependsOn")}>
            <span style={chipRow}>{f.depends_on.map((id) => <IdChip key={id} id={id} onClick={() => openFeature(id)} />)}</span>
          </Field>
        )}
        {f.integrations.length > 0 && (
          <Field label={t("linksOf")}>
            <span style={chipRow}>{f.integrations.map((id) => <IdChip key={id} id={id} onClick={() => openLink(id)} />)}</span>
          </Field>
        )}
        {f.uat.length > 0 && (
          <Field label={t("uat")}>
            <span style={{ display: "grid", gap: 4 }}>
              {f.uat.map((id) => (
                <span key={id} style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                  <Code nowrap>{id}</Code><span style={small}>{data.scenarios[id]?.story ?? ""}</span>
                </span>
              ))}
            </span>
          </Field>
        )}
        {f.guide && (
          <Field label={t("guide")}>
            <a href={docHref(f.guide)} style={{ color: K.accentDeep, display: "inline-flex", gap: 6, alignItems: "center" }}>
              <BookOpen size={13} /> {t("openGuide")}</a>
          </Field>
        )}
      </div>
    </Drawer>
  );
}

function FeaturesTab({ data, onOpen }: { data: CatalogueResp; onOpen: (id: string) => void }) {
  const t = useCatalogueT();
  const lang = useLang();
  const { get } = useUrlFilters();
  const filter = { q: get("q"), app: get("app"), status: get("status"), role: get("role"), cond: get("cond") };
  const rows = useMemo(() => data.features.filter((f) => matchesFeature(f, filter)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data, filter.q, filter.app, filter.status, filter.role, filter.cond]);
  const columns = useMemo<ColumnDef<CatalogueFeature, unknown>[]>(() => [
    { id: "feature", header: t("colFeature"), accessorFn: (f) => `${f.id} ${localName(f, lang)}`,
      cell: ({ row: { original: f } }) => (
        <div style={{ display: "grid", gap: 2 }}>
          <span style={{ fontWeight: 600, color: K.ink }}>{localName(f, lang)}</span>
          <span style={small}><Code>{f.id}</Code> · {f.module}</span>
        </div>
      ) },
    { id: "app", header: t("colApp"), accessorKey: "app",
      cell: ({ row: { original: f } }) => <span style={small}><AppName id={f.app} data={data} short /></span> },
    { id: "status", header: t("colStatus"), accessorKey: "status",
      cell: ({ row: { original: f } }) => <StatusBadge status={f.status} /> },
    { id: "roles", header: t("colRoles"), accessorFn: (f) => f.roles.join(", "),
      cell: ({ row: { original: f } }) => <span style={small}>{f.roles.join(", ")}</span> },
    { id: "needs", header: t("colNeeds"), enableSorting: false,
      cell: ({ row: { original: f } }) => <ConditionChips c={f.conditions} /> },
    { id: "uat", header: t("colEvidence"), accessorFn: (f) => f.uat.length,
      cell: ({ getValue }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{String(getValue())}</span> },
  ], [t, lang, data]);
  const apps = data.apps.map((a) => ({ value: a.id, label: localName(a, lang) }));
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
      <FilterBar testId="catalogue-feature-filters" search={{ placeholder: t("search") }} selects={[
        { key: "app", label: t("app"), allLabel: t("allApps"), options: apps, width: 200 },
        { key: "status", label: t("status"), allLabel: t("allStatuses"),
          options: STATUSES.map((s) => ({ value: s, label: t(STATUS_LABEL[s]) })), width: 150 },
        { key: "role", label: t("role"), allLabel: t("allRoles"),
          options: roleOptions(data.features).map((r) => ({ value: r, label: r })), width: 170 },
        { key: "cond", label: t("condition"), allLabel: t("anyCondition"),
          options: CONDITION_KINDS.map((k: ConditionKind) => ({ value: k, label: t(COND_LABEL[k]) })), width: 170 },
      ]} />
      <DataTable<CatalogueFeature> tableId="catalogue-features" data={rows} columns={columns} getRowId={(f) => f.id}
        rowTestId={(f) => `catalogue-feature-${f.id}`} onRowClick={(f) => onOpen(f.id)} paginate={false} searchable={false}
        tableMinWidth={900} emptyTitle={t("noRows")} enableCsv csvFilename="product-features.csv" />
    </div>
  );
}

// ── Links ──────────────────────────────────────────────────────────────────────────────────────────────────────────

function LinkDrawer({ i, data, onClose, openFeature }: {
  i: CatalogueIntegration; data: CatalogueResp; onClose: () => void; openFeature: (id: string) => void;
}) {
  const t = useCatalogueT();
  return (
    <Drawer open onClose={onClose} width={560} title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center",
      flexWrap: "wrap" }}><Code>{i.id}</Code> {i.name}</span>}>
      <div style={{ display: "grid", gap: 16 }} data-testid="catalogue-link-drawer">
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <StatusBadge status={i.status} />
          <Badge tone="purple" dot={false}>{t(KIND_LABEL[i.kind] ?? "kindm2m")}</Badge>
          {i.personal_data && <Badge tone="warning">{t("colPersonal")}</Badge>}
        </div>
        <Field label={t("colFromTo")}>
          <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <strong><AppName id={i.producer} data={data} /></strong> <ArrowRight size={13} />
            {i.consumers.map((c, n) => <span key={c}><AppName id={c} data={data} />{n < i.consumers.length - 1 && ","}</span>)}
          </span>
        </Field>
        <Field label={t("contracts")}>
          {i.contracts.length
            ? <span style={{ display: "grid", gap: 3 }}>
              {i.contracts.map((c) => <span key={c.file}><Code>{c.file}</Code> <span style={small}>v{c.version}</span></span>)}
              <span style={small}>{t("contractsWhere")}</span>
            </span>
            : <span style={small}>{t("contractsNone")}</span>}
        </Field>
        <Field label={t("auth")}>{i.auth}</Field>
        <Field label={t("needs")}><ConditionDetail c={i.conditions} data={data} /></Field>
        <Field label={t("cadence")}>{i.cadence}</Field>
        <Field label={t("failure")}>{i.failure}</Field>
        <Field label={t("killSwitch")}>{i.kill_switch}</Field>
        <Field label={t("featuresOnBothSides")}>
          <span style={chipRow}>{i.features.map((id) => <IdChip key={id} id={id} onClick={() => openFeature(id)} />)}</span>
        </Field>
        <Field label={t("owner")}><Code>{i.owner}</Code></Field>
      </div>
    </Drawer>
  );
}

function LinksTab({ data, onOpen }: { data: CatalogueResp; onOpen: (id: string) => void }) {
  const t = useCatalogueT();
  const lang = useLang();
  const { get } = useUrlFilters();
  const filter = { q: get("q"), app: get("app"), status: get("status"), kind: get("kind") };
  const rows = useMemo(() => data.integrations.filter((i) => matchesIntegration(i, filter)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data, filter.q, filter.app, filter.status, filter.kind]);
  const columns = useMemo<ColumnDef<CatalogueIntegration, unknown>[]>(() => [
    { id: "link", header: t("colLink"), accessorFn: (i) => `${i.id} ${i.name}`,
      cell: ({ row: { original: i } }) => (
        <div style={{ display: "grid", gap: 2 }}>
          <span style={{ fontWeight: 600, color: K.ink }}>{i.name}</span>
          <span style={small}><Code>{i.id}</Code></span>
        </div>
      ) },
    { id: "fromTo", header: t("colFromTo"), accessorFn: (i) => `${i.producer} ${i.consumers.join(" ")}`,
      cell: ({ row: { original: i } }) => <FromTo i={i} data={data} /> },
    { id: "kind", header: t("colKind"), accessorKey: "kind",
      cell: ({ row: { original: i } }) => <span style={small}>{t(KIND_LABEL[i.kind] ?? "kindm2m")}</span> },
    { id: "contract", header: t("colContract"), accessorFn: (i) => i.contracts.map((c) => c.file).join(" "),
      cell: ({ row: { original: i } }) => i.contracts.length
        ? <span style={{ display: "grid", gap: 1 }}>{i.contracts.slice(0, 2).map((c) => <Code key={c.file}>{c.file}</Code>)}
          {i.contracts.length > 2 && <span style={small}>+{i.contracts.length - 2}</span>}</span>
        : <span style={small}>—</span> },
    { id: "needs", header: t("colNeeds"), enableSorting: false,
      cell: ({ row: { original: i } }) => <ConditionChips c={i.conditions} /> },
    { id: "personal", header: t("colPersonal"), accessorFn: (i) => (i.personal_data ? 1 : 0),
      cell: ({ row: { original: i } }) => <span style={small}>{i.personal_data ? t("yes") : t("no")}</span> },
    { id: "status", header: t("colStatus"), accessorKey: "status",
      cell: ({ row: { original: i } }) => <StatusBadge status={i.status} /> },
  ], [t, data]);
  const apps = data.apps.map((a) => ({ value: a.id, label: localName(a, lang) }));
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
      <FilterBar testId="catalogue-link-filters" search={{ placeholder: t("search") }} selects={[
        { key: "app", label: t("app"), allLabel: t("allApps"), options: apps, width: 200 },
        { key: "status", label: t("status"), allLabel: t("allStatuses"),
          options: STATUSES.map((s) => ({ value: s, label: t(STATUS_LABEL[s]) })), width: 150 },
        { key: "kind", label: t("kind"), allLabel: t("allKinds"),
          options: KINDS.map((k) => ({ value: k, label: t(KIND_LABEL[k]) })), width: 190 },
      ]} />
      <DataTable<CatalogueIntegration> tableId="catalogue-links" data={rows} columns={columns} getRowId={(i) => i.id}
        rowTestId={(i) => `catalogue-link-${i.id}`} onRowClick={(i) => onOpen(i.id)} paginate={false} searchable={false}
        tableMinWidth={1000} emptyTitle={t("noRows")} enableCsv csvFilename="product-links.csv" />
    </div>
  );
}

// ── Journeys ───────────────────────────────────────────────────────────────────────────────────────────────────────

function JourneysTab({ data, openFeature, openLink }: {
  data: CatalogueResp; openFeature: (id: string) => void; openLink: (id: string) => void;
}) {
  const t = useCatalogueT();
  const lang = useLang();
  if (!data.journeys.length) return <EmptyState title={t("noRows")} />;
  return (
    <div style={{ display: "grid", gap: 12 }}>
      {data.journeys.map((j) => (
        <Section key={j.id} testId={`catalogue-journey-${j.id}`}
          title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><Code nowrap>{j.id}</Code>
            {localName(j, lang)}</span>}
          description={localSummary(j, lang)} bodyStyle={{ display: "grid", gap: 12 }}>
          <ol style={{ margin: 0, paddingLeft: 20, display: "grid", gap: 10 }}>
            {j.steps.map((s, n) => (
              <li key={n} style={{ fontSize: 13, color: K.ink }}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>{localName(s, lang)}</div>
                <span style={chipRow}>
                  {(s.features ?? []).map((id) => <IdChip key={id} id={id} onClick={() => openFeature(id)} />)}
                  {(s.integrations ?? []).map((id) => <IdChip key={id} id={id} onClick={() => openLink(id)} />)}
                </span>
              </li>
            ))}
          </ol>
          {j.uat.length > 0 && (
            <Field label={t("uat")}>
              <span style={chipRow}>{j.uat.map((id) => <IdChip key={id} id={id} />)}</span>
            </Field>
          )}
        </Section>
      ))}
    </div>
  );
}

// ── Graph ──────────────────────────────────────────────────────────────────────────────────────────────────────────

function GraphTab({ data }: { data: CatalogueResp }) {
  const t = useCatalogueT();
  const lang = useLang();
  const edges = useMemo(() => graphEdges(data.integrations), [data]);
  const nodes = useMemo(() => {
    const ids = data.apps.map((a) => a.id);
    return edges.some((e) => e.from === EXTERNAL || e.to === EXTERNAL) ? [...ids, EXTERNAL] : ids;
  }, [data, edges]);
  const W = 720;
  const H = 520;
  const pos = graphLayout(nodes, W, H, "cis");
  const label = (id: string) => (id === EXTERNAL ? t("external")
    : shortName(localName(data.apps.find((a) => a.id === id) ?? { name: id }, lang)));
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
      <Section bodyStyle={{ display: "grid", gap: 8 }} testId="catalogue-graph">
        <div style={small}>{t("graphNote")}</div>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={t("tabGraph")}
          style={{ width: "100%", height: "auto", maxHeight: 560 }}>
          <defs>
            <marker id="catalogue-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
              orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill={K.muted} />
            </marker>
          </defs>
          {edges.map((e) => {
            const a = pos[e.from];
            const b = pos[e.to];
            if (!a || !b) return null;
            // Curve each direction to its own side so A->B and B->A do not overlap; stop short of the node.
            const dx = b.x - a.x;
            const dy = b.y - a.y;
            const len = Math.hypot(dx, dy) || 1;
            const ux = dx / len;
            const uy = dy / len;
            const sx = a.x + ux * 46;
            const sy = a.y + uy * 22;
            const ex = b.x - ux * 50;
            const ey = b.y - uy * 26;
            const mx = (sx + ex) / 2 - uy * 30;
            const my = (sy + ey) / 2 + ux * 30;
            return (
              <g key={`${e.from}-${e.to}`}>
                <path d={`M ${sx} ${sy} Q ${mx} ${my} ${ex} ${ey}`} fill="none" stroke={K.muted} strokeWidth={1.4}
                  markerEnd="url(#catalogue-arrow)" />
                <circle cx={mx} cy={my} r={10} fill={K.card} stroke={K.line} />
                <text x={mx} y={my + 4} textAnchor="middle" fontSize={11} fontFamily={mono} fill={K.ink}>{e.ids.length}</text>
              </g>
            );
          })}
          {nodes.map((n) => {
            const p = pos[n];
            return (
              <g key={n}>
                <rect x={p.x - 62} y={p.y - 20} width={124} height={40} rx={10}
                  fill={n === EXTERNAL ? K.panel : K.accentTint} stroke={n === EXTERNAL ? K.line : K.accentBorder} />
                <text x={p.x} y={p.y + 4} textAnchor="middle" fontSize={12} fontWeight={600} fill={K.ink}>
                  {label(n)}</text>
              </g>
            );
          })}
        </svg>
      </Section>
      <Section title={t("graphTable")} padded={false}>
        <div style={{ display: "grid" }}>
          {edges.map((e) => (
            <div key={`${e.from}-${e.to}`} style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center",
              padding: "8px 14px", borderTop: `1px solid ${K.line}`, fontSize: 13 }}>
              <strong>{label(e.from)}</strong> <ArrowRight size={12} /> <strong>{label(e.to)}</strong>
              <span style={chipRow}>{e.ids.map((id) => <IdChip key={id} id={id} />)}</span>
            </div>
          ))}
        </div>
      </Section>
    </div>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────────────────────────────────────────────

function Kpis({ counts, t }: { counts: ReturnType<typeof summaryCounts>; t: CT }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 170px), 1fr))", gap: 10 }}>
      <KpiTile label={t("kpiApps")} value={counts.apps} testId="catalogue-kpi-apps" />
      <KpiTile label={t("kpiFeatures")} value={counts.features} hint={t("kpiLive", counts.live)} hintTone="success"
        testId="catalogue-kpi-features" />
      <KpiTile label={t("kpiLinks")} value={counts.links} testId="catalogue-kpi-links" />
      <KpiTile label={t("kpiJourneys")} value={counts.journeys} />
      <KpiTile label={t("kpiUat")} value={counts.uat} />
    </div>
  );
}

function TabBody({ tab, data, go, openFeature, openLink }: {
  tab: Tab; data: CatalogueResp; go: (tab: Tab, app: string) => void; openFeature: (id: string) => void;
  openLink: (id: string) => void;
}) {
  if (tab === "features") return <FeaturesTab data={data} onOpen={openFeature} />;
  if (tab === "links") return <LinksTab data={data} onOpen={openLink} />;
  if (tab === "journeys") return <JourneysTab data={data} openFeature={openFeature} openLink={openLink} />;
  if (tab === "graph") return <GraphTab data={data} />;
  return <AppsTab data={data} go={go} />;
}

function Drawers({ data, feature, link, openFeature, openLink, close }: {
  data: CatalogueResp; feature: string | null; link: string | null; openFeature: (id: string) => void;
  openLink: (id: string) => void; close: () => void;
}) {
  const f = feature ? data.features.find((x) => x.id === feature) : undefined;
  const i = link ? data.integrations.find((x) => x.id === link) : undefined;
  return (
    <>
      {f && <FeatureDrawer f={f} data={data} onClose={close} openFeature={openFeature} openLink={openLink} />}
      {i && <LinkDrawer i={i} data={data} onClose={close} openFeature={openFeature} />}
    </>
  );
}

function Content({ data, t }: { data: CatalogueResp; t: CT }) {
  const { get, setMany } = useUrlFilters();
  const tab = (TABS as string[]).includes(get("tab")) ? (get("tab") as Tab) : "apps";
  const [feature, setFeature] = useState<string | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const counts = summaryCounts(data);
  const openFeature = (id: string) => { setLink(null); setFeature(id); };
  const openLink = (id: string) => { setFeature(null); setLink(id); };
  const close = () => { setFeature(null); setLink(null); };
  const changeTab = (next: string) => setMany({ tab: next === "apps" ? null : next, q: null, app: null, status: null,
    role: null, cond: null, kind: null });
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
      <Kpis counts={counts} t={t} />
      <Tabs ariaLabel={t("title")} active={tab} onChange={changeTab} testId="catalogue-tabs" tabs={[
        { key: "apps", label: t("tabApps"), count: counts.apps },
        { key: "features", label: t("tabFeatures"), count: counts.features },
        { key: "links", label: t("tabLinks"), count: data.integrations.length },
        { key: "journeys", label: t("tabJourneys"), count: counts.journeys },
        { key: "graph", label: t("tabGraph") },
      ]} />
      <TabBody tab={tab} data={data} go={(next, app) => setMany({ tab: next, app })} openFeature={openFeature}
        openLink={openLink} />
      <Drawers data={data} feature={feature} link={link} openFeature={openFeature} openLink={openLink} close={close} />
    </div>
  );
}

/** The bundle query, the refresh (?refresh=1 re-reads the bundle) and the error to show (refresh first). */
function useCatalogue() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: KEY, queryFn: () => apiGet<CatalogueResp>(CATALOGUE_PATH), staleTime: 60_000 });
  const refresh = useMutation({
    mutationFn: () => apiGet<CatalogueResp>(`${CATALOGUE_PATH}?refresh=1`),
    onSuccess: (data) => qc.setQueryData(KEY, data),
  });
  const error = (refresh.error as Error | null) ?? (q.error as Error | null);
  return { q, refresh, error, busy: q.isFetching || refresh.isPending };
}

function Description({ data, t }: { data: CatalogueResp | undefined; t: CT }) {
  const { formatDateTime } = useFormat();
  const sha = data?.source?.sha;
  const when = data?.generated_at;
  return (
    <>
      {t("description")}
      {sha && <><br /><span style={small}>{t("source", sha.slice(0, 12), when ? formatDateTime(when) : "—")}</span></>}
    </>
  );
}

function LoadError({ error, t, onRetry }: { error: Error; t: CT; onRetry: () => void }) {
  const status = (error as { status?: number }).status;
  return status === 404
    ? <EmptyState icon={<ExternalLink size={18} />} title={t("notPublished")} />
    : <ErrorState message={`${t("loadError")}: ${error.message}`} onRetry={onRetry} />;
}

export default function ProductMap() {
  const t = useCatalogueT();
  const { q, refresh, error, busy } = useCatalogue();
  const data = q.data;
  return (
    <PageFrame>
      <SkeletonStyle />
      <PageHeader
        title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><MapIcon size={18} /> {t("title")}</span>}
        description={<Description data={data} t={t} />}
        actions={<Button size="sm" variant="secondary" onClick={() => refresh.mutate()}
          disabled={busy} testId="catalogue-refresh">
          <RefreshCw size={13} /> {t("refresh")}</Button>}
      />
      {error && <LoadError error={error} t={t} onRetry={() => void q.refetch()} />}
      {!data && !error && <KpiTile label={t("kpiFeatures")} value={undefined} />}
      {data && <Content data={data} t={t} />}
    </PageFrame>
  );
}
