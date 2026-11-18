import json
import os
import re
import time
import uuid
import logging
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

import boto3
from botocore.config import Config

import web_auth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ddb = boto3.resource("dynamodb")
reports_table = ddb.Table(os.environ["REPORTS_TABLE"])
workloads_table = ddb.Table(os.environ["WORKLOADS_TABLE"])
# Regional endpoint: presigned URLs on the global endpoint are redirected for new buckets and the
# redirected request fails its signature check.
s3_client = boto3.client("s3", endpoint_url=f"https://s3.{os.environ['AWS_REGION']}.amazonaws.com",
                         config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
cw_client = boto3.client("cloudwatch")
codecommit = boto3.client("codecommit")
codepipeline = boto3.client("codepipeline")

KNOWLEDGE_BUCKET = os.environ.get("KNOWLEDGE_BUCKET", "")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
REPOSITORY = os.environ.get("WORKLOAD_REPOSITORY", "incident-to-pr-workload")
PIPELINE = os.environ.get("WORKLOAD_PIPELINE", "incident-to-pr-workload")

# Knowledge documents: same key rules as the agent runtime (src/agents/app/main.py).
WORKLOAD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
FILE_NAME = re.compile(r"\w[\w .-]{0,99}")
TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".log")
MAX_DOCUMENT_BYTES = 1_000_000
MAX_DOCUMENTS_PER_WORKLOAD = 20
UPLOAD_TICKET_SECONDS = 300
MAX_DOCUMENTS = 200
MAX_MESSAGE_LENGTH = 4000
SLACK_WEBHOOK_PATH = re.compile(r"/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+")


class BadRequest(Exception):
    pass


def handler(event, context):
    method = event.get("httpMethod", "GET")
    path = event.get("path", "")
    params = event.get("pathParameters", {}) or {}

    try:
        if path == "/api/login" and method == "POST":
            return _login(event)
        if not web_auth.verify(web_auth.bearer_token(event.get("headers"))):
            return _resp(401, {"error": "unauthorized"})

        wid = params.get("workload_id")
        if wid is not None and not WORKLOAD_ID.fullmatch(wid):
            return _resp(400, {"error": "Invalid workload ID"})

        # Workloads
        if path == "/api/workloads" and method == "GET":
            return _list_workloads()
        if path == "/api/workloads" and method == "POST":
            return _create_workload(event)
        if "/api/workloads/" in path and "/upload-url" in path and method == "POST":
            return _get_upload_url(wid, event)
        if "/api/workloads/" in path and "/documents" in path and method == "GET":
            return _list_documents(wid)
        if "/api/workloads/" in path and "/documents" in path and method == "DELETE":
            return _delete_document(wid, event)
        if "/api/workloads/" in path and "/sync" in path and method == "POST":
            return _sync_kb()
        if "/api/workloads/" in path and method == "GET":
            return _get_workload(wid)
        if "/api/workloads/" in path and method == "PUT":
            return _update_workload(wid, event)
        if "/api/workloads/" in path and method == "DELETE":
            return _delete_workload(wid)

        # Reports
        if path == "/api/reports" and method == "GET":
            return _list_reports()
        if "/api/reports/" in path and method == "DELETE":
            return _delete_report(params.get("id", ""))
        if "/api/reports/" in path and method == "GET":
            return _get_report(params.get("id", ""))

        # Status & Chat
        if path == "/api/status" and method == "GET":
            return _get_status()
        if path == "/api/chat" and method == "POST":
            return _chat(event)

        return _resp(404, {"error": "Not found"})
    except BadRequest as e:
        return _resp(400, {"error": str(e)})
    except web_auth.NotConfigured:
        logger.exception("web password not configured")
        return _resp(503, {"error": "웹 패스워드 시크릿이 설정되지 않았습니다. README의 배포 준비 단계를 확인하세요."})
    except Exception:
        logger.exception("API error")
        return _resp(500, {"error": "Internal error"})


def _body(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        raise BadRequest("Body must be JSON") from None
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object")
    return body


def _login(event):
    session = web_auth.login(_body(event).get("password"))
    return _resp(200, session) if session else _resp(401, {"error": "패스워드가 올바르지 않습니다."})


# ========== Workloads ==========

def valid_slack_webhook(url):
    """An https://hooks.slack.com/services/... incoming webhook URL only (same rule as the agent runtime)."""
    if not isinstance(url, str) or len(url) > 300:
        return False
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.netloc in ("hooks.slack.com", "hooks.slack.com:443")
            and not parts.query and not parts.fragment and bool(SLACK_WEBHOOK_PATH.fullmatch(parts.path)))


