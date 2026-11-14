// app/_kit/api.ts
// AA-662 — small fetch helpers used with react-query across UI-v2 pages.
//
// All browser calls go through the BFF proxy (/api/admin/* or /api/tenant/*), never to the backend
// directly. These helpers add JSON handling + the admin reviewer-id header the existing pages send,
// and throw a readable Error on non-2xx so react-query's `error` is meaningful.

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Admin identity header (temporary until AA-232 per-user auth): x-reviewer-id from localStorage. */
export function adminHeaders(json = false): Record<string, string> {
  const h: Record<string, string> = {};
  const rid =
    (typeof window !== "undefined" && window.localStorage.getItem("cis_reviewer_id")) || "";
  if (rid) h["x-reviewer-id"] = rid;
  if (json) h["Content-Type"] = "application/json";
  return h;
}

/** S280 (AA-1096) — a readable message from a failed call's JSON body. FastAPI sends `detail` as a string, as an
 *  object (`{code, message}` from a router's HTTPException) or, for a pydantic 422, as a list of `{loc, msg}`; the
 *  old `body.detail || body.message` turned the last two into "[object Object]". A list item reads
 *  "<field>: <msg>" (the last part of `loc`, so "shadow_sample_pct: Input should be a valid integer"). */
export function apiErrorMessage(body: unknown, status: number): string {
  const fallback = `Request failed (${status})`;
  if (typeof body === "string") return body || fallback;
  if (!body || typeof body !== "object") return fallback;
  const b = body as { detail?: unknown; message?: unknown };
  return firstText(b.detail) ?? listText(b.detail) ?? objectText(b.detail) ?? firstText(b.message) ?? fallback;
}

/** A non-empty string, else null. */
function firstText(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

/** One pydantic error item as "<field>: <msg>" (the last part of `loc` that is not body / query / path). */
function itemText(item: unknown): string {
  if (typeof item === "string") return item;
  const it = (item ?? {}) as { loc?: unknown; msg?: unknown };
  const msg = firstText(it.msg);
  if (!msg) return "";
  const loc = Array.isArray(it.loc) ? it.loc.filter((x) => x !== "body" && x !== "query" && x !== "path") : [];
  return loc.length ? `${String(loc[loc.length - 1])}: ${msg}` : msg;
}

/** A list detail (pydantic 422) as "<field>: <msg>; …", else null. */
function listText(d: unknown): string | null {
  if (!Array.isArray(d)) return null;
  const parts = d.map(itemText).filter(Boolean);
  return parts.length ? parts.join("; ") : null;
}

/** An object detail (`{code, message}` from a router) as its message, else null. */
function objectText(d: unknown): string | null {
  if (!d || typeof d !== "object" || Array.isArray(d)) return null;
  const o = d as { message?: unknown; msg?: unknown; detail?: unknown };
  return firstText(o.message) ?? firstText(o.msg) ?? firstText(o.detail);
}

async function parseOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(apiErrorMessage(body, res.status), res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** GET JSON from a BFF path. `admin` toggles the reviewer-id header. */
export async function apiGet<T>(path: string, opts: { admin?: boolean } = {}): Promise<T> {
  const res = await fetch(path, {
    headers: opts.admin ? adminHeaders() : undefined,
  });
  return parseOrThrow<T>(res);
}

/** Send a JSON body (POST/PUT/PATCH/DELETE) to a BFF path. */
export async function apiSend<T>(
  path: string,
  opts: { method?: string; body?: unknown; admin?: boolean } = {},
): Promise<T> {
  const { method = "POST", body, admin = false } = opts;
  const res = await fetch(path, {
    method,
    headers: admin ? adminHeaders(true) : { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  return parseOrThrow<T>(res);
}
