"use client";
// app/admin/overview/_components/AlertList.tsx
// AA-1006 follow-up (S263 F1) — the Overview "Alerts" rows. An alert key can be one long token
// ("tenant_ai_budget:<uuid>"), and the row used to be one nowrap flex line: at 390 px the key and the date pushed the
// page frame (main.aa-admin-main) 65 px wider than the phone. Now the key wraps anywhere (min-width 0 so a flex
// item may shrink below its longest word), the date never breaks inside itself, and on a phone the row stacks
// (badge, key, date one under the other). Desktop keeps the one-line row; it wraps instead of overflowing when the
// card is narrow.
import { Badge, K, type Tone, useFormat, useIsPhone } from "../../../_kit";

export interface OverviewAlert {
  event_type: string;
  entity_type: string | null;
  entity_id: string | null;
  created_at: string;
}

/** The badge tone of an alert event: exhausted → danger, low / budget → warning, else neutral. */
export function alertTone(eventType: string): Tone {
  if (eventType.includes("exhausted")) return "danger";
  if (eventType.includes("low") || eventType.includes("budget")) return "warning";
  return "neutral";
}

export function AlertList({ alerts }: { alerts: OverviewAlert[] }) {
  const { formatDateTime } = useFormat();
  const isPhone = useIsPhone();
  return (
    <div data-testid="overview-alerts" style={{ display: "flex", flexDirection: "column", gap: 8, minWidth: 0 }}>
      {alerts.map((a, i) => (
        <div
          key={i}
          data-testid="overview-alert-row"
          style={{
            display: "flex",
            flexDirection: isPhone ? "column" : "row",
            alignItems: isPhone ? "flex-start" : "center",
            flexWrap: "wrap",
            gap: isPhone ? 4 : "6px 10px",
            minWidth: 0,
            padding: "8px 12px",
            background: K.bg,
            border: `1px solid ${K.line}`,
            borderRadius: 8,
          }}
        >
          <span style={{ maxWidth: "100%", minWidth: 0, overflowWrap: "anywhere" }}>
            <Badge tone={alertTone(a.event_type)} dot>
              {a.event_type}
            </Badge>
          </span>
          <span
            style={{
              flex: isPhone ? "0 1 auto" : "1 1 14rem",
              minWidth: 0,
              maxWidth: "100%",
              overflowWrap: "anywhere",
              fontSize: 12,
              color: K.body,
            }}
          >
            {a.entity_type}
            {a.entity_id ? ` · ${a.entity_id}` : ""}
          </span>
          <span style={{ marginLeft: isPhone ? 0 : "auto", fontSize: 11, color: K.muted2, whiteSpace: "nowrap" }}>
            {formatDateTime(a.created_at)}
          </span>
        </div>
      ))}
    </div>
  );
}
