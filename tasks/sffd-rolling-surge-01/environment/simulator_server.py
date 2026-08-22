#!/usr/bin/env python3
"""Unix-socket sidecar for the SFFD simulator.

The Harbor agent container receives only the ``emsctl`` client and a socket
volume.  The private scenario, calibration tables, RNG seed, and verifier
state remain in this process/container.  Requests and responses are one JSON
object per line so the transport has the same stable shape as the local CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import socketserver
import sys
import threading
from typing import Any

# Support both the container's ``PYTHONPATH`` and direct local invocation.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from simulator.simulator import Simulator


class _State:
    def __init__(self, scenario: str, state_dir: str) -> None:
        self.simulator = Simulator(scenario, state_dir=state_dir, load_existing=True)
        self.lock = threading.RLock()


class _Handler(socketserver.StreamRequestHandler):
    state: _State

    def handle(self) -> None:  # pragma: no cover - exercised through socket smoke
        for line in self.rfile:
            if not line.strip():
                continue
            try:
                command = json.loads(line.decode("utf-8"))
                if not isinstance(command, dict):
                    raise ValueError("command must be a JSON object")
                with self.state.lock:
                    name = str(command.get("command", command.get("action", ""))).lower()
                    if name in {"observe", "status"}:
                        response: Any = {
                            "ok": True,
                            "command": "observe",
                            **self.state.simulator.observe(),
                        }
                    else:
                        response = self.state.simulator.step(command)
            except Exception as exc:  # Keep the socket alive after bad input.
                response = {"ok": False, "code": "sidecar_error", "error": str(exc)}
            encoded = json.dumps(response, ensure_ascii=True, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8") + b"\n"
            self.wfile.write(encoded)
            self.wfile.flush()


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, type=pathlib.Path)
    parser.add_argument("--socket", required=True, type=pathlib.Path)
    parser.add_argument("--state-dir", default=os.environ.get("EMS_STATE_DIR", "/tmp/ems-state"),
                        type=pathlib.Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.socket.parent.mkdir(parents=True, exist_ok=True)
    try:
        args.socket.unlink()
    except FileNotFoundError:
        pass
    state = _State(str(args.scenario), str(args.state_dir))
    _Handler.state = state
    server = _Server(str(args.socket), _Handler)
    try:
        # The Agent mount is read-only, so it cannot replace or unlink this
        # endpoint.  A world-readable/writable socket avoids relying on the
        # two separately-built images assigning the same numeric GID to their
        # ``harbor`` group; the sidecar remains the only process that can
        # mutate simulator state.
        os.chmod(args.socket, 0o666)
    except OSError:
        pass
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        try:
            args.socket.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
