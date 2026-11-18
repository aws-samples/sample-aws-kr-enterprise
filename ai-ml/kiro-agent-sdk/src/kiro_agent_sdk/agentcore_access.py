"""Generate and apply narrowly scoped AgentCore invocation resource policies."""
from __future__ import annotations

import json
import re
import copy
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import KiroSDKError
_RUNTIME = re.compile(
    r"arn:(aws(?:-[a-z0-9]+)*):bedrock-agentcore:([a-z0-9-]+):([0-9]{12}):"
    r"runtime/[a-zA-Z][a-zA-Z0-9_]{0,47}-[a-zA-Z0-9]{10}")
_ROLE = re.compile(r"arn:(aws(?:-[a-z0-9]+)*):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+")
_ENDPOINT = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,47}")
_VPCE = re.compile(r"vpce-(?:[0-9a-f]{8}|[0-9a-f]{17})")


def _values(values: Iterable[str], pattern: re.Pattern, label: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 16:
        raise ValueError(f"{label} must contain between one and 16 values")
    if any(type(value) is not str or len(value) > 512 or not pattern.fullmatch(value) for value in values):
        raise ValueError(f"Invalid {label}")
    if len(set(values)) != len(values):
        raise ValueError(f"Duplicate {label}")
    return tuple(values)


@dataclass(frozen=True)
class AgentCorePrivateAccess:
    """Policies for direct SigV4 invocation by explicit roles through explicit VPC endpoints.

    This does not provision a VPC endpoint, change role policies, or authorize
    individual application users. Resource policies are applied sequentially.
    """

    runtime_arn: str
    caller_role_arns: tuple[str, ...]
    vpce_ids: tuple[str, ...]
    endpoint_names: tuple[str, ...] = ("DEFAULT",)

    def __post_init__(self) -> None:
        match = (_RUNTIME.fullmatch(self.runtime_arn)
                 if type(self.runtime_arn) is str and len(self.runtime_arn) <= 512 else None)
        if match is None:
            raise ValueError("Invalid AgentCore runtime ARN")
        roles = _values(self.caller_role_arns, _ROLE, "caller role ARNs")
        if any(_ROLE.fullmatch(role).group(1) != match.group(1) for role in roles):
            raise ValueError("Caller roles must use the runtime's AWS partition")
        endpoints = _values(self.endpoint_names, _ENDPOINT, "endpoint names")
        object.__setattr__(self, "caller_role_arns", roles)
        object.__setattr__(self, "vpce_ids", _values(self.vpce_ids, _VPCE, "VPC endpoint IDs"))
        object.__setattr__(self, "endpoint_names", endpoints)

    @property
    def region(self) -> str:
        return _RUNTIME.fullmatch(self.runtime_arn).group(2)

    @property
    def resource_arns(self) -> tuple[str, ...]:
        return (self.runtime_arn, *(self.runtime_arn + "/runtime-endpoint/" + name
                                    for name in self.endpoint_names))

    def resource_policies(self) -> dict[str, dict[str, Any]]:
        result = {}
        for arn in self.resource_arns:
            result[arn] = {"Version": "2012-10-17", "Statement": [
                {"Sid": "AllowPrivateInvocation", "Effect": "Allow",
                 "Principal": {"AWS": list(self.caller_role_arns)},
                 "Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": arn,
                 "Condition": {"StringEquals": {"aws:SourceVpce": list(self.vpce_ids)}}},
                {"Sid": "DenyInvocationOutsideVpcEndpoints", "Effect": "Deny", "Principal": "*",
                 "Action": "bedrock-agentcore:InvokeAgentRuntime*", "Resource": arn,
                 "Condition": {"StringNotEquals": {"aws:SourceVpce": list(self.vpce_ids)}}},
                {"Sid": "DenyOtherInvocationPrincipals", "Effect": "Deny", "Principal": "*",
                 "Action": "bedrock-agentcore:InvokeAgentRuntime*", "Resource": arn,
                 "Condition": {"ArnNotEquals": {"aws:PrincipalArn": list(self.caller_role_arns)}}},
            ]}
        return result

    def caller_policy(self) -> dict[str, Any]:
        return {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
            "Resource": list(self.resource_arns),
            "Condition": {"StringEquals": {"aws:SourceVpce": list(self.vpce_ids)}}}]}

    def vpc_endpoint_policy(self) -> dict[str, Any]:
        """Allow invocation of this runtime on a dedicated AgentCore VPC endpoint."""
        return {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": "*", "Action": "bedrock-agentcore:InvokeAgentRuntime",
            "Resource": list(self.resource_arns),
            "Condition": {"ArnEquals": {"aws:PrincipalArn": list(self.caller_role_arns)}}}]}

    def plan(self) -> dict[str, Any]:
        """Return JSON-serializable documents without calling AWS."""
        return {"runtime_arn": self.runtime_arn, "region": self.region,
                "resource_policies": self.resource_policies(),
                "caller_policy": self.caller_policy(), "vpc_endpoint_policy": self.vpc_endpoint_policy()}


class AgentCorePolicyApplyError(KiroSDKError):
    """Application stopped without rollback; inspect confirmed and uncertain writes.

    ``applied_resources`` contains acknowledged writes or writes confirmed by a
    read after a failed response. ``verified_resources`` contains all resources
    whose desired policy was read back, including unchanged policies.
    ``uncertain_resources`` contains an attempted write whose desired state
    could not be read back. Absence from ``applied_resources`` never proves
    that such a write had no effect.
    """

    def __init__(self, applied: tuple[str, ...], failed_resource: str, *,
                 verified_resources: tuple[str, ...] = (),
                 uncertain_resources: tuple[str, ...] = (), phase: str = "apply") -> None:
        super().__init__("AgentCore resource policy application incomplete; inspect verified and uncertain resources")
        self.applied_resources = applied
        self.failed_resource = failed_resource
        self.verified_resources = verified_resources
        self.uncertain_resources = uncertain_resources
        self.phase = phase


