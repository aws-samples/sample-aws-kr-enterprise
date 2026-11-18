import json
import os
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import AgentDefinition, KiroAgentOptions, KiroSDKClient


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class AgentSkillTests(unittest.IsolatedAsyncioTestCase):

    async def test_preloads_workspace_skill_before_kiro_home_skill(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "workspace"
            project = workspace / ".kiro" / "skills" / "review" / "SKILL.md"
            home = Path(root) / "kiro" / "skills" / "review" / "SKILL.md"
            for path in (project, home):
                path.parent.mkdir(parents=True)
                path.write_text("Test skill", encoding="utf-8")
            definition = AgentDefinition("Reviewer", "Inspect", skills=["review", "review"],
                                         kiro_resources=["skill://existing"])
            options = KiroAgentOptions(cwd=workspace, cli_path=FAKE, agent_engine="v3",
                                       env={"KIRO_HOME": str(Path(root) / "kiro")},
                                       agents={"reviewer": definition})
            path = Path(root) / "kiro" / "agents" / "reviewer.json"
            async with KiroSDKClient(options):
                resources = json.loads(path.read_text())["resources"]
                self.assertEqual(resources, ["skill://existing", project.resolve().as_uri()])
            self.assertEqual(definition.kiro_resources, ["skill://existing"])
            self.assertFalse(path.exists())

    async def test_missing_and_invalid_names_fail_before_creating_profile(self):
        with tempfile.TemporaryDirectory() as root:
            for skills in (["../escape"], ["missing-skill"], "not-a-list", [""]):
                with self.subTest(skills=skills):
                    options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                        env={"KIRO_HOME": str(Path(root) / "kiro")},
                        agents={"reviewer": AgentDefinition("Test", "Inspect", skills=skills)})
                    with self.assertRaises(ValueError):
                        await KiroSDKClient(options).connect()
                    self.assertFalse((Path(root) / "kiro" / "agents" / "reviewer.json").exists())

    async def test_kiro_home_skill_is_resolved(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "workspace"
            workspace.mkdir()
            skill = Path(root) / "kiro" / "skills" / "sdk_unique_skill_471" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("Local skill", encoding="utf-8")
            options = KiroAgentOptions(cwd=workspace, cli_path=FAKE, agent_engine="v3",
                env={"KIRO_HOME": str(Path(root) / "kiro")},
                agents={"reviewer": AgentDefinition("Test", "Inspect", skills=["sdk_unique_skill_471"])})
            profile = Path(root) / "kiro" / "agents" / "reviewer.json"
            async with KiroSDKClient(options):
                self.assertEqual(json.loads(profile.read_text())["resources"], [skill.resolve().as_uri()])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    async def test_skill_symlink_cannot_escape_skill_root(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "workspace"
            workspace.mkdir()
            skill_root = workspace / ".kiro" / "skills"
            skill_root.mkdir(parents=True)
            outside = Path(root) / "outside"
            outside.mkdir()
            (outside / "SKILL.md").write_text("outside", encoding="utf-8")
            try:
                (skill_root / "unsafe").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            options = KiroAgentOptions(cwd=workspace, cli_path=FAKE, agent_engine="v3",
                env={"KIRO_HOME": str(Path(root) / "kiro")},
                agents={"reviewer": AgentDefinition("Test", "Inspect", skills=["unsafe"])})
            with self.assertRaisesRegex(ValueError, "outside"):
                await KiroSDKClient(options).connect()


if __name__ == "__main__":
    unittest.main()
