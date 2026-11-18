import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, list_models
from kiro_agent_sdk.mcp_config import load_mcp_config


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class ProcessEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_pwd_is_consistent_for_cli_and_mcp_expansion(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "workspace"
            workspace.mkdir()
            link = Path(root) / "workspace-link"
            try:
                link.symlink_to(workspace, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks are unavailable")
            env = {"PWD": "/stale", "FAKE_REPORT_CWD": "1"}
            config = json.dumps({"mcpServers": {"probe": {"command": "server", "args": ["${PWD}"]}}})
            options = KiroAgentOptions(cwd=link, cli_path=FAKE, env=env, mcp_servers=config)
            async with KiroSDKClient(options) as client:
                self.assertEqual(client.session_info["processCwd"], str(workspace.resolve()))
                self.assertEqual(client.session_info["processPwd"], str(workspace.resolve()))
                self.assertEqual(client.session_info["received"]["mcpServers"][0]["args"],
                                 [str(workspace.resolve())])
            models = await list_models(cwd=link, cli_path=FAKE, env=env)
            self.assertEqual(models[0]["processCwd"], str(workspace.resolve()))
            self.assertEqual(models[0]["processPwd"], str(workspace.resolve()))
            self.assertEqual(env["PWD"], "/stale")

    async def test_remote_config_expansion_uses_client_environment(self):
        config = {"probe": {"command": "server", "args": ["${PWD}"]}}
        with patch.dict(os.environ, {"PWD": "/client-location"}):
            result = load_mcp_config(config, "/remote-workspace", {}, expand_programmatic=True,
                                     local_process=False)
        self.assertEqual(result["probe"]["args"], ["/client-location"])


if __name__ == "__main__":
    unittest.main()
