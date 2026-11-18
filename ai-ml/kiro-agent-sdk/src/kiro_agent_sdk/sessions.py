"""Read Kiro's supported session and model listings through its CLI."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import signal
from pathlib import Path
from typing import Any, Literal, Optional, Union

from .errors import CLIConnectionError, CLINotFoundError, ProcessError, ProtocolError
from .history import history_message
from .types import ForkSessionResult, HistoryMessage, SessionInfo
from .process_env import child_process_env
from .session_store import merge_session_info, project_key_for_directory


def _valid_session_id(value: Any) -> bool:
    from .archive import _valid_session_id as valid
    return valid(value)


async def _run_cli(arguments: list[str], cwd: Union[str, Path, None], cli_path: Union[str, Path],
                   env: Optional[dict[str, str]] = None, *, _process_user: Optional[str] = None) -> bytes:
    if cwd is not None and not Path(cwd).is_dir():
        raise CLIConnectionError(f'Working directory does not exist: {cwd}')
    try:
        kwargs: dict[str, Any] = {
            'cwd': str(cwd) if cwd else None, 'stdout': asyncio.subprocess.PIPE,
            'stderr': asyncio.subprocess.PIPE, 'env': child_process_env(cwd, env)}
        if _process_user is not None:
            from .client import _spawn_cli
            process = await _spawn_cli(str(cli_path), 'chat', *arguments, **kwargs,
                                       user=_process_user, start_new_session=True)
        else:
            process = await asyncio.create_subprocess_exec(str(cli_path), 'chat', *arguments, **kwargs)
    except FileNotFoundError as exc:
        raise CLINotFoundError(f'Kiro CLI not found: {cli_path}') from exc
    try:
        stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            try:
                if _process_user is not None and os.name == 'posix':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    if process.returncode:
        raise ProcessError('Kiro CLI failed', exit_code=process.returncode, stderr=stderr.decode(errors='replace'))
    return stdout


async def _list_cli(flag: str, cwd: Union[str, Path, None], cli_path: Union[str, Path],
                    env: Optional[dict[str, str]] = None, *, all_directories: bool = False,
                    _process_user: Optional[str] = None) -> Any:
    arguments = [flag, '--format', 'json']
    if all_directories:
        arguments.append('--all-cwds')
    stdout = (await _run_cli(arguments, cwd, cli_path, env, _process_user=_process_user)
              if _process_user is not None else await _run_cli(arguments, cwd, cli_path, env))
    try:
        return json.loads(stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError('Kiro CLI returned invalid JSON') from exc


async def _list_sessions_for_process_user(cwd: Union[str, Path, None], cli_path: Union[str, Path],
                                          env: Optional[dict[str, str]], user: str) -> list[SessionInfo]:
    """Read one V2 workspace as the explicitly selected Kiro process user."""
    result = await _list_cli('--list-sessions', cwd, cli_path, env, _process_user=user)
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise ProtocolError('Unexpected session list format')
    sessions = []
    for envelope in result:
        records = envelope.get('sessions', [])
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ProtocolError('Unexpected session list format')
        sessions.extend(SessionInfo(record) for record in records)
    sessions.sort(key=lambda item: (item.updated_at is not None, item.updated_at or 0), reverse=True)
    seen: set[str] = set()
    unique = []
    for session in sessions:
        if session.session_id and session.session_id in seen:
            continue
        if session.session_id:
            seen.add(session.session_id)
        unique.append(session)
    return unique


def _directory(directory: Union[str, Path, None], cwd: Union[str, Path, None]) -> Union[str, Path, None]:
    if directory is not None and cwd is not None:
        raise ValueError('Pass either directory or cwd, not both')
    return cwd if cwd is not None else directory


def _page(offset: int, limit: Optional[int]) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('offset must be a non-negative integer')
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError('limit must be a non-negative integer')


async def _worktree_directories(directory: Union[str, Path, None]) -> set[str]:
    """Find the current Git worktree's roots without interpreting shell output."""
    current = os.path.realpath(str(directory or os.getcwd()))
    try:
        process = await asyncio.create_subprocess_exec(
            'git', '-C', current, 'worktree', 'list', '--porcelain', '-z',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return {current}
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 10)
    except BaseException as exc:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        if isinstance(exc, asyncio.TimeoutError):
            return {current}
        raise
    paths = {current}
    if process.returncode == 0:
        for field in stdout.split(b'\0'):
            if field.startswith(b'worktree '):
                paths.add(os.path.realpath(os.fsdecode(field[len(b'worktree '):])))
    return paths


