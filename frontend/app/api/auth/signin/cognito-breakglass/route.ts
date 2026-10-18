// frontend/app/api/auth/signin/cognito-breakglass/route.ts
// AA-816 (C4) — break-glass sign-in through its own `breakglass` client (password + TOTP, ADR 0006
// §11). Production host only; every token issue raises the break-glass alarm.
import type { NextRequest } from "next/server";
import { handleSignIn } from "@/lib/cognitoRoutes";

export function GET(request: NextRequest) {
  return handleSignIn(request, "breakglass");
}
