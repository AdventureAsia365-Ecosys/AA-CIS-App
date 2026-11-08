# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# Install system deps: C/C++ toolchain for packages that build from source, and git for pip VCS deps.
# Also upgrade the Debian packages the base image already has, so a security fix Debian ships before the next base
# image tag reaches this image too (Trivy fails the build on a fixable HIGH/CRITICAL finding; same as Booking and CRM).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && apt-get install -y \
    curl \
    gcc \
    g++ \
    cmake \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# S273: the URL-pinned wheel of requirements.txt (the spaCy model) comes from a local, sha256-checked copy: the verified
# seed in .url-wheels/ (CI and Deploy Dev put it there from the actions cache) or, when the seed is empty, a download
# with retries and backoff (.github/scripts/url_wheels.py). Same wheel, same pinned hash; pip installs from the rewritten
# file. Both are bind mounts, so neither the script nor the seed lands in a layer.
RUN --mount=type=bind,source=.github/scripts/url_wheels.py,target=/tmp/url_wheels.py \
    --mount=type=bind,source=.url-wheels,target=/tmp/url-wheels-seed \
    python /tmp/url_wheels.py fetch --req requirements.txt --dest /tmp/url-wheels --seed /tmp/url-wheels-seed \
        --out /tmp/requirements-local.txt \
    && pip install --no-cache-dir -r /tmp/requirements-local.txt \
    && rm -rf /tmp/url-wheels /tmp/requirements-local.txt

# Copy source
COPY api/ ./api/
COPY shared/ ./shared/
COPY services/ ./services/
# AA-651 — the standalone job worker's `python -m worker` entrypoint (worker ECS service).
COPY worker/ ./worker/

# AA-870: the commit this image was built from, used as the Sentry release (shared/sentry_init.py).
# Declared after the COPY layers so a new SHA does not invalidate the dependency cache.
ARG GIT_SHA=unknown
ENV APP_GIT_SHA=$GIT_SHA

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Keep-alive must outlive the ALB idle timeout (100 s, AA-CIS-Infra modules/alb): with uvicorn's
# 5 s default the ALB reuses a connection uvicorn already closed and answers 502 (S231, AA-741).
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "105"]