async def _list_global_v3(cli_path: Union[str, Path], env: Optional[dict[str, str]]) -> list[dict[str, Any]]:
    """Use V3's global ACP listing; --all-cwds alone omits V3 in CLI 2.35.1."""
    from .client import KiroSDKClient
    from .types import KiroAgentOptions

    client = KiroSDKClient(KiroAgentOptions(cli_path=cli_path, env=env or {},
                                            agent_engine='v3', enable_kiro_hooks=False))
    result: list[dict[str, Any]] = []
    cursors: set[str] = set()
    cursor = None
    try:
        await client._connect_control()
        for _ in range(1000):
            try:
                page = await asyncio.wait_for(client.list_sessions(cursor=cursor), 60)
            except asyncio.TimeoutError as exc:
                raise CLIConnectionError('Timed out listing Kiro V3 sessions') from exc
            if (not isinstance(page, dict) or not isinstance(page.get('sessions'), list) or
                    any(not isinstance(item, dict) for item in page['sessions'])):
                raise ProtocolError('Unexpected Kiro V3 session list format')
            for item in page['sessions']:
                session = dict(item)
                session.setdefault('source', 'v3')
                result.append(session)
            cursor = page.get('nextCursor')
            if cursor is None:
                return result
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ProtocolError('Invalid Kiro V3 session list cursor')
            cursors.add(cursor)
        raise ProtocolError('Kiro V3 session list exceeded the page limit')
    finally:
        await client.disconnect()


async def list_sessions(directory: Union[str, Path, None] = None, limit: Optional[int] = None,
                        offset: int = 0, include_worktrees: bool = False, *, cwd: Union[str, Path, None] = None,
                        cli_path: Union[str, Path] = 'kiro-cli',
                        env: Optional[dict[str, str]] = None, all_directories: bool = False) -> list[SessionInfo]:
    """List sessions in one workspace, Git worktrees, or all local workspaces."""
    _page(offset, limit)
    if not isinstance(include_worktrees, bool) or not isinstance(all_directories, bool):
        raise ValueError('include_worktrees and all_directories must be booleans')
    selected = _directory(directory, cwd)
    if all_directories and (selected is not None or include_worktrees):
        raise ValueError('all_directories cannot be combined with a directory or include_worktrees')
    worktrees = await _worktree_directories(selected) if include_worktrees else None
    if worktrees is not None and len(worktrees) > 1:
        # CLI's global listing omits V3-only directories in 2.35.1. Query
        # each known worktree instead so both V2 and V3 sessions are visible.
        semaphore = asyncio.Semaphore(4)
        async def list_worktree(path: str) -> Any:
            async with semaphore:
                return await _list_cli('--list-sessions', path, cli_path, env)
        lists = await asyncio.gather(*(list_worktree(path) for path in sorted(worktrees) if Path(path).is_dir()))
    else:
        lists = [await _list_cli('--list-sessions', selected, cli_path, env, all_directories=all_directories)]
    if all_directories:
        lists.append([{'sessions': await _list_global_v3(cli_path, env)}])
    sessions = []
    for result in lists:
        if not isinstance(result, list) or any(not isinstance(x, dict) for x in result):
            raise ProtocolError('Unexpected session list format')
        for envelope in result:
            workspace = envelope.get('cwd')
            records = envelope.get('sessions', [])
            if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                raise ProtocolError('Unexpected session list format')
            for record in records:
                info = SessionInfo(record)
                if isinstance(workspace, str) and workspace:
                    info.setdefault('cwd', workspace)
                sessions.append(info)
    sessions.sort(key=lambda item: (item.updated_at is not None, item.updated_at or 0), reverse=True)
    seen = set()
    unique = []
    for session in sessions:
        session_id = session.session_id
        if session_id and session_id in seen:
            continue
        if session_id:
            seen.add(session_id)
        unique.append(session)
    unique = unique[offset:None if limit is None else offset + limit]
    return unique


