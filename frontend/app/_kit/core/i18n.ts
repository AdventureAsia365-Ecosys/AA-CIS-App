// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/i18n.ts
// AA-994 (S255 decision) — the UI language of an app: English by default, Vietnamese as the second language, one
// switch, no mixed-language pages. A tiny typed message catalogue; no i18n package.
//
// Messages (one dictionary per area, next to the code that shows it, in a file named `messages.ts` or
// `<name>.messages.ts` — the only files where Vietnamese text may appear, guarded by
// tests/unit/test_aa994_admin_i18n.py in AA-CIS-App):
//
//   export const M = defineMessages(
//     { title: "Jobs", rows: (n: number) => `${n} rows` },          // English: the keys and the argument types
//     { title: "Công việc", rows: (n: number) => `${n} dòng` },      // Vietnamese: the same keys, checked by tsc
//   );
//   const t = useT(M);            // core/useI18n.ts
//   t("title"); t("rows", 3);     // the arguments are typed per key
//
// A message is a string or a function of typed arguments (counts, names), so there is no placeholder syntax to
// parse and plurals are plain code. Structured content (a Guide) uses `localized({ en, vi }, lang)`.
//
// The preference: each app makes ONE store with its own cookie name (`cis_lang` in CIS admin):
//   export const langStore = createLangStore({ cookieName: "cis_lang" });
// A cookie (not only localStorage) so a server component can read it (`langFromCookie(value)`) if it renders per
// request; absent or unknown === English. `restoreScript` sets <html lang> before first paint.
//
// No "use client": no React here, and every function guards `window` / `document`, so a server module can import it.
// React bindings (provider, hooks) are in core/useI18n.ts; the switch is core/LangSwitch.tsx.

export type Lang = "en" | "vi";

export const LANGS: readonly Lang[] = ["en", "vi"];
export const DEFAULT_LANG: Lang = "en";

/** How each language names itself (an endonym, the same in every UI language). Escaped so this file stays ASCII. */
export const LANG_LABEL: Readonly<Record<Lang, { short: string; name: string }>> = {
  en: { short: "EN", name: "English" },
  vi: { short: "VI", name: "Ti\u1ebfng Vi\u1ec7t" },
};

/** "vi" (any case, surrounding space ignored) is Vietnamese; anything else, absent included, is English. */
export function parseLang(value: unknown): Lang {
  return typeof value === "string" && value.trim().toLowerCase() === "vi" ? "vi" : DEFAULT_LANG;
}

// ── messages ────────────────────────────────────────────────────────────────────────────────────────────────────

/** One message: a string, or a function of typed arguments that returns the string. */
export type Message = string | ((...args: never[]) => string);
export type Catalog = Record<string, Message>;

/** The Vietnamese side of a catalogue: every English key, a string for a string, the same arguments for a function. */
export type Translation<E extends Catalog> = {
  readonly [K in keyof E]: E[K] extends (...args: infer A) => string ? (...args: A) => string : string;
};

export interface Messages<E extends Catalog> {
  readonly en: E;
  readonly vi: Translation<E>;
}

/** The arguments a key takes: none for a string message. */
export type MessageArgs<M> = M extends (...args: infer A) => string ? A : [];

export type Translator<E extends Catalog> = <K extends keyof E & string>(key: K, ...args: MessageArgs<E[K]>) => string;

/**
 * Pair an English catalogue with its Vietnamese translation. tsc refuses a missing key, an extra key, or a function
 * whose arguments differ; at run time a key missing from `vi` (a cast, plain JS) falls back to English.
 */
export function defineMessages<E extends Catalog>(en: E, vi: Translation<E>): Messages<E> {
  return { en, vi };
}

/** Keys of `en` without a Vietnamese message (empty for a catalogue built by defineMessages under tsc). */
export function missingTranslations<E extends Catalog>(messages: Messages<E>): string[] {
  const vi = messages.vi as Record<string, unknown>;
  return Object.keys(messages.en).filter((k) => typeof vi[k] !== typeof messages.en[k] || vi[k] === "");
}

/** The `t` function for one catalogue in one language. An unknown key returns the key itself (never throws). */
export function translator<E extends Catalog>(messages: Messages<E>, lang: Lang): Translator<E> {
  const table = (lang === "vi" ? messages.vi : messages.en) as Record<string, Message | undefined>;
  const fallback = messages.en as Record<string, Message | undefined>;
  return ((key: string, ...args: unknown[]) => {
    const m = table[key] ?? fallback[key];
    if (typeof m === "function") return (m as (...a: unknown[]) => string)(...args);
    return typeof m === "string" ? m : key;
  }) as Translator<E>;
}

/** Pick the value for a language from a per-language pair (structured content such as a Guide). */
export function localized<T>(byLang: Readonly<Record<Lang, T>>, lang: Lang): T {
  return byLang[lang] ?? byLang[DEFAULT_LANG];
}

