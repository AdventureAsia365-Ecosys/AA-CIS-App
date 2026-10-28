// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/useI18n.ts
// AA-994 — React bindings for the UI language (core/i18n.ts).
//
// The app's shell (a client component) renders ONE provider around everything that shows text:
//   <I18nProvider store={langStore}>…</I18nProvider>
// and every component, kit core components included, reads the language from it:
//   const t = useT(M);              // M = defineMessages(en, vi) of the area
//   const lang = useLang();         // "en" | "vi"
//   const guide = useLocalized({ en: GUIDE_EN, vi: GUIDE_VI });
//   const { formatDate, formatDateTime } = useFormat();   // dates in the UI language (core/tokens.ts)
// Outside a provider everything is English (a page that has not opted in never shows a mixed language).
//
// Server-render safe: on the server and during hydration the language is `initialLang` (default English), so the
// HTML React hydrates always matches; right after hydration useSyncExternalStore switches to the stored choice. An
// app that renders per request may read the cookie in a server layout and pass `initialLang` to avoid that switch.
// React Compiler safe: no setState-in-effect; the only mutation is the explicit setLang handler.

import { createContext, createElement, useContext, useMemo, useSyncExternalStore, type ReactNode } from "react";
import {
  DEFAULT_LANG, localized, translator, type Catalog, type Lang, type LangStore, type Messages, type Translator,
} from "./i18n";
import { formatDate, formatDateTime } from "./tokens";

interface LangState {
  lang: Lang;
  setLang: (lang: Lang) => void;
}

const NO_STORE: LangState = { lang: DEFAULT_LANG, setLang: () => {} };
const LangContext = createContext<LangState>(NO_STORE);

export function I18nProvider({
  store, initialLang = DEFAULT_LANG, children,
}: { store: LangStore; initialLang?: Lang; children?: ReactNode }) {
  const lang = useSyncExternalStore(store.subscribe, store.getSnapshot, () => initialLang);
  const value = useMemo<LangState>(() => ({ lang, setLang: store.setLang }), [lang, store]);
  return createElement(LangContext.Provider, { value }, children);
}

/** The current UI language ("en" outside a provider). */
export function useLang(): Lang {
  return useContext(LangContext).lang;
}

/** The setter for the UI language (a no-op outside a provider). */
export function useSetLang(): (lang: Lang) => void {
  return useContext(LangContext).setLang;
}

/** The `t` function of one catalogue in the current language. */
export function useT<E extends Catalog>(messages: Messages<E>): Translator<E> {
  const lang = useLang();
  return useMemo(() => translator(messages, lang), [messages, lang]);
}

/** The current language's value of a per-language pair (structured content such as a Guide). */
export function useLocalized<T>(byLang: Readonly<Record<Lang, T>>): T {
  return localized(byLang, useLang());
}

type DateInput = string | number | Date | null | undefined;

/** The kit date formatters (core/tokens.ts) bound to the current UI language: "9 Oct 2026" / "9 thg 10, 2026". */
export function useFormat(): { formatDate: (d: DateInput) => string; formatDateTime: (d: DateInput) => string } {
  const lang = useLang();
  return useMemo(() => ({
    formatDate: (d: DateInput) => formatDate(d, lang),
    formatDateTime: (d: DateInput) => formatDateTime(d, lang),
  }), [lang]);
}
