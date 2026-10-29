// app/_kit/i18n.ts
// CIS admin's UI language store (AA-994): the kit core store (core/i18n.ts) bound to the cookie `cis_lang`.
// English by default; Vietnamese only when the user picks it in the topbar switch (core/LangSwitch.tsx).
// app/layout.tsx puts `langStore.restoreScript` in its <head>; AdminShell renders the one I18nProvider.
// No "use client": the server root layout reads the restore script from here.

import { createLangStore } from "./core/i18n";

export const LANG_COOKIE = "cis_lang";

export const langStore = createLangStore({ cookieName: LANG_COOKIE });
