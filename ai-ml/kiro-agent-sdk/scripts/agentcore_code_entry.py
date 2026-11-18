"""Bootstrap a CodeZip bundle on an ARM64 AgentCore Python 3.12 runtime."""
from __future__ import annotations

import atexit
import asyncio
from collections import deque
import concurrent.futures
import contextlib
import hashlib
import importlib.util
import inspect
import json
import lzma
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _load_agentcore_app(root: Path, config):
    """Load the explicitly bundled app factory, or the default adapter."""
    from kiro_agent_sdk.agentcore import create_agentcore_app

    source = root / "kiro_agentcore_app.py"
    if not source.exists() and not source.is_symlink():
        return create_agentcore_app(config)
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("Custom AgentCore app must be a regular file")
    module_name = "_kiro_agentcore_embedded_" + hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
    try:
        spec = importlib.util.spec_from_file_location(module_name, source)
        if spec is None or spec.loader is None:
            raise ValueError("App module could not be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        factory = getattr(module, "create_app", None)
        if not callable(factory):
            raise ValueError("App module must define create_app(config)")
        app = factory(config)
        if inspect.isawaitable(app):
            if inspect.iscoroutine(app):
                app.close()
            raise ValueError("App factory must return an ASGI app synchronously")
        if not callable(app):
            raise ValueError("App factory must return an ASGI app")
        return app
    except Exception:
        sys.modules.pop(module_name, None)
        raise RuntimeError("Custom AgentCore app factory failed") from None


def _prepare_binaries(root: Path, target: Path | None = None) -> Path:
    bundle = root / "kiro-bundle"
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    target = target or Path(tempfile.mkdtemp(prefix="kiro-codezip-bin-"))
    try:
        for name in ("kiro-cli", "kiro-cli-chat", "kiro-cli-term"):
            meta = manifest[name]
            source = bundle / (name + ".xz" if name == "kiro-cli-chat" else name)
            destination = target / name
            digest = hashlib.sha256()
            written = 0
            opener = lzma.open if name == "kiro-cli-chat" else open
            with opener(source, "rb") as inp, destination.open("wb") as out:
                while chunk := inp.read(1024 * 1024):
                    written += len(chunk)
                    if written > meta["size"]:
                        raise ValueError("Bundled executable exceeds its recorded size")
                    digest.update(chunk)
                    out.write(chunk)
            if written != meta["size"] or digest.hexdigest() != meta["sha256"]:
                raise ValueError("Bundled executable failed integrity validation")
            destination.chmod(0o755)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _wait_for_preparation(app, prepared, max_body_bytes):
    """Serve health checks immediately; defer invocations until binaries are ready."""
    async def wrapped(scope, receive, send):
        async def error(status, message):
            body = json.dumps({"error": message}).encode()
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})

        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        if prepared.done() and (prepared.cancelled() or prepared.exception() is not None):
            await error(503, "Kiro runtime preparation failed")
            return
        if scope["path"] != "/invocations" or prepared.done():
            await app(scope, receive, send)
            return

        buffered = deque()
        async def watch_request():
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return "disconnect"
                if message["type"] == "http.request":
                    size += len(message.get("body", b""))
                    if size > max_body_bytes or len(buffered) >= 8192:
                        return "too_large"
                buffered.append(message)

        async def wait_until_ready():
            wrapped_future = asyncio.wrap_future(prepared)
            try:
                await asyncio.shield(wrapped_future)
            except asyncio.CancelledError:
                wrapped_future.add_done_callback(lambda future: None if future.cancelled() else future.exception())
                raise

        watch = asyncio.create_task(watch_request())
        ready = asyncio.create_task(wait_until_ready())
        try:
            done, _ = await asyncio.wait((watch, ready), return_when=asyncio.FIRST_COMPLETED)
            if watch in done:
                outcome = watch.result()
                if outcome == "too_large":
                    await error(413, "Request is too large")
                return
            try:
                ready.result()
            except Exception:
                await error(503, "Kiro runtime preparation failed")
                return
        finally:
            for task in (watch, ready):
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        async def replay():
            return buffered.popleft() if buffered else await receive()
        await app(scope, replay, send)
    return wrapped


def main() -> None:
    root = Path(__file__).resolve().parent
    binaries = Path(tempfile.mkdtemp(prefix="kiro-codezip-bin-"))
    atexit.register(shutil.rmtree, binaries, ignore_errors=True)
    os.environ["PATH"] = str(binaries) + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("KIRO_AGENTCORE_CLI_PATH", str(binaries / "kiro-cli"))
    os.environ.setdefault("KIRO_AGENTCORE_RUNTIME_CACHE", str(root / "kiro-prewarm"))
    from kiro_agent_sdk.agentcore import AgentCoreConfig
    import uvicorn

    config = AgentCoreConfig.from_env()
    app = _load_agentcore_app(root, config)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        prepared = executor.submit(_prepare_binaries, root, binaries)
        app = _wait_for_preparation(app, prepared, config.max_body_bytes)
        uvicorn.run(app, host="0.0.0.0", port=8080, workers=1)


if __name__ == "__main__":
    main()
