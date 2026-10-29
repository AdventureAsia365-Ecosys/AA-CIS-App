// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/LangSwitch.tsx
// AA-994 — the one language switch of an app (EN | VI), placed once in the app shell. Reads and sets the language of
// the nearest I18nProvider (core/useI18n.ts). Each option is labelled with its own endonym ("English",
// "Tiếng Việt"), the usual convention, so it reads the same in either UI language.

import { K, alpha, sans } from "./tokens";
import { LANGS, LANG_LABEL, type Lang } from "./i18n";
import { useLang, useSetLang, useT } from "./useI18n";
import { KIT_MESSAGES } from "./kit.messages";

export function LangSwitch({ ariaLabel }: { ariaLabel?: string }) {
  const t = useT(KIT_MESSAGES);
  const lang = useLang();
  const setLang = useSetLang();
  return (
    <div
      role="group"
      aria-label={ariaLabel ?? t("language")}
      data-testid="lang-switch"
      style={{
        display: "inline-flex", alignItems: "center", gap: 2, padding: 2, borderRadius: 8,
        border: `1px solid ${K.line}`, background: K.bg, fontFamily: sans,
      }}
    >
      {LANGS.map((l: Lang) => {
        const active = l === lang;
        return (
          <button
            key={l}
            type="button"
            lang={l}
            onClick={() => { if (!active) setLang(l); }}
            aria-pressed={active}
            title={LANG_LABEL[l].name}
            aria-label={LANG_LABEL[l].name}
            data-lang={l}
            style={{
              minWidth: 30, height: 26, padding: "0 6px", borderRadius: 6, border: "none",
              cursor: active ? "default" : "pointer", fontSize: 11, fontWeight: 700, letterSpacing: 0.4,
              fontFamily: sans,
              background: active ? alpha(K.accent, 16) : "transparent",
              color: active ? K.ink : K.muted,
            }}
          >
            {LANG_LABEL[l].short}
          </button>
        );
      })}
    </div>
  );
}
