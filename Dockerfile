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

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
