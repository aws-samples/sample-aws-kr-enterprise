"""Fork a stored Kiro V3 archive in an isolated local Kiro home."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

from .archive import (DEFAULT_ARCHIVE_MAX_BYTES, DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                      _check_archive, _limits, _load_archive_mirror, _stored_archive, _valid_session_id,
                      _validate_timeout, _write_archive_to_store, export_session_archive, import_session_archive)
from .errors import CLINotFoundError, SessionArchiveError, StoreForkError
from .process_env import child_process_env
from .client import KiroSDKClient
from .sessions import _run_cli, _store_key, fork_session, list_sessions
from .types import ForkSessionResult, KiroAgentOptions


async def _require_tested_cli(cli_path: Union[str, Path], cwd: str,
                              env: Optional[dict[str, str]], timeout: Optional[float], *,
                              operation: str = 'Store-only forking') -> None:
    _validate_timeout(timeout)
    try:
        process = await asyncio.create_subprocess_exec(
            str(cli_path), '--version', cwd=cwd, env=child_process_env(cwd, env),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as exc:
        raise CLINotFoundError(f'Kiro CLI not found: {cli_path}') from exc
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    if process.returncode or stdout.decode(errors='replace').strip() != 'kiro-cli 2.35.1':
        raise SessionArchiveError(f'{operation} requires the tested Kiro CLI 2.35.1')


async def _verify_isolation(scratch: str, cli_path: Union[str, Path],
                            original_env: Optional[dict[str, str]], isolated_env: dict[str, str],
                            timeout: Optional[float]) -> None:
    async def sessions(env: Optional[dict[str, str]]) -> set[str]:
        values = await asyncio.wait_for(list_sessions(cwd=scratch, cli_path=cli_path, env=env), timeout)
        return {item['sessionId'] for item in values
                if isinstance(item, dict) and isinstance(item.get('sessionId'), str)}

    before = await sessions(original_env)
    probe_id: Optional[str] = None
    verified = False
    try:
        options = KiroAgentOptions(cwd=scratch, cli_path=cli_path, env=isolated_env,
                                   agent_engine='v3', startup_timeout=timeout, enable_kiro_hooks=False)
        async with KiroSDKClient(options) as probe:
            probe_id = probe.session_id
            if not isinstance(probe_id, str) or not probe_id:
                raise SessionArchiveError('Kiro did not return an isolation probe session ID')
            isolated = await sessions(isolated_env)
            normal = await sessions(original_env)
            if probe_id not in isolated or probe_id in normal:
                raise SessionArchiveError('Kiro session storage isolation could not be verified')
            verified = True
    finally:
        if not verified and probe_id and probe_id not in before:
            try:
                normal = await sessions(original_env)
                if probe_id in normal:
                    await asyncio.wait_for(_run_cli(['--delete-session', probe_id], scratch,
                                                    cli_path, original_env), timeout)
                    if probe_id in await sessions(original_env):
                        raise SessionArchiveError(f'Isolation probe {probe_id} remains in the normal store')
            except Exception as exc:
                raise SessionArchiveError(f'Unable to verify cleanup of isolation probe {probe_id}') from exc


async def fork_session_via_store(session_store: Any, session_id: str,
                                 directory: Union[str, Path, None] = None,
                                 up_to_message_id: Optional[str] = None,
                                 title: Optional[str] = None, *,
                                 cli_path: Union[str, Path] = 'kiro-cli',
                                 env: Optional[dict[str, str]] = None,
                                 max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                 max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                 max_messages: int = 10000, batch_size: int = 500,
                                 timeout: Optional[float] = 30.0,
                                 bounded_mirror_read: bool = False) -> ForkSessionResult:
    """Fork a mirrored V3 archive without creating a session in the user's Kiro home.

    The returned ID identifies a new mirror, not a session in the user's local
    Kiro store. Import it with resume_from_store=True before continuing.
    """
    _limits(max_bytes, max_uncompressed_bytes)
    if not _valid_session_id(session_id):
        raise ValueError('Invalid session ID')
    if up_to_message_id is not None and (not isinstance(up_to_message_id, str) or
                                         not up_to_message_id.strip() or '\0' in up_to_message_id):
        raise ValueError('up_to_message_id must be a non-empty Kiro message ID')
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise ValueError('title must not be empty')
    for name, value in (('max_messages', max_messages), ('batch_size', batch_size)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'{name} must be a positive integer')
    _validate_timeout(timeout)
    workspace = str(Path(directory or os.getcwd()).resolve())
    if not Path(workspace).is_dir():
        raise SessionArchiveError(f'Working directory does not exist: {workspace}')
    key = _store_key(session_id, directory)
    entries = await _load_archive_mirror(session_store, key, bounded_mirror_read)
    if not isinstance(entries, list):
        raise SessionArchiveError('Session mirror was not found')
    source = await asyncio.to_thread(_stored_archive, entries, session_id, allow_stale=False, max_bytes=max_bytes)
    if source is None:
        raise SessionArchiveError('Session mirror has no supported Kiro archive')
    await asyncio.to_thread(_check_archive, source, max_bytes, max_uncompressed_bytes)
    try:
        await _require_tested_cli(cli_path, workspace, env, timeout)
    except asyncio.TimeoutError as exc:
        raise SessionArchiveError('Timed out checking the Kiro CLI version') from exc
    original_env = {**os.environ, **(env or {})}
    data_home = original_env.get('XDG_DATA_HOME') or str(Path(original_env.get('HOME') or str(Path.home())) / '.local' / 'share')
    if not Path(data_home).is_absolute():
        raise ValueError('XDG_DATA_HOME must be an absolute path for isolated forking')
    with tempfile.TemporaryDirectory(prefix='kiro-sdk-store-fork-') as temporary:
        home = Path(temporary) / 'home'
        home.mkdir(mode=0o700)
        scratch = Path(temporary) / 'probe'
        scratch.mkdir(mode=0o700)
        isolated_env = {**(env or {}), 'HOME': str(home), 'KIRO_HOME': str(home / '.kiro'),
                        'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_CACHE_HOME': str(home / '.cache'),
                        'XDG_STATE_HOME': str(home / '.local' / 'state'), 'XDG_DATA_HOME': data_home}
        await _verify_isolation(str(scratch), cli_path, env, isolated_env, timeout)
        imported = await import_session_archive(source, cwd=workspace, cli_path=cli_path, env=isolated_env,
                                                max_bytes=max_bytes, max_uncompressed_bytes=max_uncompressed_bytes,
                                                timeout=timeout)
        result = await fork_session(imported, cwd=workspace, up_to_message_id=up_to_message_id,
                                    title=title, cli_path=cli_path, env=isolated_env, timeout=timeout)
        if result.session_id == session_id:
            raise SessionArchiveError('Kiro returned the source ID for the new fork')
        archive = await export_session_archive(result.session_id, cwd=workspace, cli_path=cli_path, env=isolated_env,
                                               max_bytes=max_bytes, max_uncompressed_bytes=max_uncompressed_bytes,
                                               timeout=timeout)
        try:
            await _write_archive_to_store(result.session_id, session_store, _store_key(result.session_id, directory),
                                          archive, batch_size=batch_size, max_messages=max_messages,
                                          title=title.strip() if title else None)
        except Exception as exc:
            raise StoreForkError(result.session_id) from exc
        return result
