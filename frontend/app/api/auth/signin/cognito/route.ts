// frontend/app/api/auth/signin/cognito/route.ts
// AA-816 (C4) — "Sign in with company email": start auth code + PKCE on the cis-admin client.
import type { NextRequest } from "next/server";
import { handleSignIn } from "@/lib/cognitoRoutes";

export function GET(request: NextRequest) {
  return handleSignIn(request, "cis-admin");
}
