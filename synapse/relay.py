# SPDX-License-Identifier: GPL-3.0-or-later
# synapse/relay.py
# -----------------------------------------------------------------------------
# Synapse Relay - the local HTTP bar that links the in-game Synapse executor
# (running inside Delta / any Luau executor) to the Python side (MCP server,
# CLI controller, or the ZeroScript bridge as an addon server).
#
# Live on 0.0.0.0 so the in-game executor can reach it from the same device.
# The executor long-polls GET /cmd, runs the code, then POSTs the result back.
# POST /execute is the synchronous bridge used by the MCP server: it queues a
# command and blocks until the executor answers (or the timeout expires).
# -----------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
SYNAPSE_LUA_PATH = HERE / "synapse.lua"
PORT = int(os.environ.get("SYNAPSE_RELAY_PORT", "18081"))

_lock = threading.Lock()
_cond = threading.Condition(_lock)
_pending: deque[str] = deque()          # "id\npayload" jobs for the executor
_results: dict[str, dict] = {}          # id -> {"ok": bool, "out": str, "err": str}
_last_result: dict | None = None        # latest completed result (for GET /result)
_last_alive: float = 0.0                # last executor heartbeat


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _free_result() -> None:
    global _last_result
    _last_result = None


def _enqueue(payload: str) -> str:
    """Queue one job for the executor and return its id."""
    job_id = f"S-{uuid.uuid4().hex[:12]}"
    with _cond:
        _pending.append(job_id + "\n" + payload)
        _cond.notify_all()
    return job_id


def _wait_result(job_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + max(0.1, timeout)
    with _cond:
        while job_id not in _results:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"ok": False, "out": "", "err": f"timeout after {timeout:.1f}s (no executor answered)"}
            _cond.wait(remaining)
        return _results.pop(job_id)


def _serve_synapse() -> bytes:
    if SYNAPSE_LUA_PATH.is_file():
        return SYNAPSE_LUA_PATH.read_text(encoding="utf-8").encode("utf-8")
    return b"-- synapse.lua not found next to relay.py --"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console clean
        return

    def _send(self, code: int, body: dict | str | bytes, ctype: str = "application/json") -> None:
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> str:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length).decode("utf-8", "replace") if length else ""

    def _json_body(self) -> dict:
        try:
            return json.loads(self._body())
        except Exception:
            return {}

    def do_GET(self) -> None:
        global _last_alive, _last_result
        path = urlparse(self.path).path
        if path == "/health":
            with _cond:
                alive = _last_alive > 0 and time.time() - _last_alive < 10
            self._send(200, {"ok": True, "relay": "synapse", "executor_alive": alive,
                             "queue": len(_pending), "results": len(_results)})
            return
        if path == "/synapse":
            self._send(200, _serve_synapse(), "text/plain; charset=utf-8")
            return
        if path == "/cmd":
            with _cond:
                _last_alive = time.time()
                if _pending:
                    raw = _pending.popleft()
                    job_id, _, payload = raw.partition("\n")
                    self._send(200, {"id": job_id, "cmd": payload})
                else:
                    self._send(200, {"id": "", "cmd": ""})
            return
        if path == "/result":
            with _cond:
                self._send(200, _last_result if _last_result else {"id": "", "ok": False,
                                                                   "out": "", "err": "no result"})
            return
        self._send(404, {"ok": False, "err": "not found"})

    def do_POST(self) -> None:
        global _last_result
        path = urlparse(self.path).path
        if path == "/cmd":
            # Direct queue from a shell/curl user: body is the raw command text.
            job_id = _enqueue(self._body())
            self._send(202, {"ok": True, "id": job_id})
            return
        if path == "/result":
            data = self._json_body()
            job_id = data.get("id", "")
            result = {
                "ok": bool(data.get("ok", True)),
                "out": data.get("out", ""),
                "err": str(data.get("err", "")),
            }
            with _cond:
                _results[job_id] = result
                _last_result = {"id": job_id, **result}
                _cond.notify_all()
            self._send(200, {"ok": True})
            return
        if path == "/execute":
            data = self._json_body()
            payload = data.get("cmd", "")
            timeout = float(data.get("timeout", 30) or 30)
            raw = bool(data.get("raw", True))
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload, separators=(",", ":"))
            job_id = _enqueue(payload)
            result = _wait_result(job_id, timeout)
            result["id"] = job_id
            self._send(200, result)
            return
        self._send(404, {"ok": False, "err": "not found"})


def main() -> int:
    print("[synapse] relay starting")
    print(f"[synapse]   http://127.0.0.1:{PORT}")
    print("[synapse]   GET /health     - status")
    print("[synapse]   GET /synapse    - serve the in-game executor")
    print("[synapse]   POST /cmd       - queue raw code")
    print("[synapse]   POST /execute   - queue + wait (MCP / synchronous)")
    print("[synapse]   POST /result    - executor reports back")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[synapse] stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
