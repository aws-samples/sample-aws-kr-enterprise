"""Local tests for the demo's security controls: AWS is mocked with moto, Kiro is replaced by stubs.

Run from the repository root: pip install -r tests/requirements.txt ../kiro-agent-sdk && python -m pytest tests
"""
import importlib
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src" / "lambdas"), str(ROOT / "src" / "agents" / "app")]

PASSWORD = "correct-horse-battery-staple"
SIGNING_KEY = "k" * 48
BUCKET = "incident-to-pr-knowledge-test"


@pytest.fixture
def aws(monkeypatch, tmp_path):
    env = {"AWS_REGION": "ap-northeast-2", "AWS_DEFAULT_REGION": "ap-northeast-2", "AWS_ACCESS_KEY_ID": "test",
           "AWS_SECRET_ACCESS_KEY": "test", "REPORTS_TABLE": "reports", "WORKLOADS_TABLE": "workloads",
           "MEMORY_TABLE": "memory", "KNOWLEDGE_BUCKET": BUCKET,
           "AGENT_RUNTIME_ARN": "", "WEB_PASSWORD_SECRET_ID": "incident-to-pr/web-password",
           "INCIDENT_STATE_DIR": str(tmp_path / "state"), "KIRO_SECRET_ID": "incident-to-pr/kiro-api-key"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("INCIDENT_EXECUTION_DIR", raising=False)
    with mock_aws():
        ddb = boto3.client("dynamodb")
        for name, key in (("reports", "report_id"), ("workloads", "workload_id")):
            ddb.create_table(TableName=name, KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                             BillingMode="PAY_PER_REQUEST")
        ddb.create_table(TableName="memory", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}])
        boto3.client("s3").create_bucket(Bucket=BUCKET,
                                         CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"})
        boto3.client("secretsmanager").create_secret(
            Name="incident-to-pr/web-password", SecretString=json.dumps({"password": PASSWORD, "signing_key": SIGNING_KEY}))
        yield


def fresh(name):
    """Import a module again so its module-level boto3 clients bind to the active moto mock."""
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


@pytest.fixture
def web_auth(aws):
    module = fresh("web_auth")
    module._cache.update(value=None, read_at=0.0)
    return module


@pytest.fixture
def api(web_auth):
    return fresh("api_handler.handler")


@pytest.fixture
def token(web_auth):
    return web_auth.login(PASSWORD)["token"]


def call(api, method, path, body=None, token=None, params=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    event = {"httpMethod": method, "path": path, "headers": headers, "pathParameters": params,
             "body": json.dumps(body) if body is not None else None}
    response = api.handler(event, None)
    return response["statusCode"], json.loads(response["body"])
