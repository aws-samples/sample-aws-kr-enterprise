import asyncio
import contextlib
import os
import signal
import sys
import unittest
from unittest.mock import patch

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient
from kiro_agent_sdk.client import _launch_cleanup_tasks, _spawn_cli, _track_process_cleanup


@unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
class ProcessCleanupOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_signals_child_before_waiting_for_full_stdin_pipe(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        client = KiroSDKClient(KiroAgentOptions())
        client._process = process
        client._connected = True
        try:
            process.stdin.write(b"x" * (2 * 1024 * 1024))
            await asyncio.sleep(0.05)
            await asyncio.wait_for(client.disconnect(), 5)
            self.assertIsNotNone(process.returncode)
            self.assertFalse(client._connected)
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await asyncio.wait_for(process.wait(), 5)

    async def test_disconnect_kills_child_that_ignores_term_and_does_not_read_stdin(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        client = KiroSDKClient(KiroAgentOptions())
        client._process = process
        client._connected = True
        try:
            self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b"ready\n")
            process.stdin.write(b"x" * (2 * 1024 * 1024))
            await asyncio.sleep(0.05)
            await asyncio.wait_for(client.disconnect(), 10)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertFalse(client._connected)
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await asyncio.wait_for(process.wait(), 5)

    async def test_disconnect_kills_descendant_holding_stdin_after_parent_exits(self):
        script = (
            "import os,signal,time\n"
            "pid=os.fork()\n"
            "if pid == 0:\n"
            " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            " print('ready',flush=True)\n"
            " os.close(1); os.close(2); time.sleep(60)\n"
            "else:\n"
            " time.sleep(60)\n")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        client = KiroSDKClient(KiroAgentOptions())
        client._process = process
        client._connected = True
        try:
            self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b"ready\n")
            process.stdin.write(b"x" * (2 * 1024 * 1024))
            await asyncio.sleep(0.05)
            await asyncio.wait_for(client.disconnect(), 10)
            self.assertEqual(process.returncode, -signal.SIGTERM)
            self.assertFalse(client._connected)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.returncode is None:
                await asyncio.wait_for(process.wait(), 5)

    async def test_cancelled_spawn_signals_child_before_waiting_for_stdin_close(self):
        released, launched, terminated = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Pipe:
            closed = False

            def close(self):
                self.closed = True

            async def wait_closed(self):
                await terminated.wait()

        class Process:
            pid = 123456
            returncode = None
            stdin = Pipe()

            async def wait(self):
                await terminated.wait()
                return self.returncode

        process = Process()

        async def spawn(*args, **kwargs):
            launched.set()
            await released.wait()
            return process

        def terminate(pid, sig):
            self.assertEqual((pid, sig), (process.pid, signal.SIGTERM))
            self.assertTrue(process.stdin.closed)
            process.returncode = -sig
            terminated.set()

        with patch("kiro_agent_sdk.client.asyncio.create_subprocess_exec", side_effect=spawn), \
                patch("kiro_agent_sdk.client.os.killpg", side_effect=terminate):
            operation = asyncio.create_task(_spawn_cli("fake", start_new_session=True))
            try:
                await asyncio.wait_for(launched.wait(), 5)
                operation.cancel()
                released.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(operation, 2)
                self.assertTrue(terminated.is_set())
            finally:
                released.set()
                terminated.set()
                if not operation.done():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)

    async def test_cancelled_spawn_kills_child_that_ignores_term_with_full_stdin(self):
        released, launched = asyncio.Event(), asyncio.Event()
        created = []
        create = asyncio.create_subprocess_exec

        async def delayed_spawn(*args, **kwargs):
            process = await create(
                sys.executable, "-c",
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            created.append(process)
            await asyncio.wait_for(process.stdout.readline(), 5)
            process.stdin.write(b"x" * (2 * 1024 * 1024))
            launched.set()
            await released.wait()
            return process

        with patch("kiro_agent_sdk.client.asyncio.create_subprocess_exec", side_effect=delayed_spawn):
            operation = asyncio.create_task(_spawn_cli("fake", start_new_session=True))
            try:
                await asyncio.wait_for(launched.wait(), 5)
                operation.cancel()
                released.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(operation, 10)
                self.assertEqual(created[0].returncode, -signal.SIGKILL)
            finally:
                released.set()
                if created and created[0].returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(created[0].pid, signal.SIGKILL)
                if not operation.done():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                if created and created[0].returncode is None:
                    await asyncio.wait_for(created[0].wait(), 5)

    async def test_unfinished_cleanup_reports_error_and_keeps_waiters_owned(self):
        release = asyncio.Event()
        signals = []

        class Pipe:
            def close(self): pass
            async def wait_closed(self):
                await release.wait()

        class Process:
            pid = 123456
            returncode = None
            stdin = Pipe()
            async def wait(self):
                await release.wait()
                self.returncode = -signal.SIGKILL
                return self.returncode

        process = Process()
        client = KiroSDKClient(KiroAgentOptions())
        client._process = process
        client._connected = True
        before = set(_launch_cleanup_tasks)
        with patch("kiro_agent_sdk.client._PROCESS_CLEANUP_TIMEOUT", 0.05), \
                patch("kiro_agent_sdk.client.os.killpg", side_effect=lambda pid, sig: signals.append((pid, sig))):
            try:
                with self.assertRaisesRegex(CLIConnectionError, "cleanup did not finish"):
                    await asyncio.wait_for(client.disconnect(), 2)
                self.assertEqual(signals, [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)])
                self.assertTrue(client._connected)
                pending = set(_launch_cleanup_tasks) - before
                self.assertEqual(len(pending), 2)
                release.set()
                await asyncio.wait_for(asyncio.gather(*pending), 2)
                await asyncio.wait_for(client.disconnect(), 2)
                self.assertFalse(client._connected)
            finally:
                release.set()

    async def test_repeated_cancel_does_not_abandon_pending_spawn_cleanup(self):
        launch, release, terminated = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Pipe:
            def close(self): pass
            async def wait_closed(self):
                await terminated.wait()

        class Process:
            pid = 123456
            returncode = None
            stdin = Pipe()
            async def wait(self):
                await terminated.wait()
                return self.returncode

        process = Process()

        async def spawn(*args, **kwargs):
            launch.set()
            await release.wait()
            return process

        def terminate(pid, sig):
            process.returncode = -sig
            terminated.set()

        with patch("kiro_agent_sdk.client.asyncio.create_subprocess_exec", side_effect=spawn), \
                patch("kiro_agent_sdk.client.os.killpg", side_effect=terminate):
            operation = asyncio.create_task(_spawn_cli("fake", start_new_session=True))
            try:
                await asyncio.wait_for(launch.wait(), 5)
                operation.cancel()
                await asyncio.sleep(0)
                operation.cancel()
                await asyncio.sleep(0)
                self.assertFalse(operation.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(operation, 2)
                self.assertTrue(terminated.is_set())
            finally:
                release.set()
                terminated.set()
                if not operation.done():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)

    async def test_cancelled_spawn_with_unfinished_cleanup_reports_failure(self):
        launched, release_spawn, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Pipe:
            def close(self): pass
            async def wait_closed(self):
                await release_cleanup.wait()

        class Process:
            pid = 123456
            returncode = None
            stdin = Pipe()
            async def wait(self):
                await release_cleanup.wait()
                self.returncode = -signal.SIGKILL
                return self.returncode

        async def spawn(*args, **kwargs):
            launched.set()
            await release_spawn.wait()
            return Process()

        before = set(_launch_cleanup_tasks)
        with patch("kiro_agent_sdk.client._PROCESS_CLEANUP_TIMEOUT", 0.05), \
                patch("kiro_agent_sdk.client.asyncio.create_subprocess_exec", side_effect=spawn), \
                patch("kiro_agent_sdk.client.os.killpg"):
            operation = asyncio.create_task(_spawn_cli("fake", start_new_session=True))
            try:
                await asyncio.wait_for(launched.wait(), 5)
                operation.cancel()
                release_spawn.set()
                with self.assertRaisesRegex(CLIConnectionError, "cleanup failed"):
                    await asyncio.wait_for(operation, 2)
                pending = {item for item in _launch_cleanup_tasks - before if not item.done()}
                self.assertEqual(len(pending), 2)
                release_cleanup.set()
                await asyncio.wait_for(asyncio.gather(*pending), 2)
            finally:
                release_spawn.set()
                release_cleanup.set()
                if not operation.done():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)

    async def test_pending_cleanup_capacity_blocks_new_spawns_until_resolved(self):
        release = asyncio.Event()
        pending = asyncio.create_task(release.wait())
        _track_process_cleanup(pending)
        marker = object()

        async def spawn(*args, **kwargs):
            return marker

        with patch("kiro_agent_sdk.client._MAX_PENDING_PROCESS_CLEANUPS", 1), \
                patch("kiro_agent_sdk.client.asyncio.create_subprocess_exec", side_effect=spawn) as create:
            try:
                with self.assertRaisesRegex(CLIConnectionError, "cleanup capacity"):
                    await _spawn_cli("fake")
                create.assert_not_called()
                release.set()
                await pending
                await asyncio.sleep(0)
                self.assertIs(await _spawn_cli("fake"), marker)
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
