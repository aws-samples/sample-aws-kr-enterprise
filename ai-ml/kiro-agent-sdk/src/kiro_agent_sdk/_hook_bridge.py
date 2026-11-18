"""One-shot native hook shim; failure denies the tool call."""
import sys

if (__name__ == "__main__" and sys.path and
        sys.path[0].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] ==
        __file__.replace("\\", "/").rsplit("/", 2)[-2]):
    # The package's types.py must not shadow the standard-library types module.
    del sys.path[0]

import json
import os
import socket
import hashlib
import stat

try:
    data = json.load(sys.stdin)
    if len(sys.argv) == 4 and sys.argv[1] == "--credentials":
        fd = os.open(sys.argv[2], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Invalid hook credentials")
            encoded = stream.read(4097)
        if len(encoded) > 4096 or hashlib.sha256(encoded).hexdigest() != sys.argv[3]:
            raise ValueError("Changed hook credentials")
        credentials = json.loads(encoded)
        port, token = credentials["port"], credentials["token"]
    else:
        port, token = int(os.environ['KIRO_SDK_HOOK_PORT']), os.environ['KIRO_SDK_HOOK_TOKEN']
    with socket.create_connection(('127.0.0.1', port), timeout=60) as sock:
        sock.sendall((token + '\n' + json.dumps(data) + '\n').encode())
        with sock.makefile('rb') as stream:
            result = json.loads(stream.readline())
    if result.get('warning'):
        print('SDK hook failed', file=sys.stderr)
    if result.get('block'):
        if str(data.get('hook_event_name', '')).casefold() == 'stop':
            print(json.dumps({'decision': 'block', 'reason': result.get('reason', 'Continue working')}))
        else:
            print(result.get('reason', 'Blocked by SDK hook'), file=sys.stderr)
            sys.exit(2)
    elif result.get('ask'):
        print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'ask',
                                                  'permissionDecisionReason': result['ask']}}))
    elif result.get('context'):
        print(result['context'])
except Exception as exc:
    print('SDK hook failed: ' + str(exc), file=sys.stderr)
    # Kiro's Stop handler interprets exit 1 as a request for another model turn.
    # Exit 2 reports a failed hook without manufacturing a continue decision.
    sys.exit(2)
