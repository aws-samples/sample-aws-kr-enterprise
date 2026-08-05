"""MCPClient + SigV4 transport + toolFilter. Spec Section 6.5, 11."""

import os
from functools import partial
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

# Cache of gateway name -> real (suffixed) gatewayId, resolved once per runtime.
_GATEWAY_ID_CACHE: dict = {}


def _resolve_gateway_id(gateway_ref: str) -> str:
    """Resolve a stored gateway reference to the real AgentCore gatewayId.

    Agent configs may store a bare gateway *name* (e.g. "awsops-cost-gateway"),
    but AgentCore assigns a suffixed id (e.g. "awsops-cost-gateway-agig5ljpye").
    The MCP endpoint URL must use the real id, so if the stored value is not
    already a valid gatewayId we look it up by name via list_gateways()
    (same matching as scripts/register-gateway-targets.py). Deploy-order
    independent: works whether the config was seeded before or after the
    gateways existed.
    """
    if gateway_ref in _GATEWAY_ID_CACHE:
        return _GATEWAY_ID_CACHE[gateway_ref]

    resolved = gateway_ref
    try:
        client = boto3.client("bedrock-agentcore-control", region_name=REGION)
        items = client.list_gateways().get("items", [])
        ids = {g.get("gatewayId") for g in items}
        if gateway_ref not in ids:
            # exact name match first, then substring, mirroring register-gateway-targets.py
            match = next((g["gatewayId"] for g in items if g.get("name") == gateway_ref), None)
            if match is None:
                match = next((g["gatewayId"] for g in items if gateway_ref in g.get("name", "")), None)
            if match:
                resolved = match
    except Exception:
        # Fall back to the stored value; connect_gateways tolerates a failed client.
        resolved = gateway_ref

    _GATEWAY_ID_CACHE[gateway_ref] = resolved
    return resolved


def _get_sigv4_headers(url: str) -> dict:
    """SigV4 서명 헤더 생성. 매 호출 시 새로운 credentials으로 서명."""
    session = boto3.Session()
    credentials = session.get_credentials().get_frozen_credentials()
    request = AWSRequest(method="POST", url=url, data=b"")
    SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(request)
    return dict(request.headers)


def _create_transport_callable(gateway_id: str):
    """MCPClient에 전달할 transport callable 생성.
    streamablehttp_client는 async context manager를 반환하는 함수."""
    url = f"https://{gateway_id}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp"
    headers = _get_sigv4_headers(url)
    return partial(streamablehttp_client, url=url, headers=headers)


def connect_gateways(config: dict) -> list[MCPClient]:
    """Config의 gateways[] 기반으로 MCPClient 초기화.
    toolFilter가 "all"이면 전체 Tool, 배열이면 선택적 바인딩. Spec Section 6.5."""
    clients = []
    for gw in config.get("gateways", []):
        gateway_id = _resolve_gateway_id(gw["gatewayId"])
        tool_filter = gw.get("toolFilter", "all")

        tool_filters = None
        if tool_filter != "all":
            tool_filters = {"allowed_tool_names": tool_filter}

        transport_callable = _create_transport_callable(gateway_id)
        client = MCPClient(
            transport_callable=transport_callable,
            tool_filters=tool_filters,
        )
        clients.append(client)

    return clients
