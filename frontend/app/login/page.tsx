// frontend/app/login/page.tsx
// AA-816 (C4) — staff login page. A server component so it can read two server-only facts and pass
// them to the client form:
//   - LEGACY_ADMIN_LOGIN_ENABLED (default OFF since AA-816 S2): whether the username/password form is shown at all;
//   - whether Cognito sign-in works on THIS host: only allowlisted hosts (production, localhost in
//     development) have registered callback URLs, and the client secret must be configured. On a
//     Vercel preview the company-email button is shown disabled with a note. AA-911: the fixed Preview host
//     aa-cis-preview.lumiguides.it.com has company sign-in and never shows the password form.
// The page never redirects to Cognito by itself (ADR 0006 §7): after a logout, an automatic redirect
// would sign the user straight back in through the managed-login session cookie.
import { headers } from "next/headers";
import { cognitoSignInAvailable, isPreviewSignInHost, workforceSignInHost } from "@/lib/cognito";
import { legacyAdminLoginEnabled } from "@/lib/staffSession";
import LoginForm from "./LoginForm";

const ERRORS: Record<string, string> = {
  denied: "Sign-in was refused. Your account may have no staff access — ask an administrator.",
  no_access: "Your company account has no access to CIS. Ask an administrator to grant a CIS role.",
  signin_failed: "Sign-in could not be completed. Please try again.",
  session_expired: "Your session has ended. Please sign in again.",
  backend: "The CIS service could not be reached. Please try again in a moment.",
  cognito_host: "Company sign-in is only available on the main CIS address.",
  cognito_config: "Company sign-in is not configured on this deployment.",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const host = (await headers()).get("host");
  const params = await searchParams;
  const errorKey = typeof params.error === "string" ? params.error : "";
  return (
    <LoginForm
      legacyEnabled={legacyAdminLoginEnabled() && !isPreviewSignInHost(host)}
      cognitoAvailable={cognitoSignInAvailable(host, "cis-admin")}
      signInHost={workforceSignInHost()}
      initialError={ERRORS[errorKey] ?? ""}
    />
  );
}
