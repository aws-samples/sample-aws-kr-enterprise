"""List or answer pending AgentCore interactions using an existing session."""
from __future__ import annotations

import argparse
import getpass
import json
import re
import warnings
from pathlib import Path
from urllib.parse import urlsplit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--runtime-arn", required=True)
    parser.add_argument("--endpoint-url", help="Trusted HTTPS AgentCore endpoint URL, such as a PrivateLink DNS name")
    parser.add_argument("--session-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    for name in ("allow", "deny", "dismiss"):
        command = commands.add_parser(name)
        command.add_argument("interaction_id")
        if name == "allow":
            command.add_argument("--permission-updates-file", type=Path,
                                 help="JSON array of explicitly enabled session permission updates")
    answer = commands.add_parser("answer")
    answer.add_argument("interaction_id")
    answer.add_argument("text")
    elicitation = commands.add_parser("elicitation")
    elicitation.add_argument("interaction_id")
    elicitation.add_argument("response_file", type=Path, help="JSON MCP elicitation response")
    oauth = commands.add_parser("oauth")
    oauth.add_argument("interaction_id")
    oauth_cancel = commands.add_parser("oauth-cancel")
    oauth_cancel.add_argument("interaction_id")
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
    if not 33 <= len(args.session_id) <= 256 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.session_id):
        parser.error("AgentCore session IDs must be 33-256 letters, digits, hyphens or underscores")
    if args.command == "list":
        payload = {"action": "interactions"}
    else:
        if not re.fullmatch(r"[0-9a-f]{32}", args.interaction_id):
            parser.error("interaction ID must be 32 lowercase hexadecimal characters")
        payload = {"action": "respond", "interaction_id": args.interaction_id}
        if args.command in ("allow", "deny"):
            payload["decision"] = args.command
            if args.command == "allow" and args.permission_updates_file is not None:
                try:
                    with args.permission_updates_file.open("rb") as source:
                        data = source.read(256 * 1024 + 1)
                    if len(data) > 256 * 1024:
                        raise ValueError("permission updates are too large")
                    updates = json.loads(data)
                    if type(updates) is not list or not updates or len(updates) > 32:
                        raise ValueError("permission updates must be a nonempty bounded array")
                    json.dumps(updates, allow_nan=False)
                except (OSError, ValueError, RecursionError) as exc:
                    parser.error(f"cannot read permission updates JSON: {type(exc).__name__}")
                payload["updated_permissions"] = updates
        elif args.command == "dismiss":
            payload["answer"] = None
        elif args.command == "answer":
            payload["answer"] = args.text
        elif args.command == "oauth":
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    payload["redirect_url"] = getpass.getpass("OAuth redirect URL: ")
            except getpass.GetPassWarning:
                parser.error("OAuth input requires a terminal that can hide input")
        elif args.command == "oauth-cancel":
            payload["redirect_url"] = None
        else:
            try:
                payload["response"] = json.loads(args.response_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                parser.error(f"cannot read response JSON: {type(exc).__name__}")

    import boto3
    from botocore.config import Config

    client = boto3.Session(region_name=args.region).client(
        "bedrock-agentcore", endpoint_url=args.endpoint_url,
        config=Config(read_timeout=30, retries={"total_max_attempts": 1}))
    response = client.invoke_agent_runtime(
        agentRuntimeArn=args.runtime_arn, runtimeSessionId=args.session_id,
        contentType="application/json", accept="application/json",
        payload=json.dumps(payload, allow_nan=False).encode("utf-8"),
    )
    result = json.loads(response["response"].read())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get("error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
