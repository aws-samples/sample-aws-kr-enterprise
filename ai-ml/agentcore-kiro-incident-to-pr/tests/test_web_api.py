"""REST and WebSocket handlers: web password, workload input, webhook handling, uploads and report status."""
import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest

from conftest import PASSWORD, BUCKET, call, fresh

WEBHOOK = "https://hooks.slack.com/services/T000/B000/abcDEF123"


# ---------------------------------------------------------------- authentication

ROUTES = [("GET", "/api/workloads", None), ("POST", "/api/workloads", None),
          ("GET", "/api/workloads/w1", {"workload_id": "w1"}), ("PUT", "/api/workloads/w1", {"workload_id": "w1"}),
          ("DELETE", "/api/workloads/w1", {"workload_id": "w1"}),
          ("POST", "/api/workloads/w1/upload-url", {"workload_id": "w1"}),
          ("GET", "/api/workloads/w1/documents", {"workload_id": "w1"}),
          ("DELETE", "/api/workloads/w1/documents", {"workload_id": "w1"}),
          ("POST", "/api/workloads/w1/sync", {"workload_id": "w1"}), ("GET", "/api/reports", None),
          ("GET", "/api/reports/r1", {"id": "r1"}), ("DELETE", "/api/reports/r1", {"id": "r1"}),
          ("GET", "/api/status", None), ("POST", "/api/chat", None)]


@pytest.mark.parametrize("method,path,params", ROUTES)
def test_every_route_needs_a_token(api, method, path, params):
    assert call(api, method, path, {}, params=params)[0] == 401
    assert call(api, method, path, {}, token="1.forged", params=params)[0] == 401


def test_login_issues_a_token_only_for_the_password(api):
    assert call(api, "POST", "/api/login", {"password": "wrong"})[0] == 401
    status, body = call(api, "POST", "/api/login", {"password": PASSWORD})
    assert status == 200 and call(api, "GET", "/api/workloads", token=body["token"])[0] == 200


def test_expired_and_tampered_tokens_are_rejected(api, web_auth, token, monkeypatch):
    expiry, signature = token.split(".")
    assert not web_auth.verify(f"{int(expiry) + 3600}.{signature}")
    monkeypatch.setattr(web_auth.time, "time", lambda: int(expiry) + 1)
    web_auth._cache["read_at"] = int(expiry) + 1
    assert call(api, "GET", "/api/workloads", token=token)[0] == 401


def test_changing_the_password_revokes_tokens_after_the_cache(api, web_auth, token):
    boto3.client("secretsmanager").put_secret_value(
        SecretId="incident-to-pr/web-password", SecretString=json.dumps({"password": "another-long-password!", "signing_key": "k" * 48}))
    assert call(api, "GET", "/api/workloads", token=token)[0] == 200  # cached for SECRET_CACHE_SECONDS
    web_auth._cache["read_at"] -= web_auth.SECRET_CACHE_SECONDS + 1
    assert call(api, "GET", "/api/workloads", token=token)[0] == 401


def test_a_weak_secret_refuses_everything(api, web_auth):
    boto3.client("secretsmanager").put_secret_value(
        SecretId="incident-to-pr/web-password", SecretString=json.dumps({"password": "short", "signing_key": "k" * 48}))
    assert call(api, "POST", "/api/login", {"password": "short"})[0] == 503
    assert call(api, "GET", "/api/workloads", token=f"{int(time.time()) + 600}.x")[0] == 503


def test_token_does_not_contain_the_password(token):
    assert PASSWORD not in token and len(token.split(".")) == 2


# ---------------------------------------------------------------- websocket

class FakeConnections:
    def __init__(self):
        self.sent, self.deleted = [], []

    def post_to_connection(self, ConnectionId, Data):
        self.sent.append(json.loads(Data))

    def delete_connection(self, ConnectionId):
        self.deleted.append(ConnectionId)


@pytest.fixture
def ws(web_auth, monkeypatch):
    module = fresh("ws_handler.handler")
    connections = FakeConnections()
    real_client = boto3.client
    monkeypatch.setattr(module.boto3, "client",
                        lambda name, **kw: connections if name == "apigatewaymanagementapi" else real_client(name, **kw))
    return module, connections


