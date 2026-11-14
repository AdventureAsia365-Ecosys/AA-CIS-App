"use client";
// app/admin/platform/settings/settingsApi.ts — the page's calls to /api/admin/settings/registry (BFF).
//
// AA-1096 (S280 UX fix round): the registry answers its refusals as {detail: {code, message}}; the kit's apiGet keeps
// only a string detail, so a structured one became "[object Object]" and the page could not tell "not set up yet"
// (not_migrated / settings_unavailable) from "the CRM did not answer" (crm_unavailable). getJson keeps the status, the
// code and the message; post answers {ok, data} or {ok: false, message}.

import { staffErrorMessage } from "@/lib/rbac";

export class RegistryError extends Error {
  status: number;
  code: string | undefined;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "RegistryError";
    this.status = status;
    this.code = code;
  }
}

type Detail = { code?: string; message?: string } | string | undefined;

async function bodyOf(res: Response): Promise<Record<string, unknown>> {
  try {
    return (await res.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function detailOf(data: Record<string, unknown>): { code?: string; message?: string; raw: Detail } {
  const raw = data.detail as Detail;
  if (raw && typeof raw === "object") return { code: raw.code, message: raw.message, raw };
  return { message: typeof raw === "string" ? raw : undefined, raw };
}

/** GET JSON; a refusal throws RegistryError with the status, the code and the server's message. */
export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path);
  const data = await bodyOf(res);
  if (res.ok) return data as T;
  const d = detailOf(data);
  throw new RegistryError(d.message || `Request failed (${res.status})`, res.status, d.code);
}

export type Sent = { ok: true; data: unknown } | { ok: false; message: string };

/** POST JSON; a refusal answers the server's message (else the staff-error wording of the status). */
export async function post(path: string, body: unknown, words: { network: string; failed: (status: number) => string;
  readOnly: string }): Promise<Sent> {
  let res: Response;
  try {
    res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
  } catch {
    return { ok: false, message: words.network };
  }
  const data = await bodyOf(res);
  if (res.ok) return { ok: true, data };
  const d = detailOf(data);
  const structured = typeof d.raw === "object" && d.message;
  return { ok: false,
    message: structured ? String(d.message) : staffErrorMessage(res.status, d.raw, words.failed(res.status), words.readOnly) };
}
