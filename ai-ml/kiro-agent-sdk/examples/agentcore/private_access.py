"""Generate a private invocation policy plan; apply resource policies only with --apply."""
from __future__ import annotations

import argparse
import json

from kiro_agent_sdk.agentcore_access import AgentCorePrivateAccess, AgentCorePolicyApplyError, apply_private_access


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-arn", required=True)
    parser.add_argument("--caller-role-arn", action="append", required=True)
    parser.add_argument("--vpce-id", action="append", required=True)
    parser.add_argument("--endpoint", action="append", help="Endpoint name; defaults to DEFAULT")
    parser.add_argument("--apply", action="store_true", help="Apply runtime and endpoint resource policies")
    parser.add_argument("--replace-existing", action="store_true", help="Replace reviewed existing resource policies")
    args = parser.parse_args()
    if args.replace_existing and not args.apply:
        parser.error("--replace-existing requires --apply")
    access = AgentCorePrivateAccess(args.runtime_arn, tuple(args.caller_role_arn), tuple(args.vpce_id),
                                   tuple(args.endpoint or ["DEFAULT"]))
    if not args.apply:
        print(json.dumps(access.plan(), indent=2))
        return
    import boto3
    from botocore.config import Config
    client = boto3.Session(region_name=access.region).client(
        "bedrock-agentcore-control",
        config=Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1}))
    try:
        applied = apply_private_access(client, access, replace_existing=args.replace_existing)
    except AgentCorePolicyApplyError as error:
        print(json.dumps({"status": "incomplete", "applied_resources": error.applied_resources,
                          "verified_resources": error.verified_resources,
                          "uncertain_resources": error.uncertain_resources,
                          "phase": error.phase, "failed_resource": error.failed_resource}))
        raise SystemExit(1) from None
    except ValueError as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        raise SystemExit(1) from None
    print(json.dumps({"status": "applied", "changed_resources": applied}))


if __name__ == "__main__":
    main()