def ws_event(route, body=None, query=None):
    return {"requestContext": {"routeKey": route, "connectionId": "c1"}, "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None}


def test_websocket_connect_needs_a_token(ws, token):
    module, _ = ws
    assert module.handler(ws_event("$connect"), None)["statusCode"] == 401
    assert module.handler(ws_event("$connect", query={"token": "1.forged"}), None)["statusCode"] == 401
    assert module.handler(ws_event("$connect", query={"token": token}), None)["statusCode"] == 200


def test_open_websocket_stops_when_the_token_expires(ws, web_auth, token, monkeypatch):
    module, connections = ws
    expiry = int(token.split(".")[0])
    monkeypatch.setattr(web_auth.time, "time", lambda: expiry + 1)
    web_auth._cache["read_at"] = expiry + 1
    module.handler(ws_event("sendMessage", {"message": "hi", "token": token}), None)
    assert connections.sent[-1]["type"] == "unauthorized" and connections.deleted == ["c1"]


def test_websocket_message_without_token_is_refused(ws):
    module, connections = ws
    module.handler(ws_event("sendMessage", {"message": "hi"}), None)
    assert connections.sent[-1]["type"] == "unauthorized"


def test_websocket_message_length_is_limited(ws, token):
    module, connections = ws
    module.handler(ws_event("sendMessage", {"message": "x" * 4001, "token": token}), None)
    assert connections.sent[-1]["type"] == "error" and not connections.deleted


# ---------------------------------------------------------------- workloads and webhook

def test_webhook_is_never_returned(api, token):
    assert call(api, "POST", "/api/workloads", {"workload_id": "w1", "notification": {"slack_webhook": WEBHOOK}},
                token=token)[0] == 201
    for status, body in (call(api, "GET", "/api/workloads", token=token),
                         call(api, "GET", "/api/workloads/w1", token=token, params={"workload_id": "w1"})):
        assert WEBHOOK not in json.dumps(body)
    assert body["notification"] == {"slack_webhook_configured": True}


@pytest.mark.parametrize("url", ["http://hooks.slack.com/services/T0/B0/x", "https://evil.example/services/T0/B0/x",
                                 "https://hooks.slack.com@evil.example/services/T0/B0/x",
                                 "https://hooks.slack.com:8443/services/T0/B0/x",
                                 "https://hooks.slack.com/services/T0/B0/x?redirect=1",
                                 "https://hooks.slack.com/api/chat.postMessage", "http://127.0.0.1:9999/sink"])
def test_only_slack_incoming_webhooks_are_accepted(api, token, url):
    status, _ = call(api, "POST", "/api/workloads", {"workload_id": "w1", "notification": {"slack_webhook": url}},
                     token=token)
    assert status == 400


def test_webhook_edit_semantics(api, token):
    params = {"workload_id": "w1"}
    call(api, "POST", "/api/workloads", {"workload_id": "w1", "notification": {"slack_webhook": WEBHOOK}}, token=token)
    table = boto3.resource("dynamodb").Table("workloads")
    assert call(api, "PUT", "/api/workloads/w1", {"name": "renamed", "notification": {}}, token=token, params=params)[0] == 200
    assert table.get_item(Key={"workload_id": "w1"})["Item"]["notification"] == {"slack_webhook": WEBHOOK}
    assert call(api, "PUT", "/api/workloads/w1", {"notification": {"slack_webhook": ""}}, token=token, params=params)[0] == 400
    assert call(api, "PUT", "/api/workloads/w1", {"notification": {"slack_webhook": None}}, token=token, params=params)[0] == 200
    assert table.get_item(Key={"workload_id": "w1"})["Item"]["notification"] == {}


def test_workload_registration_rules(api, token):
    assert call(api, "POST", "/api/workloads", {"workload_id": "w1"}, token=token)[0] == 201
    assert call(api, "POST", "/api/workloads", {"workload_id": "w1", "name": "attacker"}, token=token)[0] == 409
    assert call(api, "POST", "/api/workloads", {"workload_id": "w2", "alarm_prefixes": [""]}, token=token)[0] == 400
    assert call(api, "POST", "/api/workloads", {"workload_id": "../x"}, token=token)[0] == 400
    assert call(api, "PUT", "/api/workloads/nope", {"name": "x"}, token=token, params={"workload_id": "nope"})[0] == 404


def test_empty_prefix_from_older_data_matches_nothing(aws):
    boto3.resource("dynamodb").Table("workloads").put_item(Item={"workload_id": "old", "alarm_prefixes": [""]})
    assert fresh("orchestrator.handler")._match_workload("any-alarm") is None


# ---------------------------------------------------------------- documents

@pytest.mark.parametrize("wid,filename", [("w1", "../victim/runbook.md"), ("w1", "a/b.md"), ("w1", ".hidden.md"),
                                          ("w1", "run..book.md"), ("w1", "script.sh"), ("..", "runbook.md")])
def test_upload_paths_are_plain_names(api, token, wid, filename):
    call(api, "POST", "/api/workloads", {"workload_id": "w1"}, token=token)
    status, _ = call(api, "POST", f"/api/workloads/{wid}/upload-url", {"filename": filename}, token=token,
                     params={"workload_id": wid})
    assert status == 400


def test_upload_is_a_size_limited_presigned_post(api, token):
    call(api, "POST", "/api/workloads", {"workload_id": "w1"}, token=token)
    status, body = call(api, "POST", "/api/workloads/w1/upload-url", {"filename": "runbook.md"}, token=token,
                        params={"workload_id": "w1"})
    assert status == 200 and body["upload"]["fields"]["key"] == "workloads/w1/runbook.md"
    policy = json.loads(base64.b64decode(body["upload"]["fields"]["policy"]))
    assert ["content-length-range", 1, 1_000_000] in policy["conditions"]


def test_upload_count_is_limited_per_workload(api, token):
    call(api, "POST", "/api/workloads", {"workload_id": "w1"}, token=token)
    s3 = boto3.client("s3")
    for i in range(20):
        s3.put_object(Bucket=BUCKET, Key=f"workloads/w1/doc{i}.md", Body=b"x")
    assert call(api, "POST", "/api/workloads/w1/upload-url", {"filename": "new.md"}, token=token,
                params={"workload_id": "w1"})[0] == 400
    assert call(api, "POST", "/api/workloads/w1/upload-url", {"filename": "doc3.md"}, token=token,
                params={"workload_id": "w1"})[0] == 200  # replacing an existing document is fine


def upload_ticket(api, token, name):
    return call(api, "POST", "/api/workloads/w1/upload-url", {"filename": name}, token=token,
                params={"workload_id": "w1"})[0]


def test_outstanding_upload_tickets_count_toward_the_limit(api, token, monkeypatch):
    call(api, "POST", "/api/workloads", {"workload_id": "w1"}, token=token)
    assert [upload_ticket(api, token, f"file{i}.md") for i in range(20)] == [200] * 20
    assert upload_ticket(api, token, "file20.md") == 400  # nothing uploaded yet, 20 tickets outstanding
    assert upload_ticket(api, token, "file3.md") == 200  # a new ticket for the same key does not count twice
    later = time.time() + 400
    monkeypatch.setattr(api.time, "time", lambda: later)
    assert upload_ticket(api, token, "file20.md") == 200  # expired tickets no longer hold a place
    workload = call(api, "GET", "/api/workloads/w1", token=token, params={"workload_id": "w1"})[1]
    assert "upload_reservations" not in workload and "upload_version" not in workload


def test_sync_leaves_out_unsafe_keys_and_large_objects(api, token):
    s3 = boto3.client("s3")
    s3.put_object(Bucket=BUCKET, Key="runbooks/runbook-sg.md", Body=b"seed")
    s3.put_object(Bucket=BUCKET, Key="workloads/zzz/../victim/runbook.md", Body=b"attack")
    s3.put_object(Bucket=BUCKET, Key="workloads/w1/big.md", Body=b"x" * 1_000_001)
    status, body = call(api, "POST", "/api/workloads/w1/sync", token=token, params={"workload_id": "w1"})
    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key="_sync/manifest.json")["Body"].read())
    assert [d["key"] for d in manifest["documents"]] == ["runbooks/runbook-sg.md"]
    assert body["statistics"]["numberOfDocumentsFailed"] == 2


