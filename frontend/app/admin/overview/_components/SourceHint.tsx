// app/admin/overview/_components/SourceHint.tsx
// AA-858 — the small info icon next to every number of the ops overview. It names where the number comes
// from (AA-858 Done-when: "every number names its source in a tooltip"). Native `title` (hover) +
// `aria-label` (screen readers); focusable so a keyboard or touch user can reach it too.
import { Info } from "lucide-react";
import { K } from "../../../_kit";

export function SourceHint({ source, size = 12 }: { source: string; size?: number }) {
  const text = `Source: ${source}`;
  return (
    <span
      role="img"
      tabIndex={0}
      title={text}
      aria-label={text}
      data-testid="source-hint"
      style={{
        display: "inline-flex",
        alignItems: "center",
        color: K.muted2,
        cursor: "help",
        marginLeft: 4,
        verticalAlign: "middle",
        flexShrink: 0,
      }}
    >
      <Info size={size} aria-hidden />
    </span>
  );
}