// ── preference store ───────────────────────────────────────────────────────────────────────────────────────────

export interface LangStore {
  /** The cookie (and localStorage mirror) name that holds the choice. */
  readonly cookieName: string;
  readLang(): Lang;
  /** useSyncExternalStore subscribe: this tab's setLang and other tabs' changes (the localStorage mirror). */
  subscribe(cb: () => void): () => void;
  getSnapshot(): Lang;
  getServerSnapshot(): Lang;
  /** Persist a new choice, set <html lang>, notify subscribers. English clears the cookie (absent === English). */
  setLang(lang: Lang): void;
  /** The inline script for the root layout's <head>: sets <html lang> from the cookie before first paint. */
  readonly restoreScript: string;
}

const NAME_RE = /^[A-Za-z0-9_-]{1,64}$/;
const ONE_YEAR_S = 365 * 24 * 60 * 60;

function checkName(cookieName: string): string {
  if (!NAME_RE.test(cookieName)) {
    throw new Error(`language cookie name must match ${NAME_RE}: ${JSON.stringify(cookieName)}`);
  }
  return cookieName;
}

/** The value of one cookie in a `Cookie` header / `document.cookie` string, or null. */
export function cookieValue(cookieHeader: string | null | undefined, name: string): string | null {
  if (!cookieHeader) return null;
  for (const part of cookieHeader.split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    if (part.slice(0, eq).trim() === name) {
      try {
        return decodeURIComponent(part.slice(eq + 1).trim());
      } catch {
        return part.slice(eq + 1).trim();
      }
    }
  }
  return null;
}

/** Server side: the language of a request from its Cookie header (or `cookies().get(name)?.value`). */
export function langFromCookie(cookieHeaderOrValue: string | null | undefined, name?: string): Lang {
  return parseLang(name === undefined ? cookieHeaderOrValue : cookieValue(cookieHeaderOrValue, name));
}

/** The restore script for a cookie name (also on every store as `restoreScript`). */
export function langRestoreScript(cookieName: string): string {
  const name = checkName(cookieName);
  return `
          (function() {
            try {
              var m = document.cookie.match(/(?:^|;\\s*)${name}=([^;]*)/);
              document.documentElement.setAttribute('lang', m && m[1].trim().toLowerCase() === 'vi' ? 'vi' : 'en');
            } catch (e) {
              document.documentElement.setAttribute('lang', 'en');
            }
          })();
        `;
}

export function createLangStore({ cookieName }: { cookieName: string }): LangStore {
  const name = checkName(cookieName);
  const listeners = new Set<() => void>();
  // Cookies can be blocked: the choice then lasts for this page view only (`memory`, used only while blocked).
  let memory: Lang = DEFAULT_LANG;
  let blocked = false;

  function readLang(): Lang {
    if (typeof document === "undefined") return DEFAULT_LANG;
    try {
      const v = cookieValue(document.cookie, name);
      return v === null ? (blocked ? memory : DEFAULT_LANG) : parseLang(v);
    } catch {
      return memory;
    }
  }

  function applyLang(lang: Lang): void {
    if (typeof document !== "undefined") document.documentElement.setAttribute("lang", lang);
  }

  function emit(): void {
    for (const l of listeners) l();
  }

  function subscribe(cb: () => void): () => void {
    listeners.add(cb);
    const onStorage = (e: StorageEvent) => {
      if (e.key === name) {
        applyLang(readLang());
        emit();
      }
    };
    if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
    return () => {
      listeners.delete(cb);
      if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
    };
  }

  function setLang(lang: Lang): void {
    if (typeof document === "undefined") return;
    const value = parseLang(lang);
    memory = value;
    const secure = typeof location !== "undefined" && location.protocol === "https:" ? "; Secure" : "";
    try {
      document.cookie = value === DEFAULT_LANG
        ? `${name}=; Path=/; Max-Age=0; SameSite=Lax${secure}`
        : `${name}=${value}; Path=/; Max-Age=${ONE_YEAR_S}; SameSite=Lax${secure}`;
    } catch {
      /* cookies blocked: checked just below */
    }
    try {
      blocked = parseLang(cookieValue(document.cookie, name)) !== value;
    } catch {
      blocked = true;
    }
    try {
      // Mirror for the other tabs: a `storage` event fires there, never in this tab.
      if (value === DEFAULT_LANG) window.localStorage.removeItem(name);
      else window.localStorage.setItem(name, value);
    } catch {
      /* storage blocked: other tabs pick the choice up on their next load */
    }
    applyLang(value);
    emit();
  }

  return {
    cookieName: name,
    readLang,
    subscribe,
    getSnapshot: readLang,
    getServerSnapshot: () => DEFAULT_LANG,
    setLang,
    restoreScript: langRestoreScript(name),
  };
}
