"use client";
// app/admin/api-docs/page.tsx — AA-577 follow-up: a read-only reader for the AA-CIS OpenAPI schema.
//
// Why: AA-577 put /docs, /redoc and /openapi.json behind the X-Admin-Secret header on ECS (bots were
// scanning them), so a browser can no longer open them. This page reads the same schema through the
// same-origin admin BFF (GET /api/admin/openapi.json → backend GET /admin/openapi.json; the BFF adds
// the secret and the verified role) and renders it with the kit. Admin role only (lib/rbac.ts
// PAGE_RULES + API_READ_RULES; the backend re-checks x-admin-role).
//
// Read-only on purpose: no "Try it out", no call to the API besides the one spec fetch (react-query,
// cached 5 min). No Swagger UI / CDN script: grouping by tag, method badge, path, summary, parameters,
// request body and responses are enough to read the API, at no bundle cost.

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, RefreshCw, Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import { stripIssueIdsDeep } from "@/lib/issueIds";
import {
  Badge,
  Button,
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
  PageFrame,
  useT,
  type Tone,
  Input,
  IconButton, Pressable,
} from "../../_kit";
import { M } from "./messages";
import {
  buildOps,
  groupOps,
  isEmptySchema,
  matchesQuery,
  resolveSchema,
  schemaLabel,
  type HttpMethod,
  type Op,
  type OpenApiSpec,
  type OpGroup,
  type ParamObj,
  type SchemaObj,
} from "./openapi";

const SPEC_PATH = "/api/admin/openapi.json";

const METHOD_TONE: Record<HttpMethod, Tone> = {
  get: "info",
  post: "success",
  put: "warning",
  patch: "warning",
  delete: "danger",
  head: "neutral",
  options: "neutral",
  trace: "neutral",
};

function responseTone(code: string): Tone {
  if (code.startsWith("2")) return "success";
  if (code.startsWith("3")) return "info";
  if (code.startsWith("4")) return "warning";
  if (code.startsWith("5")) return "danger";
  return "neutral";
}

function toggleIn(set: Set<string>, key: string): Set<string> {
  const next = new Set(set);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

// ── small pieces ────────────────────────────────────────────────────────────────────────────────

function MethodBadge({ method }: { method: HttpMethod }) {
  return (
    <Badge tone={METHOD_TONE[method]} dot={false} style={{ minWidth: 58, justifyContent: "center", fontFamily: mono }}>
      {method}
    </Badge>
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

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        color: K.muted,
        margin: "14px 0 6px",
      }}
    >
      {children}
    </div>
  );
}

function Row({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "baseline",
        gap: "4px 10px",
        padding: "7px 0",
        borderTop: `1px solid ${K.line2}`,
        minWidth: 0,
      }}
    >
      {children}
    </div>
  );
}

