// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/Money.tsx
// AA-994 S4a — money on screen, upstreamed from AA-Booking `ui/booking/Money.tsx`: always an amount plus its ISO code,
// never a bare "$", always with the currency's minor units ("1,845.00 USD" next to "2,390.50 EUR"), tabular figures,
// never wrapped. Formatting is core/format.ts `formatMoney`, in the UI language.

import type { CSSProperties } from "react";
import { formatMoney } from "./format";
import { K } from "./tokens";
import { useLang } from "./useI18n";

export function Money({
  amount,
  currency,
  strong = false,
  style,
  className,
}: {
  /** A decimal string from the API (or a number); empty / unreadable shows "—". */
  amount: string | number | null | undefined;
  currency: string | null | undefined;
  /** Semibold ink (a total). */
  strong?: boolean;
  style?: CSSProperties;
  /** For Tailwind apps (Booking); inline-style apps leave it out. */
  className?: string;
}) {
  const lang = useLang();
  return (
    <span
      className={className}
      style={{
        fontVariantNumeric: "tabular-nums",
        whiteSpace: "nowrap",
        ...(strong ? { fontWeight: 600, color: K.ink } : {}),
        ...style,
      }}
    >
      {formatMoney(amount, currency, lang)}
    </span>
  );
}
