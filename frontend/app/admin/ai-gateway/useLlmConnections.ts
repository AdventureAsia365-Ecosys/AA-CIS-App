"use client";
// app/admin/ai-gateway/useLlmConnections.ts — GET /api/admin/llm-connections, the registry rows, in the react-query
// cache the Providers overview, the Connections view, Routing and useDomainText share (one read per page). Optional:
// before migration 322 or on an error the registry lines, the connection names and the domain labels fall back.

import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../_kit";
import type { RegistryFacts } from "@/lib/aiGateway";
import type { Connection } from "@/lib/llmConnections";

export type RegistryRow = RegistryFacts & Pick<Connection, "connection_key" | "kind" | "account_id">
  & { label?: string | null };

export function useLlmConnections() {
  return useQuery({
    queryKey: ["llm-connections"],
    queryFn: () => apiGet<{ connections?: RegistryRow[] }>("/api/admin/llm-connections", { admin: true }),
  });
}
