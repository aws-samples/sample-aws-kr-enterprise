"""WebSocket Chat Handler - no timeout limit.

$connect needs a valid web token in the token query parameter, and so does every sendMessage body: API Gateway only
authenticates a WebSocket when it connects, so an expired or revoked token must also stop an open connection.
"""
import json
import os
import uuid
import logging

import boto3

import web_auth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")
MAX_MESSAGE_LENGTH = 4000


def handler(event, context):
    route = event.get("requestContext", {}).get("routeKey", "")
    connection_id = event.get("requestContext", {}).get("connectionId", "")

    if route == "$connect":
        token = (event.get("queryStringParameters") or {}).get("token")
        try:
            return {"statusCode": 200 if web_auth.verify(token) else 401}
        except web_auth.NotConfigured:
            logger.exception("web password not configured")
            return {"statusCode": 503}
    elif route == "$disconnect":
        return {"statusCode": 200}
    elif route == "sendMessage":
        return _handle_message(event, connection_id)
    else:
        return {"statusCode": 400}


def _handle_message(event, connection_id):
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    message = body.get("message", "")
    session_id = body.get("session_id", "")
    if not isinstance(session_id, str) or len(session_id) < 33 or len(session_id) > 100:
        session_id = str(uuid.uuid4())

    apigw = boto3.client("apigatewaymanagementapi", endpoint_url=WS_ENDPOINT)

    def send(event_type, data):
        try:
            apigw.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps({"type": event_type, "data": data}).encode(),
            )
        except Exception as e:
            logger.warning(f"Send failed: {e}")

    try:
        authorized = web_auth.verify(body.get("token"))
    except web_auth.NotConfigured:
        logger.exception("web password not configured")
        authorized = False
    if not authorized:
        send("unauthorized", "로그인이 만료되었습니다. 다시 로그인하세요.")
        try:
            apigw.delete_connection(ConnectionId=connection_id)
        except Exception:
            logger.warning("could not close connection")
        return {"statusCode": 200}

    if not message or not isinstance(message, str):
        send("error", "message required")
        return {"statusCode": 200}
    if len(message) > MAX_MESSAGE_LENGTH:
        send("error", f"메시지는 {MAX_MESSAGE_LENGTH}자 이하로 입력하세요.")
        return {"statusCode": 200}

    send("status", "🔍 에이전트 분석 시작...")

    if not AGENT_RUNTIME_ARN:
        send("response", "AgentCore not configured.")
        send("done", json.dumps({"session_id": session_id}))
        return {"statusCode": 200}

    # Retry on ConcurrencyException
    import time
    for attempt in range(3):
        try:
            send("status", "🤖 AI 분석 중..." if attempt == 0 else f"⏳ 재시도 {attempt}...")

            from botocore.config import Config
            client = boto3.client("bedrock-agentcore", config=Config(read_timeout=840))
            # A chat that requests an RCA can run for several minutes; keep the WebSocket from idling out.
            import threading
            finished = threading.Event()
            def heartbeat():
                started = time.monotonic()
                while not finished.wait(60):
                    send("status", f"🤖 AI 분석 중... ({int((time.monotonic() - started) // 60)}분 경과)")
            threading.Thread(target=heartbeat, daemon=True).start()
            try:
                resp = client.invoke_agent_runtime(
                    agentRuntimeArn=AGENT_RUNTIME_ARN,
                    runtimeSessionId=session_id,
                    payload=json.dumps({"prompt": message, "agent": "chatbot", "session_id": session_id}).encode(),
                )
            finally:
                finished.set()

            response_body = resp.get("response")
            if response_body and hasattr(response_body, "read"):
                text = response_body.read().decode("utf-8")
            else:
                text = str(resp.get("payload", ""))

            # Unwrap double-encoded JSON
            try:
                unwrapped = json.loads(text)
                if isinstance(unwrapped, str):
                    text = unwrapped
            except (json.JSONDecodeError, TypeError):
                pass

            send("response", text)
            send("done", json.dumps({"session_id": session_id}))
            return {"statusCode": 200}

        except Exception as e:
            err_str = str(e)
            if "ConcurrencyException" in err_str or "RuntimeClientError" in err_str or "500" in err_str:
                if attempt < 2:
                    send("status", "⏳ 이전 요청 처리 중, 잠시 후 재시도합니다...")
                    time.sleep(35)
                    continue
            logger.exception("Chat error")
            send("error", "에이전트 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
            return {"statusCode": 200}

    send("error", "에이전트가 응답하지 않습니다. 잠시 후 다시 시도해 주세요.")
    return {"statusCode": 200}
