# syntax=docker/dockerfile:1
# AA-911 (root ADR 0019 d23) — CIS admin image: the Next.js standalone server, run as the `cis-admin` container of the
# ECS Dev service aa-cis-dev-admin (AA-CIS-Infra modules/environment/staff_web.tf: Fargate ARM64, port 3000, health
# check /api/healthz). Built natively on an arm64 runner
# (.github/workflows/deploy-admin-dev.yml). Same shape as AA-Partner web/Dockerfile.
FROM node:22-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# The build never needs a secret: every server value (API_URL, ADMIN_SECRET, the Cognito client ids and secrets) is
# read from the task's environment at run time. Public build-time values only (inlined into the browser bundle): the
# browser Sentry DSN (public by design; empty = off) and the release tag. No SENTRY_AUTH_TOKEN: no source-map upload.
ARG NEXT_PUBLIC_SENTRY_DSN=""
ARG NEXT_PUBLIC_ENV=dev
ARG GIT_SHA=unknown
ENV NEXT_PUBLIC_SENTRY_DSN=${NEXT_PUBLIC_SENTRY_DSN} \
    NEXT_PUBLIC_ENV=${NEXT_PUBLIC_ENV} \
    NEXT_PUBLIC_BUILD_SHA=${GIT_SHA} \
    NEXT_OUTPUT=standalone
RUN NODE_OPTIONS=--max-old-space-size=2048 npm run build

FROM node:22-alpine AS runtime
ARG GIT_SHA=unknown
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    GIT_SHA=${GIT_SHA} \
    # Keep-alive must outlive the ALB idle timeout (100 s); Node's 5 s default makes the ALB reuse a closed
    # connection and answer 502 (lesson S231, AA-741). Read by the Next standalone server.
    KEEP_ALIVE_TIMEOUT=105000
# The server needs node only: drop npm, npx and corepack from the runtime image (smaller attack surface).
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
      /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.next/standalone ./
COPY --from=build --chown=10001:10001 /app/.next/static ./.next/static
COPY --from=build --chown=10001:10001 /app/public ./public
USER 10001
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/api/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
# Fargate sets HOSTNAME to the task's own hostname at run time, overriding the ENV above, and the Next standalone
# server binds to HOSTNAME: it would listen on the private IP only and the 127.0.0.1 health check would fail (lesson
# S249, AA-935). Force all interfaces at start, without a shell.
CMD ["node", "-e", "process.env.HOSTNAME='0.0.0.0'; require('./server.js')"]