# ---------------------------------------------------------------- approvals

class FakeRepository:
    """CodeCommit and CodePipeline answers for one fix pull request."""
    def __init__(self, merged=False, deployed=False, closed=False):
        self.merged, self.deployed, self.closed, self.calls = merged, deployed, closed, 0

    def get_pull_request(self, pullRequestId):
        self.calls += 1
        merge = {"isMerged": True, "mergeCommitId": "merge-1"} if self.merged else {"isMerged": False}
        return {"pullRequest": {"pullRequestStatus": "CLOSED" if self.merged or self.closed else "OPEN",
                                "pullRequestTargets": [{"sourceCommit": "src", "mergeMetadata": merge}]}}

    def get_commit(self, repositoryName, commitId):
        return {"commit": {"committer": {"date": "1700000000 +0000"}, "parents": []}}

    def list_pipeline_executions(self, pipelineName, maxResults):
        later = datetime.fromtimestamp(1_700_000_100, timezone.utc)
        runs = [{"status": "Succeeded", "startTime": later, "pipelineExecutionId": "run-old",
                 "sourceRevisions": [{"revisionId": "unrelated"}]}]
        if self.deployed:
            runs.insert(0, {"status": "Succeeded", "startTime": later, "pipelineExecutionId": "run-1",
                            "sourceRevisions": [{"revisionId": "merge-1"}]})
        return {"pipelineExecutionSummaries": runs}


def put_report(status="PENDING", pull_request=None):
    data = {"recommended_actions": [{"action_id": "1", "description": "restore"}]}
    if pull_request is not None:
        data["pull_request"] = pull_request
    boto3.resource("dynamodb").Table("reports").put_item(Item={"report_id": "r1", "status": status, "report_data": data})


