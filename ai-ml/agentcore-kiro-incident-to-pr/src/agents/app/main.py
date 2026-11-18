"""AgentCore entrypoint of the incident agents: routes to the RCA loop (followed by the Fixer) or the Chatbot, all running on Kiro.

POST /invocations takes {"prompt", "agent", ...}; GET /ping reports HealthyBusy while a background RCA runs.
A reviewed RCA report gets one fix pull request to the workload repository. People review and merge it, and the
workload pipeline deploys it; agents never change AWS resources themselves.
"""
import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import boto3
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request as HttpRequest
from starlette.responses import JSONResponse
from starlette.routing import Route

import agents
import kiro_runtime
from incident_tools import repository

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("incident.app")
REGION = kiro_runtime.REGION
KNOWLEDGE_BUCKET = os.environ.get("KNOWLEDGE_BUCKET", "")
_tasks = set()
_last_status = {"status": "Healthy", "time": int(time.time())}


def _status():
    status = "HealthyBusy" if _tasks else "Healthy"
    if status != _last_status["status"]:
        _last_status.update(status=status, time=int(time.time()))
    return _last_status


def _start(coroutine):
    task = asyncio.get_running_loop().create_task(coroutine)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    _status()
    return task


# ========================================================================
# Knowledge documents (replaces the Bedrock Knowledge Base)
# ========================================================================

# Same key rules as the API handler: seed documents under runbooks/ and system-specs/, uploads under
# workloads/<workload_id>/<file name>. Every key segment is a plain name, so each key maps to its own local file.
WORKLOAD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
FILE_NAME = re.compile(r"\w[\w .-]{0,99}")
TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".log")
MAX_DOCUMENTS = 200
MAX_DOCUMENT_BYTES = 1_000_000
MAX_TOTAL_BYTES = 20_000_000
_knowledge = {"manifest": None}


class KnowledgeSyncError(RuntimeError):
    pass


def valid_document_key(key):
    parts = key.split("/") if isinstance(key, str) else []
    if len(parts) == 2 and parts[0] in ("runbooks", "system-specs"):
        name = parts[1]
    elif len(parts) == 3 and parts[0] == "workloads" and WORKLOAD_ID.fullmatch(parts[1]):
        name = parts[2]
    else:
        return False
    return bool(FILE_NAME.fullmatch(name)) and ".." not in name and name.lower().endswith(TEXT_SUFFIXES)


def _knowledge_documents(s3):
    try:
        manifest = json.loads(s3.get_object(Bucket=KNOWLEDGE_BUCKET, Key="_sync/manifest.json")["Body"].read())
        return manifest["documents"], manifest["job_id"]
    except s3.exceptions.NoSuchKey:
        listing = s3.get_paginator("list_objects_v2").paginate(Bucket=KNOWLEDGE_BUCKET)
        documents = [{"key": o["Key"], "etag": o["ETag"]} for page in listing for o in page.get("Contents", [])
                     if not o["Key"].startswith("_sync/") and not o["Key"].endswith("/")]
        return documents, json.dumps(documents, sort_keys=True)


def _check_documents(documents, root):
    """Reject the whole document set before anything is written, so a bad set never replaces good knowledge."""
    if not isinstance(documents, list) or len(documents) > MAX_DOCUMENTS:
        raise KnowledgeSyncError(f"the knowledge set must list at most {MAX_DOCUMENTS} documents")
    targets = set()
    for document in documents:
        key, etag = (document.get("key"), document.get("etag")) if isinstance(document, dict) else (None, None)
        if not valid_document_key(key) or not isinstance(etag, str) or not etag:
            raise KnowledgeSyncError(f"invalid knowledge document {key!r}")
        target = (root / key).resolve()
        if root.resolve() not in target.parents or target in targets:
            raise KnowledgeSyncError(f"knowledge document {key!r} collides with another document")
        targets.add(target)


def _download(s3, document, budget):
    """Bytes of the exact object version listed (IfMatch), read with a size cap."""
    try:
        body = s3.get_object(Bucket=KNOWLEDGE_BUCKET, Key=document["key"], IfMatch=document["etag"])["Body"]
    except s3.exceptions.ClientError as e:
        raise KnowledgeSyncError(f"{document['key']} changed or is unavailable since the sync: {e}") from None
    data = bytearray()
    for chunk in body.iter_chunks(64 * 1024):
        data += chunk
        if len(data) > min(MAX_DOCUMENT_BYTES, budget):
            body.close()
            raise KnowledgeSyncError(f"{document['key']} exceeds the knowledge size limits")
    return bytes(data)


