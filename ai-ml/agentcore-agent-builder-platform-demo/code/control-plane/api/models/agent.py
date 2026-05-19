"""Pydantic models for Agent Config, Card, Runtime. Spec Section 5.1."""

from pydantic import BaseModel
from typing import Optional


class GatewayBinding(BaseModel):
    gatewayId: str
    toolFilter: str | list[str] = "all"


class Delegation(BaseModel):
    targetAgent: str
    purpose: str
    scope: list[str] = []
    condition: str = "always"
    timeout: int = 60


class EvaluatorConfig(BaseModel):
    enabled: bool = False
    criteria: str = ""


class HarnessConfig(BaseModel):
    preHooks: list[str] = []
    postHooks: list[str] = []
    hitlActions: list[str] = []
    evaluator: EvaluatorConfig = EvaluatorConfig()


class InternalToolConfig(BaseModel):
    name: str
    description: str = ""
    type: str
    table: str = ""
    module: str = ""


class TriggerConfig(BaseModel):
    type: str
    source: str = ""
    pattern: dict = {}
    cron: str = ""
    description: str = ""


class AgentConfig(BaseModel):
    agentId: str
    name: str
    contextBoundary: str
    model: str = "global.anthropic.claude-sonnet-4-6"
    systemPrompt: str = ""
    gateways: list[GatewayBinding] = []
    delegations: list[Delegation] = []
    harness: HarnessConfig = HarnessConfig()
    triggers: list[TriggerConfig] = []
    internalTools: list[InternalToolConfig] = []
    createdBy: str = ""
    version: int = 1
    metadata: dict = {}


class AgentCard(BaseModel):
    agentId: str
    name: str
    description: str
    capabilities: list[str] = []
    status: str = "active"
    delegatesTo: list[str] = []
    contextBoundary: str = ""
    model: str = ""
    owner: str = ""


class AgentRuntime(BaseModel):
    agentId: str
    runtimeArn: str = ""
    status: str = "provisioning"
    createdAt: str = ""
    version: int = 1


class AgentCreateRequest(BaseModel):
    name: str
    contextBoundary: str
    model: str = "global.anthropic.claude-sonnet-4-6"
    systemPrompt: str = ""
    gateways: list[dict] = []
    delegations: list[dict] = []
    harness: dict = {}
    triggers: list[dict] = []
    internalTools: list[dict] = []
    metadata: dict = {}

    model_config = {"extra": "allow"}


class AgentUpdateRequest(BaseModel):
    name: Optional[str] = None
    contextBoundary: Optional[str] = None
    model: Optional[str] = None
    systemPrompt: Optional[str] = None
    gateways: Optional[list[GatewayBinding]] = None
    delegations: Optional[list[Delegation]] = None
    harness: Optional[HarnessConfig] = None
    triggers: Optional[list[TriggerConfig]] = None
    internalTools: Optional[list[InternalToolConfig]] = None
    metadata: Optional[dict] = None
