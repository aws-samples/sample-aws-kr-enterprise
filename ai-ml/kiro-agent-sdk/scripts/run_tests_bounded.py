"""Run unittest modules with a process deadline; never hide failures or skips."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest


def stop(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The group can outlive its leader, even after a successful test run.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-directory", type=Path, default=Path("tests"))
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("tests", nargs="*")
    args = parser.parse_args()
    if not 0 < args.timeout <= 86400:
        parser.error("--timeout must be in (0, 86400]")
    root = args.start_directory.resolve(strict=True)
    if args.child:
        loader = unittest.TestLoader()
        suite = (loader.loadTestsFromNames(args.tests) if args.tests else
                 loader.discover(str(root)))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.testsRun:
            print("ERROR: selected module ran zero tests", file=sys.stderr)
        return 0 if result.testsRun and result.wasSuccessful() else 1
    child = [sys.executable, str(Path(__file__).resolve()), "--child", "--start-directory", str(root)]
    if args.tests:
        commands = [(name, [*child, name]) for name in args.tests]
    else:
        commands = [(path.stem, [*child, path.stem]) for path in sorted(root.glob("test_*.py"))]
    if not commands:
        parser.error("No test modules selected")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (environment.get("PYTHONPATH"), str(root))))
    failures = []
    for name, command in commands:
        print(f"Running {name} (deadline {args.timeout:g}s)", flush=True)
        process = subprocess.Popen(command, env=environment, start_new_session=os.name == "posix")
        try:
            result = process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            stop(process)
            print(f"TIMEOUT: {name}", file=sys.stderr, flush=True)
            failures.append(name)
            continue
        except BaseException:
            stop(process)
            raise
        stop(process)
        if result:
            failures.append(name)
    if failures:
        print("Failed or timed out modules: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