def sync_knowledge():
    """Download the documents listed by the latest knowledge sync manifest (or the whole bucket before the first sync).

    The documents are staged and swapped in at once; on any error the previous knowledge stays in place.
    """
    if not KNOWLEDGE_BUCKET:
        return
    s3 = boto3.client("s3", region_name=REGION)
    documents, version = _knowledge_documents(s3)
    if _knowledge["manifest"] == version:
        return
    root = kiro_runtime.KNOWLEDGE_DIR
    _check_documents(documents, root)
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="knowledge-", dir=root.parent))
    try:
        budget = MAX_TOTAL_BYTES
        for document in documents:
            data = _download(s3, document, budget)
            budget -= len(data)
            target = staging / document["key"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        previous = root.with_name(f"{root.name}.old-{uuid.uuid4().hex}") if root.exists() else None
        if previous:
            root.rename(previous)
        staging.rename(root)
        if previous:
            shutil.rmtree(previous, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _knowledge["manifest"] = version


async def _prepare():
    kiro_runtime.prepare()
    try:
        await asyncio.to_thread(sync_knowledge)
    except Exception:
        logger.exception("knowledge sync failed; keeping the previous knowledge documents")


# ========================================================================
# Background tasks (same DynamoDB and Slack updates as the original agent)
# ========================================================================

SLACK_WEBHOOK_PATH = re.compile(r"/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+")
FIXES_DIR = kiro_runtime.STATE_DIR / "fixes"
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")


def valid_slack_webhook(url):
    """Same rule as the API handler: an https://hooks.slack.com/services/... incoming webhook URL only."""
    if not isinstance(url, str) or len(url) > 300:
        return False
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.netloc in ("hooks.slack.com", "hooks.slack.com:443")
            and not parts.query and not parts.fragment and bool(SLACK_WEBHOOK_PATH.fullmatch(parts.path)))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect becomes an HTTPError instead of a request to another host


_slack = build_opener(_NoRedirect)


def _reports_table():
    return boto3.resource("dynamodb", region_name=REGION).Table(os.environ.get("REPORTS_TABLE", "incident-to-pr-reports"))


def _clip(value, limit):
    return value[:limit] if isinstance(value, str) else ""


def checked_actions(actions):
    """Recommended actions as plain text for people; the fix itself is the pull request."""
    checked = []
    for index, action in enumerate(actions if isinstance(actions, list) else [], 1):
        if not isinstance(action, dict) or index > 10:
            continue
        priority = action.get("priority")
        risk = action.get("risk_level")
        checked.append({"action_id": str(index), "priority": priority if isinstance(priority, int) else index,
                        "description": _clip(action.get("description"), 1000), "change": _clip(action.get("change"), 2000),
                        "risk_level": risk if risk in RISK_LEVELS else "HIGH"})
    return checked


def _fixer_prompt(report_id, report):
    summary = {key: report.get(key) for key in ("summary", "root_cause", "impact", "recommended_actions")}
    return (f"Open the pull request that fixes this incident (report {report_id}).\n"
            f"Report link: {os.environ.get('WEB_URL', '')}/reports/{report_id}\n\n"
            f"=== Reviewed RCA report ===\n{json.dumps(summary, ensure_ascii=False, default=str)}")


async def _propose_fix(report_id, report):
    """Run the fixer once and return what its tool recorded: the pull request, a failure, or None."""
    directory = FIXES_DIR / uuid.uuid4().hex
    directory.mkdir(parents=True)
    try:
        await agents.run_fixer(_fixer_prompt(report_id, report), report_id, directory)
    except Exception as e:
        logger.exception("fixer failed")
        result = repository.read_fix_result(directory)
        return result or {"status": "FAILED", "error": f"Fixer 오류: {type(e).__name__}"}
    return repository.read_fix_result(directory)


async def _rca_background(prompt, payload):
    report_id = payload.get("report_id", "")
    alarm_data = payload.get("alarm", {})
    notification = payload.get("notification", {})
    base = {"report_id": report_id, "created_at": payload.get("created_at", ""),
            "workload": payload.get("workload_name", ""), "alarm": alarm_data}
    try:
        await _prepare()
        await kiro_runtime.check_credentials()
        report_text, passed = await agents.run_rca(prompt)
        parsed = agents.parse_report(report_text)
        parsed["recommended_actions"] = checked_actions(parsed.get("recommended_actions"))
        if not passed:
            parsed["review"] = "Reviewer가 리포트를 통과시키지 않았습니다. 수정 PR을 만들지 않았습니다."
        elif parsed["recommended_actions"]:
            fix = await _propose_fix(report_id, parsed)
            if fix:
                parsed["pull_request"] = fix
        # PENDING waits for the review of an open fix pull request; a reviewed report without one is REPORTED.
        opened = (parsed.get("pull_request") or {}).get("pull_request_id")
        status = "FAILED" if not passed else "PENDING" if opened else "REPORTED"
        report_data = {**parsed, **base, "status": status}
    except Exception as e:
        logger.exception("RCA failed")
        summary = (f"분석 오류: Kiro 인증 정보를 읽지 못했습니다. Secrets Manager {os.environ.get('KIRO_SECRET_ID', '')}를 확인하세요."
                   if isinstance(e, kiro_runtime.CredentialsUnavailable) else f"분석 오류: {type(e).__name__}")
        report_data = {**base, "summary": summary, "timeline": [],
                       "root_cause": {"description": "RCA 분석 중 오류가 발생했습니다.", "evidence": [], "confidence": "LOW"},
                       "impact": {}, "recommended_actions": [], "status": "FAILED"}
        status = "FAILED"
    try:
        _reports_table().update_item(Key={"report_id": report_id}, UpdateExpression="SET report_data = :rd, #s = :s",
                                     ExpressionAttributeNames={"#s": "status"},
                                     ExpressionAttributeValues={":rd": report_data, ":s": status})
    except Exception:
        logger.exception("report update failed")
    slack_url = notification.get("slack_webhook", "") if isinstance(notification, dict) else ""
    if not slack_url:
        return
    if not valid_slack_webhook(slack_url):
        logger.warning("skipping a notification URL that is not a Slack incoming webhook")
        return
    try:
        msg = (f"Incident Alert: {alarm_data.get('name', '')}\n\n{report_data.get('summary', '')}\n\n"
               f"📋 리포트 보기: {os.environ.get('WEB_URL', '')}/reports/{report_id}")
        _slack.open(Request(slack_url, data=json.dumps({"text": msg}).encode(),
                            headers={"Content-Type": "application/json"}), timeout=5).close()
    except Exception:
        logger.warning("Slack notification failed")


# ========================================================================
# Entrypoint
# ========================================================================

async def invocations(request: HttpRequest):
    payload = await request.json()
    prompt = payload.get("prompt", "")
    agent_name = payload.get("agent", "chatbot")
    session_id = payload.get("session_id", "")
    if payload.get("action") == "prewarm":
        await _prepare()
        if agent_name == "chatbot":
            await agents.prewarm_chat(session_id)
        return JSONResponse({"action": "prewarm", "status": "ready"})
    if not prompt:
        return JSONResponse(json.dumps({"error": "No prompt provided"}))
    if agent_name == "rca":
        task = _start(_rca_background(prompt, payload))
        return JSONResponse(json.dumps({"status": "ANALYZING", "report_id": payload.get("report_id", ""),
                                        "task_id": str(id(task))}))
    await _prepare()
    return JSONResponse(await agents.run_chat(session_id, prompt))


async def internal_rca(request: HttpRequest):
    token = request.headers.get("authorization", "")
    if request.client is None or request.client.host != "127.0.0.1" or not hmac.compare_digest(
            token, "Bearer " + kiro_runtime.INTERNAL_TOKEN):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    task = _start(agents.run_rca(body.get("query", "")))
    try:
        return JSONResponse(await task)
    except Exception as e:
        return JSONResponse(f"RCA agent error: {e}")


async def ping(request: HttpRequest):
    status = _status()
    return JSONResponse({"status": status["status"], "time_of_last_update": status["time"]})


app = Starlette(routes=[Route("/invocations", invocations, methods=["POST"]), Route("/ping", ping, methods=["GET"]),
                        Route("/internal/rca", internal_rca, methods=["POST"])])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")
