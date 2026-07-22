"""MCPClient + SigV4 transport + toolFilter. Spec Section 6.5, 11."""

import os
from functools import partial
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


REGION = os.environ.get("AWS_REGION", "ap-northeast-2")


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
        gateway_id = gw["gatewayId"]
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
