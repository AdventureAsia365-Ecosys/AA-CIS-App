// frontend/app/api/auth/callback/cognito/route.ts
// AA-816 (C4) — Cognito redirect URI for the cis-admin client. The path is registered in Terraform
// (identity_variables.tf); do not move it.
import type { NextRequest } from "next/server";
import { handleCallback } from "@/lib/cognitoRoutes";

export function GET(request: NextRequest) {
  return handleCallback(request, "cis-admin");
}
