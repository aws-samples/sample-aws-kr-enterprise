"""Bridge Kiro native pre-tool hooks to Python callbacks in the SDK process."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import re
import secrets
import shlex
import sys
import os
import tempfile
import hashlib
from pathlib import Path
from typing import Any

from .errors import ProtocolError
from .hook_messages import append_hook_warning
from .hook_output import parse_hook_output
from .hook_failure import _HookFailureRunner


# Hook option keys are Kiro hook triggers. Callbacks receive the hook_event_name Kiro reports for each trigger.
# SDK hooks run in the SDK process; native hooks are registered with Kiro and also cover session and file events.
HOOK_TRIGGERS = {"promptSubmit": "UserPromptSubmit", "preToolUse": "PreToolUse", "postToolUse": "PostToolUse",
                 "agentStop": "Stop"}
NATIVE_HOOK_TRIGGERS = {"sessionStart": "SessionStart", **HOOK_TRIGGERS, "fileCreated": "PostFileCreate",
                        "fileEdited": "PostFileSave", "fileDeleted": "PostFileDelete",
                        "preTaskExecution": "PreTaskExec", "postTaskExecution": "PostTaskExec"}


def hook_events(hooks: Any, triggers: dict[str, str], option: str) -> dict[str, list[Any]]:
    """Map a trigger-keyed hook option to Kiro event names."""
    if hooks is None:
        return {}
    if not isinstance(hooks, dict):
        raise TypeError(f"{option} must be a dictionary or None")
    unknown = sorted(str(key) for key in hooks if key not in triggers)
    if unknown:
        raise ValueError(f"Unsupported {option} triggers: {unknown}; use {', '.join(triggers)}")
    return {triggers[trigger]: matchers for trigger, matchers in hooks.items()}


def _native_outputs(event: str, *, supports_ask: bool, input_writable: bool,
                    allow_once_permitted: bool) -> set[str]:
    """Hook output keys a native Kiro trigger can act on."""
    if event == "PreToolUse":
        allowed = {"block", "warning"}
        if supports_ask:
            allowed.add("ask")
        if input_writable:
            allowed.update({"tool_input", "context"})
            if allow_once_permitted:
                allowed.add("allow")
        return allowed
    if event in ("SessionStart", "UserPromptSubmit"):
        return {"context", "warning"}
    if event == "PreTaskExec":
        return {"context", "block", "warning"}
    if event == "Stop":
        return {"continue", "warning"}
    return {"warning"}


def _native_updated_input(value: Any) -> dict[str, Any]:
    """Copy bounded JSON without coercing non-JSON callback values."""
    if type(value) is not dict:
        raise ValueError("Native updatedInput must be a JSON object")
    nodes = 0
    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > 100000 or depth > 64:
            raise ValueError("Native updatedInput exceeds limits")
        if item is None or type(item) in (str, bool, int, float):
            return item
        if type(item) is list:
            return [visit(child, depth + 1) for child in item]
        if type(item) is dict and all(type(key) is str for key in item):
            return {key: visit(child, depth + 1) for key, child in item.items()}
        raise ValueError("Native updatedInput must contain JSON values")
    result = visit(value, 0)
    if len(json.dumps(result, allow_nan=False, ensure_ascii=True).encode()) > 1048576:
        raise ValueError("Native updatedInput exceeds limits")
    return result


def _bounded_hook_group(matchers: list[Any]) -> bool:
    """Bound total callback and authorization time inside the native shim deadline."""
    count, total = 0, 0.0
    for matcher in matchers:
        hooks, timeout = getattr(matcher, "hooks", None), getattr(matcher, "timeout", None)
        if (not isinstance(hooks, list) or not hooks or any(not callable(hook) for hook in hooks) or
                isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30):
            return False
        count += len(hooks)
        total += timeout * len(hooks)
    if count == 1:
        return len(matchers) == 1 and total <= 30
    # Each callback may need a guard, a warning delivery and a second guard.
    # Initial and final guards add two seconds; the shim has a 60-second deadline.
    return 2 <= count <= 8 and total <= 30 and total + 3 * count <= 48


def _bounded_native_stop_group(matchers: list[Any]) -> bool:
    """Leave time for per-hook authorization and warning delivery in the shim."""
    count, total = 0, 0.0
    for matcher in matchers:
        hooks, timeout = getattr(matcher, "hooks", None), getattr(matcher, "timeout", None)
        if (not isinstance(hooks, list) or not hooks or any(not callable(hook) for hook in hooks) or
                isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 15):
            return False
        count += len(hooks)
        total += timeout * len(hooks)
    if count == 1:
        return len(matchers) == 1
    return 2 <= count <= 4 and total <= 15 and total + 8 * count <= 45


class NativeHookBridge:
    def __init__(self, matchers: dict[str, list[Any]], *, supports_ask: bool = False,
                 on_system_message: Any = None, failure_guard_factory: Any = None, on_stop_block: Any = None):
        self.matchers = matchers
        self.on_stop_block = on_stop_block
        self.failure_guard_factory = failure_guard_factory
        self._failure_runner = _HookFailureRunner()
        self._failure_guard_tasks: set[asyncio.Future[Any]] = set()
        self._failure_snapshot = self._failure_signature()
        self.supports_ask = supports_ask
        self.on_system_message = on_system_message
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.connections = set()
        self._credential_path = None

    def _failure_signature(self) -> Any:
        enabled = False
        groups = []
        for event, matchers in self.matchers.items():
            values = []
            for matcher in matchers:
                policy = getattr(matcher, "on_failure", None)
                if policy is not None:
                    if event not in ("PreToolUse", "Stop") or policy not in ("block", "continue"):
                        raise ValueError("Native hook on_failure requires PreToolUse or Stop")
                    enabled = True
                values.append((id(matcher), matcher.matcher, matcher.timeout, policy,
                               tuple((id(hook), getattr(hook, "_kiro_native_stop_failure_managed", False) is True,
                                      getattr(hook, "_kiro_native_stop_adapter", False) is True)
                                     for hook in matcher.hooks)))
            groups.append((event, tuple(values)))
        pretool = self.matchers.get("PreToolUse", [])
        if any(getattr(matcher, "on_failure", None) == "continue" for matcher in pretool):
            if not _bounded_hook_group(pretool):
                raise ValueError("Native PreToolUse on_failure=continue requires bounded hook timeouts")
        stop = self.matchers.get("Stop", [])
        if any(getattr(matcher, "on_failure", None) is not None for matcher in stop):
            if not _bounded_native_stop_group(stop):
                raise ValueError("Native Stop on_failure requires bounded hook timeouts")
        return tuple(sorted(groups)) if enabled else None

    def _validate_failure_signature(self) -> None:
        self._failure_runner.check()
        if self._failure_signature() != self._failure_snapshot:
            raise ValueError("Native hook failure configuration changed")

    async def _run_failure_guard(self, guard: Any, timeout: float = 5) -> None:
        if len(self._failure_guard_tasks) >= 64:
            raise ValueError("Native hook authorization capacity exhausted")
        task = asyncio.ensure_future(guard())
        self._failure_guard_tasks.add(task)
        def consume(done: asyncio.Future[Any]) -> None:
            self._failure_guard_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(consume)
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if not done or task.cancelled():
                raise ValueError("Native hook authorization unavailable")
            if task.result() is not True:
                raise ValueError("Native hook authorization denied")
        finally:
            if not task.done():
                task.cancel()

    @staticmethod
    def command() -> str:
        return ' '.join(shlex.quote(part) for part in (sys.executable, str(Path(__file__).with_name('_hook_bridge.py'))))

    async def start(self, *, private_credentials: bool = False, user: Any = None) -> tuple[str, dict[str, str]]:
        self._failure_runner.reopen()
        self.server = await asyncio.start_server(self._connected, '127.0.0.1', 0, limit=16 * 1024 * 1024)
        port = self.server.sockets[0].getsockname()[1]
        if private_credentials:
            try:
                fd, path = tempfile.mkstemp(prefix="kiro-hook-credentials-")
                self._credential_path = Path(path)
                data = json.dumps({"port": port, "token": self.token}).encode()
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    if user is not None:
                        import pwd
                        uid = pwd.getpwnam(user).pw_uid
                        if uid != os.geteuid():
                            os.fchown(stream.fileno(), uid, -1)
                command = self.command() + " --credentials " + shlex.quote(path) + " " + hashlib.sha256(data).hexdigest()
                return command, {}
            except BaseException:
                await self.close()
                raise
        return self.command(), {'KIRO_SDK_HOOK_PORT': str(port), 'KIRO_SDK_HOOK_TOKEN': self.token}

    async def _connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            auth = await asyncio.wait_for(reader.readline(), 5)
            if not secrets.compare_digest(auth.decode().strip(), self.token):
                return
            payload = json.loads(await asyncio.wait_for(reader.readline(), 10))
            response = await self.dispatch(payload)
            writer.write((json.dumps(response) + '\n').encode())
            await writer.drain()
        except (Exception, asyncio.CancelledError):
            with contextlib.suppress(Exception):
                response = self.failure_response(locals().get("payload"))
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self.connections.discard(task)

    @staticmethod
    def failure_response(payload: Any) -> dict[str, Any]:
        stop = isinstance(payload, dict) and str(payload.get("hook_event_name", "")).casefold() == "stop"
        result = {"block": not stop, "reason": "SDK hook failed"}
        if stop:
            result["warning"] = "SDK hook failed"
        return result

    async def dispatch(self, payload: dict[str, Any], *, authorize: Any = None,
                       input_writable: bool = False, allow_once_permitted: bool = False) -> dict[str, Any]:
        """Run registered hooks independently of the legacy socket transport."""
        try:
            name = payload.get('tool_name') or payload.get('file_path') or payload.get('task_name') or ''
            raw_event = str(payload.get('hook_event_name', '')).casefold()
            events = {'agentspawn': 'SessionStart', 'sessionstart': 'SessionStart',
                      'userpromptsubmit': 'UserPromptSubmit', 'pretooluse': 'PreToolUse',
                      'posttooluse': 'PostToolUse', 'stop': 'Stop',
                      'postfilecreate': 'PostFileCreate', 'postfilesave': 'PostFileSave',
                      'postfiledelete': 'PostFileDelete', 'pretaskexec': 'PreTaskExec',
                      'posttaskexec': 'PostTaskExec'}
            event = events.get(raw_event)
            if event is None or event not in self.matchers:
                raise ValueError('Unexpected native hook event')
            self._validate_failure_signature()
            guarded = any(getattr(matcher, "on_failure", None) is not None
                          for matcher in self.matchers[event])
            multi_pretool = (event == "PreToolUse" and
                             any(getattr(matcher, "on_failure", None) == "continue"
                                 for matcher in self.matchers[event]) and
                             sum(len(matcher.hooks) for matcher in self.matchers[event]) > 1)
            multi_stop = (event == "Stop" and guarded and
                          sum(len(matcher.hooks) for matcher in self.matchers[event]) > 1)
            guard_timeout = 1 if multi_pretool or multi_stop else 5
            guard = self.failure_guard_factory() if guarded and self.failure_guard_factory is not None else None
            if guard is not None:
                await self._run_failure_guard(guard, guard_timeout)
                self._validate_failure_signature()
            contexts = []
            updated_input = None
            allow_once = False
            payload = copy.deepcopy(payload)
            ask_reason = None
            warnings: list[str] = []
            for matcher in self.matchers[event]:
                if matcher.matcher and not re.search(matcher.matcher, name):
                    continue
                for hook in matcher.hooks:
                    if authorize is not None:
                        await self._run_failure_guard(authorize)
                    policy = getattr(matcher, "on_failure", None)
                    if event == "Stop" and policy == "block":
                        policy = "continue"
                    managed_stop = (event == "Stop" and policy is not None and
                                    getattr(hook, "_kiro_native_stop_failure_managed", False) is True)
                    try:
                        if policy is not None or multi_pretool or multi_stop or input_writable:
                            result = await self._failure_runner.invoke(
                                hook, copy.deepcopy(payload),
                                matcher.timeout + 6 if managed_stop and multi_stop else
                                min(45, matcher.timeout + 30) if managed_stop else
                                matcher.timeout + 2 if multi_stop and
                                getattr(hook, "_kiro_native_stop_adapter", False) is True else
                                matcher.timeout if matcher.timeout is not None else 60,
                                payload.get('tool_call_id'))
                        else:
                            result = await asyncio.wait_for(
                                hook(copy.deepcopy(payload), payload.get('tool_call_id'), {'signal': None}),
                                matcher.timeout if matcher.timeout is not None else 60)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if guard is not None:
                            await self._run_failure_guard(guard, guard_timeout)
                        self._validate_failure_signature()
                        if policy == "continue" and not managed_stop:
                            continue
                        raise
                    if authorize is not None:
                        await self._run_failure_guard(authorize)
                    if guard is not None:
                        await self._run_failure_guard(guard, guard_timeout)
                    self._validate_failure_signature()
                    allowed = _native_outputs(event, supports_ask=self.supports_ask, input_writable=input_writable,
                                              allow_once_permitted=allow_once_permitted)
                    if policy is not None and isinstance(result, dict):
                        # Keep restrictive keys so an unsupported block or ask fails closed.
                        result = {key: value for key, value in result.items()
                                  if key in allowed or key in ("block", "ask")}
                    old_ask, context_count, warning_count = ask_reason, len(contexts), len(warnings)
                    pending_input = None
                    pending_allow = False
                    pending_warning = None
                    block_reason = None
                    restrictive = (event == "PreToolUse" and isinstance(result, dict) and
                                   any(result.get(key) not in (None, False) for key in ("block", "ask")))
                    try:
                        try:
                            output = parse_hook_output(event, result, allowed)
                        except ProtocolError as exc:
                            raise ValueError(str(exc)) from exc
                        if 'warning' in output:
                            before = len(warnings)
                            append_hook_warning(warnings, event, output['warning'])
                            if len(warnings) != before:
                                if self.on_system_message is None:
                                    raise ValueError('Native hook warnings require an application callback')
                                if policy is None and not multi_pretool and not multi_stop:
                                    await self.on_system_message(event, payload, warnings[-1])
                                    if guard is not None:
                                        await self._run_failure_guard(guard, guard_timeout)
                                        self._validate_failure_signature()
                                else:
                                    pending_warning = warnings[-1]
                        if 'context' in output:
                            contexts.append(output['context'])
                            if len('\n'.join(contexts).encode()) > 65536:
                                raise ValueError('Native hook context exceeds limits')
                        if 'tool_input' in output:
                            pending_input = _native_updated_input(output['tool_input'])
                        if 'ask' in output:
                            ask_reason = output['ask'] or 'Requested by SDK hook'
                        elif 'allow' in output:
                            pending_allow = True
                        if 'block' in output:
                            block_reason = output['block'] or 'Blocked by SDK hook'
                        elif 'continue' in output:
                            block_reason = output['continue'] or 'Continue working'
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if policy != "continue" or restrictive:
                            raise
                        if guard is not None:
                            await self._run_failure_guard(guard, guard_timeout)
                        self._validate_failure_signature()
                        ask_reason = old_ask
                        del contexts[context_count:]
                        del warnings[warning_count:]
                        continue
                    if pending_warning is not None:
                        async def deliver(_payload: Any, _tool_id: Any, _context: Any) -> Any:
                            return await self.on_system_message(event, payload, pending_warning)
                        await self._failure_runner.invoke(deliver, {}, 1 if multi_pretool or multi_stop else 5)
                        if guard is not None:
                            await self._run_failure_guard(guard, guard_timeout)
                        self._validate_failure_signature()
                    if block_reason is not None:
                        if event == "Stop" and self.on_stop_block is not None:
                            self.on_stop_block(payload)
                        return {'block': True, 'reason': block_reason}
                    if pending_input is not None:
                        updated_input = pending_input
                        payload["tool_input"] = copy.deepcopy(updated_input)
                        allow_once = False
                    if pending_allow:
                        allow_once = True
            response = {'block': False}
            if ask_reason:
                response['ask'] = ask_reason
            elif allow_once:
                response['allowOnce'] = True
            if contexts:
                response['context'] = '\n'.join(contexts)
            if updated_input is not None:
                response['updatedInput'] = updated_input
            if guard is not None:
                await self._run_failure_guard(guard, guard_timeout)
                self._validate_failure_signature()
            return response
        except asyncio.CancelledError:
            raise
        except Exception:
            return self.failure_response(payload)

    async def close(self) -> None:
        if self._credential_path is not None:
            self._credential_path.unlink(missing_ok=True)
            self._credential_path = None
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        if self.connections:
            await asyncio.gather(*self.connections, return_exceptions=True)
        await self._failure_runner.close()
