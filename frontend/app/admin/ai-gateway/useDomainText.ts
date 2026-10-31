"use client";
// app/admin/ai-gateway/useDomainText.ts — C4 (S262 wave 4): a failure domain `<kind>:<account>:<scope>` (root ADR 0009
// §5; backend shared/llm_client/invariant.failure_domain, registry v2 column of migration 322) as people read it: the
// kind, the account (an AWS account ID as the API sends it, a direct vendor API's name, a Bedrock row's own account
// name or "this platform's AWS account", else "its own vendor account"), the data scope. Never an account key or an environment variable name.

import { useT } from "../../_kit";
import { domainParts } from "@/lib/aiGateway";
import { VENDOR_CONNECTION_LABELS } from "@/lib/awsAccounts";
import { M } from "./messages";

const KIND_LABEL: Record<string, string> = {
  bedrock: "Bedrock", anthropic: "Anthropic", openai: "OpenAI", azure_openai: "Azure OpenAI", typesafe: "Jev",
  dataforseo: "DataForSEO", geoapify: "Geoapify", bedrock_guardrails: "Guardrails", mock: "Mock",
};

const ACCOUNT_ID_RE = /^\d{12}$/;

/** `(domain, ownName?) => text | null`: null when the value is not a domain. */
export function useDomainText(): (domain: string | null | undefined, ownName?: string | null) => string | null {
  const t = useT(M);
  return (domain, ownName) => {
    const p = domainParts(domain);
    if (!p) return null;
    // A Bedrock row without an account ID runs in an AWS account of this platform (task role / account from the
    // environment); any other kind's account is the vendor's own (its API key), named by the vendor label.
    const account = ACCOUNT_ID_RE.test(p.account) ? p.account
      : VENDOR_CONNECTION_LABELS[p.account]
        ?? (p.kind.startsWith("bedrock") ? ownName ?? t("domainOwnAccount") : t("domainVendorAccount"));
    const scope = p.scope === "aws_only" ? t("scopeAws") : p.scope === "vendor_direct" ? t("scopeVendor") : p.scope;
    return `${KIND_LABEL[p.kind] ?? p.kind} · ${account} · ${scope}`;
  };
}