function Muted({ children }: { children: React.ReactNode }) {
  return <span style={{ fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>{children}</span>;
}

function ParamRow({ p }: { p: ParamObj }) {
  const t = useT(M);
  const name = p.name ?? (p.$ref ? p.$ref.split("/").pop() : "?");
  return (
    <Row>
      <Code>{name}</Code>
      {p.in && <Muted>{t("paramIn", p.in)}</Muted>}
      <Muted>{schemaLabel(p.schema)}</Muted>
      {p.required && <Badge tone="danger" dot={false}>{t("required")}</Badge>}
      {p.deprecated && <Badge tone="neutral" dot={false}>{t("deprecated")}</Badge>}
      {p.description && (
        <div style={{ flexBasis: "100%", fontSize: 12, color: K.body, whiteSpace: "pre-wrap" }}>{p.description}</div>
      )}
    </Row>
  );
}

/** Top-level fields of a request-body object schema (one level: field name, type, required). */
function SchemaFields({ spec, schema }: { spec: OpenApiSpec; schema: SchemaObj | undefined }) {
  const t = useT(M);
  const resolved = resolveSchema(spec, schema);
  const props = resolved?.properties;
  if (!props || !Object.keys(props).length) return null;
  const required = new Set(resolved?.required ?? []);
  return (
    <div style={{ marginTop: 6 }}>
      {Object.entries(props).map(([name, s]) => (
        <Row key={name}>
          <Code>{name}</Code>
          <Muted>{schemaLabel(s)}</Muted>
          {required.has(name) && <Badge tone="danger" dot={false}>{t("required")}</Badge>}
          {s.description && (
            <div style={{ flexBasis: "100%", fontSize: 12, color: K.body, whiteSpace: "pre-wrap" }}>{s.description}</div>
          )}
        </Row>
      ))}
    </div>
  );
}

function OpDetails({ op, spec }: { op: Op; spec: OpenApiSpec }) {
  const t = useT(M);
  return (
    <div style={{ padding: "4px 14px 14px", fontSize: 13, color: K.body, minWidth: 0 }}>
      {op.description && (
        <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", marginTop: 6 }}>{op.description}</div>
      )}
      {op.operationId && (
        <div style={{ marginTop: 8 }}>
          <Muted>operationId </Muted>
          <Code>{op.operationId}</Code>
        </div>
      )}

      <SectionLabel>{t("parameters")}</SectionLabel>
      {op.params.length ? op.params.map((p) => <ParamRow key={`${p.in}:${p.name ?? p.$ref}`} p={p} />) : <Muted>{t("none")}</Muted>}

      <SectionLabel>{t("requestBody")}</SectionLabel>
      {op.body ? (
        <>
          <Row>
            <Code>{schemaLabel(op.body.schema)}</Code>
            {op.body.contentTypes.map((ct) => (
              <Muted key={ct}>{ct}</Muted>
            ))}
            {op.body.required && <Badge tone="danger" dot={false}>{t("required")}</Badge>}
            {op.body.description && (
              <div style={{ flexBasis: "100%", fontSize: 12, color: K.body }}>{op.body.description}</div>
            )}
          </Row>
          <SchemaFields spec={spec} schema={op.body.schema} />
        </>
      ) : (
        <Muted>{t("none")}</Muted>
      )}

      <SectionLabel>{t("responses")}</SectionLabel>
      {op.responses.length ? (
        op.responses.map((r) => (
          <Row key={r.code}>
            <Badge tone={responseTone(r.code)} dot={false} style={{ fontFamily: mono }}>{r.code}</Badge>
            <span style={{ fontSize: 12, color: K.body }}>{r.description || "—"}</span>
            {!isEmptySchema(r.schema) && <Code>{schemaLabel(r.schema)}</Code>}
          </Row>
        ))
      ) : (
        <Muted>{t("noneDeclared")}</Muted>
      )}
    </div>
  );
}

function OpRow({
  op,
  spec,
  open,
  onToggle,
}: {
  op: Op;
  spec: OpenApiSpec;
  open: boolean;
  onToggle: () => void;
}) {
  const t = useT(M);
  return (
    <div style={{ borderTop: `1px solid ${K.line}` }}>
      <Pressable
        onClick={onToggle}
        aria-expanded={open}
        style={{
          display: "flex",
          alignItems: "center",
          flexWrap: "wrap",
          gap: "6px 10px",
          width: "100%",
          padding: "10px 14px",
          background: open ? K.accentTint : "transparent",
          border: "none",
          cursor: "pointer",
          textAlign: "left",
          fontFamily: sans,
          color: K.ink,
        }}
      >
        <span style={{ display: "inline-flex", color: K.muted2, flexShrink: 0 }}>
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
        <MethodBadge method={op.method} />
        <span
          style={{
            fontFamily: mono,
            fontSize: 13,
            fontWeight: 600,
            color: K.ink,
            textDecoration: op.deprecated ? "line-through" : "none",
            overflowWrap: "anywhere",
            minWidth: 0,
            flex: "1 1 220px",
          }}
        >
          {op.path}
        </span>
        {/* The strike-through alone is not announced and not self-explaining; say it in words too. */}
        {op.deprecated && <Badge tone="neutral" dot={false}>{t("deprecated")}</Badge>}
        {op.summary && (
          <span style={{ fontSize: 12, color: K.muted, minWidth: 0, flex: "1 1 200px", overflowWrap: "anywhere" }}>
            {op.summary}
          </span>
        )}
      </Pressable>
      {open && <OpDetails op={op} spec={spec} />}
    </div>
  );
}

function GroupCard({
  group,
  spec,
  open,
  onToggle,
  openOps,
  onToggleOp,
}: {
  group: OpGroup;
  spec: OpenApiSpec;
  open: boolean;
  onToggle: () => void;
  openOps: Set<string>;
  onToggleOp: (key: string) => void;
}) {
  return (
    <section
      style={{
        background: K.card,
        border: `1px solid ${K.line}`,
        borderRadius: RADIUS.lg,
        overflow: "hidden",
        minWidth: 0,
      }}
    >
      <Pressable
        onClick={onToggle}
        aria-expanded={open}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          width: "100%",
          padding: "12px 14px",
          background: "transparent",
          border: "none",
          cursor: "pointer",
          textAlign: "left",
          fontFamily: sans,
        }}
      >
        <span style={{ display: "inline-flex", color: K.muted }}>
          {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </span>
        <span style={{ fontSize: 14, fontWeight: 700, color: K.ink, overflowWrap: "anywhere", minWidth: 0 }}>
          {group.tag}
        </span>
        <Badge tone="neutral" dot={false}>{group.ops.length}</Badge>
      </Pressable>
      {group.description && open && (
        <div style={{ padding: "0 14px 10px", fontSize: 12, color: K.muted }}>{group.description}</div>
      )}
      {open &&
        group.ops.map((op) => (
          <OpRow key={op.key} op={op} spec={spec} open={openOps.has(op.key)} onToggle={() => onToggleOp(op.key)} />
        ))}
    </section>
  );
}

// ── page ────────────────────────────────────────────────────────────────────────────────────────

export default function ApiDocsPage() {
  const t = useT(M);
  const { data: spec, error, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["admin", "openapi"],
    // AA-994: the route docstrings keep their issue references as code docs; the page shows the text without them.
    queryFn: () => apiGet<OpenApiSpec>(SPEC_PATH, { admin: true }).then(stripIssueIdsDeep),
    staleTime: 5 * 60_000,
  });

  const [query, setQueryRaw] = useState("");
  const [openTags, setOpenTags] = useState<Set<string>>(() => new Set());
  const [openOps, setOpenOps] = useState<Set<string>>(() => new Set());
  // While searching every matching group starts open; this holds the ones the user then closed, and
  // is reset whenever the search text changes (so a new hit is never hidden).
  const [closedWhileSearching, setClosedWhileSearching] = useState<Set<string>>(() => new Set());
  const setQuery = (q: string) => {
    setQueryRaw(q);
    setClosedWhileSearching(new Set());
  };

  const ops = useMemo(() => (spec ? buildOps(spec) : []), [spec]);
  const searching = query.trim().length > 0;
  const groups = useMemo(
    () => groupOps(ops.filter((op) => matchesQuery(op, query)), spec?.tags),
    [ops, query, spec],
  );
  const shown = groups.reduce((n, g) => n + g.ops.length, 0);

  const info = spec?.info;
  const description = spec
    ? t("descriptionLoaded", info?.title ?? "API", info?.version ?? "", spec.openapi ?? "?", ops.length)
    : t("descriptionEmpty");

  return (
    <PageFrame style={{ fontFamily: sans }}>
      <SkeletonStyle />
      <PageHeader
        title={t("title")}
        description={description}
        actions={
          <Button variant="ghost" size="sm" onClick={() => refetch()} disabled={isFetching} ariaLabel={t("reloadSchema")}>
            {isFetching ? <Spinner size={13} /> : <RefreshCw size={13} />} {t("reload")}
          </Button>
        }
      />

      {isLoading ? (
        <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, padding: 16 }}>
          <TableSkeleton rows={8} cols={3} />
        </div>
      ) : error ? (
        <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg }}>
          <ErrorState
            message={t("loadFailed", error instanceof Error ? error.message : String(error))}
            onRetry={() => refetch()}
          />
        </div>
      ) : !ops.length ? (
        <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg }}>
          <EmptyState title={t("noEndpointsTitle")} description={t("noEndpointsBody")} />
        </div>
      ) : (
        <>
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              alignItems: "center",
              gap: 8,
              marginBottom: 14,
            }}
          >
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                flex: "1 1 260px",
                minWidth: 0,
                padding: "8px 12px",
                background: K.card,
                border: `1px solid ${K.line}`,
                borderRadius: RADIUS.md,
              }}
            >
              <Search size={14} style={{ color: K.muted2, flexShrink: 0 }} />
              <Input
                // type="text", not "search": the browser's own clear button would sit next to ours.
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setQuery("");
                }}
                placeholder={t("searchPlaceholder")}
                aria-label={t("searchEndpoints")}
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
              {searching && (
                <IconButton variant="plain" size="xs" onClick={() => setQuery("")} label={t("clearSearch")}>
                  <X size={14} />
                </IconButton>
              )}
            </label>
            <span style={{ fontSize: 12, color: K.muted }}>
              {searching ? t("shownOf", shown, ops.length) : t("groups", groups.length)}
            </span>
            {!searching && (
              <span style={{ display: "inline-flex", gap: 6 }}>
                <Button variant="ghost" size="sm" onClick={() => setOpenTags(new Set(groups.map((g) => g.tag)))}>
                  {t("expandAll")}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setOpenTags(new Set());
                    setOpenOps(new Set());
                  }}
                >
                  {t("collapseAll")}
                </Button>
              </span>
            )}
          </div>

          {groups.length === 0 ? (
            <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg }}>
              <EmptyState
                icon={<Search size={28} />}
                title={t("noMatchTitle")}
                description={t("noMatchBody", query.trim())}
                action={
                  <Button variant="ghost" size="sm" onClick={() => setQuery("")}>
                    {t("clearSearch")}
                  </Button>
                }
              />
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {groups.map((g) => (
                <GroupCard
                  key={g.tag}
                  group={g}
                  spec={spec as OpenApiSpec}
                  open={searching ? !closedWhileSearching.has(g.tag) : openTags.has(g.tag)}
                  onToggle={() =>
                    searching
                      ? setClosedWhileSearching((s) => toggleIn(s, g.tag))
                      : setOpenTags((s) => toggleIn(s, g.tag))
                  }
                  openOps={openOps}
                  onToggleOp={(key) => setOpenOps((s) => toggleIn(s, key))}
                />
              ))}
            </div>
          )}
        </>
      )}
    </PageFrame>
  );
}
