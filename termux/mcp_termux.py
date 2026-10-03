# SPDX-License-Identifier: GPL-3.0-or-later
# termux/mcp_termux.py
# -----------------------------------------------------------------------------
# Termux MCP server (stdio JSON-RPC). Gives the AI agent a shell and file
# access on the Android device itself, alongside the in-game Synapse tools:
#
#   bridge.py  --(stdio JSON-RPC)-->  mcp_termux.py  --(sh)-->  Termux
#
# Registered AUTOMATICALLY by bridge.py on Android/Termux
# (_ensure_android_local_servers) - no panel step needed. On other platforms
# add it manually from the ZeroScript panel:
#   Name: Termux   Command: termux/mcp_termux.py
# (a relative .py path is resolved against the repo root and launched with
# the bridge's own Python interpreter.)
#
# What the agent can do once connected:
#   termux.ping        - health/info about this device-side server
#   termux.exec        - run a shell command  (pip install ..., python3 ..., git,
#                        curl, am, termux-api commands, ...)
#   termux.read_file   - read a text file (path on the device)
#   termux.write_file  - write/append a text file (creates parent folders)
#
# Safety rails (deliberate, not optional): commands run NON-interactively
# (stdin closed) with a hard timeout and capped output; they run as the same
# Termux user the bridge runs as - i.e. exactly what the user could type in
# their own terminal, nothing more. Long-lived processes (servers, watchers)
# should be started detached (e.g. 'nohup ... &') or they die on timeout.
# -----------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
HOME = os.path.expanduser("~")

PROTOCOL_VERSION = os.environ.get("TERMUX_MCP_PROTOCOL", "2024-11-05")
SERVER_NAME = "termux"
SERVER_VERSION = "1.1.0"

EXEC_DEFAULT_TIMEOUT = 30.0
EXEC_MAX_TIMEOUT = 120.0
MAX_STREAM_CHARS = 60_000   # per stream; the TAIL is kept (the error is at the end)
READ_MAX_CHARS = 50_000
WRITE_MAX_CHARS = 200_000

_BOOT_TS = time.time()


# -----------------------------------------------------------------------------
#  TOOL CATALOGUE
# -----------------------------------------------------------------------------

def _tool(name: str, description: str, props: dict, required: list[str] | None = None):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "additionalProperties": False,
            "required": required or [],
        },
    }


def _seconds(desc: str):
    return {"type": "number", "description": desc}


TOOLS = [
    _tool(
        "termux.ping",
        "Health check for the Termux (on-device shell) server: Python/platform "
        "info, whether Termux markers are present, uptime.",
        {"timeout": _seconds("Seconds to wait (default 10).")},
    ),
    _tool(
        "termux.exec",
        "Run a shell command on the bridge host (Termux, or cmd.exe on Windows) (e.g. 'pip install "
        "mss', 'python3 script.py', 'git pull', 'am broadcast ...', 'termux-"
        "battery-status'). Returns exit code, stdout and stderr. Runs non-"
        "interactively with stdin closed; for long-lived processes start them "
        "detached ('nohup cmd >log 2>&1 &').",
        {
            "command": {"type": "string", "description": "Shell command line (cmd.exe /c on Windows, /bin/sh -c on POSIX)."},
            "cwd": {"type": "string", "description": "Working directory (default: the Termux home)."},
            "timeout": _seconds(f"Seconds to wait (default 30, max {int(EXEC_MAX_TIMEOUT)})."),
        },
        ["command"],
    ),
    _tool(
        "termux.read_file",
        "Read a text file on the device. Large files are returned from the "
        "start, capped, with the total byte size reported.",
        {
            "path": {"type": "string", "description": "Absolute or ~ path to the file."},
            "timeout": _seconds("Seconds to wait (default 10)."),
        },
        ["path"],
    ),
    _tool(
        "termux.write_file",
        "Write (or append) text to a file on the device. Parent folders are "
        "created as needed. Returns the byte count written.",
        {
            "path": {"type": "string", "description": "Absolute or ~ path to the file."},
            "content": {"type": "string", "description": "Text content to write (UTF-8)."},
            "append": {"type": "boolean", "description": "Append instead of overwrite (default false)."},
            "timeout": _seconds("Seconds to wait (default 10)."),
        },
        ["path", "content"],
    ),
]


