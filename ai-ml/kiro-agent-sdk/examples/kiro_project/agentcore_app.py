"""Serve the Kiro project in project/ from Amazon Bedrock AgentCore Runtime.

Each AgentCore session gets its own workspace directory. The first time a session starts, the project files
(including project/.kiro) are copied into that workspace, so Kiro loads the same agent, steering and skill as in
the IDE. A resumed session keeps its workspace as it was.

    uvicorn agentcore_app:app --host 0.0.0.0 --port 8080 --workers 1

The app has no session_authorizer, so it starts only with KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS=true. Use that
for a single-user deployment only; with more users, add a session_authorizer that checks who owns each session.
"""
import shutil
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, PermissionResultAllow, PermissionResultDeny
from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app

PROJECT = Path(__file__).resolve().parent / "project"


def read_only_in(workspace):
    root = Path(workspace).resolve()

    async def read_only(name, data, context):
        # Kiro reports the file in the consent request; the tool input itself can be unavailable.
        consent = context.kiro_consent or {}
        resource = consent.get("resource")
        if consent.get("capability") == "fs_read" and isinstance(resource, str):
            path = (Path(consent.get("workspaceRoot") or root) / resource).resolve()
            if path == root or root in path.parents:
                return PermissionResultAllow()
        return PermissionResultDeny(message="This deployment only allows reading files in the project")

    return read_only


def options_for_session(workspace, resume):
    if resume is None:
        shutil.copytree(PROJECT, workspace, dirs_exist_ok=True)
    return KiroAgentOptions(agent="release-writer", can_use_tool=read_only_in(workspace))


app = create_agentcore_app(AgentCoreConfig.from_env(), options_factory=options_for_session)
