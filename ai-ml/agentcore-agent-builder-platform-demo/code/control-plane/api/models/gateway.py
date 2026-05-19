"""Pydantic models for Gateway, Tool. Spec Section 5.1."""

from pydantic import BaseModel


class GatewayConfig(BaseModel):
    gatewayId: str
    name: str
    description: str = ""
    domain: str = ""
    toolCount: int = 0


class ToolConfig(BaseModel):
    toolId: str
    name: str
    description: str = ""
    lambdaArn: str = ""
    inputSchema: dict = {}
    permission: str = "read"
