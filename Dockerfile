# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# Install system deps including C++ compiler for chroma-hnswlib and git for pip VCS deps
RUN apt-get update && apt-get install -y \
    curl \
    gcc \
    g++ \
    cmake \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

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
