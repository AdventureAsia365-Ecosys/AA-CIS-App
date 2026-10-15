// app/admin/api-docs/openapi.ts
// AA-577 follow-up — the small, dependency-free OpenAPI 3.x reader behind /admin/api-docs. Pure
// functions only (no React): flatten the spec into operations, group them by tag, filter by a search
// string, and turn a schema into a short human label ("TourOut[]", "string | null", ...).
// Only what the read-only page shows is modelled; unknown keys are ignored.

export const METHODS = ["get", "post", "put", "patch", "delete", "head", "options", "trace"] as const;
export type HttpMethod = (typeof METHODS)[number];

export interface SchemaObj {
  $ref?: string;
  type?: string | string[];
  format?: string;
  title?: string;
  description?: string;
  items?: SchemaObj;
  anyOf?: SchemaObj[];
  oneOf?: SchemaObj[];
  allOf?: SchemaObj[];
  properties?: Record<string, SchemaObj>;
  required?: string[];
  enum?: unknown[];
  const?: unknown;
  additionalProperties?: boolean | SchemaObj;
}

export interface ParamObj {
  $ref?: string;
  name?: string;
  in?: string;
  required?: boolean;
  deprecated?: boolean;
  description?: string;
  schema?: SchemaObj;
}

interface MediaTypeObj {
  schema?: SchemaObj;
}

interface RequestBodyObj {
  $ref?: string;
  required?: boolean;
  description?: string;
  content?: Record<string, MediaTypeObj>;
}

interface ResponseObj {
  $ref?: string;
  description?: string;
  content?: Record<string, MediaTypeObj>;
}

interface OperationObj {
  tags?: string[];
  summary?: string;
  description?: string;
  operationId?: string;
  deprecated?: boolean;
  parameters?: ParamObj[];
  requestBody?: RequestBodyObj;
  responses?: Record<string, ResponseObj>;
}

type PathItemObj = Partial<Record<HttpMethod, OperationObj>> & { parameters?: ParamObj[] };

export interface OpenApiSpec {
  openapi?: string;
  info?: { title?: string; version?: string; description?: string };
  tags?: { name: string; description?: string }[];
  paths?: Record<string, PathItemObj>;
  components?: { schemas?: Record<string, SchemaObj> };
}

export interface OpBody {
  required: boolean;
  description: string;
  contentTypes: string[];
  schema?: SchemaObj;
}

export interface OpResponse {
  code: string;
  description: string;
  schema?: SchemaObj;
}

export interface Op {
  key: string;
  method: HttpMethod;
  path: string;
  tag: string;
  summary: string;
  description: string;
  operationId: string;
  deprecated: boolean;
  params: ParamObj[];
  body: OpBody | null;
  responses: OpResponse[];
}

export interface OpGroup {
  tag: string;
  description: string;
  ops: Op[];
}

export const UNTAGGED = "untagged";

/** "#/components/schemas/TourOut" → "TourOut". */
export function refName(ref: string): string {
  return ref.split("/").pop() || ref;
}

/** Follow a local `#/components/schemas/X` ref one hop (the page never needs deeper resolution). */
export function resolveSchema(spec: OpenApiSpec, s: SchemaObj | undefined): SchemaObj | undefined {
  if (!s?.$ref) return s;
  return spec.components?.schemas?.[refName(s.$ref)];
}

/** A short type label: ref name, `X[]`, unions joined with " | ", enum values, or the plain type. */
export function schemaLabel(s: SchemaObj | undefined, depth = 0): string {
  if (!s || depth > 4) return "any";
  if (s.$ref) return refName(s.$ref);
  const union = s.anyOf ?? s.oneOf;
  if (union && union.length) return union.map((u) => schemaLabel(u, depth + 1)).join(" | ");
  if (s.allOf && s.allOf.length) return s.allOf.map((u) => schemaLabel(u, depth + 1)).join(" & ");
  if (s.const !== undefined) return JSON.stringify(s.const);
  if (s.enum && s.enum.length) {
    const vals = s.enum.slice(0, 6).map((v) => JSON.stringify(v)).join(" | ");
    return s.enum.length > 6 ? `${vals} | …` : vals;
  }
  const type = Array.isArray(s.type) ? s.type.join(" | ") : s.type;
  if (type === "array") return `${schemaLabel(s.items, depth + 1)}[]`;
  if (type === "object" && s.additionalProperties && typeof s.additionalProperties === "object") {
    return `Record<string, ${schemaLabel(s.additionalProperties, depth + 1)}>`;
  }
  if (type) return s.format ? `${type} (${s.format})` : type;
  if (s.properties) return "object";
  return "any";
}

