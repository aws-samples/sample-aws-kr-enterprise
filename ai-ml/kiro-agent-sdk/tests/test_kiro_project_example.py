"""The read-only policies of examples/kiro_project allow only reads of files inside the project."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import PermissionResultAllow, ToolPermissionContext
from test_agentcore import HAS_STARLETTE

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "kiro_project"


def load(name):
    spec = importlib.util.spec_from_file_location(f"kiro_project_{name}", EXAMPLE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def context(consent=None, title="Read File"):
    metadata = {"kiro": {"toolId": "read_file", "consent": consent}} if consent is not None else None
    return ToolPermissionContext(title=title, request_metadata=metadata, kiro_input_unavailable=True)


class ReadOnlyPolicyCases:
    async def allowed(self, consent, title="Read File"):
        decision = await self.policy(title, {}, context(consent, title))
        return isinstance(decision, PermissionResultAllow)

    async def test_allows_relative_project_file(self):
        self.assertTrue(await self.allowed({"capability": "fs_read", "resource": "changes/2026-11.md",
                                            "workspaceRoot": str(self.root)}))

    async def test_allows_absolute_project_file(self):
        self.assertTrue(await self.allowed({"capability": "fs_read",
                                            "resource": str(self.root / "changes" / "2026-11.md")}))

    async def test_denies_files_outside_the_project(self):
        outside = Path(self.temp.name) / "outside.txt"
        for resource in (str(outside), "../outside.txt", "changes/../../outside.txt", "/proc/self/environ",
                         str(Path.home() / ".aws" / "credentials")):
            with self.subTest(resource=resource):
                self.assertFalse(await self.allowed({"capability": "fs_read", "resource": resource,
                                                     "workspaceRoot": str(self.root)}))

    async def test_denies_symlink_leaving_the_project(self):
        target = Path(self.temp.name) / "secret.txt"
        target.write_text("synthetic")
        (self.root / "link.txt").symlink_to(target)
        self.assertFalse(await self.allowed({"capability": "fs_read", "resource": "link.txt",
                                             "workspaceRoot": str(self.root)}))

    async def test_denies_requests_without_a_reported_file(self):
        self.assertFalse(await self.allowed(None))
        self.assertFalse(await self.allowed({"capability": "fs_read"}))

    async def test_denies_other_capabilities_whatever_the_title(self):
        self.assertFalse(await self.allowed({"capability": "fs_write", "resource": "changes/2026-11.md",
                                             "workspaceRoot": str(self.root)}, title="Read and write"))
        self.assertFalse(await self.allowed({"capability": "shell", "resource": "cat changes/2026-11.md"},
                                            title="Read with shell"))


@unittest.skipUnless(os.name == "posix", "POSIX paths are used")
class LocalExampleTests(ReadOnlyPolicyCases, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        (self.root / "changes").mkdir(parents=True)
        (self.root / "changes" / "2026-11.md").write_text("synthetic")
        module = load("run")
        module.PROJECT = self.root
        self.policy = module.read_only


@unittest.skipUnless(HAS_STARLETTE and os.name == "posix", "install the agentcore extra")
class AgentCoreExampleTests(ReadOnlyPolicyCases, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        (self.root / "changes").mkdir(parents=True)
        (self.root / "changes" / "2026-11.md").write_text("synthetic")
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic-key", "KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS": "true",
                                     "KIRO_AGENTCORE_STATE_DIR": str(Path(self.temp.name) / "state")}):
            module = load("agentcore_app")
        self.policy = module.read_only_in(self.root)


if __name__ == "__main__":
    unittest.main()
