"""Prepare subprocess environment consistently with the selected workspace."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union


# Inherited variables kept in "minimal" environment mode; everything else is passed blank.
INHERITED_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TERM", "TMPDIR", "TEMP", "TMP",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "NODE_USE_ENV_PROXY",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "KIRO_HOME",
})


def blank_non_allowlisted_environment() -> dict[str, str]:
    """Blank every inherited variable outside the allowlist (values only; names may remain visible)."""
    return {name: "" for name in os.environ
            if name not in INHERITED_ENV_ALLOWLIST and not name.startswith("LC_")}


def child_process_env(cwd: Union[str, Path, None], overrides: Optional[dict[str, str]]) -> dict[str, str]:
    environment = {**os.environ, **(overrides or {})}
    if cwd is not None:
        environment["PWD"] = str(Path(cwd).resolve())
    return environment