# -----------------------------------------------------------------------------
#  TOOL IMPLEMENTATIONS
# -----------------------------------------------------------------------------

def _text_result(text: str, is_error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _tail(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return f"[... {len(text) - cap} chars trimmed from the start ...]\n" + text[-cap:]


def _clamp_timeout(value, default=EXEC_DEFAULT_TIMEOUT) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return default
    return max(1.0, min(EXEC_MAX_TIMEOUT, t))


def _in_termux() -> bool:
    return bool(
        "TERMUX_VERSION" in os.environ
        or os.environ.get("PREFIX", "").startswith("/data/data/com.termux")
    )


def _shell_argv(command: str) -> list[str]:
    """Host-native shell. /bin/sh does not exist on Windows (WinError 2)."""
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/c", command]
    sh = "/bin/sh"
    if not os.path.isfile(sh):
        sh = "sh"
    return [sh, "-c", command]


def _tool_ping(_args: dict) -> dict:
    in_termux = _in_termux()
    argv = _shell_argv("echo ok")
    uptime = int(time.time() - _BOOT_TS)
    host = "Termux/Android" if in_termux else (
        "Windows (not Termux — exec uses cmd.exe)" if os.name == "nt"
        else "POSIX host (not Termux)"
    )
    lines = [
        "termux.ping OK",
        f"host: {host}",
        f"termux_markers: {'yes' if in_termux else 'no'}",
        f"shell: {' '.join(argv[:2])}",
        f"python: {platform.python_version()} ({sys.executable})",
        f"platform: {platform.platform()}",
        f"home: {HOME}",
        f"repo_root: {REPO_ROOT}",
        f"server_uptime: {uptime}s",
    ]
    if os.name == "nt":
        lines.append(
            "note: this is the PC the bridge is on, not an Android phone. "
            "termux.exec runs cmd.exe here (winget/choco/where, not pkg). "
            "Blender is a desktop app — check with: where blender"
        )
    return _text_result("\n".join(lines))


_DRAIN_MEM_CAP = 256 * 1024  # rolling byte cap per stream (tail kept)
_DRAIN_GRACE = 1.5           # s of silence after the shell exits before we stop draining


def _run_shell(command: str, cwd: str, timeout: float):
    """Run the host shell -> (returncode|None, stdout, stderr, timed_out).

    Two failures of the naive subprocess.run(..., timeout=...) it replaces
    (seen live as 'it keeps timing out when it's running cmd'):

    1. communicate() waits for EOF on the output pipes, and EOF only arrives
       when EVERY descendant holding them exits. Any command backgrounding a
       child (`cmd &`, a dev server, npm/pip build steps) burned the FULL
       timeout even though the real work finished instantly - and then came
       back mislabelled TIMEOUT.
    2. timeout kills ONLY the direct child; background grandchildren kept
       running on the phone after the call had 'failed'.

    Design: a single-threaded non-blocking select() pump - the shell is
    watched for EXIT, not pipe EOF (an earlier threaded version deadlocked
    here instead: file.close() blocks while a reader thread is mid-read on
    the same pipe). Once the foreground shell exits we drain for a short
    grace of silence and stop - background daemons keep running detached,
    exactly as documented, without stalling the call. On timeout the WHOLE
    process group is killed (start_new_session makes the shell a group
    leader), so nothing leaks onto the device.
    """
    if os.name != "posix":
        # select() doesn't cover pipes on Windows; keep the old behaviour
        # there (Termux is POSIX, so the fixed path is what matters).
        return _run_shell_fallback(command, cwd, timeout)
    import select
    proc = subprocess.Popen(
        _shell_argv(command),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,   # own process group -> timeout kills children too
    )
    fd_out, fd_err = proc.stdout.fileno(), proc.stderr.fileno()
    bufs = {fd_out: [], fd_err: []}
    for fd in (fd_out, fd_err):
        os.set_blocking(fd, False)
    open_fds = {fd_out, fd_err}
    deadline = time.monotonic() + timeout
    drain_until = None          # set once the shell has exited
    timed_out = False

    def _append(fd, data):
        buf = bufs[fd]
        buf.append(data)
        if sum(map(len, buf)) > _DRAIN_MEM_CAP * 2:
            joined = b"".join(buf)
            bufs[fd] = [joined[-_DRAIN_MEM_CAP:]]

    while open_fds:
        now = time.monotonic()
        shell_gone = proc.poll() is not None
        if not shell_gone and now >= deadline:
            timed_out = True
            break
        if shell_gone and drain_until is None:
            drain_until = now + _DRAIN_GRACE
        if drain_until is not None and now >= drain_until:
            break
        wait_s = 0.2
        if drain_until is not None:
            wait_s = max(0.05, min(wait_s, drain_until - now))
        else:
            wait_s = max(0.05, min(wait_s, deadline - now))
        try:
            ready, _, _ = select.select(sorted(open_fds), [], [], wait_s)
        except (OSError, ValueError):
            break
        if ready:
            for fd in ready:
                try:
                    data = os.read(fd, 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    open_fds.discard(fd)
                    continue
                if data:
                    _append(fd, data)
                    # Fresh output after a finished shell: keep the drain
                    # window from collapsing while output is still flowing.
                    if drain_until is not None:
                        drain_until = max(drain_until, min(time.monotonic() + _DRAIN_GRACE, deadline))
                else:
                    open_fds.discard(fd)  # EOF: every writer on this pipe closed
        elif drain_until is not None and time.monotonic() >= drain_until:
            break

    if timed_out:
        # Kill the GROUP, not just the shell: background children would
        # otherwise outlive the 'failed' call, leaking on the phone.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    for fd in (fd_out, fd_err):
        try:
            os.close(fd)
        except OSError:
            pass
    out = b"".join(bufs[fd_out]).decode("utf-8", errors="replace")
    err = b"".join(bufs[fd_err]).decode("utf-8", errors="replace")
    return proc.returncode, out, err, timed_out


def _run_shell_fallback(command: str, cwd: str, timeout: float):
    """Non-POSIX path (Windows manual use): plain subprocess.run semantics."""
    try:
        proc = subprocess.run(
            _shell_argv(command),
            cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        return (proc.returncode,
                proc.stdout.decode("utf-8", errors="replace"),
                proc.stderr.decode("utf-8", errors="replace"), False)
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        out = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        raw2 = exc.stderr or b""
        err = raw2 if isinstance(raw2, str) else raw2.decode("utf-8", errors="replace")
        return None, out, err, True


def _tool_exec(args: dict) -> dict:
    command = str(args.get("command") or "").strip()
    if not command:
        return _text_result("ERROR: 'command' is required.", True)
    cwd = os.path.abspath(os.path.expanduser(str(args.get("cwd") or HOME)))
    if not os.path.isdir(cwd):
        return _text_result(f"ERROR: cwd does not exist or is not a directory: {cwd}", True)
    timeout = _clamp_timeout(args.get("timeout"))
    started = time.monotonic()
    try:
        code, out, err, timed_out = _run_shell(command, cwd, timeout)
    except Exception as exc:
        return _text_result(f"ERROR: could not run command: {exc}", True)
    elapsed = time.monotonic() - started
    if timed_out:
        return _text_result(
            f"cwd: {cwd}\nexit_code: TIMEOUT (process group killed after {elapsed:.1f}s; cap {timeout:.0f}s)\n"
            f"--- stdout (partial) ---\n{_tail(out.rstrip(), MAX_STREAM_CHARS)}\n"
            f"--- stderr (partial) ---\n{_tail(err.rstrip(), MAX_STREAM_CHARS)}\n"
            f"(foreground command killed with its children; if this should keep running, "
            f"relaunch detached: nohup <cmd> >log 2>&1 &)",
            True,
        )
    text = (
        f"cwd: {cwd}\nexit_code: {code}\nelapsed: {elapsed:.1f}s\n"
        f"--- stdout ---\n{_tail(out.rstrip(), MAX_STREAM_CHARS) or '(empty)'}\n"
        f"--- stderr ---\n{_tail(err.rstrip(), MAX_STREAM_CHARS) or '(empty)'}"
    )
    return _text_result(text)


def _resolve_path(raw, must_exist: bool) -> str:
    path = os.path.abspath(os.path.expanduser(str(raw or "").strip()))
    if not path or path == os.path.abspath(os.sep):
        raise ValueError("a file 'path' is required")
    if must_exist and not os.path.isfile(path):
        raise FileNotFoundError(f"no such file: {path}")
    return path


def _tool_read_file(args: dict) -> dict:
    try:
        path = _resolve_path(args.get("path"), must_exist=True)
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            raw = fh.read(READ_MAX_CHARS * 2)
        text = raw.decode("utf-8", errors="replace")[:READ_MAX_CHARS]
        truncated = size > READ_MAX_CHARS
        header = f"path: {path}\nsize: {size} bytes"
        if truncated:
            header += f"  (showing first {READ_MAX_CHARS} chars)"
        return _text_result(f"{header}\n---\n{text}")
    except (ValueError, FileNotFoundError, PermissionError, IsADirectoryError, OSError) as exc:
        return _text_result(f"ERROR: {exc}", True)


def _tool_write_file(args: dict) -> dict:
    try:
        path = _resolve_path(args.get("path"), must_exist=False)
        content = str(args.get("content") or "")
        if len(content) > WRITE_MAX_CHARS:
            return _text_result(
                f"ERROR: content is {len(content)} chars (cap {WRITE_MAX_CHARS}). Split the write or use termux.exec with a heredoc.",
                True,
            )
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        append = bool(args.get("append"))
        data = content.encode("utf-8")
        with open(path, "ab" if append else "wb") as fh:
            fh.write(data)
        verb = "appended" if append else "wrote"
        return _text_result(f"{verb} {len(data)} bytes -> {path}")
    except (ValueError, PermissionError, IsADirectoryError, OSError) as exc:
        return _text_result(f"ERROR: {exc}", True)


def _call_tool(name: str, arguments: dict) -> dict:
    if name == "termux.ping":
        return _tool_ping(arguments)
    if name == "termux.exec":
        return _tool_exec(arguments)
    if name == "termux.read_file":
        return _tool_read_file(arguments)
    if name == "termux.write_file":
        return _tool_write_file(arguments)
    return _text_result(f"ERROR: unknown tool: {name}", True)


# -----------------------------------------------------------------------------
#  STDIO JSON-RPC LOOP  (mirrors synapse/mcp_synapse.py)
# -----------------------------------------------------------------------------

def _respond(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def _notify(method: str, params):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
    sys.stdout.flush()


def _tools_result() -> dict:
    return {"tools": TOOLS}


def main() -> int:
    sys.stderr.write(f"[termux] MCP server '{SERVER_NAME}' v{SERVER_VERSION}\n")
    sys.stderr.flush()
    _notify("notifications/initialized", {})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as exc:
            _respond(None, error={"code": -32700, "message": f"parse error: {exc}"})
            continue
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        if msg_id is None:
            continue  # stray notification from the bridge

        try:
            if method == "initialize":
                _respond(msg_id, {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                })
            elif method == "ping":
                _respond(msg_id, {})
            elif method == "tools/list":
                _respond(msg_id, _tools_result())
            elif method == "tools/call":
                name = params.get("name", "")
                arguments = params.get("arguments") or {}
                if name not in {t["name"] for t in TOOLS}:
                    _respond(msg_id, error={"code": -32602, "message": f"unknown tool: {name}"})
                else:
                    try:
                        result = _call_tool(name, arguments)
                    except Exception as exc:
                        result = {"content": [{"type": "text", "text": f"ERROR: {exc}"}], "isError": True}
                    _respond(msg_id, result)
            else:
                _respond(msg_id, error={"code": -32601, "message": f"method not found: {method}"})
        except Exception as exc:
            _respond(msg_id, error={"code": -32603, "message": str(exc)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
