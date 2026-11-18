#!/usr/bin/env python3
"""Synthetic Kiro CLI device flow for AgentCore tests."""
import os
import sys
import time
from pathlib import Path

home = Path(os.environ["HOME"])
authenticated = home / ".fake-authenticated"
approved = home / ".fake-approve"
command = sys.argv[1] if len(sys.argv) > 1 else ""
if command == "whoami":
    sys.exit(0 if authenticated.exists() else 1)
if command == "logout":
    authenticated.unlink(missing_ok=True)
    sys.exit(0)
if command == "login":
    if not sys.stdin.isatty() or os.environ.get("KIRO_API_KEY") != "":
        sys.exit(2)
    if "pro" in sys.argv:
        print("? Enter Start URL › default", end="", flush=True)
        sys.stdin.readline()
        print("\r? Enter Region › default", end="", flush=True)
        sys.stdin.readline()
    url = "https://evil.example/steal" if os.environ.get("FAKE_DEVICE_BAD_URL") else "https://view.awsapps.com/start/#/device"
    print("\nCode: ABCD-1234\n\nOpen this URL: " + url, flush=True)
    while not approved.exists():
        time.sleep(0.05)
    approved.unlink()
    authenticated.touch()
    sys.exit(0)
os.execv(str(Path(__file__).with_name("fake_kiro.py")), ["fake_kiro.py", *sys.argv[1:]])
