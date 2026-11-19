// frontend/lib/overviewAlerts.ts
// AA-1127 C3 (S283 UI audit, Overview alerts) — the readable line of an Overview alert. The API sends each row's
// `payload` (shared.notifications) as a JSON string, sometimes already parsed; its `message` is the sentence a
// business reader needs ("Jev (TypeSafe) credit is running low: …"). Pure, tested in lib/aa1127C3.test.mjs.

/** The payload's `message` (trimmed), or null when there is none or the payload is not readable. */
export function alertMessage(payload: unknown): string | null {
  let value: unknown = payload;
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const msg = (value as { message?: unknown }).message;
  return typeof msg === "string" && msg.trim() ? msg.trim() : null;
}
