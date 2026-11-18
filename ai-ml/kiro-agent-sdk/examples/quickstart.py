"""Run one prompt with Kiro CLI."""
import asyncio
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, ResultMessage, query


async def main() -> None:
    options = KiroAgentOptions(cwd=Path.cwd(), agent_engine="v3")
    async for message in query(
        prompt="Reply with a short greeting. Do not use tools.",
        options=options,
    ):
        if isinstance(message, ResultMessage):
            print(message.result)
            print(f"Session ID: {message.session_id}")


if __name__ == "__main__":
    asyncio.run(main())