def _public_workload(item):
    """The webhook URL is a credential: responses only say whether one is set. Upload bookkeeping stays internal."""
    notification = item.get("notification") or {}
    public = {k: v for k, v in item.items() if k not in ("upload_reservations", "upload_version")}
    return {**public, "notification": {"slack_webhook_configured": bool(notification.get("slack_webhook"))}}


def _text(body, key, limit, default=""):
    value = body.get(key, default)
    if not isinstance(value, str) or len(value) > limit:
        raise BadRequest(f"{key} must be text of at most {limit} characters")
    return value


def _alarm_prefixes(value):
    if not isinstance(value, list) or len(value) > 10 or not all(
            isinstance(p, str) and 3 <= len(p.strip()) <= 100 for p in value):
        raise BadRequest("alarm_prefixes must list at most 10 prefixes of 3 to 100 characters")
    return [p.strip() for p in value]


def _resources(value):
    if not isinstance(value, dict) or len(json.dumps(value)) > 4000:
        raise BadRequest("resources must be an object of at most 4000 characters")
    return value


def _notification(value):
    """{"slack_webhook": url} sets the webhook, {"slack_webhook": null} removes it."""
    if not isinstance(value, dict) or set(value) - {"slack_webhook"}:
        raise BadRequest("notification may only contain slack_webhook")
    url = value.get("slack_webhook")
    if url is None:
        return {}
    if not valid_slack_webhook(url):
        raise BadRequest("slack_webhook must be a https://hooks.slack.com/services/... URL")
    return {"slack_webhook": url}


def _list_workloads():
    items = workloads_table.scan().get("Items", [])
    return _resp(200, _dec([_public_workload(i) for i in items]))


def _get_workload(wid):
    item = workloads_table.get_item(Key={"workload_id": wid}).get("Item")
    return _resp(200, _dec(_public_workload(item))) if item else _resp(404, {"error": "Not found"})


