"""Create an AgentCore Runtime from an existing ECR image or S3 CodeZip."""
from __future__ import annotations

import argparse
import json
import re
from urllib.parse import urlsplit


def build_request(args: argparse.Namespace) -> dict:
    secret_id = getattr(args, "secret_id", None)
    device_login = getattr(args, "device_login", False)
    if bool(secret_id) == bool(device_login):
        raise ValueError("Specify exactly one secret ID or device login")
    license = getattr(args, "device_license", "free")
    start_url = getattr(args, "device_start_url", None)
    device_region = getattr(args, "device_region", None)
    if device_login:
        if args.secret_json_key:
            raise ValueError("Device login cannot use a secret JSON key")
        if license not in ("free", "pro"):
            raise ValueError("Device license must be free or pro")
        if license == "pro":
            try:
                url = urlsplit(start_url) if start_url else None
                valid = bool(start_url and start_url == start_url.strip() and len(start_url) <= 2048 and
                             not any(ord(char) < 32 for char in start_url) and url.scheme == "https" and
                             url.hostname and url.port in (None, 443) and url.username is None and
                             url.password is None and not url.query and not url.fragment)
            except ValueError:
                valid = False
            if not valid or not isinstance(device_region, str) or not re.fullmatch(
                    r"[a-z]{2}(?:-[a-z0-9]+)+-\d+", device_region):
                raise ValueError("Pro device login requires an HTTPS Identity Center start URL and region")
        elif start_url or device_region:
            raise ValueError("Identity Center settings require the pro license")
        environment = {"KIRO_AGENTCORE_AUTH_MODE": "device", "KIRO_AGENTCORE_DEVICE_LICENSE": license}
        if start_url:
            environment["KIRO_AGENTCORE_DEVICE_START_URL"] = start_url
            environment["KIRO_AGENTCORE_DEVICE_REGION"] = device_region
    else:
        if license != "free" or start_url or device_region:
            raise ValueError("Device login settings require --device-login")
        environment = {"KIRO_AGENTCORE_SECRET_ID": secret_id}
        if args.secret_json_key:
            environment["KIRO_AGENTCORE_SECRET_JSON_KEY"] = args.secret_json_key
    if getattr(args, "allow_unauthorized_sessions", False):
        environment["KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS"] = "true"
    if args.allow_sdk_events:
        environment["KIRO_AGENTCORE_ALLOW_SDK_EVENTS"] = "true"
    if getattr(args, "allow_interactions", False):
        environment["KIRO_AGENTCORE_ALLOW_INTERACTIONS"] = "true"
    subnets = args.subnet or []
    security_groups = args.security_group or []
    if bool(subnets) != bool(security_groups):
        raise ValueError("VPC deployment requires both subnets and security groups")
    network = ({"networkMode": "VPC", "networkModeConfig": {
        "subnets": subnets, "securityGroups": security_groups}} if subnets else {"networkMode": "PUBLIC"})
    code_uri = getattr(args, "code_s3_uri", None)
    code_version = getattr(args, "code_version_id", None)
    image_uri = getattr(args, "image_uri", None)
    if bool(image_uri) == bool(code_uri):
        raise ValueError("Specify exactly one container image or CodeZip S3 URI")
    if code_uri:
        parsed = urlsplit(code_uri)
        if (parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/") or
                parsed.query or parsed.fragment or "@" in parsed.netloc or ":" in parsed.netloc):
            raise ValueError("CodeZip URI must be s3://bucket/object-key")
        location = {"bucket": parsed.netloc, "prefix": parsed.path.lstrip("/")}
        if code_version:
            location["versionId"] = code_version
        artifact = {"codeConfiguration": {"code": {"s3": location},
                                           "runtime": "PYTHON_3_12", "entryPoint": ["agentcore_entry.py"]}}
    else:
        if code_version:
            raise ValueError("CodeZip version ID requires a CodeZip S3 URI")
        artifact = {"containerConfiguration": {"containerUri": image_uri}}
    request = {
        "agentRuntimeName": args.name,
        "agentRuntimeArtifact": artifact,
        "roleArn": args.role_arn,
        "networkConfiguration": network,
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": environment,
    }
    if args.session_storage:
        if (not re.fullmatch(r"/mnt/[A-Za-z0-9._-]+", args.session_storage) or
                args.session_storage.rsplit("/", 1)[-1] in (".", "..")):
            raise ValueError("session storage mount must be /mnt followed by one directory name")
        request["filesystemConfigurations"] = [{"sessionStorage": {"mountPath": args.session_storage}}]
        environment["KIRO_AGENTCORE_STATE_DIR"] = args.session_storage + "/kiro-agent-sdk"
    return request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--region", required=True)
    artifact = parser.add_mutually_exclusive_group(required=True)
    artifact.add_argument("--image-uri", help="Existing ARM64 ECR image")
    artifact.add_argument("--code-s3-uri", help="Existing S3 CodeZip, e.g. s3://bucket/key.zip")
    parser.add_argument("--code-version-id", help="Optional S3 object version for --code-s3-uri")
    parser.add_argument("--role-arn", required=True)
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--secret-id")
    auth.add_argument("--device-login", action="store_true", help="Enable session-scoped browser device login")
    parser.add_argument("--secret-json-key")
    parser.add_argument("--device-license", choices=("free", "pro"), default="free")
    parser.add_argument("--device-start-url", help="Identity Center start URL for pro device login")
    parser.add_argument("--device-region", help="Identity Center region for pro device login")
    parser.add_argument("--session-storage", help="Optional AgentCore session mount, e.g. /mnt/kiro")
    parser.add_argument("--subnet", action="append", default=[], help="VPC subnet; repeat as needed")
    parser.add_argument("--security-group", action="append", default=[], help="VPC security group; repeat as needed")
    parser.add_argument("--allow-unauthorized-sessions", action="store_true",
                        help="Run an app without session_authorizer; every caller may use every session")
    parser.add_argument("--allow-sdk-events", action="store_true", help="Allow callers to request raw SDK events")
    parser.add_argument("--allow-interactions", action="store_true", help="Enable remote permission and input requests")
    args = parser.parse_args()

    import boto3

    client = boto3.Session(region_name=args.region).client("bedrock-agentcore-control")
    response = client.create_agent_runtime(**build_request(args))
    print(json.dumps({key: response[key] for key in ("agentRuntimeArn", "agentRuntimeId", "status")
                      if key in response}, indent=2))


if __name__ == "__main__":
    main()
