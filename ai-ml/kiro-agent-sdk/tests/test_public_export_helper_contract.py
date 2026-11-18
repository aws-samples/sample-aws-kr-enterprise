"""Small exported helpers whose contracts are otherwise only exercised indirectly."""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import InMemoryMcpOAuthStorage, McpOAuthConfig, project_key_for_directory


async def authorize(url):
    return url


class InMemoryOAuthStorageContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_tokens_and_client_info_are_process_local_per_instance(self):
        first, second = InMemoryMcpOAuthStorage(), InMemoryMcpOAuthStorage()
        tokens, client = object(), object()
        self.assertIsNone(await first.get_tokens())
        self.assertIsNone(await first.get_client_info())
        self.assertIsNone(await first.set_tokens(tokens))
        self.assertIsNone(await first.set_client_info(client))
        self.assertIs(await first.get_tokens(), tokens)
        self.assertIs(await first.get_client_info(), client)
        self.assertIsNone(await second.get_tokens())
        self.assertIsNone(await second.get_client_info())
        self.assertIsNone(await first.clear_tokens())
        self.assertIsNone(await first.get_tokens())
        self.assertIs(await first.get_client_info(), client)

    def test_each_oauth_config_gets_its_own_default_storage(self):
        one = McpOAuthConfig("http://127.0.0.1:9999/callback", authorize)
        two = McpOAuthConfig("http://127.0.0.1:9999/callback", authorize)
        self.assertIsInstance(one.storage, InMemoryMcpOAuthStorage)
        self.assertIsNot(one.storage, two.storage)
        self.assertNotIn("storage", repr(one))


class ProjectKeyContractTests(unittest.TestCase):
    def test_key_is_an_opaque_digest_of_the_resolved_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            key = project_key_for_directory(str(first))
            self.assertRegex(key, re.compile(r"^[0-9a-f]{64}$"))
            self.assertNotIn("first", key)
            self.assertEqual(project_key_for_directory(first), key)
            self.assertEqual(project_key_for_directory(str(root / "second" / ".." / "first")), key)
            self.assertNotEqual(project_key_for_directory(second), key)
            if hasattr(os, "symlink"):
                alias = root / "alias"
                alias.symlink_to(first, target_is_directory=True)
                self.assertEqual(project_key_for_directory(alias), key)
            with patch("os.getcwd", return_value=str(first)):
                self.assertEqual(project_key_for_directory(), key)
                self.assertEqual(project_key_for_directory(None), key)


if __name__ == "__main__":
    unittest.main()
