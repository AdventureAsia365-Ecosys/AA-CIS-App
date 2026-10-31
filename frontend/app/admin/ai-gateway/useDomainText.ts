"use client";
// app/admin/ai-gateway/useDomainText.ts — C4 (S262 wave 4): a failure domain `<kind>:<account>:<scope>` (root ADR 0009
// §5; backend shared/llm_client/invariant.failure_domain, registry v2 column of migration 322) as people read it: the
// kind, the account (lib/aiGateway domainAccountText: an AWS account ID as the API sends it, a direct vendor API's
// name, the registry label of a connection whose key stands in the account part, a Bedrock row's own account name or
// "this platform's AWS account", else "its own vendor account"), the data scope. Never an account key, a connection
// key or an environment variable name. C4 polish (N4): the connection labels come from the registry read
// (useLlmConnections, the cache the Providers overview and the Connections view share).

import { useMemo } from "react";
import { useT } from "../../_kit";
import { domainAccountText, domainParts } from "@/lib/aiGateway";
import { M } from "./messages";
import { useLlmConnections } from "./useLlmConnections";

const KIND_LABEL: Record<string, string> = {
  bedrock: "Bedrock", anthropic: "Anthropic", openai: "OpenAI", azure_openai: "Azure OpenAI", typesafe: "Jev",
  dataforseo: "DataForSEO", geoapify: "Geoapify", bedrock_guardrails: "Guardrails", mock: "Mock",
};

/** `(domain, ownName?) => text | null`: null when the value is not a domain. */
export function useDomainText(): (domain: string | null | undefined, ownName?: string | null) => string | null {
  const t = useT(M);
  const conns = useLlmConnections();
  const labels = useMemo(() => {
    const out: Record<string, string> = {};
    for (const c of conns.data?.connections ?? []) if (c.label) out[c.connection_key] = c.label;
    return out;
  }, [conns.data]);
  return (domain, ownName) => {
    const p = domainParts(domain);
    if (!p) return null;
    const account = domainAccountText(p.kind, p.account,
      { own: t("domainOwnAccount"), vendor: t("domainVendorAccount") }, { ownName, labels });
    const scope = p.scope === "aws_only" ? t("scopeAws") : p.scope === "vendor_direct" ? t("scopeVendor") : p.scope;
    return `${KIND_LABEL[p.kind] ?? p.kind} · ${account} · ${scope}`;
  };
}