def get_report(api, token):
    return call(api, "GET", "/api/reports/r1", token=token, params={"id": "r1"})[1]


def use_repository(api, monkeypatch, fake):
    monkeypatch.setattr(api, "codecommit", fake)
    monkeypatch.setattr(api, "codepipeline", fake)
    return fake


@pytest.mark.parametrize("merged,deployed,closed,status,state", [
    (False, False, False, "PENDING", "OPEN"), (True, False, False, "MERGED", "MERGED"),
    (True, True, False, "RESOLVED", "DEPLOYED"), (False, False, True, "REPORTED", "CLOSED"),
])
def test_report_status_follows_the_fix_pull_request(api, token, monkeypatch, merged, deployed, closed, status, state):
    use_repository(api, monkeypatch, FakeRepository(merged, deployed, closed))
    put_report(pull_request={"pull_request_id": "7", "branch": "incident/fix-r1"})
    report = get_report(api, token)
    assert report["status"] == status and report["report_data"]["pull_request"]["state"] == state
    assert report["report_data"]["pull_request"]["url"].endswith("/pull-requests/7/details?region=ap-northeast-2")


def test_deployed_fix_is_stored_and_not_checked_again(api, token, monkeypatch):
    fake = use_repository(api, monkeypatch, FakeRepository(True, True))
    put_report(pull_request={"pull_request_id": "7"})
    get_report(api, token)
    item = boto3.resource("dynamodb").Table("reports").get_item(Key={"report_id": "r1"})["Item"]
    assert item["status"] == "RESOLVED" and item["report_data"]["pull_request"]["state"] == "DEPLOYED"
    assert item["report_data"]["pull_request"]["deployed_by"] == {"execution_id": "run-1", "revision": "merge-1"}
    calls = fake.calls
    assert get_report(api, token)["status"] == "RESOLVED" and fake.calls == calls


def test_report_without_a_pull_request_keeps_its_status(api, token, monkeypatch):
    fake = use_repository(api, monkeypatch, FakeRepository())
    put_report()
    assert get_report(api, token)["status"] == "PENDING"
    put_report(status="FAILED", pull_request={"pull_request_id": "7"})
    assert get_report(api, token)["status"] == "FAILED" and fake.calls == 0


def test_approve_route_is_gone(api, token):
    put_report()
    status, _ = call(api, "POST", "/api/reports/r1/approve", {"action_id": "1"}, token=token, params={"id": "r1"})
    assert status == 404


def test_internal_errors_are_not_returned(api, token, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("arn:aws:secret-internal-detail")
    monkeypatch.setattr(api, "_list_reports", boom)
    status, body = call(api, "GET", "/api/reports", token=token)
    assert status == 500 and "internal-detail" not in json.dumps(body)


def alarm_event(name, event_id="event-1"):
    return {"Records": [{"body": json.dumps({"id": event_id, "detail": {
        "alarmName": name, "state": {"value": "ALARM", "reason": "r", "timestamp": "2026-11-18T00:00:00Z"}}})}]}


def test_only_registered_alarms_are_analyzed_once_per_event(api, token):
    call(api, "POST", "/api/workloads", {"workload_id": "w1", "alarm_prefixes": ["incident-to-pr-"]}, token=token)
    orchestrator = fresh("orchestrator.handler")
    reports = boto3.resource("dynamodb").Table("reports")
    assert "unregistered" in orchestrator.handler(alarm_event("other-team-alarm"), None)["body"]
    assert reports.scan()["Items"] == []
    first = json.loads(orchestrator.handler(alarm_event("incident-to-pr-alb-unhealthy"), None)["body"])
    again = json.loads(orchestrator.handler(alarm_event("incident-to-pr-alb-unhealthy"), None)["body"])
    assert again == {"duplicate": first["report_id"]} and len(reports.scan()["Items"]) == 1
    orchestrator.handler(alarm_event("incident-to-pr-alb-unhealthy", "event-2"), None)
    assert len(reports.scan()["Items"]) == 2


def test_pull_request_check_runs_without_cdk_roles():
    stack = (Path(__file__).resolve().parents[1] / "infra/lib/gitops-stack.ts").read_text()
    check = stack[stack.index('new codebuild.Project(this, "WorkloadPullRequestCheck"'):stack.index("onPullRequestStateChange")]
    assert "assumeCdkRoles" not in check and "sts:AssumeRole" not in check
    assert "--method=template" in check and "--ignore-scripts" in check
    assert stack.count("addToRolePolicy(assumeCdkRoles)") == 1  # the main deployment only


def test_login_route_is_throttled_separately_in_cdk():
    stack = (Path(__file__).resolve().parents[1] / "infra/lib/platform-stack.ts").read_text()
    assert '"/api/login/POST": { throttlingRateLimit: 1' in stack
