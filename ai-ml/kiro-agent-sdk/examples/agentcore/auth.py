"""Start, inspect, cancel, or sign out a session-scoped Kiro device login."""
from __future__ import annotations

import argparse
import json
import re
from urllib.parse import urlsplit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--runtime-arn", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--endpoint-url", help="Trusted HTTPS AgentCore endpoint URL")
    parser.add_argument("command", choices=("start", "status", "cancel", "logout"))
    args = parser.parse_args()
    if not 33 <= len(args.session_id) <= 256 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.session_id):
        parser.error("AgentCore session IDs must be 33-256 letters, digits, hyphens or underscores")
    if args.endpoint_url:
        try:
            endpoint = urlsplit(args.endpoint_url)
            valid = (endpoint.scheme == "https" and endpoint.hostname and endpoint.port in (None, 443) and
                     endpoint.username is None and endpoint.password is None and endpoint.path in ("", "/") and
                     not endpoint.query and not endpoint.fragment)
        except ValueError:
            valid = False
        if not valid:
            parser.error("--endpoint-url must be a trusted HTTPS endpoint without a path or credentials")

    import boto3
    from botocore.config import Config

    client = boto3.Session(region_name=args.region).client(
        "bedrock-agentcore", endpoint_url=args.endpoint_url,
        config=Config(read_timeout=60, retries={"total_max_attempts": 1}))
    response = client.invoke_agent_runtime(
        agentRuntimeArn=args.runtime_arn, runtimeSessionId=args.session_id,
        contentType="application/json", accept="application/json",
        payload=json.dumps({"action": "auth_" + args.command}).encode(),
    )
    result = json.loads(response["response"].read())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and (result.get("error") or result.get("is_error")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
