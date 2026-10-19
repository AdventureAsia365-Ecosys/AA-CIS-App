// app/_kit/savedViews.ts
// AA-662 — DataTable saved views, persisted in localStorage per user.
//
// A "view" is a named snapshot of a table's state (column visibility, sorting, filters, page size).
// It is deliberately opaque: the kit stores whatever JSON-serialisable object the table hands it,
// keyed by (tableId, userId). This keeps it reusable across admin and portal without the kit
// knowing a table's column shape.

export type SavedView<S = unknown> = {
  id: string;
  name: string;
  state: S;
};

const PREFIX = "kit:views:";

function storageKey(tableId: string, userId: string): string {
  return `${PREFIX}${userId}:${tableId}`;
}

function safeRead(key: string): SavedView[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as SavedView[]) : [];
  } catch {
    return [];
  }
}

function safeWrite(key: string, views: SavedView[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(views));
  } catch {
    // localStorage full or unavailable — saved views are a convenience, never block the UI.
  }
}

export function listViews<S = unknown>(tableId: string, userId: string): SavedView<S>[] {
  return safeRead(storageKey(tableId, userId)) as SavedView<S>[];
}

/** Create or overwrite a view by name. Returns the full list after the change. */
export function saveView<S = unknown>(
  tableId: string,
  userId: string,
  name: string,
  state: S,
): SavedView<S>[] {
  const key = storageKey(tableId, userId);
  const views = safeRead(key) as SavedView<S>[];
  const id = name.trim().toLowerCase().replace(/\s+/g, "-");
  const next = views.filter((v) => v.id !== id);
  next.push({ id, name: name.trim(), state });
  safeWrite(key, next as SavedView[]);
  return next;
}

export function deleteView<S = unknown>(
  tableId: string,
  userId: string,
  id: string,
): SavedView<S>[] {
  const key = storageKey(tableId, userId);
  const next = (safeRead(key) as SavedView<S>[]).filter((v) => v.id !== id);
  safeWrite(key, next as SavedView[]);
  return next;
}