async def get_session_info(session_id: str, directory: Union[str, Path, None] = None, *, cwd: Union[str, Path, None] = None,
                           cli_path: Union[str, Path] = 'kiro-cli', env: Optional[dict[str, str]] = None,
                           all_directories: bool = False, include_worktrees: bool = False) -> Optional[SessionInfo]:
    return next((s for s in await list_sessions(directory, cwd=cwd, cli_path=cli_path, env=env,
                                                all_directories=all_directories, include_worktrees=include_worktrees)
                 if s.get('sessionId') == session_id), None)


async def delete_session(session_id: str, directory: Union[str, Path, None] = None, *,
                         cwd: Union[str, Path, None] = None, cli_path: Union[str, Path] = 'kiro-cli',
                         env: Optional[dict[str, str]] = None) -> None:
    """Delete a saved session using Kiro CLI. Kiro treats an absent ID as success."""
    if not _valid_session_id(session_id):
        raise ValueError('Invalid session ID')
    selected = _directory(directory, cwd)
    await _run_cli(['--delete-session', session_id], selected, cli_path, env)


async def rename_session(session_id: str, title: str, directory: Union[str, Path, None] = None, *,
                         cwd: Union[str, Path, None] = None, cli_path: Union[str, Path] = 'kiro-cli',
                         env: Optional[dict[str, str]] = None, timeout: Optional[float] = 60.0) -> None:
    """Rename a Kiro V3 session without opening or creating a chat session."""
    from .archive import _valid_session_id
    from .client import KiroSDKClient
    from .types import KiroAgentOptions

    if not _valid_session_id(session_id):
        raise ValueError('Invalid session ID')
    if not isinstance(title, str) or not title.strip():
        raise ValueError('title must not be empty')
    options = KiroAgentOptions(cwd=_directory(directory, cwd), cli_path=cli_path, env=env or {},
                               agent_engine='v3', startup_timeout=timeout, enable_kiro_hooks=False)
    client = KiroSDKClient(options)
    try:
        await client._connect_control()
        try:
            result = await asyncio.wait_for(client.rename_session(title.strip(), session_id), timeout)
        except asyncio.TimeoutError as exc:
            raise CLIConnectionError('Timed out renaming the Kiro session') from exc
        if not isinstance(result, dict) or result.get('success') is not True:
            raise ProtocolError(f'Kiro did not confirm session rename: {result}')
    finally:
        await client.disconnect()


async def fork_session(session_id: str, directory: Union[str, Path, None] = None,
                       up_to_message_id: Optional[str] = None, title: Optional[str] = None, *,
                       cwd: Union[str, Path, None] = None, cli_path: Union[str, Path] = 'kiro-cli',
                       env: Optional[dict[str, str]] = None,
                       timeout: Optional[float] = 60.0) -> ForkSessionResult:
    """Fork a Kiro V3 session without creating an unrelated chat session."""
    from .archive import _valid_session_id
    from .client import KiroSDKClient
    from .types import KiroAgentOptions

    if not _valid_session_id(session_id):
        raise ValueError('Invalid session ID')
    if up_to_message_id is not None and (not isinstance(up_to_message_id, str) or
                                         not up_to_message_id.strip() or '\0' in up_to_message_id):
        raise ValueError('up_to_message_id must be a non-empty Kiro message ID')
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise ValueError('title must not be empty')
    options = KiroAgentOptions(cwd=_directory(directory, cwd), cli_path=cli_path, env=env or {},
                               agent_engine='v3', startup_timeout=timeout, enable_kiro_hooks=False)
    client = KiroSDKClient(options)
    try:
        await client._connect_control()
        try:
            result = await asyncio.wait_for(client.fork_session(
                session_id, message_id=up_to_message_id, title=title.strip() if title else None), timeout)
        except asyncio.TimeoutError as exc:
            raise CLIConnectionError('Timed out forking the Kiro session') from exc
        fork_id = result.get('sessionId') if isinstance(result, dict) else None
        if not _valid_session_id(fork_id) or fork_id == session_id:
            raise ProtocolError('Kiro did not return a distinct forked session ID')
        return ForkSessionResult(session_id=fork_id)
    finally:
        await client.disconnect()


