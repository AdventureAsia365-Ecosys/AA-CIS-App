// frontend/lib/gateLabels.ts
// AA-924 (scope 4) — admin wording for T10 gate names: the plain label, "(retired)" on a gate the T10 stack no longer
// runs, and the Content Quality gate filter's two groups (active gates, then the retired ones older pieces still carry
// in their ledgers).
//
// The labels and the retired set come from the quality registry (services/quality/taxonomy.yaml →
// lib/quality-registry.generated.ts); they are passed in rather than imported so `npm run test:unit` (node --test with
// --experimental-strip-types, no bundler path resolution) can run this file directly. app/admin/content-quality/shared.ts
// binds them to the generated registry.
// AA-994: the retired suffix is a parameter too (English by default); the page passes the UI language's
// (lib/gateLabels.messages.ts) and the Vietnamese labels (lib/quality-registry.generated.vi.ts).

export const RETIRED_SUFFIX = " (retired)";

/** "Cliché / banned phrase (retired)" for a retired gate, the plain label for an active one, the code itself for a
 *  name the registry does not label. */
export function gateLabelOf(
  labels: Record<string, string>, retired: ReadonlySet<string>, gate: string, suffix: string = RETIRED_SUFFIX,
): string {
  const label = labels[gate] ?? gate;
  return retired.has(gate) ? `${label}${suffix}` : label;
}

export interface GateOption {
  value: string;
  text: string;
}

/** The gate filter's options, in registry order: active gates first, then the retired ones (each labelled
 *  "(retired)"), every option naming its code too. */
export function gateFilterGroups(
  labels: Record<string, string>,
  retired: ReadonlySet<string>,
  suffix: string = RETIRED_SUFFIX,
): { active: GateOption[]; retired: GateOption[] } {
  const option = (g: string): GateOption => ({ value: g, text: `${gateLabelOf(labels, retired, g, suffix)} (${g})` });
  const names = Object.keys(labels);
  return {
    active: names.filter((g) => !retired.has(g)).map(option),
    retired: names.filter((g) => retired.has(g)).map(option),
  };
}
