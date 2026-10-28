"use client";
// AA-998 — the AWS account directory (account key → account ID) for every admin page that names an account.
// One request per session (the IDs do not change while a page is open); pages render "AWS account" until it loads.
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../_kit";
import type { AwsAccounts, AwsAccountsResp } from "@/lib/awsAccounts";

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
