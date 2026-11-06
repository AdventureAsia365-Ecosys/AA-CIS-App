// frontend/app/api/healthz/route.ts
// AA-911 (root ADR 0019 d23) — liveness of the CIS admin container on ECS (service aa-cis-dev-admin, AA-CIS-Infra
// modules/environment/staff_web.tf): the container health check and the ALB target group call it. No auth, no
// upstream call and no session: it says only that this Next server answers, and which commit it runs (the deploy
// workflow compares it with the commit it deployed). Outside proxy.ts (its matcher is /admin/* and /login only).
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json(
    { status: "ok", app: "cis-admin", sha: process.env.GIT_SHA ?? "unknown" },
    { headers: { "Cache-Control": "no-store" } },
  );
}
