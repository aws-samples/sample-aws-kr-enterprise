"""Agent runtime: the fixer and its pull request tool, report status, knowledge sync, Slack and memory."""
import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

import boto3
import pytest

from conftest import BUCKET, fresh

STACK_FILE = "lib/sample-app-stack.ts"
GOOD = b"ec2Sg.addIngressRule(albSg, ec2.Port.tcp(80));\n"
BAD = b"ec2Sg.addIngressRule(albSg, ec2.Port.tcp(8080));\n"


class FakeCodeCommit:
    """The CodeCommit calls the repository tools make, on an in-memory repository."""
    class exceptions:
        class FileDoesNotExistException(Exception):
            pass

    def __init__(self):
        self.files = {"main": {STACK_FILE: BAD, "app/app.py": b"print('app')\n"}}
        self.heads = {"main": "c-main"}
        self.calls = []
        self.pull_requests = {}
        self.fail_on = None

    def _call(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.fail_on == action:
            raise RuntimeError(f"{action} refused")

    def get_branch(self, repositoryName, branchName):
        return {"branch": {"commitId": self.heads[branchName]}}

    def get_file(self, repositoryName, commitSpecifier, filePath):
        branch = next((b for b, c in self.heads.items() if c == commitSpecifier), commitSpecifier)
        if filePath not in self.files.get(branch, {}):
            raise self.exceptions.FileDoesNotExistException(filePath)
        return {"fileContent": self.files[branch][filePath], "commitId": self.heads.get(branch, commitSpecifier)}

    def create_branch(self, **kwargs):
        self._call("create_branch", **kwargs)
        self.files[kwargs["branchName"]] = dict(self.files["main"])
        self.heads[kwargs["branchName"]] = kwargs["commitId"]

    def put_file(self, **kwargs):
        self._call("put_file", **kwargs)
        assert kwargs["parentCommitId"] == self.heads[kwargs["branchName"]]
        self.files[kwargs["branchName"]][kwargs["filePath"]] = kwargs["fileContent"]
        self.heads[kwargs["branchName"]] = f"c-{len(self.calls)}"
        return {"commitId": self.heads[kwargs["branchName"]]}

    def create_pull_request(self, **kwargs):
        self._call("create_pull_request", **kwargs)
        self.pull_requests["7"] = kwargs
        return {"pullRequest": {"pullRequestId": "7"}}


@pytest.fixture
def repository(aws, monkeypatch, tmp_path):
    module = fresh("incident_tools.repository")
    fake = FakeCodeCommit()
    monkeypatch.setattr(module, "codecommit", fake)
    fix_dir = tmp_path / "fix"
    fix_dir.mkdir()
    monkeypatch.setenv(module.FIX_DIR_ENV, str(fix_dir))
    monkeypatch.setenv(module.REPORT_ID_ENV, "report-1")
    return SimpleNamespace(module=module, fake=fake, dir=fix_dir)


@pytest.fixture
def tools(aws):
    return fresh("incident_tools.aws")


@pytest.fixture
def main(tools, monkeypatch):
    fresh("incident_tools.repository")
    fresh("kiro_runtime")
    fresh("agents")
    module = fresh("main")

    async def prepared():
        return None
    monkeypatch.setattr(module, "_prepare", prepared)
    monkeypatch.setenv("KIRO_API_KEY", "test-key")  # the RCA reads the Kiro credentials before any agent starts
    return module


def restore(content=GOOD):
    return [{"path": STACK_FILE, "content": content.decode()}]


# ---------------------------------------------------------------- fix pull request tool

def test_pull_request_is_opened_from_a_fix_branch_into_main(repository):
    reply = json.loads(repository.module.open_fix_pull_request("포트 복구", "근거와 변경", restore()))
    fake = repository.fake
    assert reply == {"pull_request_id": "7", "branch": "incident/fix-report-1", "files": [STACK_FILE]}
    assert [name for name, _ in fake.calls] == ["create_branch", "put_file", "create_pull_request"]
    assert fake.calls[0][1]["commitId"] == "c-main" and fake.files["main"][STACK_FILE] == BAD
    target = fake.pull_requests["7"]["targets"][0]
    assert (target["sourceReference"], target["destinationReference"]) == ("incident/fix-report-1", "main")
    result = repository.module.read_fix_result(repository.dir)
    assert result["status"] == "OPEN" and result["pull_request_id"] == "7" and result["base_commit"] == "c-main"


def test_only_one_pull_request_per_fixer_session(repository):
    repository.module.open_fix_pull_request("t", "d", restore())
    assert repository.module.open_fix_pull_request("t", "d", restore()).startswith("Error")
    assert sum(name == "create_pull_request" for name, _ in repository.fake.calls) == 1


def test_tool_refuses_outside_a_fixer_session(repository, monkeypatch):
    monkeypatch.delenv(repository.module.FIX_DIR_ENV)
    assert repository.module.open_fix_pull_request("t", "d", restore()).startswith("Error")
    assert repository.fake.calls == []


@pytest.mark.parametrize("path", ["package.json", "package-lock.json", "cdk.json", "bin/app.ts", "tsconfig.json"])
def test_build_configuration_is_not_changeable(repository, path):
    repository.fake.files["main"][path] = b"{}\n"
    reply = repository.module.open_fix_pull_request("t", "d", [{"path": path, "content": '{"scripts": {}}'}])
    assert reply.startswith("Error:") and "lib/ or app/" in reply
    assert repository.fake.calls == [] and not (repository.dir / "result.json").exists()


@pytest.mark.parametrize("changes", [
    [], [{"path": "../etc/passwd", "content": "x"}], [{"path": "/lib/a.ts", "content": "x"}],
    [{"path": "lib/new-file.ts", "content": "x"}], [{"path": STACK_FILE, "content": BAD.decode()}],
    [{"path": STACK_FILE, "content": "x" * 200_001}], restore() + restore(),
    [{"path": f"lib/{i}.ts", "content": "x"} for i in range(6)], "not a list",
])
def test_invalid_changes_open_nothing(repository, changes):
    assert repository.module.open_fix_pull_request("t", "d", changes).startswith("Error")
    assert repository.fake.calls == [] and repository.module.read_fix_result(repository.dir) is None


def test_refused_write_is_recorded_as_failed(repository):
    repository.fake.fail_on = "create_pull_request"
    assert repository.module.open_fix_pull_request("t", "d", restore()).startswith("Error")
    result = repository.module.read_fix_result(repository.dir)
    assert result["status"] == "FAILED" and "refused" in result["error"]


class FakeState:
    # later-1 was committed on top of the merge commit; other shares no history with it.
    HISTORY = {"later-1": ["merge-1"], "merge-1": ["base"], "other": ["base"], "base": []}

    def __init__(self, merged, status="OPEN", runs=()):
        self.merged, self.status, self.runs = merged, status, list(runs)

    def get_pull_request(self, pullRequestId):
        merge = {"isMerged": self.merged, "mergeCommitId": "merge-1"} if self.merged else {"isMerged": False}
        return {"pullRequest": {"pullRequestStatus": self.status,
                                "pullRequestTargets": [{"sourceCommit": "src", "mergeMetadata": merge}]}}

    def get_commit(self, repositoryName, commitId):
        return {"commit": {"committer": {"date": "1700000000 +0000"}, "parents": self.HISTORY.get(commitId, [])}}

    def list_pipeline_executions(self, pipelineName, maxResults):
        return {"pipelineExecutionSummaries": self.runs}


def run(status, started, revision="other"):
    return {"status": status, "startTime": datetime.fromtimestamp(started, timezone.utc),
            "sourceRevisions": [{"revisionId": revision}]}


@pytest.mark.parametrize("fake,state", [
    (FakeState(False), "OPEN"), (FakeState(False, "CLOSED"), "CLOSED"),
    (FakeState(True, "CLOSED", [run("InProgress", 1_700_000_100)]), "MERGED"),
    (FakeState(True, "CLOSED", [run("Succeeded", 1_699_999_000)]), "MERGED"),
    (FakeState(True, "CLOSED", [run("Succeeded", 1_699_999_000, "merge-1")]), "DEPLOYED"),
    (FakeState(True, "CLOSED", [run("Succeeded", 1_700_000_100, "later-1")]), "DEPLOYED"),
    # A later run of a revision that does not contain the merge is not a deployment of the fix.
    (FakeState(True, "CLOSED", [run("Succeeded", 1_700_000_100)]), "MERGED"),
    (FakeState(True, "CLOSED", [run("Succeeded", 1_700_000_100, "base")]), "MERGED"),
])
def test_pull_request_state_follows_merge_and_deployment(aws, monkeypatch, fake, state):
    module = fresh("incident_tools.repository")
    monkeypatch.setattr(module, "codecommit", fake)
    monkeypatch.setattr(module, "codepipeline", fake)
    assert module.pull_request_state({"pull_request_id": "7"}) == state


# ---------------------------------------------------------------- RCA report and fixer

def test_checked_actions_keep_text_fields_only(main):
    actions = [{"action_id": "x", "description": "restore", "change": "lib/sample-app-stack.ts: 8080 -> 80",
                "status": "COMPLETED", "operation": "start_instance"}, "not an action"]
    checked = main.checked_actions(actions)
    assert checked == [{"action_id": "1", "priority": 1, "description": "restore",
                        "change": "lib/sample-app-stack.ts: 8080 -> 80", "risk_level": "HIGH"}]


def test_unreadable_review_counts_as_failed(main):
    agents = fresh("agents")
    assert agents._verdict("looks fine to me") == "FAIL"
    assert agents._verdict('{"verdict": "PASS"}') == "PASS"


def stored(report_id="r1"):
    return boto3.resource("dynamodb").Table("reports").get_item(Key={"report_id": report_id})["Item"]


def rca_returns(main, monkeypatch, report, passed):
    boto3.resource("dynamodb").Table("reports").put_item(Item={"report_id": "r1", "status": "ANALYZING"})

    async def rca(prompt):
        return json.dumps(report), passed
    monkeypatch.setattr(main.agents, "run_rca", rca)


def test_rca_that_never_passes_review_opens_no_pull_request(main, monkeypatch):
    rca_returns(main, monkeypatch, {"summary": "s", "recommended_actions": [{"description": "fix"}]}, False)
    called = []

    async def fixer(*args):
        called.append(args)
    monkeypatch.setattr(main.agents, "run_fixer", fixer)
    asyncio.run(main._rca_background("p", {"report_id": "r1"}))
    assert stored()["status"] == "FAILED" and called == [] and "pull_request" not in stored()["report_data"]


def test_missing_kiro_credentials_are_named_in_the_report(main, monkeypatch):
    rca_returns(main, monkeypatch, {"summary": "s"}, True)
    monkeypatch.delenv("KIRO_API_KEY")  # and the secret does not exist yet
    asyncio.run(main._rca_background("p", {"report_id": "r1"}))
    assert stored()["status"] == "FAILED" and "Kiro 인증 정보" in stored()["report_data"]["summary"]


def test_writer_cannot_set_platform_fields(main, monkeypatch):
    rca_returns(main, monkeypatch, {"summary": "s", "recommended_actions": [], "report_id": "other",
                                    "alarm": {"name": "invented"}, "status": "RESOLVED",
                                    "pull_request": {"pull_request_id": "999", "branch": "incident/model-invented"}}, True)

    async def fixer(*args):
        raise AssertionError("the fixer must not run")
    monkeypatch.setattr(main.agents, "run_fixer", fixer)
    asyncio.run(main._rca_background("p", {"report_id": "r1", "alarm": {"name": "incident-to-pr-alb-unhealthy"}}))
    data = stored()["report_data"]
    assert stored()["status"] == "REPORTED" and "pull_request" not in data
    assert data["report_id"] == "r1" and data["alarm"]["name"] == "incident-to-pr-alb-unhealthy"


@pytest.mark.parametrize("review,verdict", [
    ("VERDICT: PASS\nSCORE: 9", "PASS"), ('{"verdict": "PASS"}', "PASS"), ("VERDICT: FAIL", "FAIL"),
    ("no verdict here", "FAIL"), ("Example: VERDICT: PASS\nActual VERDICT: FAIL", "FAIL"),
])
def test_reviewer_verdict_must_be_unambiguous(main, review, verdict):
    assert main.agents._verdict(review) == verdict


def test_pull_request_comes_from_the_tool_record_not_the_model_text(main, monkeypatch):
    rca_returns(main, monkeypatch, {"summary": "s", "recommended_actions": [{"description": "fix"}]}, True)
    seen = {}

    async def fixer(prompt, report_id, fix_dir):
        seen.update(prompt=prompt, report_id=report_id)
        (fix_dir / "result.json").write_text(json.dumps({"status": "OPEN", "pull_request_id": "9",
                                                         "branch": "incident/fix-r1", "files": [STACK_FILE]}))
        return "I opened pull request 1234 and merged it"
    monkeypatch.setattr(main.agents, "run_fixer", fixer)
    asyncio.run(main._rca_background("p", {"report_id": "r1"}))
    item = stored()
    assert item["status"] == "PENDING" and seen["report_id"] == "r1" and "r1" in seen["prompt"]
    assert item["report_data"]["pull_request"]["pull_request_id"] == "9"


def test_fixer_without_a_pull_request_leaves_the_report_without_one(main, monkeypatch):
    rca_returns(main, monkeypatch, {"summary": "s", "recommended_actions": [{"description": "rerun pipeline"}]}, True)

    async def fixer(prompt, report_id, fix_dir):
        return "The deployed state differs from main; run the pipeline again"
    monkeypatch.setattr(main.agents, "run_fixer", fixer)
    asyncio.run(main._rca_background("p", {"report_id": "r1"}))
    assert stored()["status"] == "REPORTED" and "pull_request" not in stored()["report_data"]


def test_no_fixer_run_when_there_is_nothing_to_change(main, monkeypatch):
    rca_returns(main, monkeypatch, {"summary": "client traffic", "recommended_actions": []}, True)

    async def fixer(*args):
        raise AssertionError("the fixer must not run")
    monkeypatch.setattr(main.agents, "run_fixer", fixer)
    asyncio.run(main._rca_background("p", {"report_id": "r1"}))
    assert stored()["status"] == "REPORTED"


def test_fixer_error_keeps_a_pull_request_it_already_opened(main, monkeypatch):
    rca_returns(main, monkeypatch, {"summary": "s", "recommended_actions": [{"description": "fix"}]}, True)

    async def fixer(prompt, report_id, fix_dir):
        (fix_dir / "result.json").write_text(json.dumps({"status": "OPEN", "pull_request_id": "9"}))
        raise RuntimeError("turn failed afterwards")
    monkeypatch.setattr(main.agents, "run_fixer", fixer)
    asyncio.run(main._rca_background("p", {"report_id": "r1"}))
    assert stored()["report_data"]["pull_request"]["pull_request_id"] == "9"


def test_only_the_fixer_can_reach_the_change_tool(main):
    runtime = main.kiro_runtime
    context = SimpleNamespace(kiro_metadata={}, title="@changes/open_fix_pull_request")

    async def decide(role):
        return await runtime._policy(role)("@changes/open_fix_pull_request", {}, context)
    from kiro_agent_sdk import PermissionResultAllow, PermissionResultDeny
    assert isinstance(asyncio.run(decide("fixer")), PermissionResultAllow)
    for role in ("collector", "chatbot", "writer", "reviewer"):
        assert isinstance(asyncio.run(decide(role)), PermissionResultDeny)


# ---------------------------------------------------------------- knowledge sync

def manifest(documents):
    boto3.client("s3").put_object(Bucket=BUCKET, Key="_sync/manifest.json", Body=json.dumps(
        {"job_id": str(uuid.uuid4()), "documents": documents}).encode())


def document(key, body):
    s3 = boto3.client("s3")
    s3.put_object(Bucket=BUCKET, Key=key, Body=body)
    return {"key": key, "etag": s3.head_object(Bucket=BUCKET, Key=key)["ETag"]}


def local(main, key):
    return (main.kiro_runtime.KNOWLEDGE_DIR / key).read_bytes()


def test_sync_replaces_the_knowledge_at_once(main):
    manifest([document("runbooks/a.md", b"v1")])
    main.sync_knowledge()
    manifest([document("runbooks/b.md", b"v2")])
    main.sync_knowledge()
    assert local(main, "runbooks/b.md") == b"v2" and not (main.kiro_runtime.KNOWLEDGE_DIR / "runbooks/a.md").exists()


@pytest.mark.parametrize("bad_key", ["workloads/zzz/../victim/runbook.md", "workloads/zzz/../../runbooks/a.md",
                                     "../outside.md", "workloads/w1/.hidden.md", "other/a.md"])
def test_unsafe_manifest_keeps_the_previous_knowledge(main, bad_key):
    manifest([document("workloads/victim/runbook.md", b"good")])
    main.sync_knowledge()
    manifest([document("workloads/victim/runbook.md", b"good"), document(bad_key, b"attack")])
    with pytest.raises(main.KnowledgeSyncError):
        main.sync_knowledge()
    assert local(main, "workloads/victim/runbook.md") == b"good"


def test_object_replaced_after_the_manifest_is_not_used(main):
    manifest([document("runbooks/a.md", b"good")])
    main.sync_knowledge()
    manifest([document("runbooks/a.md", b"reviewed")])
    boto3.client("s3").put_object(Bucket=BUCKET, Key="runbooks/a.md", Body=b"swapped after sync")
    with pytest.raises(main.KnowledgeSyncError):
        main.sync_knowledge()
    assert local(main, "runbooks/a.md") == b"good"


def test_actual_object_size_is_enforced(main):
    manifest([{**document("runbooks/a.md", b"x" * (main.MAX_DOCUMENT_BYTES + 1)), "size": 10}])
    with pytest.raises(main.KnowledgeSyncError):
        main.sync_knowledge()


def test_first_sync_without_a_manifest_uses_the_same_rules(main):
    document("runbooks/a.md", b"ok")
    document("workloads/zzz/../runbooks/a.md", b"attack")
    with pytest.raises(main.KnowledgeSyncError):
        main.sync_knowledge()


def test_total_size_is_enforced(main, monkeypatch):
    monkeypatch.setattr(main, "MAX_TOTAL_BYTES", 5)
    manifest([document("runbooks/a.md", b"abc"), document("runbooks/b.md", b"def")])
    with pytest.raises(main.KnowledgeSyncError):
        main.sync_knowledge()


# ---------------------------------------------------------------- Slack

class Redirecting(BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):
        Redirecting.hits.append(self.path)
        self.send_response(302)
        self.send_header("Location", "/internal-target")
        self.end_headers()

    do_GET = do_POST

    def log_message(self, *args):
        pass


def test_notification_does_not_follow_redirects(main):
    server = HTTPServer(("127.0.0.1", 0), Redirecting)
    threading.Thread(target=server.handle_request, daemon=True).start()
    with pytest.raises(HTTPError):
        main._slack.open(Request(f"http://127.0.0.1:{server.server_port}/hook", data=b"{}"), timeout=5)
    server.server_close()
    assert Redirecting.hits == ["/hook"]


def test_invalid_stored_webhook_is_not_called(main, monkeypatch):
    opened = []
    monkeypatch.setattr(main._slack, "open", lambda *a, **k: opened.append(a))

    async def rca(prompt):
        return '{"summary": "s"}', True
    monkeypatch.setattr(main.agents, "run_rca", rca)
    boto3.resource("dynamodb").Table("reports").put_item(Item={"report_id": "r1"})
    asyncio.run(main._rca_background("p", {"report_id": "r1",
                                           "notification": {"slack_webhook": "http://169.254.169.254/latest"}}))
    assert not opened


# ---------------------------------------------------------------- chat memory

class FakeKiro:
    def __init__(self, memories=()):
        self.created, self.memories = [], list(memories)

    async def create_kiro_memory(self, **kw):
        self.created.append(kw["title"])

    async def list_kiro_memories(self, **kw):
        return {"memories": self.memories}


def test_expired_memories_are_not_restored(main):
    agents = fresh("agents")
    table = boto3.resource("dynamodb").Table("memory")
    table.put_item(Item={"pk": "memory#user", "sk": "old", "title": "old", "content": "x", "expires_at": int(time.time()) - 1})
    table.put_item(Item={"pk": "memory#user", "sk": "legacy", "title": "legacy", "content": "x"})
    table.put_item(Item={"pk": "memory#user", "sk": "new", "title": "new", "content": "x", "expires_at": int(time.time()) + 60})
    client = FakeKiro()
    asyncio.run(agents._restore_memories(client))
    assert client.created == ["new"]


def test_unchanged_memories_keep_their_expiry(main):
    agents = fresh("agents")
    table = boto3.resource("dynamodb").Table("memory")
    table.put_item(Item={"pk": "memory#user", "sk": "fact", "title": "fact", "content": "same", "expires_at": 100})
    agents._chat["memories"] = {"fact": "same"}
    asyncio.run(agents._persist_memories(FakeKiro([{"title": "fact", "content": "same"},
                                                   {"title": "fresh", "content": "new"}])))
    assert table.get_item(Key={"pk": "memory#user", "sk": "fact"})["Item"]["expires_at"] == 100
    assert table.get_item(Key={"pk": "memory#user", "sk": "fresh"})["Item"]["expires_at"] > time.time()


def test_fixer_first_prompt_waits_for_discovery(main, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    runtime = main.kiro_runtime
    repository = {"name": "repository", "status": "connected", "tools": [
        {"name": "get_commit_changes"}, {"name": "read_repository_file"}]}
    changes = {"name": "changes", "status": "connected", "tools": [{"name": "open_fix_pull_request"}]}
    snapshots = iter([{}, {"mcpServers": [repository, {**changes, "tools": []}]},
                      {"mcpServers": [repository, changes]}])
    client = MagicMock()
    client.get_mcp_status = AsyncMock(side_effect=lambda: next(snapshots))
    client.query = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__.return_value = client
    monkeypatch.setattr(runtime, "KiroSDKClient", lambda _: ctx)
    monkeypatch.setattr(runtime, "options", lambda *a, **kw: None)
    monkeypatch.setattr(runtime, "prepare", lambda: None)
    monkeypatch.setattr(runtime, "response", AsyncMock(return_value="done"))
    assert asyncio.run(runtime.ask("fixer", "test")) == "done"
    client.query.assert_awaited_once_with("test")
    assert client.get_mcp_status.await_count == 3


def test_fixer_unavailable_tools_fail_within_deadline(main):
    from unittest.mock import AsyncMock
    client = SimpleNamespace(get_mcp_status=AsyncMock(return_value={"mcpServers": []}))
    with pytest.raises(TimeoutError, match="repository, changes"):
        asyncio.run(main.kiro_runtime.wait_for_fixer_tools(client, timeout=0))