function firstSchema(content: Record<string, MediaTypeObj> | undefined): SchemaObj | undefined {
  if (!content) return undefined;
  const json = content["application/json"];
  if (json?.schema) return json.schema;
  for (const mt of Object.values(content)) if (mt?.schema) return mt.schema;
  return undefined;
}

/** True when a schema says nothing (FastAPI emits `{}` for an untyped `dict` / no response_model). */
export function isEmptySchema(s: SchemaObj | undefined): boolean {
  return !s || Object.keys(s).length === 0;
}

/** Flatten `paths` into one row per operation, path-level parameters merged in (operation wins). */
export function buildOps(spec: OpenApiSpec): Op[] {
  const out: Op[] = [];
  for (const [path, item] of Object.entries(spec.paths ?? {})) {
    if (!item || typeof item !== "object") continue;
    const shared = item.parameters ?? [];
    for (const method of METHODS) {
      const op = item[method];
      if (!op) continue;
      const own = op.parameters ?? [];
      const ownKeys = new Set(own.map((p) => `${p.in}:${p.name}`));
      const params = [...shared.filter((p) => !ownKeys.has(`${p.in}:${p.name}`)), ...own];
      const rb = op.requestBody;
      const body: OpBody | null = rb
        ? {
            required: !!rb.required,
            description: rb.description ?? "",
            contentTypes: Object.keys(rb.content ?? {}),
            schema: rb.$ref ? { $ref: rb.$ref } : firstSchema(rb.content),
          }
        : null;
      const responses: OpResponse[] = Object.entries(op.responses ?? {})
        .map(([code, r]) => ({
          code,
          description: r?.description ?? "",
          schema: r?.$ref ? { $ref: r.$ref } : firstSchema(r?.content),
        }))
        .sort((a, b) => a.code.localeCompare(b.code));
      out.push({
        key: `${method} ${path}`,
        method,
        path,
        tag: op.tags?.[0] || UNTAGGED,
        summary: op.summary ?? "",
        description: op.description ?? "",
        operationId: op.operationId ?? "",
        deprecated: !!op.deprecated,
        params,
        body,
        responses,
      });
    }
  }
  return out;
}

/** Case-insensitive match on path, summary, operationId, tag and method; every word must match. */
export function matchesQuery(op: Op, query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = `${op.method} ${op.path} ${op.summary} ${op.operationId} ${op.tag}`.toLowerCase();
  return words.every((w) => hay.includes(w));
}

/** Group by first tag: the spec's own `tags` order first, then alphabetical; untagged last. */
export function groupOps(ops: Op[], specTags: OpenApiSpec["tags"] = []): OpGroup[] {
  const byTag = new Map<string, Op[]>();
  for (const op of ops) {
    const list = byTag.get(op.tag);
    if (list) list.push(op);
    else byTag.set(op.tag, [op]);
  }
  const declared = new Map((specTags ?? []).map((t, i) => [t.name, i]));
  const rank = (tag: string) => (tag === UNTAGGED ? 2 : declared.has(tag) ? 0 : 1);
  return [...byTag.entries()]
    .sort(([a], [b]) => {
      const ra = rank(a);
      const rb = rank(b);
      if (ra !== rb) return ra - rb;
      if (ra === 0) return (declared.get(a) ?? 0) - (declared.get(b) ?? 0);
      return a.localeCompare(b);
    })
    .map(([tag, list]) => ({
      tag,
      description: (specTags ?? []).find((t) => t.name === tag)?.description ?? "",
      ops: list.sort((x, y) => x.path.localeCompare(y.path) || METHODS.indexOf(x.method) - METHODS.indexOf(y.method)),
    }));
}
