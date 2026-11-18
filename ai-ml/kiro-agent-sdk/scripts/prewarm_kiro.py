"""Download Kiro's V3 runtime during image build without real credentials."""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient


async def main(destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="kiro-sdk-prewarm-") as directory:
        home = Path(directory)
        environment = {
            "HOME": directory,
            "KIRO_HOME": str(home / ".kiro"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            # Session initialization does not invoke a model. This is not a credential.
            "KIRO_API_KEY": "ksk_invalid_kiro_sdk_image_prewarm",
        }
        options = KiroAgentOptions(cwd=home, agent_engine="v3", tools=[], env=environment,
                                   startup_timeout=180)
        async with KiroSDKClient(options):
            pass
        source = home / ".local" / "share" / "kiro-cli"
        if not (source / "node").is_file() or not (source / "kas").is_dir():
            raise RuntimeError("Kiro V3 did not prepare its node and kas runtime")
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "node", destination / "node")
        if (source / "node.sha256").is_file():
            shutil.copy2(source / "node.sha256", destination / "node.sha256")
        shutil.copytree(source / "kas", destination / "kas", symlinks=True,
                        ignore=shutil.ignore_patterns("*.lock"))
        print("Kiro V3 runtime prepared without copying credential or session databases")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/prewarm_kiro.py DESTINATION")
    asyncio.run(main(Path(sys.argv[1])))
