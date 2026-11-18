"""Session-scoped Kiro CLI device authorization without exposing CLI output."""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CODE = re.compile(r"(?i)\bCode:\s*([A-Z0-9]{4}-[A-Z0-9]{4})\b")
_URL = re.compile(r"(?i)\bOpen this URL:\s*(https://[^\s\x1b]+)(?=[ \t\r\n])")


def _verification_url(value: str, identity_provider: Optional[str]) -> bool:
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        provider_host = urlsplit(identity_provider).hostname if identity_provider else None
        allowed = (host == "view.awsapps.com" or host == provider_host or
                   bool(re.fullmatch(r"device\.sso\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?", host)))
        return bool(len(value) <= 2048 and url.scheme == "https" and allowed and
                    url.username is None and url.password is None and url.port in (None, 443))
    except ValueError:
        return False


class DeviceLogin:
    """Own one short-lived login process; never retain or log its raw output."""

    def __init__(self, cli: str, home: Path, env: dict[str, str], *, license: str,
                 identity_provider: Optional[str], region: Optional[str], timeout: float):
        self.cli, self.home, self.env = cli, home, env
        self.license, self.identity_provider, self.region, self.timeout = license, identity_provider, region, timeout
        self.status = "starting"
        self.failure_type: Optional[str] = None
        self.details: Optional[dict[str, str]] = None
        self.challenge = asyncio.get_running_loop().create_future()
        self.process: Optional[asyncio.subprocess.Process] = None
        self._cancelling = False
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        master = slave = None
        transport = None
        try:
            if os.name != "posix":
                raise RuntimeError("Device login requires a POSIX terminal")
            import pty
            master, slave = pty.openpty()
            args = [self.cli, "login", "--license", self.license, "--use-device-flow"]
            if self.license == "pro":
                args.extend(["--identity-provider", self.identity_provider, "--region", self.region])
            env = {**os.environ, **self.env, "TERM": "xterm"}
            launch = asyncio.create_task(asyncio.create_subprocess_exec(*args, stdin=slave, stdout=slave, stderr=slave,
                env=env, cwd=str(self.home), start_new_session=True))
            try:
                self.process = await asyncio.shield(launch)
            except asyncio.CancelledError:
                self.process = await asyncio.shield(launch)
                raise
            os.close(slave)
            slave = None
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            pipe = os.fdopen(os.dup(master), "rb", buffering=0)
            try:
                transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, pipe)
            except BaseException:
                pipe.close()
                raise
            async def consume() -> None:
                buffer = ""
                accepted_url = accepted_region = False
                while True:
                    try:
                        chunk = await reader.read(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer = _ANSI.sub("", buffer + chunk.decode("utf-8", "replace"))[-8192:]
                    if self.license == "pro":
                        if not accepted_url and "Enter Start URL" in buffer:
                            os.write(master, b"\n")
                            accepted_url = True
                        if not accepted_region and re.search(r"(?i)Enter (?:AWS )?Region", buffer):
                            os.write(master, b"\n")
                            accepted_region = True
                    if self.details is None:
                        code, url = _CODE.search(buffer), _URL.search(buffer)
                        if code and url:
                            link = url.group(1)
                            if not _verification_url(link, self.identity_provider):
                                raise ValueError("Untrusted device verification URL")
                            self.details = {"verification_url": link, "user_code": code.group(1).upper()}
                            self.status = "pending"
                            if not self.challenge.done():
                                self.challenge.set_result(True)
                await self.process.wait()
            await asyncio.wait_for(consume(), self.timeout)
            self.status = "authenticated" if self.process.returncode == 0 and self.details else "failed"
        except asyncio.CancelledError:
            self.status = "cancelled"
            raise
        except Exception as exc:
            self.status = "failed"
            self.failure_type = type(exc).__name__
        finally:
            self.details = None
            async def cleanup() -> None:
                try:
                    if self.process is not None:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(self.process.pid, signal.SIGTERM)
                        try:
                            await asyncio.wait_for(self.process.wait(), 3)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            pass
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(self.process.pid, signal.SIGKILL)
                        while self.process.returncode is None:
                            try:
                                await asyncio.shield(self.process.wait())
                            except asyncio.CancelledError:
                                continue
                finally:
                    if transport is not None:
                        transport.close()
                    for fd in (slave, master):
                        if fd is not None:
                            with contextlib.suppress(OSError):
                                os.close(fd)
                    if not self.challenge.done():
                        self.challenge.set_result(None)
            self._cleanup_task = asyncio.create_task(cleanup())
            cancelled = False
            while not self._cleanup_task.done():
                try:
                    await asyncio.shield(self._cleanup_task)
                except asyncio.CancelledError:
                    cancelled = True
            self._cleanup_task.result()
            if cancelled:
                raise asyncio.CancelledError

    async def cancel(self) -> None:
        if not self.task.done() and not self._cancelling:
            self._cancelling = True
            self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(self.task)


async def cli_auth_command(cli: str, home: Path, env: dict[str, str], command: str) -> bool:
    """Return only a success flag; Kiro may print account and token details."""
    process = await asyncio.create_subprocess_exec(cli, command, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, **env}, cwd=str(home), start_new_session=True)
    try:
        return await asyncio.wait_for(process.wait(), 20) == 0
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
