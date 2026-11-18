"""Invoke an AgentCore Runtime using AWS credentials and a reusable session ID."""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--runtime-arn", required=True)
    parser.add_argument("--endpoint-url", help="Trusted HTTPS AgentCore endpoint URL, such as a PrivateLink DNS name")
    parser.add_argument("--session-id", default=None,
                        help="Reuse an earlier ID to continue its conversation")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--events", action="store_true", help="Request SDK events using SSE")
    parser.add_argument("--prompt-file", type=Path, help="UTF-8 JSON file containing prompt content blocks")
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--interrupt", action="store_true", help="Interrupt the active turn")
    controls.add_argument("--steer", metavar="MESSAGE", help="Send steering to an active V3 turn")
    controls.add_argument("--cancel-mcp-tools", metavar="SERVER", help="Request cancellation of a server's active MCP tools")
    controls.add_argument("--reconnect-mcp", metavar="SERVER", help="Reconnect an SDK-managed remote MCP server")
    controls.add_argument("--reauthorize-mcp", metavar="SERVER", help="Clear local OAuth tokens and reconnect a remote MCP server")
    controls.add_argument("--revoke-mcp-oauth", metavar="SERVER", help="Request server-side OAuth token revocation")
    controls.add_argument("--prewarm", action="store_true", help="Start the session's Kiro process without a prompt")
    controls.add_argument("--code-status", action="store_true", help="Get code intelligence status")
    controls.add_argument("--code-init", action="store_true", help="Initialize workspace code intelligence")
    controls.add_argument("--code-overview", action="store_true", help="Get a workspace code overview")
    controls.add_argument("--compact", action="store_true", help="Compact an idle authorized V3 session")
    controls.add_argument("--session-messages", action="store_true", help="Read the authorized session mirror")
    parser.add_argument("--history-limit", type=int, default=None, help="Messages per page (1-50)")
    parser.add_argument("--history-offset", type=int, default=None, help="Message offset (0-20000)")
    parser.add_argument("--history-coalesce", action="store_true", help="Combine adjacent matching text chunks")
    parser.add_argument("--history-exclude-subagents", action="store_true", help="Exclude identified child chunks")
    parser.add_argument("--history-original-strings", action="store_true", help="Restore verified original strings")
    parser.add_argument("--history-tools", action="store_true", help="Include observed tool input and result messages")
    parser.add_argument("prompt", nargs="?")
    args = parser.parse_args()
    if args.endpoint_url:
        try:
            endpoint = urlsplit(args.endpoint_url)
            valid_endpoint = (endpoint.scheme == "https" and endpoint.hostname and endpoint.port in (None, 443)
                              and endpoint.username is None and endpoint.password is None
                              and endpoint.path in ("", "/") and not endpoint.query and not endpoint.fragment)
        except ValueError:
            valid_endpoint = False
        if not valid_endpoint:
            parser.error("--endpoint-url must be a trusted HTTPS endpoint without a path or credentials")
    supplied = sum((args.prompt is not None, args.prompt_file is not None,
                    args.interrupt, args.steer is not None, args.cancel_mcp_tools is not None,
                    args.reconnect_mcp is not None, args.reauthorize_mcp is not None,
                    args.revoke_mcp_oauth is not None, args.prewarm,
                    args.code_status, args.code_init, args.code_overview,
                    args.compact, args.session_messages))
    if supplied != 1:
        parser.error("provide one prompt, prompt file, or control request")
    control = (args.interrupt or args.steer is not None or args.cancel_mcp_tools is not None or
               args.reconnect_mcp is not None or args.reauthorize_mcp is not None or
               args.revoke_mcp_oauth is not None or args.prewarm or
               args.code_status or args.code_init or args.code_overview or
               args.compact or args.session_messages)
    if control and (args.session_id is None or args.stream or args.events):
        parser.error("control requests require --session-id and do not support streaming")
    history_options = (args.history_limit is not None or args.history_offset is not None or
                       args.history_coalesce or args.history_exclude_subagents or
                       args.history_original_strings or args.history_tools)
    if history_options and not args.session_messages:
        parser.error("history options require --session-messages")
    if ((args.history_limit is not None and not 1 <= args.history_limit <= 50) or
            (args.history_offset is not None and not 0 <= args.history_offset <= 20000)):
        parser.error("history limit must be 1-50 and offset must be 0-20000")
    if args.steer is not None and (not args.steer.strip() or "\0" in args.steer):
        parser.error("--steer requires a nonempty message")
    server_name = next((name for name in (args.cancel_mcp_tools, args.reconnect_mcp, args.reauthorize_mcp,
                                         args.revoke_mcp_oauth)
                        if name is not None), None)
    if server_name is not None:
        try:
            valid_name = (bool(server_name.strip()) and "\0" not in server_name and
                          len(server_name.encode("utf-8")) <= 256)
        except UnicodeEncodeError:
            valid_name = False
        if not valid_name:
            parser.error("MCP control requires a nonempty server name of at most 256 UTF-8 bytes")
    if args.prompt_file is not None:
        try:
            prompt = json.loads(args.prompt_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(f"cannot read prompt JSON: {type(exc).__name__}")
        if not isinstance(prompt, list):
            parser.error("--prompt-file must contain a JSON array of content blocks")
    else:
        prompt = args.prompt
    session_id = args.session_id or str(uuid.uuid4())
    stream = args.stream or args.events
    if not 33 <= len(session_id) <= 256 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", session_id):
        parser.error("AgentCore session IDs must be 33-256 letters, digits, hyphens or underscores")

    import boto3
    from botocore.config import Config

    client = boto3.Session(region_name=args.region).client(
        "bedrock-agentcore", endpoint_url=args.endpoint_url,
        config=Config(read_timeout=600, retries={"total_max_attempts": 1}))
    print(f"Runtime session ID: {session_id}", file=sys.stderr)
    payload = ({"action": "interrupt"} if args.interrupt else
               {"action": "steer", "message": args.steer} if args.steer is not None else
               {"action": "mcp_cancel_tools", "server_name": args.cancel_mcp_tools} if args.cancel_mcp_tools is not None else
               {"action": "mcp_revoke_oauth", "server_name": args.revoke_mcp_oauth} if args.revoke_mcp_oauth is not None else
               {"action": "prewarm"} if args.prewarm else
               {"action": "mcp_reconnect", "server_name": server_name,
                "start_oauth": args.reauthorize_mcp is not None} if server_name is not None else
               {"action": "code_status"} if args.code_status else
               {"action": "code_init"} if args.code_init else
               {"action": "code_overview"} if args.code_overview else
               {"action": "compact"} if args.compact else
               {"action": "session_messages",
                **({"limit": args.history_limit} if args.history_limit is not None else {}),
                **({"offset": args.history_offset} if args.history_offset is not None else {}),
                **({"coalesce_chunks": True} if args.history_coalesce else {}),
                **({"exclude_observed_subagents": True} if args.history_exclude_subagents else {}),
                **({"prefer_original_strings": True} if args.history_original_strings else {}),
                **({"include_observed_tool_messages": True} if args.history_tools else {})}
               if args.session_messages else
               {"prompt": prompt, "stream": stream, **({"include_events": True} if args.events else {})})
    response = client.invoke_agent_runtime(
        agentRuntimeArn=args.runtime_arn,
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="text/event-stream" if stream else "application/json",
        payload=json.dumps(payload).encode("utf-8"),
    )
    body = response["response"]
    if "text/event-stream" in response.get("contentType", ""):
        failed = False
        for line in body.iter_lines():
            if line:
                text = line.decode("utf-8")
                print(text)
                if text.startswith("data: "):
                    event = json.loads(text[6:])
                    failed = failed or event.get("type") == "error" or bool(event.get("is_error"))
        if failed:
            raise SystemExit(1)
    else:
        result = json.loads(body.read())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("is_error") or result.get("error"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
