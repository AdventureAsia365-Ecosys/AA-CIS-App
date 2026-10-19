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

async function parseOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail || body?.message || "";
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail || `Request failed (${res.status})`, res.status);
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