async def list_models(*, cwd: Union[str, Path, None] = None, cli_path: Union[str, Path] = 'kiro-cli',
                      env: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
    result = await _list_cli('--list-models', cwd, cli_path, env)
    if not isinstance(result, dict) or not isinstance(result.get('models'), list):
        raise ProtocolError('Unexpected model list format')
    return result['models']


async def get_session_history(session_id: str, *, cwd: Union[str, Path, None] = None,
                              cli_path: Union[str, Path] = 'kiro-cli',
                              agent_engine: Optional[str] = None,
                              env: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
    """Load a saved session and return Kiro's replay updates in arrival order."""
    if not _valid_session_id(session_id):
        raise ValueError('Invalid session ID')
    from .client import KiroSDKClient
    from .types import KiroAgentOptions

    options = KiroAgentOptions(cwd=cwd, cli_path=cli_path, agent_engine=agent_engine, resume=session_id, env=env or {})
    async with KiroSDKClient(options) as client:
        return list(client.history)


def _replay_messages(events: list[dict[str, Any]], session_id: str,
                     coalesce_chunks: bool, exclude_observed_subagents: bool,
                     include_observed_tool_messages: bool = False) -> list[HistoryMessage]:
    """Convert replay chunks, joining only consecutive chunks with identical identity."""
    if include_observed_tool_messages:
        from .replay_tools import observed_tool_messages
        tool_messages = observed_tool_messages(events, session_id)
    else:
        tool_messages = {}
    roles = {'user_message_chunk': 'user', 'agent_message_chunk': 'assistant'}
    messages: list[HistoryMessage] = []
    pending_key: Any = None
    pending_blocks: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal pending_key, pending_blocks
        if pending_key is None:
            return
        role, message_id, _ = pending_key
        blocks: list[dict[str, Any]] = []
        text_parts: list[str] = []

        def flush_text() -> None:
            if text_parts:
                blocks.append({'type': 'text', 'text': ''.join(text_parts)})
                text_parts.clear()

        for block in pending_blocks:
            if set(block) == {'type', 'text'} and block['type'] == 'text' and isinstance(block['text'], str):
                text_parts.append(block['text'])
            else:
                flush_text()
                blocks.append(copy.deepcopy(block))
        flush_text()
        messages.append(history_message(role, message_id, session_id, blocks))
        pending_key, pending_blocks = None, []

    for index, event in enumerate(events):
        if index in tool_messages:
            flush()
            messages.append(tool_messages[index])
            continue
        if not isinstance(event, dict):
            flush()
            continue
        metadata = event.get('_meta')
        kiro = metadata.get('kiro') if isinstance(metadata, dict) else None
        subagent_id = kiro.get('agentSubtaskId') if isinstance(kiro, dict) else None
        if exclude_observed_subagents and isinstance(subagent_id, str) and subagent_id:
            # A filtered child event is still a boundary between parent chunks.
            flush()
            continue
        role = roles.get(event.get('sessionUpdate'))
        content = event.get('content')
        if role is None or not isinstance(content, dict):
            flush()
            continue
        message_id = event.get('messageId')
        if not coalesce_chunks or not isinstance(message_id, str) or not message_id:
            flush()
            messages.append(history_message(role, message_id, session_id, [content]))
            continue
        # Other fields may distinguish agents or other contexts even when IDs
        # match. An intervening event always closes the group.
        context = {key: value for key, value in event.items()
                   if key not in ('sessionUpdate', 'messageId', 'content')}
        identity = (role, message_id, context)
        if pending_key != identity:
            flush()
            pending_key = identity
        pending_blocks.append(content)
    flush()
    return messages


async def get_session_messages(session_id: str, directory: Union[str, Path, None] = None,
                               limit: Optional[int] = None, offset: int = 0, *, cwd: Union[str, Path, None] = None,
                               cli_path: Union[str, Path] = 'kiro-cli',
                               agent_engine: Optional[str] = None,
                               env: Optional[dict[str, str]] = None,
                               coalesce_chunks: bool = False,
                               exclude_observed_subagents: bool = False,
                               kiro_history_source: Literal['replay', 'archive-text', 'archive-tools'] = 'replay',
                               include_observed_tool_messages: bool = False) -> list[HistoryMessage]:
    """Read replay chunks or an explicitly selected local V3 archive text preview."""
    if not _valid_session_id(session_id):
        raise ValueError('Invalid session ID')
    _page(offset, limit)
    if not isinstance(coalesce_chunks, bool):
        raise ValueError('coalesce_chunks must be a boolean')
    if not isinstance(exclude_observed_subagents, bool):
        raise ValueError('exclude_observed_subagents must be a boolean')
    if not isinstance(include_observed_tool_messages, bool):
        raise ValueError('include_observed_tool_messages must be a boolean')
    if kiro_history_source not in ('replay', 'archive-text', 'archive-tools'):
        raise ValueError('kiro_history_source must be replay, archive-text, or archive-tools')
    selected = _directory(directory, cwd)
    if kiro_history_source in ('archive-text', 'archive-tools'):
        if agent_engine != 'v3':
            raise ValueError('Archive history requires agent_engine="v3"')
        if coalesce_chunks or exclude_observed_subagents or include_observed_tool_messages:
            raise ValueError('Replay options do not apply to archive history')
        from .session_archive_history import get_session_archive_messages
        return await get_session_archive_messages(session_id, directory=selected, limit=limit, offset=offset,
                                                  cli_path=cli_path, env=env,
                                                  include_tool_messages=kiro_history_source == 'archive-tools')
    events = await get_session_history(session_id, cwd=selected, cli_path=cli_path,
                                       agent_engine=agent_engine, env=env)
    messages = _replay_messages(events, session_id, coalesce_chunks, exclude_observed_subagents,
                                include_observed_tool_messages)
    return messages[offset:None if limit is None else offset + limit]


def _store_key(session_id: str, directory: Union[str, Path, None]) -> dict[str, str]:
    if not _valid_session_id(session_id):
        raise ValueError('Invalid session ID')
    return {'project_key': project_key_for_directory(str(directory or os.getcwd())), 'session_id': session_id}


async def _store_session_info(session_store: Any, project_key: str) -> Optional[dict[str, dict[str, Any]]]:
    listing = getattr(session_store, 'list_session_info', None)
    if not callable(listing):
        return None
    try:
        records = await listing(project_key)
    except NotImplementedError:
        return None
    return {record['session_id']: record for record in records
            if isinstance(record, dict) and isinstance(record.get('session_id'), str)
            and isinstance(record.get('mtime'), int) and isinstance(record.get('info'), dict)}


def _stored_session(session_id: str, mtime: Any, info: dict[str, Any]) -> SessionInfo:
    record = {**info, 'sessionId': session_id}
    if isinstance(mtime, int):
        record['mtime'] = mtime
    return SessionInfo(record)


async def list_sessions_from_store(session_store: Any, directory: Union[str, Path, None] = None,
                                   limit: Optional[int] = None, offset: int = 0) -> list[SessionInfo]:
    """List mirrored sessions with the Kiro metadata recorded in the store, most recently written first."""
    _page(offset, limit)
    project_key = project_key_for_directory(str(directory or os.getcwd()))
    indexed = await _store_session_info(session_store, project_key)
    listing = getattr(session_store, 'list_sessions', None)
    entries = None
    if callable(listing):
        try:
            entries = await listing(project_key)
        except NotImplementedError:
            pass
    if entries is None:
        if indexed is None:
            raise ProtocolError('Session store does not support listing')
        entries = [{'session_id': record['session_id'], 'mtime': record['mtime']} for record in indexed.values()]
    def written(item: dict[str, Any]) -> int:
        record = (indexed or {}).get(item['session_id'])
        return max(item.get('mtime', 0), record['mtime'] if record is not None else 0)
    entries = sorted(entries, key=written, reverse=True)[offset:None if limit is None else offset + limit]
    async def describe(item: dict[str, Any]) -> SessionInfo:
        record = (indexed or {}).get(item['session_id'])
        if record is not None and record['mtime'] >= item.get('mtime', 0):
            return _stored_session(item['session_id'], record['mtime'], record['info'])
        stored = await session_store.load(_store_key(item['session_id'], directory))
        return _stored_session(item['session_id'], item.get('mtime'), merge_session_info(None, stored or []))
    result = []
    for index in range(0, len(entries), 16):
        result.extend(await asyncio.gather(*(describe(item) for item in entries[index:index + 16])))
    return result


async def get_session_info_from_store(session_store: Any, session_id: str,
                                       directory: Union[str, Path, None] = None) -> Optional[SessionInfo]:
    """Return the Kiro metadata a store recorded for one mirrored session."""
    entries = await session_store.load(_store_key(session_id, directory))
    if entries is None:
        return None
    mtime = None
    if callable(getattr(session_store, 'list_sessions', None)):
        try:
            sessions = await session_store.list_sessions(project_key_for_directory(str(directory or os.getcwd())))
        except NotImplementedError:
            pass
        else:
            mtime = next((item.get('mtime') for item in sessions if item.get('session_id') == session_id), None)
    return _stored_session(session_id, mtime, merge_session_info(None, entries))


async def rename_session_via_store(session_store: Any, session_id: str, title: str,
                                   directory: Union[str, Path, None] = None) -> None:
    """Record a new title in the mirror only; Kiro's own session title is unchanged."""
    if not isinstance(session_id, str) or not session_id.strip() or '\0' in session_id:
        raise ValueError('Invalid session ID')
    if not isinstance(title, str) or not title.strip():
        raise ValueError('title must not be empty')
    await session_store.append(_store_key(session_id, directory),
                               [{'type': 'session_info', 'data': {'title': title.strip()}}])


def _mirrored_prompt_ids(entries: list[Any]) -> dict[int, str]:
    """Associate only unambiguous SDK prompt/result pairs from a mirror."""
    grouped: dict[str, dict[str, list[Any]]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        key = entry.get('kiro_sdk_prompt_key')
        if not isinstance(key, str) or len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
            continue
        kind = entry.get('type')
        if kind not in ('prompt', 'result'):
            continue
        record = grouped.setdefault(key, {'prompts': [], 'results': []})
        if kind == 'prompt':
            if len(record['prompts']) < 2:
                record['prompts'].append(index)
        else:
            data = entry.get('data')
            value = data.get('kiro_sdk_unique_user_message_id') if isinstance(data, dict) else None
            if len(record['results']) < 2:
                record['results'].append((index, value))
    matched = {}
    for record in grouped.values():
        prompt_indices, results = record['prompts'], record['results']
        if len(prompt_indices) != 1 or len(results) != 1:
            continue
        prompt_index = prompt_indices[0]
        result_index, value = results[0]
        if (result_index <= prompt_index or not isinstance(value, str) or not value.strip() or
                len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            continue
        matched[prompt_index] = value
    return matched


def _mirrored_observed_tool_messages(entries: list[Any], session_id: str) -> dict[int, Any]:
    """Project tool updates only inside one unambiguous mirrored prompt/result pair."""
    from .replay_tools import observed_tool_messages
    grouped: dict[str, dict[str, list[Any]]] = {}
    tool_prefix = [0]
    malformed_prefix = [0]
    for index, entry in enumerate(entries):
        data = entry.get('data') if isinstance(entry, dict) else None
        is_update = isinstance(entry, dict) and entry.get('type') == 'update'
        is_tool = is_update and isinstance(data, dict) and data.get('sessionUpdate') in ('tool_call', 'tool_call_update')
        tool_prefix.append(tool_prefix[-1] + int(is_tool))
        malformed = (is_update and (not isinstance(data, dict) or
                                    not isinstance(data.get('sessionUpdate'), str) or not data['sessionUpdate']))
        malformed_prefix.append(malformed_prefix[-1] + int(malformed))
        if not isinstance(entry, dict):
            continue
        key = entry.get('kiro_sdk_prompt_key')
        if not isinstance(key, str) or len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
            continue
        kind = entry.get('type')
        if kind not in ('prompt', 'result', 'update'):
            continue
        record = grouped.setdefault(key, {'prompts': [], 'results': [], 'updates': []})
        if kind == 'prompt':
            if len(record['prompts']) < 2:
                record['prompts'].append(index)
        elif kind == 'result':
            if len(record['results']) < 2:
                record['results'].append(index)
        else:
            if is_tool:
                record['updates'].append((index, data))
    projected: dict[int, Any] = {}
    for record in grouped.values():
        prompts, results, updates = record['prompts'], record['results'], record['updates']
        if len(prompts) != 1 or len(results) != 1 or prompts[0] >= results[0]:
            continue
        if any(not prompts[0] < index < results[0] for index, _ in updates):
            continue
        if (tool_prefix[results[0]] - tool_prefix[prompts[0] + 1] != len(updates) or
                malformed_prefix[results[0]] - malformed_prefix[prompts[0] + 1]):
            continue
        local = observed_tool_messages([event for _, event in updates], session_id)
        for index, message in local.items():
            projected[updates[index][0]] = message
    return projected


async def get_session_messages_from_store(session_store: Any, session_id: str,
                                           directory: Union[str, Path, None] = None, limit: Optional[int] = None,
                                           offset: int = 0, *,
                                           coalesce_chunks: bool = False,
                                           exclude_observed_subagents: bool = False,
                                           include_observed_tool_messages: bool = False,
                                           bounded_without_archives: bool = False) -> list[HistoryMessage]:
    """Read mirrored messages, optionally using a bounded snapshot without archive payloads."""
    _page(offset, limit)
    if not isinstance(coalesce_chunks, bool):
        raise ValueError('coalesce_chunks must be a boolean')
    if not isinstance(exclude_observed_subagents, bool):
        raise ValueError('exclude_observed_subagents must be a boolean')
    if not isinstance(include_observed_tool_messages, bool):
        raise ValueError('include_observed_tool_messages must be a boolean')
    if type(bounded_without_archives) is not bool:
        raise ValueError('bounded_without_archives must be a boolean')
    key = _store_key(session_id, directory)
    if bounded_without_archives:
        from .session_store import InMemorySessionStore
        from .sqlite_store import SQLiteSessionStore
        if type(session_store) not in (InMemorySessionStore, SQLiteSessionStore):
            raise ValueError('bounded_without_archives requires a built-in session store')
        entries = await session_store.load_bounded(key, max_entries=20000, max_bytes=8 * 1024 * 1024,
                                                   skip_archives=True) or []
    else:
        entries = await session_store.load(key) or []
    if not isinstance(entries, list):
        entries = list(entries)
    return _messages_from_mirror_entries(
        entries, session_id, limit=limit, offset=offset, coalesce_chunks=coalesce_chunks,
        exclude_observed_subagents=exclude_observed_subagents,
        include_observed_tool_messages=include_observed_tool_messages)


def _messages_from_mirror_entries(entries: list[Any], session_id: str, *, limit: Optional[int] = None,
                                  offset: int = 0, coalesce_chunks: bool = False,
                                  exclude_observed_subagents: bool = False,
                                  include_observed_tool_messages: bool = False) -> list[HistoryMessage]:
    """Project a caller-validated mirror snapshot without reloading it."""
    observed_ids = _mirrored_prompt_ids(entries)
    tool_messages = (_mirrored_observed_tool_messages(entries, session_id)
                     if include_observed_tool_messages else {})
    messages = []
    updates: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        data = entry.get('data')
        if entry.get('type') == 'update' and isinstance(data, dict):
            if index in tool_messages:
                messages.extend(_replay_messages(updates, session_id, coalesce_chunks, exclude_observed_subagents))
                updates.clear()
                messages.append(tool_messages[index])
                continue
            # The mirror exposes only assistant replay updates, never user
            # updates that may duplicate a submitted prompt.
            updates.append(data if data.get('sessionUpdate') != 'user_message_chunk' else {})
            continue
        messages.extend(_replay_messages(updates, session_id, coalesce_chunks, exclude_observed_subagents))
        updates.clear()
        if entry.get('type') == 'prompt' and isinstance(data, list):
            message_id = entry.get('uuid') if isinstance(entry.get('uuid'), str) else observed_ids.get(index)
            messages.append(history_message('user', message_id, session_id, data))
    messages.extend(_replay_messages(updates, session_id, coalesce_chunks, exclude_observed_subagents))
    return messages[offset:None if limit is None else offset + limit]


async def delete_session_via_store(session_store: Any, session_id: str,
                                   directory: Union[str, Path, None] = None) -> None:
    """Delete a mirror from the supplied store; the Kiro session is unaffected."""
    delete = getattr(session_store, 'delete', None)
    if not callable(delete):
        raise ProtocolError('Session store does not support deletion')
    await delete(_store_key(session_id, directory))
