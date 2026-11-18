"""Example of an AgentCore app with application-owned Kiro options.

Without a session_authorizer it starts only with KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS=true (single user).
"""
from kiro_agent_sdk import KiroAgentOptions
from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app


def options_for_session(workspace, resume):
    # Add MCP servers, tools or hooks here only after reviewing their permissions.
    return KiroAgentOptions(system_prompt="Give concise answers.", tools=[])


app = create_agentcore_app(AgentCoreConfig.from_env(), options_factory=options_for_session)
