"""Start, inspect, and stop SDK background tasks in an AgentCore session."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--runtime-arn", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--endpoint-url", help="Trusted HTTPS AgentCore endpoint, such as a PrivateLink DNS name")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--request-id", required=True, help="32 lowercase hex characters; reuse for retries")
    start.add_argument("--prompt-file", type=Path, help="UTF-8 text file")
    start.add_argument("prompt", nargs="?")
    retry = commands.add_parser("retry")
    retry.add_argument("--previous-request-id", required=True, help="ID of the previously claimed request")
    retry.add_argument("--request-id", required=True, help="New 32-character lowercase hex request ID")
    retry.add_argument("--acknowledge-side-effects", action="store_true",
                       help="Accept that the original request may already have had effects")
    retry.add_argument("--prompt-file", type=Path, help="UTF-8 text file")
    retry.add_argument("prompt", nargs="?")
    listing = commands.add_parser("list")
    listing.add_argument("--all", action="store_true", help="Include completed tasks")
    events = commands.add_parser("events")
    events.add_argument("task_id")
    events.add_argument("--after", type=int, default=0, help="Cursor from a previous response")
    events.add_argument("--limit", type=int, default=32)
    events.add_argument("--follow", action="store_true", help="Stream event pages as SSE")
    archived_events = commands.add_parser("archive-events-get")
    archived_events.add_argument("task_id")
    archived_events.add_argument("--after", type=int, default=0)
    archived_events.add_argument("--limit", type=int, default=32)
    checkpoint_events = commands.add_parser("checkpoint-events-get")
    checkpoint_events.add_argument("task_id")
    checkpoint_events.add_argument("--after", type=int, default=0)
    checkpoint_events.add_argument("--limit", type=int, default=32)
    for name in ("status", "result", "stop", "forget", "archive", "archive-get", "archive-delete",
                 "archive-events", "archive-events-delete", "checkpoint-events", "checkpoint-events-delete"):
        command = commands.add_parser(name)
        command.add_argument("task_id")
    args = parser.parse_args()

    if not 33 <= len(args.session_id) <= 256 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.session_id):
        parser.error("AgentCore session IDs must be 33-256 letters, digits, hyphens or underscores")
    if args.endpoint_url:
        try:
            endpoint = urlsplit(args.endpoint_url)
            valid = (endpoint.scheme == "https" and endpoint.hostname and endpoint.port in (None, 443)
                     and endpoint.username is None and endpoint.password is None and
                     endpoint.path in ("", "/") and not endpoint.query and not endpoint.fragment)
        except ValueError:
            valid = False
        if not valid:
            parser.error("--endpoint-url must be a trusted HTTPS endpoint without a path or credentials")

    if args.command in ("start", "retry"):
        if not re.fullmatch(r"[0-9a-f]{32}", args.request_id):
            parser.error("--request-id must be 32 lowercase hexadecimal characters")
        if args.command == "retry" and (
                not re.fullmatch(r"[0-9a-f]{32}", args.previous_request_id) or
                args.previous_request_id == args.request_id or not args.acknowledge_side_effects):
            parser.error("retry requires a distinct --previous-request-id and --acknowledge-side-effects")
        if (args.prompt is None) == (args.prompt_file is None):
            parser.error("provide one prompt or --prompt-file")
        if args.prompt_file is not None:
            try:
                with args.prompt_file.open("r", encoding="utf-8") as source:
                    prompt = source.read(1024 * 1024 + 1)
            except (OSError, UnicodeError) as exc:
                parser.error(f"cannot read prompt file: {type(exc).__name__}")
        else:
            prompt = args.prompt
        try:
            valid = bool(prompt.strip()) and "\0" not in prompt and len(prompt.encode("utf-8")) <= 1024 * 1024
        except UnicodeError:
            valid = False
        if not valid:
            parser.error("prompt must be nonempty UTF-8 text of at most 1 MiB")
        payload = {"action": "background_task_" + args.command, "request_id": args.request_id, "prompt": prompt}
        if args.command == "retry":
            payload.update({"previous_request_id": args.previous_request_id, "acknowledge_side_effects": True})
    elif args.command == "list":
        payload = {"action": "background_tasks"}
        if args.all:
            payload["include_completed"] = True
    else:
        if not re.fullmatch(r"sdk-task-[0-9a-f]{32}", args.task_id):
            parser.error("task ID must be an SDK background task ID")
        payload = {"action": "background_task_" + args.command.replace("-", "_"), "task_id": args.task_id}
        if args.command in ("events", "archive-events-get", "checkpoint-events-get"):
            if not 0 <= args.after <= 2**64 - 1 or not 1 <= args.limit <= 64:
                parser.error("--after must be nonnegative and --limit must be between 1 and 64")
            payload.update({"after": args.after, "limit": args.limit})
            if args.command == "events" and args.follow:
                payload["action"] = "background_task_events_stream"

    import boto3
    from botocore.config import Config

    client = boto3.Session(region_name=args.region).client(
        "bedrock-agentcore", endpoint_url=args.endpoint_url,
        config=Config(read_timeout=600, retries={"total_max_attempts": 1}))
    response = client.invoke_agent_runtime(
        agentRuntimeArn=args.runtime_arn, runtimeSessionId=args.session_id,
        contentType="application/json",
        accept="text/event-stream" if args.command == "events" and args.follow else "application/json",
        payload=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    body = response["response"]
    if args.command == "events" and args.follow and "text/event-stream" in response.get("contentType", ""):
        failed = False
        ended = False
        for line in body.iter_lines():
            if not line:
                continue
            if len(line) > 2 * 1024 * 1024:
                raise SystemExit("AgentCore event exceeds the limit")
            text = line.decode("utf-8")
            print(text)
            if text.startswith("data: "):
                event = json.loads(text[6:])
                ended = ended or event.get("type") in ("complete", "timeout", "error")
                failed = failed or event.get("type") == "error" or (
                    event.get("type") == "complete" and event.get("task", {}).get("state") in ("failed", "cancelled"))
        if failed:
            raise SystemExit(1)
        if not ended:
            raise SystemExit("AgentCore stream ended without a completion or timeout event")
        return
    body = body.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise SystemExit("AgentCore response exceeds the limit")
    result = json.loads(body)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("is_error") or result.get("error"):
        raise SystemExit(1)
    if args.command == "stop" and result.get("task", {}).get("cleanup_pending"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
