"""Unit-test environment — the same dummy values CI sets (.github/workflows/ci.yml, "Run unit
tests"), applied only when the variable is not already set, so `pytest tests/unit` behaves the same
locally as in CI. Set at import time: some modules read these while being imported (JWT_SECRET).

Before this file, 3 tests passed in CI but failed locally because they built an OpenAI client or
opened a DB connection without the env CI provides (aa324 ×2, aa652).
"""
import os

_CI_UNIT_ENV = {
    "ENVIRONMENT": "test",
    "AWS_REGION": "us-west-1",
    "AWS_DEFAULT_REGION": "us-west-1",
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "OPENAI_API_KEY": "sk-test-dummy",
    "JWT_SECRET": "test-jwt-secret-ci-only-not-real",
    "DATAFORSEO_LOGIN": "test",
    "DATAFORSEO_PASSWORD": "test",
    "LANGFUSE_SECRET_KEY": "test",
    "LANGFUSE_PUBLIC_KEY": "test",
    "LANGFUSE_HOST": "http://localhost:3000",
}
for _k, _v in _CI_UNIT_ENV.items():
    os.environ.setdefault(_k, _v)
