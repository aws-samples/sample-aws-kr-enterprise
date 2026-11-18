"""Send consecutive prompts in one Kiro session."""
import asyncio
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ResultMessage


async def main() -> None:
    options = KiroAgentOptions(cwd=Path.cwd(), agent_engine="v3")
    async with KiroSDKClient(options) as client:
        print(f"Session ID: {client.session_id}")
        for prompt in (
            "Remember the word ALPHA. Reply OK. Do not use tools.",
            "What word did I ask you to remember? Do not use tools.",
        ):
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    print(message.result)


if __name__ == "__main__":
    asyncio.run(main())
