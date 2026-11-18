"""Stdio-to-local-socket shim started by Kiro; tool handlers stay in the SDK host."""
import sys

if (__name__ == "__main__" and sys.path and
        sys.path[0].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] ==
        __file__.replace("\\", "/").rsplit("/", 2)[-2]):
    # The package's types.py must not shadow the standard-library types module.
    del sys.path[0]

import os
import socket
import threading

sock = socket.create_connection(("127.0.0.1", int(os.environ["KIRO_SDK_MCP_PORT"])))
sock.sendall((os.environ["KIRO_SDK_MCP_TOKEN"] + "\n").encode())

def forward_input():
    try:
        # A daemon blocked in BufferedReader.readline() can hold its lock while
        # Python finalizes after the peer closes. Raw reads preserve the byte
        # stream without leaving the buffered stdin lock held at shutdown.
        descriptor = sys.stdin.fileno()
        while data := os.read(descriptor, 65536):
            sock.sendall(data)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

threading.Thread(target=forward_input, daemon=True).start()
try:
    with sock.makefile("rb") as stream:
        for line in stream:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
finally:
    sock.close()
