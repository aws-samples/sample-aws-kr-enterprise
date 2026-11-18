#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${KIRO_SDK_PYTHON:-python3}"
cd "$repo_dir"
PYTHONPATH="$repo_dir/src${PYTHONPATH:+:$PYTHONPATH}" "$python_bin" scripts/run_tests_bounded.py \
    --start-directory tests --timeout "${KIRO_SDK_TEST_MODULE_TIMEOUT:-900}"
PYTHONPATH="$repo_dir/src${PYTHONPATH:+:$PYTHONPATH}" "$python_bin" scripts/generate_api_reference.py --check
