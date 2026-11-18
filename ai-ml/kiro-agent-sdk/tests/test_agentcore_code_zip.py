import importlib.util
import asyncio
import concurrent.futures
import hashlib
import json
import lzma
import os
import stat
import tempfile
import traceback
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BUILD_SPEC = importlib.util.spec_from_file_location("codezip_builder", ROOT / "scripts" / "build_agentcore_code_zip.py")
BUILD = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(BUILD)
ENTRY_SPEC = importlib.util.spec_from_file_location("codezip_entry", ROOT / "scripts" / "agentcore_code_entry.py")
ENTRY = importlib.util.module_from_spec(ENTRY_SPEC)
ENTRY_SPEC.loader.exec_module(ENTRY)


def elf(machine=183):
    return b"\x7fELF\x02\x01\x01" + b"\0" * 11 + machine.to_bytes(2, "little") + b"test"


class CodeZipTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("starlette"), "install the agentcore extra")
    def test_compressed_runtime_cache_round_trip_and_integrity(self):
        from kiro_agent_sdk.agentcore import _copy_runtime_cache
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            (cache / "kas" / "bundle").mkdir(parents=True)
            (cache / "kas" / "bundle" / "server.js").write_text("server")
            node = elf() + b"runtime"
            (cache / "node").write_bytes(node)
            (cache / "node.sha256").write_text(hashlib.sha256(node).hexdigest())
            BUILD._compress_runtime_node(cache)
            self.assertFalse((cache / "node").exists())
            self.assertEqual(lzma.decompress((cache / "node.xz").read_bytes()), node)
            home = root / "home"
            _copy_runtime_cache(cache, home)
            target = home / ".local/share/kiro-cli"
            self.assertEqual((target / "node").read_bytes(), node)
            self.assertEqual(stat.S_IMODE((target / "node").stat().st_mode), 0o755)
            self.assertTrue((target / "kas" / "bundle" / "server.js").exists())
            _copy_runtime_cache(cache, home)
            (cache / "node.sha256").write_text("0" * 64)
            with self.assertRaisesRegex(ValueError, "integrity"):
                _copy_runtime_cache(cache, root / "other")
            failed = root / "other/.local/share/kiro-cli"
            self.assertFalse((failed / "node").exists())
            self.assertEqual(list(failed.glob(".runtime-*")), [])
            (cache / "node.sha256").write_text(hashlib.sha256(node).hexdigest())
            with patch("kiro_agent_sdk.agentcore._MAX_RUNTIME_NODE_BYTES", len(node) - 1):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    _copy_runtime_cache(cache, root / "limited")
            (cache / "node.sha256").write_text("0" * 129)
            with self.assertRaisesRegex(ValueError, "checksum"):
                _copy_runtime_cache(cache, root / "bad-checksum")
            (cache / "node.sha256").write_text(hashlib.sha256(node).hexdigest())
            (cache / "node.xz").write_bytes(b"corrupt")
            with self.assertRaises((lzma.LZMAError, ValueError)):
                _copy_runtime_cache(cache, root / "corrupt")
            self.assertFalse((root / "corrupt/.local/share/kiro-cli/node").exists())
            compressed = lzma.compress(node)
            (cache / "node.xz").write_bytes(compressed[:-4])
            with self.assertRaisesRegex(ValueError, "truncated"):
                _copy_runtime_cache(cache, root / "truncated")
            (cache / "node.xz").write_bytes(compressed + b"extra")
            with self.assertRaisesRegex(ValueError, "trailing"):
                _copy_runtime_cache(cache, root / "trailing")
            (cache / "node.xz").write_bytes(compressed)
            with patch("kiro_agent_sdk.agentcore._MAX_RUNTIME_NODE_DECODER_MEMORY", 1024):
                with self.assertRaises(lzma.LZMAError):
                    _copy_runtime_cache(cache, root / "limited-decoder")
            (cache / "node").write_bytes(node)
            with self.assertRaisesRegex(ValueError, "both node"):
                _copy_runtime_cache(cache, root / "ambiguous")

    def test_runtime_node_compression_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            node = elf() + b"runtime"
            (cache / "node").write_bytes(node)
            (cache / "node.sha256").write_text("0" * 64)
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                BUILD._compress_runtime_node(cache)
            self.assertTrue((cache / "node").exists())
            self.assertFalse((cache / "node.xz").exists())
            (cache / "node.sha256").write_text(hashlib.sha256(node).hexdigest())
            with patch.object(BUILD, "MAX_RUNTIME_NODE_BYTES", len(node) - 1):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    BUILD._compress_runtime_node(cache)
            self.assertTrue((cache / "node").exists())
            self.assertFalse((cache / "node.xz").exists())
            (cache / "node.sha256").write_text("0" * 129)
            with self.assertRaisesRegex(ValueError, "checksum"):
                BUILD._compress_runtime_node(cache)
            (cache / "node.sha256").unlink()
            BUILD._compress_runtime_node(cache)
            self.assertEqual((cache / "node.sha256").read_text(), hashlib.sha256(node).hexdigest())
            self.assertEqual(lzma.decompress((cache / "node.xz").read_bytes()), node)

    def test_image_must_include_agentcore_and_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            with self.assertRaisesRegex(ValueError, "AgentCore adapter"):
                BUILD._check_stage_packages(stage)
            (stage / "kiro_agent_sdk").mkdir()
            (stage / "kiro_agent_sdk" / "agentcore.py").touch()
            with self.assertRaisesRegex(ValueError, "does not support compressed"):
                BUILD._check_stage_packages(stage)
            (stage / "kiro_agent_sdk" / "agentcore.py").write_text("_CODEZIP_COMPRESSED_NODE_VERSION = 1\n")
            with self.assertRaisesRegex(ValueError, "PyYAML"):
                BUILD._check_stage_packages(stage)
            (stage / "yaml").mkdir()
            (stage / "yaml" / "__init__.py").touch()
            BUILD._check_stage_packages(stage)

    def test_custom_app_source_is_explicit_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            stage.mkdir()
            source = root / "app.py"
            source.write_text("def create_app(config):\n    return config\n", encoding="utf-8")
            BUILD._stage_custom_app(stage, source)
            self.assertEqual((stage / "kiro_agentcore_app.py").read_bytes(), source.read_bytes())
            with self.assertRaisesRegex(ValueError, "reserved"):
                BUILD._stage_custom_app(stage, source)
            (stage / "kiro_agentcore_app.py").unlink()
            with patch.object(BUILD, "MAX_APP_SOURCE_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "at most"):
                    BUILD._stage_custom_app(stage, source)
            with self.assertRaisesRegex(ValueError, "regular Python"):
                BUILD._stage_custom_app(stage, root / "not-python.txt")
            if hasattr(os, "symlink"):
                link = root / "link.py"
                link.symlink_to(source)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    BUILD._stage_custom_app(stage, link)
            if hasattr(os, "mkfifo"):
                fifo = root / "fifo.py"
                os.mkfifo(fifo)
                with self.assertRaisesRegex(ValueError, "regular Python"):
                    BUILD._stage_custom_app(stage, fifo)

    @unittest.skipUnless(importlib.util.find_spec("starlette"), "install the agentcore extra")
    def test_custom_app_factory_fails_closed_without_exposing_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = lambda scope, receive, send: None
            source = root / "kiro_agentcore_app.py"
            source.write_text("def create_app(config):\n    return config\n", encoding="utf-8")
            self.assertIs(ENTRY._load_agentcore_app(root, config), config)
            for contents in ("pass\n", "def create_app(config):\n    return None\n",
                             "async def create_app(config):\n    return config\n",
                             "def create_app(config):\n    raise RuntimeError('private-detail')\n"):
                source.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "factory failed") as caught:
                    ENTRY._load_agentcore_app(root, config)
                self.assertNotIn("private-detail", "".join(traceback.format_exception(
                    type(caught.exception), caught.exception, caught.exception.__traceback__)))

    @unittest.skipUnless(importlib.util.find_spec("starlette"), "install the agentcore extra")
    def test_missing_custom_app_uses_default_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            config = object()
            app = object()
            with patch("kiro_agent_sdk.agentcore.create_agentcore_app", return_value=app) as factory:
                self.assertIs(ENTRY._load_agentcore_app(Path(directory), config), app)
                factory.assert_called_once_with(config)

    def test_binary_bundle_round_trip_and_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "kiro-bundle"
            bundle.mkdir()
            for name in ("kiro-cli", "kiro-cli-chat", "kiro-cli-term"):
                (bundle / name).write_bytes(elf() + name.encode())
            BUILD._prepare_binary_bundle(root)
            target = ENTRY._prepare_binaries(root)
            try:
                for name in ("kiro-cli", "kiro-cli-chat", "kiro-cli-term"):
                    self.assertEqual((target / name).read_bytes(), elf() + name.encode())
                    self.assertEqual(stat.S_IMODE((target / name).stat().st_mode), 0o755)
            finally:
                import shutil
                shutil.rmtree(target)
            manifest = json.loads((bundle / "manifest.json").read_text())
            manifest["kiro-cli-chat"]["sha256"] = "0" * 64
            (bundle / "manifest.json").write_text(json.dumps(manifest))
            failed = root / "failed-bin"
            failed.mkdir()
            with patch.object(ENTRY.tempfile, "mkdtemp", return_value=str(failed)):
                with self.assertRaisesRegex(ValueError, "integrity"):
                    ENTRY._prepare_binaries(root)
            self.assertFalse(failed.exists())

    def test_rejects_wrong_architecture_and_invalid_native_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong = root / "wrong"
            wrong.write_bytes(elf(62))
            with self.assertRaisesRegex(ValueError, "Non-ARM64"):
                BUILD._check_arm64(wrong)
            native = root / "bad.so"
            native.write_text("not an ELF library")
            with self.assertRaisesRegex(ValueError, "Invalid native"):
                BUILD._check_arm64(native)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "symlink"), "requires POSIX symlinks")
    def test_archive_preserves_safe_symlinks_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            stage.mkdir()
            (stage / "binary").write_bytes(elf())
            (stage / "binary").chmod(0o755)
            (stage / "alias").symlink_to("binary")
            output = root / "archive.zip"
            BUILD._write_archive(stage, output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(stat.S_IFMT(archive.getinfo("alias").external_attr >> 16), stat.S_IFLNK)
                self.assertEqual(archive.read("alias"), b"binary")
                self.assertEqual(stat.S_IMODE(archive.getinfo("binary").external_attr >> 16), 0o755)
            outside = root / "outside"
            outside.write_text("outside")
            (stage / "escape").symlink_to("../outside")
            with self.assertRaisesRegex(ValueError, "Unsafe symlink"):
                list(BUILD._files(stage))

    def test_archive_size_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            stage.mkdir()
            (stage / "file").write_text("small")
            with patch.object(BUILD, "MAX_EXPANDED_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "expanded"):
                    BUILD._write_archive(stage, root / "archive.zip")
            with patch.object(BUILD, "MAX_ZIP_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "compressed"):
                    BUILD._write_archive(stage, root / "archive.zip")


class CodeZipStartupTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec("starlette"), "install the agentcore extra")
    async def test_bundled_custom_app_enforces_session_owner_and_enables_controls(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig
        from test_agentcore import HEADER, request

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "app.py"
            source.write_text('''from kiro_agent_sdk import KiroAgentOptions
from kiro_agent_sdk.agentcore import create_agentcore_app
async def owner(request, session_id):
    return request.headers.get("x-test-owner") == "owner"
def create_app(config):
    def options(workspace, resume):
        return KiroAgentOptions(tools=[], enable_kiro_code_intelligence=True)
    return create_agentcore_app(config, options_factory=options, session_authorizer=owner)
''', encoding="utf-8")
            stage = root / "stage"
            stage.mkdir()
            BUILD._stage_custom_app(stage, source)
            archive_path = root / "bundle.zip"
            BUILD._write_archive(stage, archive_path)
            extracted = root / "extracted"
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(extracted)
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "state",
                                     cli_path=str(ROOT / "tests" / "fake_kiro.py"), api_key="synthetic")
            app = ENTRY._load_agentcore_app(extracted, config)
            owner = [(HEADER, b"session-a"), (b"x-test-owner", b"owner")]
            other = [(HEADER, b"session-a"), (b"x-test-owner", b"other")]
            try:
                denied = await request(app, "POST", "/invocations", b'{"prompt":"no"}', other)
                self.assertEqual(denied[0], 403)
                self.assertEqual((await request(app, "POST", "/invocations", b'{"prompt":"ok"}', owner))[0], 200)
                status, body, headers = await request(app, "POST", "/invocations",
                                                      b'{"action":"code_status"}', owner)
                self.assertEqual((status, json.loads(body)["result"]["success"]), (200, True))
                self.assertEqual(headers[b"cache-control"], b"no-store")
            finally:
                await app.state.kiro_agentcore.close()

    async def test_health_is_immediate_and_invocation_waits_and_replays_body(self):
        prepared = concurrent.futures.Future()
        called = []
        async def app(scope, receive, send):
            called.append(scope["path"])
            if scope["path"] == "/invocations":
                called.append((await receive())["body"])
        wrapped = ENTRY._wait_for_preparation(app, prepared, 16)
        queue = asyncio.Queue()
        await queue.put({"type": "http.request", "body": b"hello", "more_body": False})
        async def send(message):
            pass
        await wrapped({"type": "http", "path": "/ping"}, queue.get, send)
        self.assertEqual(called, ["/ping"])
        task = asyncio.create_task(wrapped({"type": "http", "path": "/invocations"}, queue.get, send))
        await asyncio.sleep(0.01)
        self.assertEqual(called, ["/ping"])
        prepared.set_result(None)
        await task
        self.assertEqual(called, ["/ping", "/invocations", b"hello"])

    async def test_disconnect_during_preparation_does_not_invoke(self):
        prepared = concurrent.futures.Future()
        called = []
        async def app(scope, receive, send):
            called.append(True)
        wrapped = ENTRY._wait_for_preparation(app, prepared, 16)
        queue = asyncio.Queue()
        await queue.put({"type": "http.disconnect"})
        async def send(message):
            pass
        await wrapped({"type": "http", "path": "/invocations"}, queue.get, send)
        prepared.set_result(None)
        await asyncio.sleep(0)
        self.assertFalse(called)

    async def test_failed_preparation_and_oversized_body_are_rejected(self):
        for fail in (False, True):
            prepared = concurrent.futures.Future()
            called, sent = [], []
            async def app(scope, receive, send):
                called.append(True)
            wrapped = ENTRY._wait_for_preparation(app, prepared, 3)
            queue = asyncio.Queue()
            if fail:
                prepared.set_exception(ValueError("not logged"))
            else:
                await queue.put({"type": "http.request", "body": b"large", "more_body": False})
            async def send(message):
                sent.append(message)
            await wrapped({"type": "http", "path": "/invocations"}, queue.get, send)
            self.assertFalse(called)
            self.assertEqual(sent[0]["status"], 503 if fail else 413)

    async def test_preparation_failure_while_request_waits_returns_503(self):
        prepared = concurrent.futures.Future()
        sent, called = [], []
        async def app(scope, receive, send):
            called.append(True)
        async def send(message):
            sent.append(message)
        queue = asyncio.Queue()
        await queue.put({"type": "http.request", "body": b"ok", "more_body": False})
        task = asyncio.create_task(ENTRY._wait_for_preparation(app, prepared, 16)(
            {"type": "http", "path": "/invocations"}, queue.get, send))
        await asyncio.sleep(0.01)
        prepared.set_exception(ValueError("not logged"))
        await task
        self.assertFalse(called)
        self.assertEqual(sent[0]["status"], 503)


if __name__ == "__main__":
    unittest.main()
