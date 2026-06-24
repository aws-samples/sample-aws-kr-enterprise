import os

import pytest


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDESIGNER_ENVIRONMENT", "test")
    monkeypatch.setenv("MDESIGNER_AWS_REGION", "us-east-1")
    monkeypatch.setenv("MDESIGNER_DYNAMODB_ENDPOINT_URL", "http://localhost:8000")
    monkeypatch.setenv("MDESIGNER_S3_ENDPOINT_URL", "http://localhost:5000")
    monkeypatch.setenv("MDESIGNER_S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("MDESIGNER_JWT_SECRET_NAME", "test/jwt-keys")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
