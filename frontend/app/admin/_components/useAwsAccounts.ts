"use client";
// AA-998 — the AWS account directory (account key → account ID) for every admin page that names an account.
// One request per session (the IDs do not change while a page is open); pages render "AWS account" until it loads.
import { useQuery } from "@tanstack/react-query";
import { apiGet, useLang } from "../../_kit";
import { AWS_ACCOUNT_MESSAGES } from "@/lib/awsAccounts.messages";
import type { AccountText, AwsAccounts, AwsAccountsResp } from "@/lib/awsAccounts";

export function useAwsAccounts(): AwsAccounts | undefined {
  const q = useQuery({
    queryKey: ["aws-accounts"],
    queryFn: () => apiGet<AwsAccountsResp>("/api/admin/aws-accounts"),
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
  });
  return q.data?.accounts;
}

/** AA-994: the words accountName() / accountLabel() use, in the UI language (pass as their last argument). */
export function useAccountText(): AccountText {
  return AWS_ACCOUNT_MESSAGES[useLang()];
}
