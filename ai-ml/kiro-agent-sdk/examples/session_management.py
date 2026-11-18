"""List local V3 sessions without creating a chat or submitting a prompt."""
import asyncio
import json
from pathlib import Path

from kiro_agent_sdk import KiroSessionClient


async def main() -> None:
    async with KiroSessionClient(cwd=Path.cwd()) as client:
        page = await client.list_sessions()
        for session in page["sessions"]:
            print(json.dumps(session, ensure_ascii=False))
        if page.get("nextCursor"):
            print("More sessions are available; pass nextCursor as cursor to list_sessions().")


if __name__ == "__main__":
    asyncio.run(main())
