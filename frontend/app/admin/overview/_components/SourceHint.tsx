// app/admin/overview/_components/SourceHint.tsx
// AA-858 — the small (i) next to every number of /admin/overview. It names where the number comes from (AA-858
// Done-when: "each number traceable to its source in a tooltip"). Built on the kit InfoTip (S243): a real button,
// keyboard and touch accessible (focus or tap opens it, Escape or a tap elsewhere closes it), the text tied to the
// button with aria-describedby. The wording lives in ./sources.ts (plain business words, no table names or codes).
import { InfoTip } from "../../../_kit";

export function SourceHint({ source, size = 12 }: { source: string; size?: number }) {
  return <InfoTip text={`Source: ${source}`} label="Where this number comes from" size={size} testId="source-hint" />;
}
