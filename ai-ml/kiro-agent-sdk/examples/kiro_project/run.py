"""Run a Kiro project from Python: the agent, steering and skill under project/.kiro were written in Kiro.

    python examples/kiro_project/run.py

Open project/ in Kiro (IDE or `kiro-cli chat --agent release-writer`) to change the agent, then run this
script again: the application uses the same files.
"""
import asyncio
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, PermissionResultAllow, PermissionResultDeny, ResultMessage

PROJECT = Path(__file__).resolve().parent / "project"


async def read_only(name, data, context):
    # The application decides what the Kiro agent may do; here only reads of project files are allowed.
    # Kiro reports the file in the consent request; the tool input itself can be unavailable.
    consent = context.kiro_consent or {}
    resource = consent.get("resource")
    if consent.get("capability") == "fs_read" and isinstance(resource, str):
        root = PROJECT.resolve()
        path = (Path(consent.get("workspaceRoot") or root) / resource).resolve()
        if path == root or root in path.parents:
            return PermissionResultAllow()
    return PermissionResultDeny(message="This example only allows reading files in the project")


async def main():
    options = KiroAgentOptions(cwd=PROJECT, agent_engine="v3", agent="release-writer", can_use_tool=read_only)
    async with KiroSDKClient(options) as client:
        await client.query("Write the release notes for 2026-11 from changes/2026-11.md.")
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                if message.is_error:
                    raise RuntimeError(message.result or "Kiro turn failed")
                print(message.result)


if __name__ == "__main__":
    asyncio.run(main())