def _policy_document(value: Any) -> dict[str, Any]:
    if type(value) is not str or len(value.encode("utf-8")) > 20480:
        raise ValueError("Invalid AgentCore resource policy")
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate AgentCore policy key")
            result[key] = item
        return result
    def invalid_constant(value):
        raise ValueError("Invalid AgentCore policy constant")
    result = json.loads(value, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if type(result) is not dict:
        raise ValueError("Invalid AgentCore resource policy")
    return result


def _existing_policy(client: Any, arn: str) -> Any:
    try:
        response = client.get_resource_policy(resourceArn=arn)
    except Exception as error:
        response = getattr(error, "response", {})
        # The caller must supply existing runtime/endpoint ARNs. A missing
        # policy is reported as ResourceNotFoundException by this API.
        details = response.get("Error") if isinstance(response, dict) else None
        if isinstance(details, dict) and details.get("Code") == "ResourceNotFoundException":
            return None
        raise
    if type(response) is not dict:
        raise ValueError("Invalid AgentCore resource policy response")
    value = response.get("policy")
    if value in (None, ""):
        return None
    return _policy_document(value)


def _same_policy(first: Any, second: Any) -> bool:
    """Compare IAM scalar/list spellings only where the policy grammar permits them."""
    def values(value):
        if type(value) is str:
            return [value]
        if type(value) is list and value and all(type(item) is str for item in value):
            return sorted(value)
        return value

    def canonical(policy):
        policy = copy.deepcopy(policy)
        if type(policy) is dict:
            statements = policy.get("Statement")
            if type(statements) is dict:
                statements = [statements]
                policy["Statement"] = statements
            if type(statements) is list:
                for statement in statements:
                    if type(statement) is not dict:
                        continue
                    for key in ("Action", "NotAction", "Resource", "NotResource"):
                        if key in statement:
                            statement[key] = values(statement[key])
                    for key in ("Principal", "NotPrincipal"):
                        principal = statement.get(key)
                        if type(principal) is dict:
                            for kind in ("AWS", "Service", "Federated", "CanonicalUser"):
                                if kind in principal:
                                    principal[kind] = values(principal[kind])
                    conditions = statement.get("Condition")
                    if type(conditions) is dict:
                        for condition in conditions.values():
                            if type(condition) is dict:
                                for key in condition:
                                    condition[key] = values(condition[key])
        return json.dumps(policy, sort_keys=True, ensure_ascii=True, allow_nan=False)
    return canonical(first) == canonical(second)


def apply_private_access(client: Any, access: AgentCorePrivateAccess, *, replace_existing: bool = False) -> tuple[str, ...]:
    """Apply runtime first, then endpoint policies; never roll back to weaker access.

    Preflight refuses different existing policies unless explicitly requested.
    AgentCore provides no conditional policy write, so administrators must
    coordinate concurrent changes. Caller and VPC endpoint policies are only
    generated; attaching them requires ownership of those separate resources.
    Failed writes are not retried here. A read is attempted after a write
    exception, and the error reports any remaining uncertainty before stopping.
    Configure timeouts and retry limits on the supplied AWS client.
    """
    if type(access) is not AgentCorePrivateAccess or type(replace_existing) is not bool:
        raise ValueError("Invalid private access application options")
    policies = access.resource_policies()
    existing = {}
    for arn in policies:
        try:
            existing[arn] = _existing_policy(client, arn)
        except (Exception, KeyboardInterrupt):
            raise AgentCorePolicyApplyError((), arn, phase="preflight") from None
    if not replace_existing and any(existing[arn] is not None and not _same_policy(existing[arn], policy)
                                    for arn, policy in policies.items()):
        raise ValueError("An existing resource policy differs; review it before enabling replace_existing")
    applied: list[str] = []
    verified: list[str] = []
    for arn, policy in policies.items():
        attempted = False
        try:
            current = _existing_policy(client, arn)
            if not _same_policy(current, existing[arn]):
                raise ValueError("AgentCore resource policy changed during application")
            if not _same_policy(current, policy):
                attempted = True
                try:
                    response = client.put_resource_policy(resourceArn=arn, policy=json.dumps(policy, sort_keys=True))
                except Exception:
                    # A lost response can follow a successful AWS write. One
                    # read helps diagnose it; it cannot rule out a late write.
                    try:
                        if _same_policy(_existing_policy(client, arn), policy):
                            applied.append(arn)
                            verified.append(arn)
                    except Exception:
                        pass
                    raise
                applied.append(arn)
                try:
                    echoed = type(response) is dict and _same_policy(_policy_document(response.get("policy")), policy)
                except (ValueError, UnicodeError, RecursionError):
                    echoed = False
                if _same_policy(_existing_policy(client, arn), policy):
                    verified.append(arn)
                if not echoed or arn not in verified:
                    raise ValueError("AgentCore did not confirm the applied policy")
            else:
                verified.append(arn)
        except (Exception, KeyboardInterrupt):
            uncertain = (arn,) if attempted and arn not in verified else ()
            raise AgentCorePolicyApplyError(tuple(applied), arn, verified_resources=tuple(verified),
                                            uncertain_resources=uncertain) from None
    return tuple(applied)