def _create_workload(event):
    body = _body(event)
    wid = body.get("workload_id") or str(uuid.uuid4())[:8]
    if not isinstance(wid, str) or not WORKLOAD_ID.fullmatch(wid):
        raise BadRequest("workload_id may only use letters, digits, - and _ (at most 64)")
    item = {
        "workload_id": wid,
        "name": _text(body, "name", 100, wid) or wid,
        "description": _text(body, "description", 2000),
        "alarm_prefixes": _alarm_prefixes(body.get("alarm_prefixes", [])),
        "notification": _notification(body.get("notification", {})),
        "resources": _resources(body.get("resources", {})),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        workloads_table.put_item(Item=item, ConditionExpression="attribute_not_exists(workload_id)")
    except workloads_table.meta.client.exceptions.ConditionalCheckFailedException:
        return _resp(409, {"error": f"Workload {wid} already exists"})
    return _resp(201, _dec(_public_workload(item)))


def _update_workload(wid, event):
    body = _body(event)
    fields = {}
    if "name" in body:
        fields["name"] = _text(body, "name", 100)
    if "description" in body:
        fields["description"] = _text(body, "description", 2000)
    if "alarm_prefixes" in body:
        fields["alarm_prefixes"] = _alarm_prefixes(body["alarm_prefixes"])
    if "resources" in body:
        fields["resources"] = _resources(body["resources"])
    if "notification" in body:
        # A notification without slack_webhook keeps the stored webhook.
        if not isinstance(body["notification"], dict):
            raise BadRequest("notification must be an object")
        if "slack_webhook" in body["notification"]:
            fields["notification"] = _notification(body["notification"])
    if not fields:
        return _resp(400, {"error": "No fields to update"})
    names = {f"#f{i}": key for i, key in enumerate(fields)}
    values = {f":f{i}": value for i, value in enumerate(fields.values())}
    try:
        workloads_table.update_item(
            Key={"workload_id": wid},
            UpdateExpression="SET " + ", ".join(f"#f{i} = :f{i}" for i in range(len(fields))),
            ConditionExpression="attribute_exists(workload_id)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except workloads_table.meta.client.exceptions.ConditionalCheckFailedException:
        return _resp(404, {"error": "Not found"})
    return _resp(200, {"status": "updated"})


def _delete_workload(wid):
    workloads_table.delete_item(Key={"workload_id": wid})
    return _resp(200, {"status": "deleted"})


def valid_document_key(key):
    parts = key.split("/") if isinstance(key, str) else []
    if len(parts) == 2 and parts[0] in ("runbooks", "system-specs"):
        name = parts[1]
    elif len(parts) == 3 and parts[0] == "workloads" and WORKLOAD_ID.fullmatch(parts[1]):
        name = parts[2]
    else:
        return False
    return _valid_file_name(name)


def _valid_file_name(name):
    return (isinstance(name, str) and bool(FILE_NAME.fullmatch(name)) and ".." not in name
            and name.lower().endswith(TEXT_SUFFIXES))


def _workload_documents(wid):
    prefix = f"workloads/{wid}/"
    resp = s3_client.list_objects_v2(Bucket=KNOWLEDGE_BUCKET, Prefix=prefix)
    return [obj for obj in resp.get("Contents", []) if obj["Key"] != prefix]


def _get_upload_url(wid, event):
    body = _body(event)
    filename = body.get("filename", "")
    if not _valid_file_name(filename):
        raise BadRequest(f"filename must be a plain file name ending in {', '.join(TEXT_SUFFIXES)}")
    if not workloads_table.get_item(Key={"workload_id": wid}).get("Item"):
        return _resp(404, {"error": "Not found"})
    key = f"workloads/{wid}/{filename}"
    _reserve_upload(wid, key)
    # A presigned POST lets S3 itself enforce the size limit for this one key.
    post = s3_client.generate_presigned_post(
        Bucket=KNOWLEDGE_BUCKET, Key=key, Conditions=[["content-length-range", 1, MAX_DOCUMENT_BYTES]],
        ExpiresIn=UPLOAD_TICKET_SECONDS)
    return _resp(200, {"upload": post, "s3_key": key, "max_bytes": MAX_DOCUMENT_BYTES})


def _reserve_upload(wid, key):
    """Count stored documents and unexpired upload tickets together, so the per-workload limit holds before uploads land.

    Each ticket is recorded on the workload item until it expires; a version check makes concurrent requests retry.
    Replacing an existing document or reissuing a ticket for the same key does not count twice.
    """
    for _ in range(5):
        item = workloads_table.get_item(Key={"workload_id": wid}, ConsistentRead=True).get("Item")
        if not item:
            raise BadRequest("workload not found")
        now = int(time.time())
        tickets = {k: int(v) for k, v in (item.get("upload_reservations") or {}).items() if int(v) > now}
        held = {obj["Key"] for obj in _workload_documents(wid)} | set(tickets)
        if key not in held and len(held) >= MAX_DOCUMENTS_PER_WORKLOAD:
            raise BadRequest(f"a workload can hold at most {MAX_DOCUMENTS_PER_WORKLOAD} documents, "
                             "including uploads in progress")
        tickets[key] = now + UPLOAD_TICKET_SECONDS + 60
        version = int(item.get("upload_version", 0))
        try:
            workloads_table.update_item(
                Key={"workload_id": wid},
                UpdateExpression="SET upload_reservations = :t, upload_version = :next",
                ConditionExpression="attribute_exists(workload_id) AND "
                                    "(attribute_not_exists(upload_version) OR upload_version = :v)",
                ExpressionAttributeValues={":t": tickets, ":next": version + 1, ":v": version})
            return
        except workloads_table.meta.client.exceptions.ConditionalCheckFailedException:
            continue
    raise BadRequest("too many concurrent uploads; try again")


def _list_documents(wid):
    files = [{"key": obj["Key"], "name": obj["Key"].split("/")[-1],
              "size": obj["Size"], "last_modified": obj["LastModified"].isoformat()}
             for obj in _workload_documents(wid)]
    return _resp(200, {"documents": files})


def _delete_document(wid, event):
    key = _body(event).get("key", "")
    if not isinstance(key, str) or not key.startswith(f"workloads/{wid}/") or not valid_document_key(key):
        return _resp(400, {"error": "Invalid key"})
    s3_client.delete_object(Bucket=KNOWLEDGE_BUCKET, Key=key)
    return _resp(200, {"deleted": key})


def _sync_kb():
    """Publish the current knowledge documents for the agents (no ingestion job or vector store).

    The AgentCore Runtime downloads the documents listed in the manifest before each analysis or chat. Keys that do
    not follow the document rules and oversized objects are left out and counted as failed.
    """
    if not KNOWLEDGE_BUCKET:
        return _resp(400, {"error": "Knowledge bucket not configured"})
    try:
        previous = {d["key"]: d["etag"] for d in json.loads(
            s3_client.get_object(Bucket=KNOWLEDGE_BUCKET, Key="_sync/manifest.json")["Body"].read())["documents"]}
    except s3_client.exceptions.NoSuchKey:
        previous = {}
    documents, failed = [], 0
    for page in s3_client.get_paginator("list_objects_v2").paginate(Bucket=KNOWLEDGE_BUCKET):
        for obj in page.get("Contents", []):
            if obj["Key"].startswith("_sync/") or obj["Key"].endswith("/"):
                continue
            if not valid_document_key(obj["Key"]) or obj["Size"] > MAX_DOCUMENT_BYTES or len(documents) >= MAX_DOCUMENTS:
                failed += 1
                continue
            documents.append({"key": obj["Key"], "etag": obj["ETag"], "size": obj["Size"]})
    job_id = str(uuid.uuid4())
    s3_client.put_object(Bucket=KNOWLEDGE_BUCKET, Key="_sync/manifest.json", ContentType="application/json",
                         Body=json.dumps({"job_id": job_id, "synced_at": datetime.now(timezone.utc).isoformat(),
                                          "documents": documents}).encode())
    new = sum(1 for d in documents if d["key"] not in previous)
    modified = sum(1 for d in documents if d["key"] in previous and previous[d["key"]] != d["etag"])
    return _resp(200, {"status": "COMPLETE", "job_id": job_id,
                       "statistics": {"numberOfDocumentsScanned": len(documents) + failed,
                                      "numberOfNewDocumentsIndexed": new,
                                      "numberOfModifiedDocumentsIndexed": modified,
                                      "numberOfDocumentsFailed": failed}})


# ========== Reports ==========
# A reviewed report gets a fix pull request to the workload repository. Its state decides the report status:
# open -> PENDING, merged -> MERGED, deployed by a succeeded pipeline run -> RESOLVED, closed unmerged -> REPORTED.

ANCESTRY_LIMIT = 50


def _includes(revision, commit_id):
    """Whether revision is commit_id or a later commit built on it (CodeCommit has no ancestry API; walks parents)."""
    pending, seen = [revision], set()
    while pending and len(seen) < ANCESTRY_LIMIT:
        current = pending.pop()
        if current == commit_id:
            return True
        if not current or current in seen:
            continue
        seen.add(current)
        pending.extend(codecommit.get_commit(repositoryName=REPOSITORY, commitId=current)["commit"].get("parents") or [])
    return False


def deployment(pull_request):
    """(state, run): OPEN, CLOSED, MERGED, or DEPLOYED with the succeeded pipeline run whose source includes the merge.

    A run counts only if the commit it built is the merge commit or a descendant of it; ancestry is only walked for
    runs that started after the merge commit was made. A later rollback does not change a DEPLOYED result.
    """
    pr = codecommit.get_pull_request(pullRequestId=str(pull_request["pull_request_id"]))["pullRequest"]
    target = (pr.get("pullRequestTargets") or [{}])[0]
    merge = target.get("mergeMetadata") or {}
    if not merge.get("isMerged"):
        return ("OPEN" if pr.get("pullRequestStatus") == "OPEN" else "CLOSED"), None
    merged = merge.get("mergeCommitId") or target.get("sourceCommit")
    merged_at = int(codecommit.get_commit(repositoryName=REPOSITORY, commitId=merged)["commit"]["committer"]["date"].split()[0])
    runs = codepipeline.list_pipeline_executions(pipelineName=PIPELINE, maxResults=20)
    for run in runs.get("pipelineExecutionSummaries", []):
        if run.get("status") != "Succeeded":
            continue
        recent = run["startTime"].timestamp() >= merged_at - 300
        for source in run.get("sourceRevisions", []):
            revision = source.get("revisionId")
            if revision == merged or (revision and recent and _includes(revision, merged)):
                return "DEPLOYED", {"execution_id": run.get("pipelineExecutionId", ""), "revision": revision}
    return "MERGED", None


def pull_request_state(pull_request):
    """OPEN, CLOSED, MERGED, DEPLOYED, or UNKNOWN when CodeCommit or CodePipeline cannot be read (same rule as the agents)."""
    return pull_request_deployment(pull_request)[0]


def pull_request_deployment(pull_request):
    try:
        return deployment(pull_request)
    except Exception:
        logger.exception("pull request state unavailable")
        return "UNKNOWN", None


def pull_request_url(pull_request_id):
    return (f"https://{REGION}.console.aws.amazon.com/codesuite/codecommit/repositories/{REPOSITORY}"
            f"/pull-requests/{pull_request_id}/details?region={REGION}")


def with_fix_state(item):
    """The report with its pull request's current state and status. A deployed fix is stored as RESOLVED."""
    data = item.get("report_data", {})
    pull_request = data.get("pull_request") or {}
    status = item.get("status")
    if not pull_request.get("pull_request_id"):
        return item
    pull_request = {**pull_request, "url": pull_request_url(pull_request["pull_request_id"])}
    if status in ("PENDING", "MERGED"):
        state, run = pull_request_deployment(pull_request)
        pull_request["state"] = state
        status = {"DEPLOYED": "RESOLVED", "MERGED": "MERGED", "CLOSED": "REPORTED"}.get(state, "PENDING")
        if status == "RESOLVED":
            pull_request["deployed_by"] = run
            reports_table.update_item(Key={"report_id": item["report_id"]},
                                      UpdateExpression="SET #s = :s, report_data.pull_request.#st = :d, "
                                                       "report_data.pull_request.deployed_by = :r",
                                      ExpressionAttributeNames={"#s": "status", "#st": "state"},
                                      ExpressionAttributeValues={":s": "RESOLVED", ":d": "DEPLOYED", ":r": run})
    return {**item, "status": status, "report_data": {**data, "pull_request": pull_request}}


def _list_reports():
    items = sorted(reports_table.scan().get("Items", []), key=lambda x: x.get("created_at", ""), reverse=True)
    return _resp(200, _dec([with_fix_state(i) for i in items[:50]]))


def _delete_report(rid):
    reports_table.delete_item(Key={"report_id": rid})
    return _resp(200, {"deleted": rid})


def _get_report(rid):
    item = reports_table.get_item(Key={"report_id": rid}).get("Item")
    return _resp(200, _dec(with_fix_state(item))) if item else _resp(404, {"error": "Not found"})


# ========== Status & Chat ==========

def _get_status():
    alarms = cw_client.describe_alarms(MaxRecords=100)
    alarm_list = [{"name": a["AlarmName"], "state": a["StateValue"], "reason": a.get("StateReason", "")}
                  for a in alarms.get("MetricAlarms", [])]
    ok = sum(1 for a in alarm_list if a["state"] == "OK")
    alarm_count = sum(1 for a in alarm_list if a["state"] == "ALARM")
    workloads = workloads_table.scan().get("Items", [])
    return _resp(200, {
        "alarms": alarm_list,
        "summary": {"ok": ok, "alarm": alarm_count, "total": len(alarm_list)},
        "workload_count": len(workloads),
        "ws_url": os.environ.get("WS_URL", ""),
    })


def _chat(event):
    body = _body(event)
    msg = body.get("message", "")
    if not msg or not isinstance(msg, str):
        return _resp(400, {"error": "message required"})
    if len(msg) > MAX_MESSAGE_LENGTH:
        return _resp(400, {"error": f"message must be at most {MAX_MESSAGE_LENGTH} characters"})
    if not AGENT_RUNTIME_ARN:
        return _resp(200, {"response": "AgentCore not configured."})

    client = boto3.client("bedrock-agentcore", config=Config(read_timeout=540))
    sid = body.get("session_id", "")
    if not isinstance(sid, str) or len(sid) < 33 or len(sid) > 100:
        sid = str(uuid.uuid4())

    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN, runtimeSessionId=sid,
            payload=json.dumps({"prompt": msg, "agent": "chatbot", "session_id": sid}).encode(),
        )
        logger.info(f"AgentCore response keys: {resp.keys()}")

        # Parse response - AgentCore returns 'response' key with StreamingBody
        response_body = resp.get("response")
        if response_body:
            if hasattr(response_body, 'read'):
                text = response_body.read().decode("utf-8")
            else:
                text = str(response_body)
        else:
            # Fallback to 'payload' key
            payload = resp.get("payload")
            if payload:
                if isinstance(payload, bytes):
                    text = payload.decode("utf-8")
                else:
                    text = str(payload)
            else:
                text = ""

        logger.info(f"Parsed text length: {len(text)}")

        # AgentCore double-encodes: unwrap if needed
        try:
            unwrapped = json.loads(text)
            if isinstance(unwrapped, str):
                text = unwrapped
        except (json.JSONDecodeError, TypeError):
            pass

        return _resp(200, {"response": text, "session_id": sid})
    except Exception:
        logger.exception("Chat error")
        return _resp(500, {"error": "에이전트 처리 중 오류가 발생했습니다."})


# ========== Helpers ==========

def _dec(obj):
    if isinstance(obj, list): return [_dec(i) for i in obj]
    if isinstance(obj, dict): return {k: _dec(v) for k, v in obj.items()}
    if isinstance(obj, Decimal): return int(obj) if obj == int(obj) else float(obj)
    return obj


def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*",
                     "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
                     "Access-Control-Allow-Headers": "Content-Type,Authorization"},
        "body": json.dumps(body, default=str),
    }
