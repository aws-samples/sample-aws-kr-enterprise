#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${KIRO_SDK_PYTHON:-python3}"
temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "$temporary_dir"' EXIT

"$python_bin" -m pip wheel "$repo_dir" --no-deps --wheel-dir "$temporary_dir/wheels"
shopt -s nullglob
wheels=("$temporary_dir"/wheels/kiro_agent_sdk-*.whl)
if [[ ${#wheels[@]} -ne 1 ]]; then
    printf '%s\n' "Expected a kiro_agent_sdk wheel. Upgrade pip and try again." >&2
    exit 1
fi
"$python_bin" -m pip install --no-deps --target "$temporary_dir/site" "${wheels[0]}"
mkdir "$temporary_dir/bin"
ln -s "$repo_dir/tests/fake_kiro.py" "$temporary_dir/bin/kiro-cli"
cd "$temporary_dir"
export PATH="$temporary_dir/bin:$PATH"
export PYTHONPATH="$temporary_dir/site"
export KIRO_SDK_CHECK_README="$repo_dir/README.md"
export KIRO_SDK_CHECK_SITE="$temporary_dir/site"
"$python_bin" -c 'import importlib.metadata, os, pathlib, kiro_agent_sdk; p = pathlib.Path(kiro_agent_sdk.__file__).parent; assert p == pathlib.Path(os.environ["KIRO_SDK_CHECK_SITE"]) / "kiro_agent_sdk"; assert importlib.metadata.version("kiro-agent-sdk") == kiro_agent_sdk.__version__; assert all((p / name).is_file() for name in ("py.typed", "_mcp_bridge.py", "_hook_bridge.py"))'
"$python_bin" -c 'from kiro_agent_sdk.sync import list_models; assert list_models()[0]["model_id"] == "example"'
"$python_bin" "$repo_dir/examples/quickstart.py"
"$python_bin" "$repo_dir/examples/conversation.py"
"$python_bin" "$repo_dir/examples/session_management.py"
"$python_bin" - <<'PY'
import os
import re
import subprocess
import sys
from pathlib import Path

text = Path(os.environ["KIRO_SDK_CHECK_README"]).read_text(encoding="utf-8")
examples = re.findall(r"(?ms)^```python\n(.*?)^```", text)
if not examples:
    raise RuntimeError("README contains no Python examples")
for example in examples:
    subprocess.run([sys.executable, "-c", example], check=True, timeout=30)
PY
# The installed wheel, not the checkout, must pass the hermetic suite.
PYTHONPATH="$temporary_dir/site:$repo_dir/tests" "$python_bin" "$repo_dir/scripts/run_tests_bounded.py" \
    --start-directory "$repo_dir/tests" --timeout "${KIRO_SDK_TEST_MODULE_TIMEOUT:-900}"
"$python_bin" - <<'PY'
import asyncio
import json
import os
import shlex

from kiro_agent_sdk import HookMatcher
from kiro_agent_sdk.hooks import NativeHookBridge

async def main():
    async def block(event, tool_id, context):
        return {"block": "blocked"}
    bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[block])]})
    command, env = await bridge.start()
    try:
        process = await asyncio.create_subprocess_exec(
            *shlex.split(command), env={**os.environ, **env},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(json.dumps(
            {"hook_event_name": "preToolUse", "tool_name": "example"}
        ).encode())
        assert process.returncode == 2 and b"blocked" in stderr
    finally:
        await bridge.close()

asyncio.run(main())
PY
