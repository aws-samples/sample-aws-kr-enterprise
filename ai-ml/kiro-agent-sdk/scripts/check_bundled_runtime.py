"""Check that a bundled V3 runtime can initialize without a real API key."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient
from kiro_agent_sdk.agentcore import _copy_runtime_cache


async def main() -> None:
    cache = Path(os.environ.get("KIRO_AGENTCORE_RUNTIME_CACHE", "/opt/kiro-prewarm"))
    with tempfile.TemporaryDirectory(prefix="kiro-sdk-bundle-check-") as directory:
        root = Path(directory)
        home = root / "home"
        workspace = root / "workspace"
        home.mkdir()
        workspace.mkdir()
        await asyncio.to_thread(_copy_runtime_cache, cache, home)
        env = {"HOME": str(home), "KIRO_HOME": str(home / ".kiro"),
               "XDG_DATA_HOME": str(home / ".local/share"),
               "XDG_CONFIG_HOME": str(home / ".config"),
               "XDG_CACHE_HOME": str(home / ".cache"),
               "XDG_STATE_HOME": str(home / ".local/state"),
               "KIRO_API_KEY": "ksk_invalid_kiro_sdk_bundle_check"}
        timeout = float(os.environ.get("KIRO_SDK_BUNDLE_CHECK_TIMEOUT", "300"))
        async with KiroSDKClient(KiroAgentOptions(cwd=workspace, env=env, agent_engine="v3",
                                                  tools=[], startup_timeout=timeout)) as client:
            if not client.session_id:
                raise RuntimeError("Kiro did not create a session")
    print("Bundled Kiro V3 initialized; no model invocation or real API key used.")


if __name__ == "__main__":
    asyncio.run(main())
