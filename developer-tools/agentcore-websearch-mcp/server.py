#!/usr/bin/env python3
"""
AgentCore Web Search -> local stdio MCP proxy.

Bridges the MCP stdio transport (spoken by MCP clients such as Claude Code) to an
Amazon Bedrock AgentCore Gateway's Streamable HTTP MCP endpoint, signing every
HTTP request with SigV4 using the standard AWS credential chain.

  MCP client  --stdio JSON-RPC-->  this proxy  --SigV4 HTTPS-->  AgentCore Gateway (web-search)

Configuration (environment variables):
  AGENTCORE_GATEWAY_URL     Gateway MCP URL (required; output by the CloudFormation stack)
  AGENTCORE_SIGNING_REGION  Override the SigV4 signing region (optional)

AWS credentials are resolved via the standard boto/botocore chain -- environment
variables, AWS_PROFILE, shared config/credentials files, SSO, and instance/container
roles all work with no code changes. There is no baked-in profile.

No third-party HTTP library required: botocore (for credentials + SigV4) + stdlib urllib.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error

from botocore.session import Session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

GATEWAY_URL = os.environ.get("AGENTCORE_GATEWAY_URL", "").strip()
SERVICE = "bedrock-agentcore"  # SigV4 signing name for the AgentCore data plane


def _log(msg):
    sys.stderr.write(f"[agentcore-websearch] {msg}\n")
    sys.stderr.flush()


def _resolve_region():
    """SigV4 must sign for the endpoint's own region. Derive it from the gateway
    URL so a stray AWS_REGION in the shell (e.g. us-west-2) can't break signing.
    AGENTCORE_SIGNING_REGION can still override explicitly if ever needed."""
    explicit = os.environ.get("AGENTCORE_SIGNING_REGION")
    if explicit:
        return explicit
    m = re.search(r"\.([a-z]{2}-[a-z]+-\d)\.amazonaws\.com", GATEWAY_URL)
    if m:
        return m.group(1)
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )


if not GATEWAY_URL:
    _log(
        "AGENTCORE_GATEWAY_URL is not set. Deploy the CloudFormation stack "
        "(cloudformation/agentcore-websearch-gateway.yaml) and set AGENTCORE_GATEWAY_URL "
        "to its GatewayMcpUrl output."
    )
    sys.exit(1)

REGION = _resolve_region()

# Standard AWS credential chain: env vars, AWS_PROFILE, shared config/credentials,
# SSO, and instance/container roles all resolve here with no proxy-specific config.
_session = Session()
_credentials = _session.get_credentials()
if _credentials is None:
    _log(
        "No AWS credentials found. Configure credentials via the standard AWS "
        "credential chain (e.g. `aws configure`, AWS_PROFILE, or an assumed role)."
    )
    sys.exit(1)

# Streamable HTTP session id, returned by the server on 'initialize' and echoed back.
_mcp_session_id = None

# The upstream gateway returns the web-search tool with no top-level "description",
# so the MCP client has no signal for *when* to reach for it. Inject one for any
# tool whose name looks like the web-search tool, without clobbering an existing one.
_WEB_SEARCH_DESCRIPTION = (
    "Search the web for current, real-time, or post-training-cutoff information. "
    "Use this when you need facts that may have changed, recent events, the latest "
    "documentation or software versions, or anything you're not confident about from "
    "memory. Returns ranked web search results for a query."
)


def _looks_like_web_search(name):
    """True for tool names like 'WebSearch', 'web-search-tool', 'web_search'."""
    normalized = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return "websearch" in normalized


def _inject_tool_descriptions(payload):
    """Add a description to web-search tools in a tools/list result, in place."""
    try:
        tools = payload["result"]["tools"]
    except (KeyError, TypeError):
        return
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("description"):
            continue
        if _looks_like_web_search(tool.get("name", "")):
            tool["description"] = _WEB_SEARCH_DESCRIPTION


def _sign_and_send(body_bytes):
    """POST body to the gateway with a fresh SigV4 signature. Returns (status, content_type, text)."""
    global _mcp_session_id
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _mcp_session_id:
        headers["Mcp-Session-Id"] = _mcp_session_id

    aws_req = AWSRequest(method="POST", url=GATEWAY_URL, data=body_bytes, headers=headers)
    # Credentials may be refreshable; SigV4Auth re-freezes them on each add_auth call.
    SigV4Auth(_credentials, SERVICE, REGION).add_auth(aws_req)
    prepared = aws_req.prepare()

    req = urllib.request.Request(GATEWAY_URL, data=body_bytes, method="POST")
    for k, v in prepared.headers.items():
        req.add_header(k, v)

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            _mcp_session_id = sid
        return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, "", f"__TRANSPORT_ERROR__:{type(e).__name__}:{e}"


def _extract_jsonrpc(text, content_type):
    """Pull the JSON-RPC response object out of a plain-JSON or SSE response body."""
    text = (text or "").strip()
    if not text:
        return None
    if "text/event-stream" in content_type:
        result = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("result" in obj or "error" in obj or "jsonrpc" in obj):
                result = obj
        return result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    _log(f"starting | gateway={GATEWAY_URL} | region={REGION}")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _log(f"skipping non-JSON stdin line: {line[:200]}")
            continue

        msg_id = message.get("id")
        status, content_type, text = _sign_and_send(line.encode("utf-8"))

        # Notifications (no id) expect no response body.
        if msg_id is None:
            if status and status >= 400:
                _log(f"notification returned HTTP {status}: {text[:300]}")
            continue

        payload = _extract_jsonrpc(text, content_type)
        if payload is not None:
            if message.get("method") == "tools/list":
                _inject_tool_descriptions(payload)
            _emit(payload)
            continue

        # Could not parse a valid JSON-RPC response: surface an error to the client.
        _log(f"HTTP {status} unparseable response: {text[:500]}")
        _emit({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32603,
                "message": f"Gateway proxy error (HTTP {status})",
                "data": text[:500],
            },
        })


if __name__ == "__main__":
    main()
