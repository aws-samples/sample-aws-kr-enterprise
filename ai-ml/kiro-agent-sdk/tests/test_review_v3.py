"""v3 regressions. Execution is deferred; these are runnable specifications."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

from kiro_agent_sdk import CLIConnectionError, HookMatcher, KiroAgentOptions, KiroSDKClient
from kiro_agent_sdk.hook_failure import _HookFailureRunner
from kiro_agent_sdk.loop_lock import LazyLoopLock
from kiro_agent_sdk.mcp_output_tracker import McpOutputTracker
from kiro_agent_sdk.native_hook_runtime import DEPENDENCY_GUARD
from kiro_agent_sdk.oauth_errors import sanitized_oauth
from kiro_agent_sdk.mcp import SdkMcpBridge, create_mcp_server, tool


class ReviewV3AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_transformed_schema_error_never_discloses_injected_values(self):
        executed = []
        @tool("probe", "Probe", {"value": str})
        async def probe(args):
            executed.append(args)
            return {"content": []}
        async def inject(name, args):
            return {"value": {"api_key": "PRIVATE_TEST_TOKEN"}}
        bridge = SdkMcpBridge(create_mcp_server("review", tools=[probe]), input_transform=inject)
        try:
            response = await bridge._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "probe", "arguments": {"value": "before"}}})
            self.assertTrue(response["result"]["isError"])
            self.assertNotIn("PRIVATE_TEST_TOKEN", json.dumps(response))
            self.assertNotIn("api_key", json.dumps(response))
            self.assertEqual(executed, [])
        finally:
            await bridge.close()

    @unittest.skipUnless(os.name == "posix", "POSIX inherited stdout")
    async def test_parent_exit_does_not_wait_for_inherited_stdout(self):
        process = await asyncio.create_subprocess_exec(sys.executable, "-c",
            "import os,time\nif os.fork()==0: time.sleep(30)\n",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        client = KiroSDKClient()
        client._process = process
        async def read():
            return [message async for message in client._messages()]
        try:
            self.assertEqual(await asyncio.wait_for(read(), 2), [])
            self.assertEqual(process.returncode, 0)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), 3)
            client._process = None

    async def test_interrupt_during_prompt_hook_prevents_prompt_dispatch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def hook(*_):
            entered.set()
            await release.wait()
            return {}
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "requests"
            client = KiroSDKClient(KiroAgentOptions(cwd=folder,
                cli_path=str(Path(__file__).with_name("fake_kiro.py")), agent_engine="v3",
                env={"KIRO_HOME": str(Path(folder) / "kiro"), "FAKE_LOG_REQUESTS": str(log)},
                hooks={"promptSubmit": [HookMatcher(hooks=[hook])]}))
            operation = None
            try:
                await client.connect()
                operation = asyncio.create_task(client.query("must not reach the model"))
                await asyncio.wait_for(entered.wait(), 2)
                await client.interrupt()
                release.set()
                with self.assertRaises(CLIConnectionError):
                    await asyncio.wait_for(operation, 2)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertFalse(any(call.get("method") == "session/prompt" for call in calls))
            finally:
                release.set()
                if operation is not None:
                    if not operation.done():
                        operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                await client.disconnect()

    async def test_resistant_callback_close_is_incomplete_and_retry_succeeds(self):
        runner = _HookFailureRunner()
        release = asyncio.Event()
        async def callback(*_):
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return {"late": True}
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await runner.invoke(callback, {}, .01)
            with self.assertRaises(CLIConnectionError):
                await asyncio.wait_for(runner.close(), .5)
            with self.assertRaises(CLIConnectionError):
                runner.reopen()
        finally:
            release.set()
            await asyncio.gather(*tuple(runner.tasks), return_exceptions=True)
            await runner.close()
        runner.reopen()

    async def test_disconnect_cancellation_does_not_cancel_application_task(self):
        lock = LazyLoopLock()
        entered = asyncio.Event()
        continued = []
        async def application():
            try:
                async with lock:
                    entered.set()
                    await asyncio.Event().wait()
            except CLIConnectionError:
                continued.append("after SDK call")
        task = asyncio.create_task(application())
        await entered.wait()
        lock.cancel_others({asyncio.current_task()})
        await asyncio.wait_for(task, 1)
        self.assertFalse(task.cancelled())
        self.assertEqual(continued, ["after SDK call"])
        self.assertFalse(lock.locked())

    async def test_user_cancellation_still_propagates(self):
        lock = LazyLoopLock()
        entered = asyncio.Event()
        async def application():
            async with lock:
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(application())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_direct_prompt_hook_reentry_fails_before_waiting_for_query_lock(self):
        async def hook(*_):
            await client.query("nested")
            return {}
        client = KiroSDKClient(KiroAgentOptions(hooks={"promptSubmit": [HookMatcher(hooks=[hook])]}))
        try:
            async with client._query_lock:
                with self.assertRaises(CLIConnectionError):
                    await asyncio.wait_for(client._run_hooks("UserPromptSubmit", {"prompt": "outer"}), .5)
        finally:
            await client.disconnect()

    async def test_oauth_error_does_not_retain_transport_exception(self):
        async def failed():
            secret_request = {"token": "PRIVATE_TEST_TOKEN"}
            error = RuntimeError("private")
            error.request = secret_request
            raise error
        with self.assertRaises(ValueError) as caught:
            await sanitized_oauth(failed(), "OAuth failed")
        error = caught.exception
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assertNotIn("PRIVATE", str(error))

    async def test_cancelled_mcp_id_is_rejected_without_poisoning_next_request(self):
        sent, failures = [], []
        async def transform(*_):
            return {"content": []}
        async def send(message):
            sent.append(message)
        tracker = McpOutputTracker(transform, send, lambda: failures.append(True))
        def call(ident):
            return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                    "params": {"name": "probe", "arguments": {}}}
        try:
            self.assertTrue(await tracker.request(call(1)))
            await tracker.cancel(1)
            self.assertFalse(await tracker.request(call(1)))
            self.assertEqual(sent[-1]["error"]["code"], -32600)
            self.assertTrue(await tracker.request(call(2)))
            self.assertEqual(failures, [])
        finally:
            await tracker.close()


class ReviewV3StaticContractTests(unittest.TestCase):

    @unittest.skipUnless(os.name == "posix", "owned POSIX process group")
    def test_watchdog_kills_term_resistant_child_after_leader_exit(self):
        runner = Path(__file__).resolve().parents[1] / "scripts/run_tests_bounded.py"
        if not runner.exists():
            self.skipTest("source runner is not installed in a wheel")
        spec = importlib.util.spec_from_file_location("bounded_runner_v3", runner)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "child"
            code = ("import os,signal,time,sys\n"
                    "pid=os.fork()\n"
                    "if pid==0:\n"
                    " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                    " open(sys.argv[1],'w').write(str(os.getpid()))\n"
                    " time.sleep(60)\n"
                    "else:\n"
                    " while not os.path.exists(sys.argv[1]): time.sleep(.01)\n")
            process = subprocess.Popen([sys.executable, "-c", code, str(pidfile)], start_new_session=True)
            try:
                process.wait(timeout=5)
                child = int(pidfile.read_text())
                module.stop(process)
                # A killed orphan may remain a zombie until its reaper runs.
                status = Path(f"/proc/{child}/stat")
                if status.exists():
                    import time
                    deadline = time.monotonic() + 2
                    while status.exists() and status.read_text().split(") ", 1)[1][0] != "Z":
                        if time.monotonic() > deadline:
                            self.fail("test child survived watchdog cleanup")
                        time.sleep(.01)
            finally:
                module.stop(process)

    def test_runtime_refuses_optional_module_from_ancestor_directory(self):
        node = shutil.which("node") or str(Path.home() / ".local/share/kiro-cli/node")
        if not Path(node).is_file():
            self.skipTest("Node unavailable")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage, deps = root / "stage", root / "dependencies"
            stage.mkdir()
            deps.mkdir()
            (stage / "node_modules").symlink_to(deps, target_is_directory=True)
            malicious = root / "node_modules" / "bufferutil"
            malicious.mkdir(parents=True)
            sentinel = root / "executed"
            (malicious / "index.js").write_text(
                "require('fs').writeFileSync(" + json.dumps(str(sentinel)) + ",'unsafe');")
            allowed = deps / "allowed"
            allowed.mkdir()
            (allowed / "index.js").write_text("module.exports=42;")
            entry = stage / "entry.mjs"
            entry.write_text(DEPENDENCY_GUARD + """
const require=kiroSdkModule.createRequire(import.meta.url);
try{require("bufferutil");process.exitCode=9}
catch(error){if(error.code!=="MODULE_NOT_FOUND")throw error}
try{await import("bufferutil");process.exitCode=10}
catch(error){if(error.code!=="MODULE_NOT_FOUND")throw error}
if(require("allowed")!==42||(await import("allowed")).default!==42)process.exitCode=11;
""")
            result = subprocess.run([node, str(entry)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
