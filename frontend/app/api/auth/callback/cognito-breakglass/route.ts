// frontend/app/api/auth/callback/cognito-breakglass/route.ts
// AA-816 (C4) — Cognito redirect URI for the breakglass client. The path is registered in Terraform
// (identity_variables.tf); do not move it.
import type { NextRequest } from "next/server";
import { handleCallback } from "@/lib/cognitoRoutes";

export function GET(request: NextRequest) {
  return handleCallback(request, "breakglass");
}
