"""Deferred source-only specifications for CodeZip package provenance."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_agentcore_code_zip import BUILD


class CodeZipSourceProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "src" / "kiro_agent_sdk"
        self.stage = self.root / "stage"
        self.source.mkdir(parents=True)
        (self.source / "__init__.py").write_text("version = 'fixture'\n")
        (self.source / "py.typed").touch()
        (self.source / "client.py").write_text("class Client: pass\n")
        self.stage.mkdir()
        shutil.copytree(self.source, self.stage / "kiro_agent_sdk")
        self.patched_root = patch.object(BUILD, "ROOT", self.root)
        self.patched_root.start()
        self.addCleanup(self.patched_root.stop)

    def test_matching_source_records_relative_hashes_without_execution(self):
        BUILD._check_sdk_sources(self.stage)
        manifest = json.loads((self.stage / "sdk-source-manifest.json").read_text())
        package = manifest["packages"]["kiro_agent_sdk"]
        self.assertEqual(package["file_count"], 3)
        self.assertEqual(set(package["files"]), {"__init__.py", "client.py", "py.typed"})
        self.assertNotIn(str(self.root), json.dumps(manifest))

    def test_changed_missing_and_extra_modules_reject_old_images(self):
        package = self.stage / "kiro_agent_sdk"
        for operation in ("changed", "missing", "extra"):
            with self.subTest(operation=operation):
                shutil.rmtree(package)
                shutil.copytree(self.source, package)
                if operation == "changed":
                    (package / "client.py").write_text("old implementation\n")
                elif operation == "missing":
                    (package / "client.py").unlink()
                else:
                    (package / "obsolete.py").write_text("stale module\n")
                with self.assertRaisesRegex(ValueError, "rebuild the image"):
                    BUILD._check_sdk_sources(self.stage)
                self.assertFalse((self.stage / "sdk-source-manifest.json").exists())

    def test_compiled_caches_do_not_change_the_source_digest(self):
        package = self.stage / "kiro_agent_sdk"
        (package / "__pycache__").mkdir()
        (package / "__pycache__" / "client.cpython.pyc").write_bytes(b"compiled cache")
        BUILD._check_sdk_sources(self.stage)
        self.assertEqual(json.loads((self.stage / "sdk-source-manifest.json").read_text())
                         ["packages"]["kiro_agent_sdk"]["file_count"], 3)

    @unittest.skipUnless(os.name == "posix", "POSIX symlinks")
    def test_source_symlinks_and_incomplete_packages_are_rejected(self):
        package = self.stage / "kiro_agent_sdk"
        (package / "client.py").unlink()
        (package / "client.py").symlink_to(self.source / "client.py")
        with self.assertRaisesRegex(ValueError, "regular files"):
            BUILD._check_sdk_sources(self.stage)
        (package / "client.py").unlink()
        (package / "py.typed").unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            BUILD._check_sdk_sources(self.stage)

    def test_existing_manifest_and_source_size_limit_are_rejected(self):
        with patch.object(BUILD, "MAX_SDK_SOURCE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "limits"):
                BUILD._check_sdk_sources(self.stage)
        manifest = self.stage / "sdk-source-manifest.json"
        manifest.write_text("retain")
        with self.assertRaisesRegex(ValueError, "reserved"):
            BUILD._check_sdk_sources(self.stage)
        self.assertEqual(manifest.read_text(), "retain")
