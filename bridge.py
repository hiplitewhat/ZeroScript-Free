# # SPDX-License-Identifier: GPL-3.0-or-later
# bridge.py
# ──────────────────────────────────────────────────────────────────────────
#  ZeroScript Bridge
#  Local WebSocket <-> Roblox Studio MCP server.
#  The browser extension talks to this over ws://127.0.0.1:<PORT>.
#
#  What this bridge exposes to Kimi (aggregated into one tools/list):
#    - Every MCP server declared in config.json (by default: roblox), each
#      spawned as a stdio child and routed by tool name.
#
#  Design goals (robustness first):
#   - Each MCP stdio process is read by ONE dedicated thread; responses are
#     matched by JSON-RPC id (no "read the next line and hope" races).
#   - stderr is drained so a child never blocks on a full pipe.
#   - A dead server is auto-restarted and the failing call retried once.
#   - Tool calls are locked PER SERVER, so a slow server never blocks another.
#   - Every call ALWAYS produces a reply: a result OR a structured error.
#     Nothing ever hangs the agentic loop silently.
# ──────────────────────────────────────────────────────────────────────────
import asyncio
import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

try:
    # Sibling script (same folder as bridge.py, which Python puts on sys.path
    # automatically) - reused here purely to detect a Studio version bump
    # (see _current_studio_exe below), not to launch anything.
    import launch_studio_mcp as _studio_scan
except Exception:
    _studio_scan = None

try:
    import websockets
except ImportError:
    print("[bridge] Missing dependency. Run:  pip install websockets")
    sys.exit(1)

# Windows consoles often default to a legacy codepage (cp1252): printing
# non-ASCII text then raises UnicodeEncodeError INSIDE the WS handler, which
# kills the connection. Force UTF-8 (best effort). We also keep all console
# output strictly ASCII (no arrows / dots) so nothing garbles on a console that
# stayed on a legacy codepage anyway.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _enable_ansi_colors():
    """On Windows, turn on ANSI escape processing so color codes render instead
    of printing as literal gibberish like "<ESC>[92m". Returns True on success."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def _host_is_android():
    """True on Android (Termux and friends). Uses the same marker convention as
    synapse/android_screenshot.py."""
    env = os.environ
    return bool(
        "TERMUX_VERSION" in env
        or "TERMUX_APP__PACKAGE_NAME" in env
        or env.get("PREFIX", "").startswith("/data/data/com.termux")
        or env.get("ANDROID_ROOT") == "/system"
    )


def _platform_supports_studio_mcp():
    """The primary 'roblox' server launches StudioMCP, which ships only with
    desktop Roblox Studio (Windows / macOS). On any other host (Android/Termux,
    Linux) that child exits instantly - and server_watch would otherwise
    restart-loop it forever (seen live on Termux: 'exit code 1' + 'Broken
    pipe' spam every ~15s burying the bridge's real status)."""
    return sys.platform in ("win32", "darwin")


HOST = "127.0.0.1"
# Keep in sync with zeroscript-extension/manifest.json "version" - printed at
# startup so a user's terminal output alone tells us which build they're on.
BRIDGE_VERSION = "1.4.9"
PORT = int(os.environ.get("ZS_BRIDGE_PORT", "17613"))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# The primary server. It is always present, added by the installer, and can
# never be edited/removed through the extension (it is what ZeroScript is FOR).
PRIMARY_SERVER_ID = "roblox"

if _enable_ansi_colors():
    C = {
        "reset": "\033[0m", "dim": "\033[2m", "gr": "\033[92m",
        "yl": "\033[93m", "rd": "\033[91m", "cy": "\033[96m",
        # Bold white-on-red: for a non-technical user, an "ACTION NEEDED" step
        # must look nothing like the routine cyan/yellow status noise around
        # it, or it gets scrolled past unread (seen live 2026-07-13 - the
        # toggle instruction and the boot banner's own yellow re-explanation
        # of the SAME step were visually indistinguishable). Bright-yellow-bg
        # with black text was tried first but reads as low-contrast/washed
        # out on several real terminal color schemes (also seen live) - white
        # on red is the universal high-contrast "act now" pairing.
        "act": "\033[1m\033[97m\033[41m",
    }
else:
    C = {k: "" for k in ("reset", "dim", "gr", "yl", "rd", "cy", "act")}

# Every run appends here (never truncated), so a whole test session - across
# multiple restarts - stays in one file the user can just send us. Each
# process start writes a banner (see main()) so restarts are easy to spot.
LOGS_DIR = os.path.join(HERE, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOGS_DIR, "bridge_debug.log")
try:
    _log_file = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
except Exception:
    _log_file = None


class _Spinner:
    """Terminal-only progress indicator for waits that can run several seconds
    (server launch/handshake, Studio attach grace period) so the console never
    just sits there looking dead - the #1 thing that makes a user assume the
    bridge hung and close the window. Purely cosmetic: writes over its own line
    with \\r, never touches bridge_debug.log, and is skipped entirely when
    stdout isn't a real console (redirected to a file, no ANSI)."""
    FRAMES = "|/-\\"
    # Only ONE spinner may animate at a time: server launches now run in
    # PARALLEL (see MCPManager.start_all), and several spinners fighting over
    # the same console line with \r produced interleaved garbage. Whoever
    # acquires this lock animates; the others silently skip (the log lines
    # around them still tell the story).
    _active = threading.Lock()

    def __init__(self, label):
        self.label = label
        self._stop = threading.Event()
        self._thread = None
        self._owns_lock = False

    def __enter__(self):
        if sys.stdout.isatty() and _Spinner._active.acquire(blocking=False):
            self._owns_lock = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            # Wipe the spinner line so the next log() line doesn't get glued
            # onto trailing spinner characters.
            print("\r" + " " * (len(self.label) + 4) + "\r", end="", flush=True)
        if self._owns_lock:
            _Spinner._active.release()

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            print(f"\r{C['dim']}{self.label} {frame}{C['reset']}", end="", flush=True)
            i += 1
            self._stop.wait(0.15)


def _clear_spinner_line():
    """Wipe whatever a live _Spinner (running on its own thread, mid-frame) left
    on the current console line via bare \\r writes, so the next print() below
    doesn't get glued onto its trailing characters - seen live 2026-07-14: an
    action_banner() fired while '[roblox] starting... -' was still mid-line and
    the red box rendered smashed onto it instead of starting on a fresh line.
    \\033[K (clear to end of line) doesn't depend on knowing the spinner's label
    length the way Spinner.__exit__'s own wipe does."""
    if sys.stdout.isatty():
        print("\r\033[K", end="", flush=True)


def log(msg, color="dim", terminal=True):
    """terminal=False: written to bridge_debug.log only, not the console. Use
    for noisy/technical detail (raw stderr from child MCP servers, per-call
    traces) that would bury the handful of lines a non-technical user actually
    needs to read. Nothing is ever lost - it all still lands in the file."""
    if terminal:
        _clear_spinner_line()
        ts = time.strftime("%H:%M:%S")
        print(f"{C['dim']}{ts}{C['reset']} {C.get(color,'')}{msg}{C['reset']}", flush=True)
    if _log_file:
        try:
            _log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            _log_file.flush()
        except Exception:
            pass


def action_banner(lines):
    """Print a step the USER must physically go do, styled so it cannot be
    mistaken for routine status/warning noise (see the 'act' color above).
    Framed with blank lines so it visually stands alone in a scrolling
    terminal - a non-technical user should be able to glance at the window
    and immediately spot this without reading everything above it.

    Every line (header, content, footer) is padded to the SAME width so the
    yellow block renders as one clean rectangle - an earlier version padded
    each line to a fixed guess independently, which produced a ragged block
    with mismatched edges on a real console (seen live 2026-07-13)."""
    header = "ACTION NEEDED"
    width = max([len(header) + 8] + [len(ln) for ln in lines]) + 2
    top = f">>> {header} " + ">" * max(0, width - len(header) - 5)
    _clear_spinner_line()
    print()
    print(f"{C['act']}  {top.ljust(width)}{C['reset']}")
    for ln in lines:
        print(f"{C['act']}  {ln.ljust(width)}{C['reset']}")
    print(f"{C['act']}  {'>' * width}{C['reset']}")
    print()
    if _log_file:
        try:
            _log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ACTION NEEDED: "
                             f"{' | '.join(lines)}\n")
            _log_file.flush()
        except Exception:
            pass


# Roblox Studio exposes its built-in MCP server on this loopback port. StudioMCP
# (and our bridge, via it) reaches Studio through it.
STUDIO_MCP_PORT = 13469


def _port_owner(port):
    """(pid, name, path) of the process LISTENING on `port`, or None. Win32 only."""
    if sys.platform != "win32":
        return None
    # BOTH stacks: "-p TCP" alone is IPv4-only, and a squatter listening on
    # [::1]:<port> (IPv6 loopback) was then completely invisible to this probe
    # even while Get-NetTCPConnection showed it plainly (the likely reason the
    # boot-time squatter check stayed silent on a machine where ropilot
    # provably held the port - see the 2026-07-13 live report).
    out = ""
    for proto in ("TCP", "TCPv6"):
        try:
            out += subprocess.run(
                ["netstat", "-ano", "-p", proto],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=8,
            ).stdout
        except Exception:
            pass
    if not out:
        return None
    pid = None
    # v4 lines end the local address in ":<port>", v6 in "]:<port>" - matching
    # on the ":<port> " suffix (with the column gap) covers both shapes.
    needle = f":{port} "
    for line in out.splitlines():
        if "LISTENING" in line and needle in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pid = parts[-1]
                break
    if not pid:
        return None
    name, path = "?", ""
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
             f"if($p){{$p.Name; $p.Path}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout.splitlines()
        ps = [l.strip() for l in ps if l.strip()]
        if ps:
            name = ps[0]
            path = ps[1] if len(ps) > 1 else ""
    except Exception:
        pass
    return (pid, name, path)


def _roblox_studio_app_running():
    """True/False whether a real Roblox Studio window process exists, or None
    if this can't be determined (non-Windows, or the check itself failed)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RobloxStudioBeta.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return None
    return "RobloxStudioBeta.exe" in out


def _kill_orphan_studio_mcp():
    """Kill leftover StudioMCP.exe processes from a PREVIOUS session/crash.

    StudioMCP.exe is Roblox's own MCP proxy; launch_studio_mcp.py spawns one
    as a direct child every time the bridge starts. If an earlier restart's
    tree-kill missed the grandchild (a reparenting race), or Studio itself
    crashed and left its own StudioMCP.exe running (seen live 2026-07-11:
    RobloxStudioBeta.exe zombied after two RobloxCrashHandler.exe events),
    the orphan keeps LISTENING on Studio's MCP port. Every StudioMCP.exe we
    launch afterward - even a freshly restarted one - just connects to that
    zombie instead of a real Studio, so the bridge reports "Studio connected"
    forever even with Studio fully closed. studio_watch's auto-restart cannot
    fix this on its own: restarting our proxy still lands on the same zombie.

    Only acts when NO real Studio app is running at all - in that state any
    existing StudioMCP.exe is unambiguously orphaned (a legitimate one only
    exists to serve a live Studio), so it is safe to auto-kill without asking.
    If Studio IS running (or this can't be determined), this is a no-op: a
    live StudioMCP.exe might be legitimately serving it, so nothing is
    touched - this must never risk killing a working connection.
    """
    if sys.platform != "win32":
        return
    if _roblox_studio_app_running() is not False:
        return
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq StudioMCP.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return
    if "StudioMCP.exe" not in out:
        return
    log("Found leftover StudioMCP.exe process(es) with no Roblox Studio running - "
        "cleaning them up (known cause of a phantom 'Studio connected' state).", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/IM", "StudioMCP.exe"],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not clean up orphaned StudioMCP.exe: {e}", "rd")


def _descendant_pids(root_pid):
    """Set of PIDs = root_pid + every descendant, or None if the process tree
    could not be read (in which case callers must NOT make kill decisions)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object "
             "{ \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        ).stdout
    except Exception:
        return None
    children = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    if not children:
        return None
    pids = {int(root_pid)}
    stack = [int(root_pid)]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in pids:
                pids.add(c)
                stack.append(c)
    return pids


def _reclaim_studio_port(client):
    """Kill a StudioMCP.exe that owns Studio's MCP port but is NOT our own child.

    The deadlock this breaks (reported live, survives every restart combo):
    a zombie StudioMCP.exe from a crashed session keeps LISTENING on 13469.
    The user reopens Studio -> its MCP plugin does its ONE-SHOT registration
    against the ZOMBIE (wasted). The user restarts the bridge -> Studio is now
    running, so _kill_orphan_studio_mcp's safety guard skips the cleanup, and
    check_studio_port waves the zombie through too (its path IS under Roblox).
    Our fresh StudioMCP can't own the port, Studio never re-registers on its
    own -> 0 tools forever, no restart order can fix it by hand.

    Ownership is decided by PID, not heuristics: we know the PID of the
    launcher we spawned (client.proc), so a StudioMCP.exe holding the port
    outside that process tree is a leftover by definition - Studio open or
    not. If the process tree can't be read, we do nothing (never risk killing
    our own healthy child on bad data). Returns True if a zombie was killed;
    the caller must then restart the roblox proxy (safe here even with Studio
    open: the plugin's single registration already went to the zombie, so
    there is no attempt left for a restart to collide with) AND tell the user
    to open Assistant Settings > MCP Servers so the plugin re-registers.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if not owner:
        return False
    pid, name, path = owner
    # Only ever kill a StudioMCP.exe. Studio itself holding the port is fine;
    # a non-Roblox squatter is check_studio_port's (interactive) job.
    if "studiomcp" not in (name or "").lower():
        return False
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if client is not None and client.proc is not None and client.is_alive():
        tree = _descendant_pids(client.proc.pid)
        if tree is None or pid_i in tree:
            return False  # ours, or unknowable - leave it alone
    log(f"port {STUDIO_MCP_PORT} is held by a StudioMCP.exe (pid {pid_i}) that this "
        "bridge did NOT launch - a leftover from a previous session. Studio "
        "registered to it, so our proxy sees 0 tools.", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid_i)],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not kill the leftover StudioMCP.exe: {e}", "rd")
        return False
    log(f"killed the leftover StudioMCP.exe (pid {pid_i}) to free Studio's MCP port.", "cy")
    return True


def _process_cmdline(pid):
    """Full command line of `pid`, or "" if it can't be read. Win32 only.

    Used to tell OUR OWN kind of process (a python running bridge.py) apart
    from an unrelated app that merely happens to listen on the same port -
    the process NAME is just "python"/"py"/"pythonw", far too generic to kill
    on. The command line is what proves it is a leftover bridge."""
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" "
             f"-ErrorAction SilentlyContinue).CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return ""
    return (out or "").strip()


def _reclaim_bridge_port():
    """Free OUR OWN listen port (17613) from a leftover bridge before we bind.

    The common failure (reported live, WinError 10048 on bind): the user
    relaunches start.bat while an earlier bridge.py is still running - window
    closed with the X instead of Ctrl+C, a previous crash that left a detached
    python, or a double double-click. The old process still holds the port, so
    websockets.serve() dies on bind with a cryptic (localised) OSError and the
    whole bridge exits code 1.

    We reuse _port_owner (already generic over the port) and only ever kill a
    process we can PROVE is another bridge.py - never a same-name innocent
    (some unrelated python listening on 17613): the guard is the command line
    containing "bridge.py", plus an explicit self-exclusion by PID. Anything
    else (a non-python app, or a python whose cmdline we can't read) is left
    alone and surfaced to the user by the caller's friendly bind-error path.
    Returns True if a leftover bridge was killed."""
    owner = _port_owner(PORT)
    if not owner:
        return False
    pid, name, path = owner
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_i == os.getpid():
        return False  # never kill ourselves (defensive; we haven't bound yet)
    # Must look like a python interpreter AND be running bridge.py. Killing on
    # the port alone would murder whatever legitimately owns 17613.
    if "python" not in (name or "").lower() and "py" != (name or "").lower():
        return False
    cmdline = _process_cmdline(pid_i)
    if "bridge.py" not in cmdline.lower():
        log(f"port {PORT} is held by pid {pid_i} ('{name}') but it does not look "
            f"like a ZeroScript bridge - leaving it alone.", "yl")
        return False
    log(f"port {PORT} is held by a leftover ZeroScript bridge (pid {pid_i}) from a "
        "previous session - killing it so this one can start.", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid_i)],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not kill the leftover bridge (pid {pid_i}): {e}", "rd")
        return False
    log(f"killed the leftover bridge (pid {pid_i}); the port is free now.", "cy")
    return True


def _kill_port_squatter():
    """Kill a NON-Roblox process holding Studio's MCP port, no questions asked.

    Called only when the child's stderr has PROVEN the port is hijacked (see
    MCPClient.saw_foreign_ws_host - StudioMCP connected to a foreign host and
    could not parse its protocol; the ropilot case). At that point there is no
    ambiguity left to justify check_studio_port's interactive prompt, and the
    prompt was itself a trap: many users never answer it, and the one-shot boot
    check often runs a beat before a background helper (ropilot) grabs the
    port. Here we have hard evidence, so kill the squatter outright. Returns
    (killed, name) so the caller can tell the user which app to uninstall /
    remove from startup, since it will otherwise reclaim the port on next boot.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if owner:
        pid, name, path = owner
        if "roblox" in (path or "").lower() or "studiomcp" in (name or "").lower():
            return False, None  # legitimate Studio-side owner; not a squatter
        log(f"port {STUDIO_MCP_PORT} is hijacked by '{name}' (pid {pid}, {path}).", "yl")
        log("    StudioMCP connected to it instead of Roblox Studio - that is why "
            "there are 0 tools.", "yl")
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=8)
        except Exception as e:
            log(f"could not kill '{name}': {e}", "rd")
            return False, name
        log(f"killed '{name}' so Studio can use the port.", "cy")
        return True, name
    # We could NOT resolve who owns the port, yet StudioMCP's stderr proved the
    # port is hijacked (this function is only called under that proof). This is
    # the state that used to fail SILENTLY: _port_owner returning None (e.g. a
    # squatter listening on IPv6 loopback that an IPv4-only netstat missed, or
    # any netstat quirk) left the user staring at 0 tools with no explanation.
    # Never be silent here. Try a name-based fallback for the known offender
    # (ropilot ships a background helper that squats this port), then always
    # tell the user what we know.
    log(f"port {STUDIO_MCP_PORT} is hijacked (StudioMCP could not talk to Roblox "
        "Studio on it) but the owning process could not be identified by port.", "yl")
    # ropilot is a multi-process app (validated live 2026-07-13): the port is
    # held by ropilot-infra-helper.exe, supervised by ropilot-infra.exe. Kill
    # both so the supervisor can't just respawn the helper and re-grab the port.
    killed_name = None
    for img in ("ropilot-infra-helper.exe", "ropilot-infra.exe", "ropilot.exe"):
        try:
            res = subprocess.run(["taskkill", "/F", "/IM", img],
                                 capture_output=True, text=True, timeout=8)
        except Exception:
            continue
        if res.returncode == 0:
            killed_name = img
            log(f"killed '{img}' (known port squatter) so Studio can use the port.", "cy")
    if killed_name:
        return True, killed_name
    log("    Could not auto-kill it. Find it manually: run  netstat -ano | "
        f"findstr {STUDIO_MCP_PORT}  then end that PID in Task Manager.", "yl")
    return False, None


def _print_squatter_hint(name):
    """After killing a port squatter (e.g. ropilot), tell the user how to stop
    it coming back - it is a background helper that respawns on the next boot
    and re-grabs the port before Studio, which is why a PC reboot never fixed
    this class of 0-tools report."""
    app = name or "the other app"
    action_banner([
        f"'{app}' fights Roblox Studio for its connection - it will keep",
        "coming back after every restart until you remove it.",
        f"1. Uninstall '{app}' (or remove it from Windows startup).",
        "2. In Roblox Studio: Assistant Settings > MCP Servers,",
        "   turn OFF then back ON 'Enable Studio as MCP server'.",
    ])


def _print_reregister_hint():
    """The one user action that completes a zombie-kill recovery: Studio's MCP
    plugin registers only once per boot and that attempt went to the zombie,
    so after the kill + proxy restart the user must make it register again."""
    # Opening the panel alone is technically enough to re-register, but we tell
    # the user to toggle OFF/ON to be sure - a toggle strictly implies opening
    # the panel, so it can never do less, and it removes any ambiguity about
    # whether "just looking at it" counted. Same wording as the squatter/no-place
    # banners so all three read as one identical instruction, not three variants.
    action_banner([
        "Go to Roblox Studio now.",
        "Turn OFF then back ON: Assistant Settings > MCP Servers",
        "         > 'Enable Studio as MCP server'",
        "Wait about 10 seconds - this window will turn green.",
    ])


def check_studio_port():
    """Warn (and optionally kill) a NON-Roblox process squatting Studio's MCP port.

    A third-party tool (e.g. "ropilot") that binds 13469 before Studio does
    hijacks the MCP channel: StudioMCP connects to IT instead of Studio, the
    handshake succeeds but tools/list never answers -> the bridge sees 0 tools.
    This is silent and brutal to diagnose, so we surface it up front.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if not owner:
        return False
    pid, name, path = owner
    # The legitimate holder is Studio itself / a Roblox helper: its path lives
    # under a "...\Roblox\..." folder. Anything else is an intruder.
    if "roblox" in (path or "").lower():
        return False
    where = path or name
    log(f"port {STUDIO_MCP_PORT} (Studio's MCP port) is held by a non-Roblox process:", "yl")
    log(f"    {name} (pid {pid})  {where}", "yl")
    log("    This will block Studio's tools (the bridge will see 0 tools).", "yl")
    try:
        ans = input("    Kill this process so Studio can use the port? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans in ("y", "yes", "o", "oui"):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=8)
            log(f"killed {name} (pid {pid}). Studio can use the port now.", "cy")
            # Tell the user the finishing step IMMEDIATELY, here, instead of only
            # after the ~48s server-launch grace loop that follows: killing the
            # squatter frees the port, but Studio's MCP plugin registers only
            # once per boot and that attempt already went to the squatter, so it
            # will NOT re-attach on its own - a toggle is needed. Printing this
            # now (not 48s later, after start_all's grace loop) is what turns a
            # ~1-minute "why is nothing happening" wait into an act-right-away
            # instruction. Uses action_banner (not log) so a non-technical user
            # visually cannot miss it among the surrounding status lines - seen
            # live indistinguishable when both used the same plain color.
            action_banner([
                "Go to Roblox Studio now.",
                "Turn OFF then back ON: Assistant Settings > MCP Servers",
                "         > 'Enable Studio as MCP server'",
                "Wait about 10 seconds - this window will turn green.",
            ])
            return True  # a squatter WAS killed -> Studio must reclaim the port
        except Exception as e:
            log(f"could not kill it: {e}", "rd")
    else:
        log("left it running. Close it yourself, then restart the bridge.", "yl")
    return False


_TRANSIENT_STUDIO_MARKERS = (
    "no roblox studio instance", "no active studio", "studio instance is connect",
    "studio instance connected", "not connected to", "no studio instance",
)


def _looks_like_transient_studio_drop(text):
    low = (text or "").lower()
    return any(m in low for m in _TRANSIENT_STUDIO_MARKERS)


# ── config.json read / write (for extension-driven add/remove) ──────────────
def _read_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                cfg.setdefault("mcpServers", {})
                return cfg
        except Exception as e:
            log(f"config.json unreadable ({e}) - starting from a fresh one", "yl")
    return {"mcpServers": {PRIMARY_SERVER_ID: {"command": "launch_studio_mcp.py", "args": []}}}


def _write_config(cfg):
    """Atomic write so a crash mid-write never leaves a truncated config.json."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def config_add_server(server_id, command, args=None, env=None):
    """Add/replace an addon server in config.json. Refuses to touch the primary
    (roblox) server. Returns (ok, error)."""
    sid = (server_id or "").strip()
    if not sid:
        return False, "server id is required"
    if sid == PRIMARY_SERVER_ID:
        return False, f"'{PRIMARY_SERVER_ID}' is the primary server and cannot be edited"
    if not (command or "").strip():
        return False, "a command is required"
    cfg = _read_config()
    spec = {"command": command.strip(), "args": list(args or [])}
    if env:
        spec["env"] = dict(env)
    cfg["mcpServers"][sid] = spec
    try:
        _write_config(cfg)
    except Exception as e:
        return False, f"could not write config.json: {e}"
    return True, None


def config_remove_server(server_id):
    """Remove an addon server from config.json. Refuses the primary server."""
    sid = (server_id or "").strip()
    if sid == PRIMARY_SERVER_ID:
        return False, f"'{PRIMARY_SERVER_ID}' is the primary server and cannot be removed"
    cfg = _read_config()
    if sid not in cfg.get("mcpServers", {}):
        return False, f"server '{sid}' is not in the config"
    del cfg["mcpServers"][sid]
    try:
        _write_config(cfg)
    except Exception as e:
        return False, f"could not write config.json: {e}"
    return True, None


# Local servers the bridge wires into config.json BY ITSELF on Android/Termux:
# everything they need (this repo's own python files) lives next to the bridge,
# so no panel click should ever be required on a phone. Each server has its
# own done-once marker, so (a) a deliberate removal is respected forever and
# (b) a FIX that introduces a NEW auto server later still lands for users who
# already auto-received an earlier one (seen live: synapse added by 471a35e,
# termux added right after - both must arrive independently).
_ANDROID_LOCAL_SERVERS = (
    # (server id, command relative to the repo root, done-once config marker)
    ("synapse", "synapse/mcp_synapse.py", "synapse_auto_added"),
    ("termux", "termux/mcp_termux.py", "termux_auto_added"),
)


def _ensure_android_local_servers():
    """Register the local Android MCP servers (Synapse in-game + Termux shell)
    automatically, ONCE each.

    Until now an Android user had to add these through the tiny panel UI on
    their phone (MCP servers > Add server > ...) - and when that click
    silently didn't take, the panel sat at the bare-bridge guidance with a
    disabled Start and no way to see why (seen live). The device-side setup
    already lives entirely on the machine the bridge runs on, so there is no
    reason to demand the panel click at all.

    - Idempotent per server: skips any id already present in config.json.
    - Runs ONCE per server: its '<id>_auto_added' marker records that the
      auto-registration happened - even if the entry later disappears for any
      reason, it is NOT silently re-added (a deliberate removal is respected).
    - Any failure (e.g. unwritable config) is logged with the manual steps -
      never raised.
    """
    cfg = _read_config()
    servers = cfg.setdefault("mcpServers", {})
    added = []
    for sid, cmd, marker in _ANDROID_LOCAL_SERVERS:
        if sid in servers or cfg.get(marker):
            continue
        servers[sid] = {"command": cmd, "args": []}
        cfg[marker] = True
        added.append(sid)
    if not added:
        return
    try:
        _write_config(cfg)
    except Exception as e:
        log(f"could not auto-register the local servers ({e}). Add them manually in the "
            "panel: MCP servers > Add server (commands 'synapse/mcp_synapse.py' and "
            "'termux/mcp_termux.py').", "yl")
        return
    log(f"auto-registered local servers: {', '.join(added)} - no panel step needed on "
        "Android. For in-game control also run:  python3 synapse/relay.py", "cy")


def restart_self():
    """Replace this process with a fresh one so config.json is reloaded from
    scratch. Children are killed first to free their stdio pipes / ports before
    the new instance claims them. Never returns on success (os.execv)."""
    log("restarting bridge to load new server config...", "yl")
    try:
        for c in mgr.clients.values():
            c.stop()
    except Exception:
        pass
    if _log_file:
        try:
            _log_file.flush()
        except Exception:
            pass
    # sys.argv[0] may be relative ('bridge.py'); make it absolute so the restart
    # works regardless of the current working directory.
    argv = list(sys.argv)
    script = os.path.abspath(argv[0]) if argv else os.path.abspath(__file__)
    argv = [script] + argv[1:]
    try:
        os.execv(sys.executable, [sys.executable] + argv)
    except Exception as e:
        # execv failed (rare) - fall back to spawning a detached copy and exiting
        # so the user still ends up with a running, up-to-date bridge.
        log(f"in-place restart failed ({e}); spawning a fresh bridge...", "rd")
        try:
            subprocess.Popen([sys.executable] + argv, cwd=HERE)
        except Exception as e2:
            log(f"could not spawn a fresh bridge: {e2} - please restart it manually", "rd")
        os._exit(0)


# ══════════════════════════════════════════════════════════════════════════
#  HARDENED MCP CLIENT  (one per server in config.json)
# ══════════════════════════════════════════════════════════════════════════
class MCPClient:
    def __init__(self, server_id, command, args, env=None):
        self.id = server_id
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.proc = None
        self.req_id = 1
        self.write_lock = threading.Lock()
        self.call_lock = threading.Lock()   # serialize tool calls (single stdio pipe)
        self.pending = {}                    # id -> queue.Queue (one slot)
        self.pend_lock = threading.Lock()
        self.tools_cache = []
        self.start_lock = threading.Lock()
        self._reader_thread = None
        # Crash-loop forensics (read by server_watch). The auto-restart used to
        # hide a server that something else kills over and over: the terminal
        # showed an endless quiet restart cycle with no explanation at all. We
        # keep just enough state to NAME the problem in the terminal instead:
        #  - last_exit: exit code from the final _reader EOF (crash vs kill hint)
        #  - stderr_tail: the last few stderr lines (usually the actual reason -
        #    port bind failure, missing dependency, crash trace)
        #  - restart_times: recent auto-restart timestamps (loop detector input)
        #  - loop_warned_at: throttle so the big red banner prints once per
        #    cooldown, not every 5s poll
        self.last_exit = None
        self.stderr_tail = []
        self.restart_times = []
        self.loop_warned_at = 0.0
        # Set when the configured command itself couldn't be launched at all
        # (e.g. 'uvx' not installed / not on PATH). This is NOT a crash - the
        # process never existed, so last_exit/stderr_tail stay empty and the
        # generic crash-loop banner used to print "the server printed no error
        # output before dying", which is misleading for a config problem the
        # user can fix in seconds. Kept across restarts so the banner can name
        # the real cause instead.
        self.start_error = None
        # Set when StudioMCP's stderr shows it connected to a FOREIGN WS host on
        # Studio's MCP port (not Studio). The unmistakable signature is a parse
        # error on the host's messages ("missing field `type`") - Studio speaks
        # the expected protocol, a squatter like ropilot speaks its own. This is
        # a timing-independent proof that the port is hijacked, unlike the
        # one-shot check_studio_port() boot probe which can miss a squatter that
        # grabs the port a moment after boot (seen live 2026-07-13: ropilot took
        # the port ~1s after the boot check ran, so nothing was flagged).
        self.saw_foreign_ws_host = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _resolve(self, s):
        return os.path.expandvars(os.path.expanduser(str(s)))

    def start(self):
        with self.start_lock:
            if self.is_alive():
                return
            cmd = [self._resolve(self.command)] + [self._resolve(a) for a in self.args]
            # A bare .py command (relative paths resolve against the bridge dir)
            # is run with the SAME interpreter the bridge itself uses, so it works
            # even on installs where only the `py` launcher exists (no `python`
            # on PATH). This is how the Studio MCP launcher is wired by default.
            if cmd[0].lower().endswith(".py"):
                script = cmd[0]
                if not os.path.isabs(script):
                    script = os.path.join(HERE, script)
                cmd = [sys.executable, script] + cmd[1:]
            # On Windows, npx/npm/yarn/pnpm/bunx are .cmd shims that Popen can't
            # launch directly (WinError 2). Run them through cmd.exe so any
            # node-based MCP server "just works" from config.json.
            if sys.platform == "win32":
                base = os.path.basename(cmd[0]).lower()
                if base in ("npx", "npm", "yarn", "pnpm", "bunx"):
                    cmd = ["cmd.exe", "/c"] + cmd
            env = dict(os.environ)
            for k, v in self.env.items():
                env[k] = self._resolve(v)
            log(f"[{self.id}] launching  ({' '.join(cmd)})", "cy")
            with _Spinner(f"    [{self.id}] starting..."):
                try:
                    self.proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        encoding="utf-8",
                        errors="replace",
                        cwd=HERE,
                        env=env,
                    )
                except FileNotFoundError:
                    # The OS couldn't find cmd[0] at all - this is a config
                    # problem (missing dependency, typo, not on PATH), not a
                    # transient crash. Auto-restart will keep retrying (the
                    # user may install it later), but name the real cause so
                    # it doesn't just look like an endless silent restart loop.
                    self.start_error = (
                        f"command not found: '{cmd[0]}' - is it installed and on PATH? "
                        f"(configured for server '{self.id}' in config.json)"
                    )
                    log(f"[{self.id}] {self.start_error}", "rd")
                    raise
                except OSError as e:
                    self.start_error = f"could not launch '{cmd[0]}': {e}"
                    log(f"[{self.id}] {self.start_error}", "rd")
                    raise
                else:
                    self.start_error = None
                with self.pend_lock:
                    self.pending.clear()
                self.saw_foreign_ws_host = False  # fresh process, fresh verdict
                self._reader_thread = threading.Thread(target=self._reader, args=(self.proc,), daemon=True)
                self._reader_thread.start()
                threading.Thread(target=self._stderr_drain, args=(self.proc,), daemon=True).start()

                # MCP handshake.
                self._request("initialize", {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "zeroscript-bridge", "version": "1.0"},
                }, timeout=30)
                self._notify("notifications/initialized")
                # Some MCP servers (notably Roblox's StudioMCP) advertise 0 tools at
                # the instant initialize returns, because they connect to their
                # backend (the running Studio) a moment AFTER the stdio handshake.
                # A single tools/list then caches an empty list forever. So if we
                # get nothing, retry for a few seconds to let the backend attach.
                # Short per-attempt timeout so the bridge never looks frozen if the
                # server stays silent (e.g. Studio not open yet); ~12s total budget.
                for _ in range(12):
                    if self.refresh_tools(timeout=3):
                        break
                    if not self.is_alive():
                        break
                    time.sleep(1.0)
            log(f"[{self.id}] MCP server up  ({len(self.tools_cache)} tools advertised)", "cy")

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def restart(self):
        log(f"[{self.id}] restarting...", "yl")
        self.stop()
        time.sleep(0.4)
        self.start()

    def stop(self):
        with self.pend_lock:
            for q in self.pending.values():
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self.pending.clear()
        proc = self.proc
        if proc:
            # proc.terminate() (TerminateProcess on Windows) only kills THIS
            # pid. Our command is often a wrapper (e.g. launch_studio_mcp.py)
            # that Popen()s a real child (StudioMCP.exe) to own the stdio
            # pipes - terminate() would leave that child orphaned, still bound
            # to Studio's MCP port, fighting the next restart's fresh instance.
            # taskkill /T kills the whole tree.
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, timeout=8,
                    )
                else:
                    proc.terminate()
            except Exception:
                pass
            # See _close_pipe: close what we still hold so its finalization
            # never prints 'Exception ignored ...' GC noise later.
            self._close_pipe(proc.stdin)
            self._close_pipe(proc.stdout)
            self._close_pipe(proc.stderr)
        self.proc = None

    @staticmethod
    def _close_pipe(stream):
        """Close a child's pipe fd ourselves, swallowing every error. If we
        leave it open, the garbage collector finalizes it eventually - and
        finalizing a pipe whose far end is already gone raises
        BrokenPipeError INSIDE the finalizer, which CPython prints as
        'Exception ignored while finalizing file <_io.TextIOWrapper ...>',
        splattered over whatever line the terminal was drawing (seen live:
        mid-spinner during the Termux crash loop). close() is idempotent, so
        the reader/drain threads and stop() can all call it safely."""
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass

    # ── io threads ────────────────────────────────────────────────────────
    def _reader(self, proc):
        stream = proc.stdout
        while True:
            try:
                line = stream.readline()
            except Exception:
                break
            if line == "":  # EOF -> process exited
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue  # stray non-JSON log on stdout
            mid = msg.get("id")
            if mid is None:
                continue  # server notification, nothing waits on it
            with self.pend_lock:
                q = self.pending.get(mid)
            if q is not None:
                try:
                    q.put_nowait(msg)
                except Exception:
                    pass
        code = proc.poll()
        self.last_exit = code  # kept for the crash-loop banner in server_watch
        log(f"[{self.id}] stdout closed (process ended, exit code {code})", "rd")
        with self.pend_lock:
            for q in self.pending.values():
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
        # See _close_pipe: close our ends of the dead child's pipes HERE, so
        # their finalization never leaks 'Exception ignored ...' GC noise onto
        # the terminal later (mid-spinner, mid-banner, anywhere).
        self._close_pipe(proc.stdout)
        self._close_pipe(proc.stdin)

    def _stderr_drain(self, proc):
        # Surface the child's stderr instead of silently discarding it - this
        # is often the ONLY clue why a server died (crash trace, port bind
        # failure, missing Studio, etc).
        try:
            for line in iter(proc.stderr.readline, ""):
                line = line.rstrip()
                if line:
                    # Ring buffer of the last stderr lines: when the server
                    # enters a crash loop, these are printed in the terminal
                    # banner - they are usually the only real explanation
                    # (port already in use, module not found, crash trace).
                    self.stderr_tail.append(line)
                    if len(self.stderr_tail) > 8:
                        self.stderr_tail.pop(0)
                    # Squatter signature: StudioMCP connected to a non-Studio host
                    # on the MCP port and can't parse its protocol. This is the
                    # ropilot hijack, timing-independent (see saw_foreign_ws_host).
                    low = line.lower()
                    if "failed to parse message from ws host" in low or "missing field `type`" in low:
                        self.saw_foreign_ws_host = True
                    log(f"[{self.id}] stderr: {line}", "yl", terminal=False)
        except Exception:
            pass
        finally:
            self._close_pipe(proc.stderr)  # see _close_pipe: preempt GC noise

    # ── jsonrpc ───────────────────────────────────────────────────────────
    def _next_id(self):
        with self.write_lock:
            rid = self.req_id
            self.req_id += 1
            return rid

    def _write_line(self, payload):
        """Write one JSON-RPC message line to the server's stdin.

        A server that exits instantly (its app missing, a command that cannot
        work on this platform) leaves us writing to a dead pipe. Without this
        guard that surfaced as a bare '[Errno 32] Broken pipe' (seen live:
        launch_studio_mcp.py exiting 1 under Termux read like a bridge bug in
        every auto-restart log line) - raise an error that names the real
        cause instead, and let the crash-loop banner print the child's actual
        stderr as the reason."""
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            # BrokenPipeError (an OSError): the child is already dead.
            # ValueError: stdin already closed (pipe teardown raced this write).
            raise RuntimeError(
                f"server '{self.id}' died before it could answer "
                f"(stdin write failed: {e}). Its stderr lines above should say why."
            ) from e

    def _notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        with self.write_lock:
            self._write_line(payload)

    def _request(self, method, params, timeout):
        if not self.is_alive():
            raise RuntimeError(f"server '{self.id}' is not running")
        rid = self._next_id()
        q = queue.Queue(maxsize=1)
        with self.pend_lock:
            self.pending[rid] = q
        try:
            payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            with self.write_lock:
                self._write_line(payload)
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                return None
        finally:
            with self.pend_lock:
                self.pending.pop(rid, None)

    # ── high-level ────────────────────────────────────────────────────────
    def refresh_tools(self, timeout=20):
        msg = self._request("tools/list", {}, timeout=timeout)
        if msg and "result" in msg:
            self.tools_cache = msg["result"].get("tools", [])
        return self.tools_cache

    def call_tool(self, name, arguments, timeout):
        """Returns {"text":..., "images":[...]}. Raises on error/timeout."""
        with self.call_lock:
            for attempt in (1, 2):
                if not self.is_alive():
                    self.restart()
                msg = self._request("tools/call",
                                    {"name": name, "arguments": arguments}, timeout)
                if msg is None:
                    if not self.is_alive():
                        self.restart()
                        msg = self._request("tools/call",
                                            {"name": name, "arguments": arguments}, timeout)
                    if msg is None:
                        raise TimeoutError(
                            f"No response from server '{self.id}' after {timeout}s.")
                if msg.get("error"):
                    err = msg["error"]
                    err_text = err.get("message", json.dumps(err))
                    if attempt == 1 and _looks_like_transient_studio_drop(err_text):
                        log(f"[{self.id}] {name}: transient Studio drop, retrying once...", "yl")
                        time.sleep(1.5)
                        continue
                    raise RuntimeError(err_text)
                content = msg.get("result", {}).get("content", [])
                text = "\n".join(it.get("text", "") for it in content if it.get("type") == "text")
                images = [{"data": it["data"], "mimeType": it.get("mimeType", "image/jpeg")}
                          for it in content if it.get("type") == "image" and it.get("data")]
                if not text and not images and content:
                    text = json.dumps(content)[:4000]
                # Studio's own MCP proxy briefly loses its binding to the Studio
                # app every few seconds on some machines (seen live: repeated
                # "Bound studio ... disconnected for proxy ..." stderr, self-
                # healing within ~1-4s). A tool call landing in that window
                # fails with a "no Studio instance connected" style message
                # even though Studio is genuinely open - confirmed live via
                # start_stop_play. One short retry rides through it instead of
                # surfacing a spurious error to the user.
                if attempt == 1 and _looks_like_transient_studio_drop(text):
                    log(f"[{self.id}] {name}: transient Studio drop, retrying once...", "yl")
                    time.sleep(1.5)
                    continue
                return {"text": text, "images": images}


# ══════════════════════════════════════════════════════════════════════════
#  REMOTE MCP SERVERS  (config: {"url": "https://..."})
# ══════════════════════════════════════════════════════════════════════════
# Until now every configured server was a COMMAND we spawn as a stdio child.
# That cannot reach a hosted server like Replit's (https://mcp.replit.com/server/mcp),
# which speaks MCP over "streamable HTTP" and authenticates with OAuth. This
# section adds that second kind of server, so an entry like
#
#     "replit": {"url": "https://mcp.replit.com/server/mcp"}
#
# puts Replit's tools (list_apps, create_app_from_prompt, publish_app, ...) into
# the SAME aggregated catalogue as Roblox/Synapse/Termux - the AI in the
# extension then builds and inspects Replit apps with no separate account setup.
#
# Sign-in is deliberately NOT part of bridge startup: an OAuth flow needs a
# human at a browser, and boot must never sit waiting for one. Instead:
#     python bridge.py --login replit
# walks through it once (browser opens, loopback callback catches the code) and
# stores the tokens in .mcp_tokens.json (0600, gitignored). At runtime a remote
# server that needs a sign-in is reported as such - never retried in a loop -
# and expires refresh themselves.
MCP_TOKENS_PATH = os.path.join(HERE, ".mcp_tokens.json")
MCP_OAUTH_CALLBACK_PORT = int(os.environ.get("ZS_MCP_CALLBACK_PORT", "17615"))
MCP_OAUTH_SCOPE_DEFAULT = "apps:read apps:write offline_access"
_mcp_tokens_lock = threading.Lock()


class _McpNeedsAuth(RuntimeError):
    """Remote server answered 401 and no (valid) stored token can fix it."""


def _mcp_tokens_load():
    try:
        with open(MCP_TOKENS_PATH, "r", encoding="utf-8") as f:
            store = json.load(f)
        return store if isinstance(store, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"{os.path.basename(MCP_TOKENS_PATH)} unreadable ({e}) - remote servers need a fresh sign-in", "yl")
        return {}


def _mcp_tokens_save(store):
    tmp = MCP_TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    try:
        os.chmod(tmp, 0o600)   # access + refresh tokens, nobody else's business
    except Exception:
        pass
    os.replace(tmp, MCP_TOKENS_PATH)


def _http_fetch(url, method=None, json_body=None, form=None, headers=None, timeout=30):
    """Stdlib HTTP that ALWAYS returns (status, headers, text).

    Error responses are DATA here, not exceptions: OAuth discovery is built
    entirely out of reading 401s, redirect bodies and error payloads, and a
    bare urlopen() would raise on every one of them.
    """
    hdrs = dict(headers or {})
    data = None
    if method is None:
        # A body means POST: every registration/token/code exchange is a POST,
        # and defaulting to GET silently 404s (found the hard way - the mock
        # OAuth server answered 404 and the error blamed the registration).
        method = "POST" if (json_body is not None or form is not None) else "GET"
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in hdrs.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status, {k.lower(): v for k, v in resp.headers.items()},
                    resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return (e.code, {k.lower(): v for k, v in (e.headers or {}).items()},
                e.read().decode("utf-8", "replace"))


def _json_or_none(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def _mcp_http_messages(hdrs, text):
    """The JSON-RPC messages in a streamable-HTTP answer.

    Either a plain JSON body (what we ask for) or SSE frames (what a server is
    allowed to answer with instead) - both are normal, so both are parsed.
    """
    if not (text or "").strip():
        return []
    if "text/event-stream" in (hdrs.get("content-type") or "").lower():
        out = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                obj = _json_or_none(line[5:].strip())
                if obj is not None:
                    out.append(obj)
        return out
    obj = _json_or_none(text)
    if obj is None:
        return []
    return obj if isinstance(obj, list) else [obj]


# ── OAuth (RFC 9728 protected-resource discovery + PKCE + loopback) ───────
def _oauth_resource_metadata(mcp_url, headers=None, timeout=20):
    """Replit's protected-resource metadata, found the way the spec says.

    Step one is the 401's WWW-Authenticate header. If a server is silent about
    it (permitted), try the well-known shapes: Replit answers the PATH-SCOPED
    one - /.well-known/oauth-protected-resource/server/mcp - while others only
    serve the origin-root one, so both are tried before giving up.
    """
    probe = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": _MCP_PROTOCOL_LATEST, "capabilities": {},
                        "clientInfo": {"name": "ZeroScript Bridge", "version": BRIDGE_VERSION}}}
    status, hdrs, _ = _http_fetch(mcp_url, json_body=probe, headers=headers, timeout=timeout)
    candidates = []
    m = re.search(r'resource_metadata="([^"]+)"', hdrs.get("www-authenticate") or "")
    if m:
        candidates.append(m.group(1))
    parts = urllib.parse.urlsplit(mcp_url)
    base = f"{parts.scheme}://{parts.netloc}"
    candidates.append(f"{base}/.well-known/oauth-protected-resource{parts.path.rstrip('/')}")
    candidates.append(f"{base}/.well-known/oauth-protected-resource")
    for url in dict.fromkeys(candidates):
        st, _, body = _http_fetch(url, headers=headers, timeout=timeout)
        prm = _json_or_none(body)
        if st == 200 and isinstance(prm, dict) and prm.get("authorization_servers"):
            log(f"[mcp] OAuth: protected-resource metadata from {url}", "dim")
            return prm
    raise RuntimeError(f"no OAuth metadata for {mcp_url} (HTTP {status}) - "
                       "is the URL right, and does the server expect a header instead?")


def _oauth_as_metadata(issuer, timeout=20):
    """Authorization-server metadata (RFC 8414), plus the OIDC fallback."""
    issuer = (issuer or "").rstrip("/")
    parts = urllib.parse.urlsplit(issuer)
    root = f"{parts.scheme}://{parts.netloc}"
    candidates = [f"{issuer}/.well-known/oauth-authorization-server",
                  f"{root}/.well-known/oauth-authorization-server{parts.path}",
                  f"{issuer}/.well-known/openid-configuration",
                  f"{root}/.well-known/openid-configuration{parts.path}"]
    for url in dict.fromkeys(candidates):
        st, _, body = _http_fetch(url, timeout=timeout)
        meta = _json_or_none(body)
        if st == 200 and isinstance(meta, dict) and meta.get("authorization_endpoint"):
            log(f"[mcp] OAuth: authorization server metadata from {url}", "dim")
            return meta
    raise RuntimeError(f"no authorization-server metadata for issuer {issuer}")


class _OAuthCallback(BaseHTTPRequestHandler):
    """One-shot loopback listener that catches ?code=... after the sign-in."""
    captured = None

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        _OAuthCallback.captured = {k: v[0] for k, v in query.items()}
        ok = "code" in _OAuthCallback.captured
        page = ("<html><body style='font-family:sans-serif;padding:2rem'>"
                f"<h2>{'Signed in' if ok else 'Sign-in failed'}</h2>"
                f"<p>{'ZeroScript Bridge can close this tab and use the server now.' if ok else 'Go back to the terminal for the reason.'}</p>"
                "</body></html>")
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):   # keep the terminal clean
        pass


def _open_browser(url):
    """Best effort - the URL is always printed too, because on Android/Termux
    there may be no handler at all and the user can just tap the printed link."""
    if os.environ.get("ZS_MCP_NO_BROWSER"):
        return
    try:
        import webbrowser
        if webbrowser.open(url):
            return
    except Exception:
        pass
    try:
        # Termux: put the URL on the clipboard too. The printed link is long and
        # tapping it in a terminal is fiddly, so a phone user can just paste it
        # into any browser even when no opener app is installed.
        clip = shutil.which("termux-clipboard-set")
        if clip:
            subprocess.run([clip], input=url.encode(), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
            log("[mcp] sign-in URL copied to the clipboard", "dim")
    except Exception:
        pass
    try:
        opener = shutil.which("termux-open-url")
        if opener:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _oauth_login(server_id, mcp_url, scope=None, client_id=None, headers=None,
                 callback_port=None, timeout=300):
    """Interactive sign-in. Returns the stored entry, raises on failure."""
    prm = _oauth_resource_metadata(mcp_url, headers=headers)
    issuers = prm.get("authorization_servers") or []
    if not issuers:
        raise RuntimeError("protected-resource metadata lists no authorization server")
    meta = _oauth_as_metadata(issuers[0])
    scopes = (scope or " ".join(prm.get("scopes_supported") or []) or MCP_OAUTH_SCOPE_DEFAULT)

    # Bind the callback FIRST: the redirect_uri (port included) is part of both
    # client registration and the token exchange, so it has to exist before we
    # register, not after.
    port = int(callback_port or MCP_OAUTH_CALLBACK_PORT)
    try:
        httpd = HTTPServer(("127.0.0.1", port), _OAuthCallback)
    except OSError:
        httpd = HTTPServer(("127.0.0.1", 0), _OAuthCallback)   # port busy -> ephemeral
    port = httpd.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    store = _mcp_tokens_load()
    saved = store.get(server_id) or {}
    reg = meta.get("registration_endpoint")
    if client_id:
        cid = client_id
    elif saved.get("client_id") and saved.get("redirect_uri") == redirect_uri:
        cid = saved["client_id"]      # same port as last time: reuse the registration
    elif reg:
        body = {"client_name": "ZeroScript Bridge",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": scopes}
        st, _, text = _http_fetch(reg, json_body=body, headers=headers)
        regr = _json_or_none(text)
        if st not in (200, 201) or not isinstance(regr, dict) or not regr.get("client_id"):
            raise RuntimeError(f"dynamic client registration failed (HTTP {st}): {text[:200]}")
        cid = regr["client_id"]
        log(f"[mcp] registered an OAuth client for {server_id} ({cid})", "cy")
    else:
        raise RuntimeError("no registration_endpoint and no client_id configured "
                           "(add \"client_id\" to that server in config.json)")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    auth_params = {"response_type": "code", "client_id": cid, "redirect_uri": redirect_uri,
                   "scope": scopes, "state": state, "code_challenge": challenge,
                   "code_challenge_method": "S256",
                   # RFC 8707: name the resource the token is FOR (MCP requires it).
                   "resource": prm.get("resource") or mcp_url}
    auth_url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(auth_params)

    print()
    log(f"[mcp] Sign in to {server_id} - open this URL if a browser did not open:", "cy")
    print(f"\n  {auth_url}\n")
    log(f"[mcp] waiting for the sign-in to finish (up to {timeout}s)...", "dim")
    _open_browser(auth_url)

    _OAuthCallback.captured = None
    httpd.timeout = 2
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and not _OAuthCallback.captured:
            httpd.handle_request()
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
    got = _OAuthCallback.captured or {}
    if not got:
        raise RuntimeError("timed out waiting for the browser to come back with a code")
    if got.get("error"):
        raise RuntimeError(f"the sign-in was refused: {got.get('error')} "
                           f"{got.get('error_description') or ''}".strip())
    if got.get("state") != state:
        raise RuntimeError("state mismatch on the OAuth callback - ignoring it")
    if not got.get("code"):
        raise RuntimeError("the callback carried no authorization code")

    token_body = {"grant_type": "authorization_code", "code": got["code"],
                  "redirect_uri": redirect_uri, "client_id": cid,
                  "code_verifier": verifier,
                  "resource": prm.get("resource") or mcp_url}
    st, _, text = _http_fetch(meta["token_endpoint"], form=token_body, headers=headers)
    tok = _json_or_none(text)
    if st != 200 or not isinstance(tok, dict) or not tok.get("access_token"):
        raise RuntimeError(f"token exchange failed (HTTP {st}): {text[:200]}")

    entry = {"client_id": cid, "redirect_uri": redirect_uri,
             "token_endpoint": meta["token_endpoint"],
             "resource": prm.get("resource") or mcp_url, "scope": scopes,
             "access_token": tok["access_token"],
             "refresh_token": tok.get("refresh_token"),
             "expires_at": (time.time() + float(tok["expires_in"]) if tok.get("expires_in") else 0)}
    with _mcp_tokens_lock:
        store = _mcp_tokens_load()
        store[server_id] = entry
        _mcp_tokens_save(store)
    log(f"[mcp] signed in to {server_id} - token valid for "
        f"{int(float(tok.get('expires_in') or 0))}s"
        f"{', refresh token stored' if tok.get('refresh_token') else ' (no refresh token)'}", "gr")
    return entry


def _oauth_refresh(server_id):
    """Swap the stored refresh token for a fresh access token. True on success."""
    with _mcp_tokens_lock:
        store = _mcp_tokens_load()
        entry = store.get(server_id) or {}
    if not entry.get("refresh_token") or not entry.get("token_endpoint"):
        return False
    body = {"grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
            "client_id": entry.get("client_id")}
    if entry.get("resource"):
        body["resource"] = entry["resource"]
    st, _, text = _http_fetch(entry["token_endpoint"], form=body)
    tok = _json_or_none(text)
    if st != 200 or not isinstance(tok, dict) or not tok.get("access_token"):
        log(f"[{server_id}] token refresh failed (HTTP {st}) - sign in again: "
            f"python bridge.py --login {server_id}", "yl")
        return False
    entry["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):
        entry["refresh_token"] = tok["refresh_token"]
    if tok.get("expires_in"):
        entry["expires_at"] = time.time() + float(tok["expires_in"])
    with _mcp_tokens_lock:
        store = _mcp_tokens_load()
        store[server_id] = entry
        _mcp_tokens_save(store)
    return True


class MCPHttpClient:
    """A remote MCP server, duck-typed to match MCPClient.

    The manager, server_watch, /status and the extension all treat it like any
    other server; only the transport (HTTP + a bearer token instead of a child
    process on a pipe) differs.
    """

    def __init__(self, server_id, url, headers=None, auth=None, scope=None,
                 client_id=None):
        self.id = server_id
        self.url = url
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.auth = (auth or ("headers" if self.headers else "oauth")).lower()
        self.scope = scope
        self.client_id = client_id
        self.tools_cache = []
        self.session_id = None
        self.protocol_version = None
        self.initialized = False
        self.needs_auth = False
        self.start_lock = threading.Lock()
        self.call_lock = threading.Lock()
        self._req_id = 1
        # MCPClient-compatible forensics fields (server_watch reads them).
        self.last_exit = None
        self.stderr_tail = []
        self.restart_times = []
        self.loop_warned_at = 0.0
        self.start_error = None
        self.saw_foreign_ws_host = False

    # ── auth ──────────────────────────────────────────────────────────────
    def has_token(self):
        return bool((_mcp_tokens_load().get(self.id) or {}).get("access_token"))

    def _bearer(self):
        if self.auth != "oauth":
            return None
        entry = (_mcp_tokens_load().get(self.id) or {})
        token = entry.get("access_token")
        expires = float(entry.get("expires_at") or 0)
        if token and (not expires or expires - 60 > time.time()):
            return token
        if _oauth_refresh(self.id):
            return (_mcp_tokens_load().get(self.id) or {}).get("access_token")
        return None

    # ── transport ─────────────────────────────────────────────────────────
    def _post(self, payload, timeout):
        headers = dict(self.headers)
        headers.setdefault("Accept", "application/json, text/event-stream")
        if self.auth == "oauth":
            token = self._bearer()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        status, hdrs, text = _http_fetch(self.url, json_body=payload,
                                         headers=headers, timeout=timeout)
        if status == 401:
            # One refresh-and-retry, then it is a human problem.
            if self.auth == "oauth" and _oauth_refresh(self.id):
                headers["Authorization"] = "Bearer " + (_mcp_tokens_load().get(self.id) or {}).get("access_token", "")
                status, hdrs, text = _http_fetch(self.url, json_body=payload,
                                                 headers=headers, timeout=timeout)
            if status == 401:
                self.needs_auth = True
                raise _McpNeedsAuth(
                    f"{self.id} needs a sign-in: run  python bridge.py --login {self.id}")
        if status >= 400:
            raise RuntimeError(f"{self.id}: HTTP {status} from {self.url}: {text[:200]}")
        self.needs_auth = False
        if hdrs.get("mcp-session-id"):
            self.session_id = hdrs["mcp-session-id"]
        return _mcp_http_messages(hdrs, text)

    def _request(self, method, params, timeout):
        with self.call_lock:
            self._req_id += 1
            rid = self._req_id
            msgs = self._post({"jsonrpc": "2.0", "id": rid, "method": method,
                               "params": params or {}}, timeout)
        for m in msgs:
            if isinstance(m, dict) and m.get("id") == rid:
                return m
        return msgs[0] if msgs else None

    # ── lifecycle ─────────────────────────────────────────────────────────
    def is_alive(self):
        return bool(self.initialized)

    def start(self):
        with self.start_lock:
            self.session_id = None
            try:
                msgs = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                   "params": {"protocolVersion": _MCP_PROTOCOL_LATEST,
                                              "capabilities": {},
                                              "clientInfo": {"name": "ZeroScript Bridge",
                                                             "version": BRIDGE_VERSION}}},
                                  timeout=20)
                result = next((m.get("result") for m in msgs
                               if isinstance(m, dict) and m.get("id") == 0), None)
                if not result:
                    raise RuntimeError("initialize returned nothing usable")
                self.protocol_version = result.get("protocolVersion") or _MCP_PROTOCOL_LATEST
                server = (result.get("serverInfo") or {}).get("name") or self.id
                # Tell the server we are ready for requests (a notification: the
                # server answers 202 and we send nothing back to the pipe).
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=20)
                self.initialized = True
                self.start_error = None
                self.refresh_tools()
                log(f"[{self.id}] remote MCP server '{server}' ready "
                    f"({len(self.tools_cache)} tools) at {self.url}", "gr")
            except _McpNeedsAuth as e:
                self.initialized = False
                self.start_error = str(e)
                self.tools_cache = []
                log(f"[{self.id}] not signed in yet - {e}", "yl")
            except Exception as e:
                self.initialized = False
                self.start_error = str(e)
                self.tools_cache = []
                log(f"[{self.id}] remote MCP server unavailable: {e}", "yl")

    def restart(self):
        self.initialized = False
        self.protocol_version = None
        self.session_id = None
        self.start()

    def stop(self):
        self.initialized = False
        self.session_id = None

    def refresh_tools(self, timeout=20):
        msg = self._request("tools/list", {}, timeout)
        if msg and "result" in msg:
            self.tools_cache = msg["result"].get("tools", [])
        return self.tools_cache

    def call_tool(self, name, arguments, timeout):
        """Same contract as MCPClient.call_tool: {"text","images"} or raise."""
        if not self.is_alive():
            self.start()
        msg = self._request("tools/call", {"name": name, "arguments": arguments}, timeout)
        if msg is None:
            raise TimeoutError(f"No response from remote server '{self.id}' after {timeout}s.")
        if msg.get("error"):
            raise RuntimeError(msg["error"].get("message", json.dumps(msg["error"])))
        result = msg.get("result") or {}
        content = result.get("content") or []
        text = "\n".join(it.get("text", "") for it in content if it.get("type") == "text")
        images = [{"data": it["data"], "mimeType": it.get("mimeType", "image/jpeg")}
                  for it in content if it.get("type") == "image" and it.get("data")]
        if not text and not images and content:
            text = json.dumps(content)[:4000]
        if result.get("isError") and not text:
            text = f"{self.id} reported the tool call as failed"
        return {"text": text, "images": images}


def mcp_cli(argv):
    """`python bridge.py --login [server]` / `--mcp-status`.

    Sign-in lives in a command rather than at startup on purpose: the OAuth
    flow needs a human at a browser, and a bridge that stops booting until
    someone signs in would be a worse bridge. Returns an exit code.
    """
    if not any(f in argv for f in ("--login", "--mcp-status", "--add-server", "--remove-server")):
        return None
    try:
        servers = _read_config().get("mcpServers", {}) or {}
    except Exception as e:
        log(f"config.json unreadable: {e}", "rd")
        return 1
    if "--add-server" in argv or "--remove-server" in argv:
        flag = "--add-server" if "--add-server" in argv else "--remove-server"
        rest = [a for a in argv[argv.index(flag) + 1:] if not a.startswith("--")]
        if flag == "--remove-server":
            if not rest:
                log("usage: python bridge.py --remove-server <server-id>", "yl")
                return 2
            ok, err = config_remove_server(rest[0])
            log(f"removed '{rest[0]}' from config.json" if ok else f"could not remove: {err}",
                "gr" if ok else "rd")
            return 0 if ok else 1
        # --add-server <id> [url] [--header Name=value ...] [--scope "a b"]
        sid = rest[0] if rest else ""
        url = rest[1] if len(rest) > 1 else ("https://mcp.replit.com/server/mcp" if sid == "replit" else "")
        if not sid or not url:
            log("usage: python bridge.py --add-server <server-id> <url> "
                "[--header Name=value] [--scope scopes]", "yl")
            return 2
        spec = {"url": url}
        headers = {}
        for i, a in enumerate(argv):
            if a == "--header" and i + 1 < len(argv) and "=" in argv[i + 1]:
                k, v = argv[i + 1].split("=", 1)
                headers[k.strip()] = v.strip()
        if headers:
            spec["headers"] = headers
        for key in ("--scope", "--auth"):
            if key in argv and argv.index(key) + 1 < len(argv):
                spec[key[2:]] = argv[argv.index(key) + 1]
        cfg = _read_config()
        cfg.setdefault("mcpServers", {})[sid] = spec
        try:
            _write_config(cfg)
        except Exception as e:
            log(f"could not write config.json: {e}", "rd")
            return 1
        log(f"added '{sid}' to config.json: {url}", "gr")
        if spec.get("headers"):
            log(f"    header auth ({', '.join(spec['headers'])}) - restart the bridge to use it", "dim")
        else:
            log(f"    next:  python bridge.py --login {sid}", "cy")
            log("    then restart the bridge (or it picks the sign-in up on its own)", "dim")
        return 0
    remote = {sid: spec for sid, spec in servers.items() if spec.get("url")}
    if "--mcp-status" in argv:
        if not remote:
            log("no remote (url) MCP servers in config.json", "yl")
        for sid, spec in remote.items():
            entry = _mcp_tokens_load().get(sid) or {}
            left = int(float(entry.get("expires_at") or 0) - time.time())
            if spec.get("headers"):
                state = f"header auth ({', '.join(spec['headers'])}) - no sign-in needed"
            elif entry:
                state = (f"signed in, token {left}s left, refresh token: "
                         f"{'yes' if entry.get('refresh_token') else 'no'}")
            else:
                state = f"not signed in - run: python bridge.py --login {sid}"
            log(f"[{sid}] {spec['url']} - {state}")
        return 0
    want = []
    i = argv.index("--login")
    if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
        want = [argv[i + 1]]
    if not want:
        want = [sid for sid, spec in remote.items()
                if not (_mcp_tokens_load().get(sid) or {}).get("access_token")]
    if not want:
        log("nothing to sign in to (all remote servers already have a token)", "gr")
        return 0
    rc = 0
    for sid in want:
        spec = remote.get(sid)
        if not spec:
            log(f"'{sid}' is not a remote server in config.json "
                f"(remote: {', '.join(remote) or 'none'})", "rd")
            rc = 1
            continue
        try:
            _oauth_login(sid, spec["url"], scope=spec.get("scope"),
                         client_id=spec.get("client_id"),
                         headers=spec.get("headers"))
        except KeyboardInterrupt:
            log("sign-in cancelled", "yl")
            rc = 1
        except Exception as e:
            log(f"[{sid}] sign-in failed: {e}", "rd")
            rc = 1
    return rc


# ══════════════════════════════════════════════════════════════════════════
#  MANAGER  - aggregates every MCP server, routes by tool name.
# ══════════════════════════════════════════════════════════════════════════
class MCPManager:
    def __init__(self):
        self.clients = {}          # server_id -> MCPClient
        self.index = {}            # advertised_name -> (holder, real_name)
        self.index_lock = threading.Lock()

    def load_config(self):
        servers = _read_config().get("mcpServers", {}) or {}
        for sid, spec in servers.items():
            if spec.get("url"):
                # Remote server: hosted MCP over streamable HTTP (Replit,
                # Zapier, any add-URL server) instead of a child on a pipe.
                self.clients[sid] = MCPHttpClient(
                    sid, spec["url"], headers=spec.get("headers"),
                    auth=spec.get("auth"), scope=spec.get("scope"),
                    client_id=spec.get("client_id"))
            else:
                self.clients[sid] = MCPClient(
                    sid, spec.get("command"), spec.get("args"), spec.get("env"))
        log(f"configured {len(self.clients)} MCP server(s): {', '.join(self.clients) or '(none)'}"
            f"{' (remote: ' + ', '.join(s for s, v in servers.items() if v.get('url')) + ')' if any(v.get('url') for v in servers.values()) else ''}", "cy")

    def start_all(self):
        # Launch every configured server IN PARALLEL, not one after another.
        # client.start() can block for up to ~12s (its own "wait for Studio's
        # tools to appear" grace loop) - with a sequential for-loop, Roblox
        # being first in config.json meant every OTHER server (Blender, any
        # addon) didn't even begin launching until Roblox's grace loop gave
        # up, even though that addon has nothing to do with Roblox and could
        # have been ready in 1-2s. A thread per client removes that
        # dependency entirely: a slow/absent Roblox Studio no longer holds up
        # an addon server the user actually wants right now.
        threads = []
        for sid, client in self.clients.items():
            def _run(sid=sid, client=client):
                try:
                    client.start()
                except Exception as e:
                    log(f"[{sid}] failed to start: {e}  (other servers continue)", "rd")
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self.rebuild_index()

    def rebuild_index(self):
        """Aggregate server tools. Collisions get a 'server/' prefix."""
        with self.index_lock:
            self.index = {}
            for sid, client in self.clients.items():
                for t in (client.tools_cache or []):
                    name = t.get("name")
                    if not name:
                        continue
                    advertised = name if name not in self.index else f"{sid}/{name}"
                    self.index[advertised] = (client, name)

    def list_tools(self, refresh=False):
        if refresh:
            for sid, client in self.clients.items():
                try:
                    if not client.is_alive():
                        client.start()
                    else:
                        client.refresh_tools()
                except Exception as e:
                    log(f"[{sid}] refresh failed: {e}", "yl")
            self.rebuild_index()
        out = []
        for sid, client in self.clients.items():
            for t in (client.tools_cache or []):
                name = t.get("name")
                advertised = name
                with self.index_lock:
                    # find the advertised key that maps to this (client, name)
                    for k, (holder, real) in self.index.items():
                        if holder is client and real == name:
                            advertised = k
                            break
                tt = dict(t)
                tt["name"] = advertised
                tt["server"] = sid
                out.append(tt)
        return out

    def call(self, name, arguments, timeout):
        with self.index_lock:
            entry = self.index.get(name)
        if entry is None:
            # Maybe a freshly added tool - rebuild once and retry.
            self.rebuild_index()
            with self.index_lock:
                entry = self.index.get(name)
        if entry is None:
            raise RuntimeError(f"unknown tool '{name}'")
        holder, real_name = entry
        return holder.call_tool(real_name, arguments, timeout)

    def restart(self, server_id=None):
        targets = [self.clients[server_id]] if server_id and server_id in self.clients else list(self.clients.values())
        for client in targets:
            try:
                client.restart()
            except Exception as e:
                log(f"[{client.id}] restart failed: {e}", "rd")
        self.rebuild_index()

    def health(self):
        out = []
        for sid, c in self.clients.items():
            row = {"id": sid, "alive": c.is_alive(), "tools": len(c.tools_cache)}
            if isinstance(c, MCPHttpClient):
                row["remote"] = True
                row["needs_auth"] = bool(c.needs_auth)
            out.append(row)
        return out

    def any_alive(self):
        return any(c.is_alive() for c in self.clients.values())


# ══════════════════════════════════════════════════════════════════════════
#  WEBSOCKET SERVER
# ══════════════════════════════════════════════════════════════════════════
mgr = MCPManager()
clients = set()
# Keyless /chat ("No key", 2026-08-28 user request): when no LLM provider key
# is configured, the HTTP /chat route is relayed to the BROWSER EXTENSION,
# which drives the FREE web chat (DeepSeek web, Gemini, ...) instead of a paid
# API. The extension advertises a "chat" capability in its "hello" handshake;
# relays wait on futures keyed by request id and are failed loudly if the
# extension's socket drops mid-chat.
ext_caps = {}                 # ws -> set of capability strings the extension reported
ext_seen = {}                 # ws -> monotonic of the last frame received (hello counts)
ext_build = {}                # ws -> build tag the extension reported (None = old build)
_ext_chat_waiters = {}        # rid -> (ws, asyncio.Future) awaiting "chat_result"
_ext_chat_seq = 0             # relay request id counter
_ws_debug = os.environ.get("ZS_WS_DEBUG") == "1"  # per-frame relay tracing

# ── Studio connectivity probe ──────────────────────────────────────────────
# The MCP server process stays alive even when Roblox Studio is closed or its
# MCP option is disabled - tool calls then return instantly with an "Unable to
# find an active Studio instance" text. So "mcp_alive" alone is misleading.
#
# TWO LEVELS (validated live 2026-06):
#  - list_roblox_studios: instant, side-effect-free. studios == [] means NO Studio
#    is connected to the MCP (app closed, OR its "Studio as MCP Server" option is
#    disabled - the two are indistinguishable at this layer). A non-empty list
#    means a Studio app IS connected, BUT note its entry stays present (active:true)
#    even when no place is open - only its "name" goes null. So presence != usable.
#  - get_studio_state: tells whether a PLACE is actually loaded. With a place open
#    it returns "Available DataModels: ..."; with the Studio on the home screen (or
#    the active place closed) it returns "...doesn't have a place opened / previously
#    active Studio has disconnected". That is the authoritative "place loaded" signal
#    (same phrase the call path already recognises in core/main.js).
STUDIO_PROBE_TOOL = "list_roblox_studios"
STUDIO_STATE_TOOL = "get_studio_state"
# Substrings get_studio_state emits when a Studio is connected but no place is open.
NO_PLACE_MARKERS = ("doesn't have a place", "no place opened", "place opened",
                    "has disconnected", "no active studio")


def _probe_tool_text(tool):
    """Call a side-effect-free probe tool with no args; return its text, or None if
    the tool is unavailable / the server is busy / it errored (best-effort)."""
    with mgr.index_lock:
        entry = mgr.index.get(tool)
    if entry is None:
        return None
    holder, real_name = entry
    # Never queue behind a long-running tool call (the probe is best-effort).
    if not holder.call_lock.acquire(blocking=False):
        return None
    try:
        if not holder.is_alive():
            return None
        msg = holder._request("tools/call", {"name": real_name, "arguments": {}}, timeout=8)
        if not msg or msg.get("error"):
            return None
        content = msg.get("result", {}).get("content", [])
        return "\n".join(it.get("text", "") for it in content if it.get("type") == "text")
    except Exception:
        return None
    finally:
        holder.call_lock.release()


def probe_studio():
    """Two-level Studio connectivity. Returns {"app": x, "place": y} where each is
    True / False / None (None = unknown: probe tool missing or server busy).
      app   - a Roblox Studio instance is connected to the MCP server. False = Studio
              closed OR its MCP-server option disabled (indistinguishable here).
      place - a place/datamodel is actually loaded and usable. False = Studio open on
              the home screen, or the active place was closed. Only meaningful when
              app is True (when app is False/None, place mirrors it)."""
    roblox = mgr.clients.get(PRIMARY_SERVER_ID)
    if roblox is not None and roblox.is_alive() and not roblox.tools_cache:
        # StudioMCP advertises ZERO tools - including list_roblox_studios itself -
        # until Studio actually attaches. That makes _probe_tool_text() below
        # return None (tool missing) the same way it would for a genuinely
        # transient "probe busy" blip, even though "Studio is simply closed" is
        # the common, SUSTAINED case here, not a blip. Left unhandled, the
        # extension's "unknown = don't degrade" rule (by design, for real
        # transient blips) then leaves the status dot stuck GREEN forever with
        # Studio fully closed (seen live 2026-07-11: dot stayed "on", tooltip
        # showing only an addon server's tool count). An alive client with an
        # empty catalogue is an unambiguous "not connected", so short-circuit
        # straight to that verdict instead of falling through to "unknown".
        return {"app": False, "place": False}
    text = _probe_tool_text(STUDIO_PROBE_TOOL)
    if text is None:
        return {"app": None, "place": None}
    try:
        studios = json.loads(text).get("studios") or []
    except Exception:
        return {"app": None, "place": None}
    if not studios:
        return {"app": False, "place": False}
    # A Studio app is connected - now check whether a place is actually open.
    state = _probe_tool_text(STUDIO_STATE_TOOL)
    if state is None:
        return {"app": True, "place": None}
    low = state.lower()
    place = not any(m in low for m in NO_PLACE_MARKERS)
    return {"app": True, "place": place}


def safe_call(name, arguments, timeout):
    """Never raises. Always returns a dict the extension can feed back to DeepSeek."""
    try:
        result = mgr.call(name, arguments, timeout)
        return {"ok": True, "text": result["text"], "images": result["images"]}
    except TimeoutError as e:
        return {"ok": False, "error": str(e), "kind": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e), "kind": type(e).__name__}


async def run_tool_task(ws, name, args, timeout, rid):
    """Execute one tool off the socket read loop and send its result back.

    Kept as a standalone task (not awaited inline in handler) so a long tool
    never starves the connection's ability to answer app-level pings - see the
    call_tool branch in handler() for the full rationale."""
    t0 = time.monotonic()
    res = await asyncio.to_thread(safe_call, name, args, timeout)
    elapsed = time.monotonic() - t0
    tag = "gr" if res.get("ok") else "rd"
    summary = (res.get("text") or res.get("error") or "")[:80].replace("\n", " ")
    slow = "  [SLOW]" if elapsed > 5 else ""
    # Routine per-call traces are technical noise for a non-dev user watching
    # the console; they still land in bridge_debug.log. A failed/slow call
    # DOES surface on the terminal - that's the signal a user should notice.
    log(f"<- {name} ({elapsed:.1f}s){slow}: {summary}", tag, terminal=not res.get("ok") or elapsed > 5)
    try:
        await ws.send(json.dumps({"type": "tool_result", "id": rid, **res}))
    except websockets.ConnectionClosed:
        pass


async def broadcast_status():
    """Push a fresh status snapshot to every currently-connected extension tab.

    Needed because the socket now starts listening (see _boot_and_diagnose in
    main()) before every MCP server has necessarily finished launching in the
    background - an extension that connects in that window gets an early,
    incomplete "connected" snapshot (e.g. an addon server not started yet).
    The extension's own periodic poll only reads a passively cached copy of
    the LAST message it received (background.js never re-probes on its own),
    so without a follow-up push that stale snapshot can persist forever (seen
    live 2026-07-11: Blender not yet alive at connect-time froze the "Start
    Roblox agent" button in its fully-disabled, non-degraded state even long
    after Blender was actually up). background.js already handles a second
    "connected" message arriving at any time (updates its cache and re-renders
    the bar), so re-sending this exact shape once startup truly settles is
    enough to self-correct with zero extension-side changes needed.
    """
    if not clients:
        return
    try:
        _st = await asyncio.to_thread(probe_studio)
        _proc = await asyncio.to_thread(_roblox_studio_app_running)
        payload = json.dumps({
            "type": "connected",
            "mcp_alive": mgr.any_alive(),
            "studio": _st["place"], "studio_app": _st["app"],
            # Whether a Roblox Studio WINDOW process exists at all - lets the
            # extension word the corrective step correctly ("open the MCP
            # panel in your already-open Studio" vs "launch Studio").
            "studio_proc": _proc,
            # False on hosts that can NEVER run Roblox Studio (Android/Termux,
            # Linux - where we skipped the 'roblox' server at boot). The
            # extension uses it to stop demanding Studio in its UI (seen
            # live: an Android bridge with zero Studio anywhere still told
            # the user to "open Roblox Studio" and disabled Start).
            "studio_capable": _platform_supports_studio_mcp(),
            "servers": mgr.health(),
            "tools": mgr.list_tools(),
            "port": PORT,
        })
    except Exception:
        return
    for ws in list(clients):
        try:
            await ws.send(payload)
        except Exception:
            pass


async def handler(ws):
    peer = getattr(ws, "remote_address", ("?",))[0]
    clients.add(ws)
    log(f"extension connected  ({peer})  [{len(clients)} client(s)]", "gr")
    try:
        _st = await asyncio.to_thread(probe_studio)
        await ws.send(json.dumps({
            "type": "connected",
            "mcp_alive": mgr.any_alive(),
            "studio": _st["place"], "studio_app": _st["app"],
            "studio_proc": await asyncio.to_thread(_roblox_studio_app_running),
            "studio_capable": _platform_supports_studio_mcp(),
            "servers": mgr.health(),
            "tools": mgr.list_tools(),
            "port": PORT,
        }))
        if _ws_debug:
            log(f"greeting flushed to ({peer}) - receive loop live", "dim")
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            ext_seen[ws] = time.monotonic()   # liveness stamp for relay targeting
            mtype = msg.get("type")
            rid = msg.get("id")
            if _ws_debug:
                log(f"frame from ({peer}): {mtype} id={rid}", "dim")

            if mtype == "ping":
                await ws.send(json.dumps({"type": "pong", "id": rid}))

            elif mtype == "hello":
                # Capability handshake (background.js): the bridge routes /chat
                # relays only to sockets that advertised "chat" - an older
                # extension never sends this and is simply never picked. The
                # build tag answers "is the user running the CURRENT extension
                # or a stale unpacked folder?" - logged once per connection and
                # exposed via /status for the doctor.
                ext_caps[ws] = set(msg.get("caps") or [])
                ext_build[ws] = msg.get("build") or None
                log(f"extension hello: build {ext_build[ws] or 'OLD (no build tag)'}"
                    f", caps {sorted(ext_caps[ws]) or '[]'}", "dim")

            elif mtype == "chat_result":
                # The extension's answer to a keyless HTTP /chat relay.
                waiter = _ext_chat_waiters.pop(rid, None)
                if waiter is not None and not waiter[1].done():
                    if _ws_debug:
                        log(f"chat_result matched relay {rid}: ok={msg.get('ok')}", "dim")
                    waiter[1].set_result(msg)
                elif _ws_debug:
                    # A chat_result nobody is waiting on: late answer past the
                    # timeout, duplicate, or a relay id mismatch - each is its
                    # own bug class, and silence here used to cost hours.
                    log(f"chat_result with NO waiter (id={rid}) - late/duplicate answer ignored", "yl")

            elif mtype == "studio_status":
                studio = await asyncio.to_thread(probe_studio)
                await ws.send(json.dumps({
                    "type": "studio_status", "id": rid,
                    "studio": studio["place"], "studio_app": studio["app"],
                    "studio_proc": await asyncio.to_thread(_roblox_studio_app_running),
                    "studio_capable": _platform_supports_studio_mcp(),
                    "mcp_alive": mgr.any_alive(),
                }))

            elif mtype == "list_tools":
                try:
                    tools = await asyncio.to_thread(mgr.list_tools, True)
                except Exception as e:
                    tools = mgr.list_tools()
                    log(f"list_tools error: {e}", "yl")
                _st = await asyncio.to_thread(probe_studio)
                await ws.send(json.dumps({
                    "type": "tools", "id": rid,
                    "tools": tools, "mcp_alive": mgr.any_alive(),
                    "studio": _st["place"], "studio_app": _st["app"],
                    "studio_proc": await asyncio.to_thread(_roblox_studio_app_running),
                    "studio_capable": _platform_supports_studio_mcp(),
                    "servers": mgr.health(),
                }))

            elif mtype == "call_tool":
                name = msg.get("name", "")
                args = msg.get("arguments") or {}
                timeout = float(msg.get("timeout", 120000)) / 1000.0
                log(f"-> tool  {name}({', '.join(args.keys())})", "cy", terminal=False)
                # Run the tool as a BACKGROUND task instead of awaiting it here.
                # Awaiting inline parks this read loop for the WHOLE tool call, so
                # a long tool (e.g. wait_job_finished > 25s) means the client's
                # app-level pings are never read/answered - its half-open-socket
                # watchdog then force-closes the connection and the in-flight call
                # is dropped as "bridge unreachable" (reported live). As a task,
                # the loop stays free to answer pings/status while the tool runs.
                # The extension only ever has ONE call_tool in flight (its agent
                # loop awaits each result before sending the next), so this never
                # overlaps tool executions.
                asyncio.create_task(run_tool_task(ws, name, args, timeout, rid))

            elif mtype in ("add_server", "remove_server"):
                # Adding/removing an addon MCP server rewrites config.json, which
                # the bridge only reads at launch - so we ack, then restart the
                # whole process to pick it up cleanly. The primary Roblox server
                # is protected inside config_add/remove_server.
                if mtype == "add_server":
                    ok, err = await asyncio.to_thread(
                        config_add_server,
                        msg.get("server_id"), msg.get("command"),
                        msg.get("args"), msg.get("env"))
                else:
                    ok, err = await asyncio.to_thread(
                        config_remove_server, msg.get("server_id"))
                await ws.send(json.dumps({
                    "type": "server_changed", "id": rid,
                    "ok": ok, "error": err, "restarting": ok,
                }))
                if ok:
                    # Give the ack a beat to flush over the socket, then restart.
                    async def _do_restart():
                        await asyncio.sleep(0.4)
                        restart_self()
                    asyncio.create_task(_do_restart())

            elif mtype == "restart_mcp":
                sid = msg.get("server")
                try:
                    await asyncio.to_thread(mgr.restart, sid)
                    ok, err = True, None
                except Exception as e:
                    ok, err = False, str(e)
                await ws.send(json.dumps({
                    "type": "mcp_status", "id": rid,
                    "alive": mgr.any_alive(), "ok": ok, "error": err,
                    "servers": mgr.health(), "tools": mgr.list_tools(),
                }))

            else:
                await ws.send(json.dumps({
                    "type": "error", "id": rid,
                    "error": f"unknown message type: {mtype}",
                }))
    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        log(f"handler error: {e}", "rd")
    finally:
        clients.discard(ws)
        ext_caps.pop(ws, None)
        ext_seen.pop(ws, None)
        ext_build.pop(ws, None)
        # Fail any keyless /chat relays still waiting on this exact socket -
        # the HTTP caller must hear "extension gone", not hang to its timeout.
        for _rid, (_ws, _fut) in list(_ext_chat_waiters.items()):
            if _ws is ws and not _fut.done():
                _fut.set_exception(RuntimeError("the extension disconnected mid-chat"))
                _ext_chat_waiters.pop(_rid, None)
        log(f"extension disconnected  [{len(clients)} client(s)]", "yl")


async def server_watch():
    """Poll every MCP server and restart any that died unexpectedly (e.g. the
    StudioMCP proxy crashing on its own - see stop()'s taskkill /T fix and the
    stderr logging above for why this used to happen silently). Without this,
    a dead server only got noticed on the NEXT real tool call, which is what
    made "Studio looks connected but nothing responds" possible."""
    # Crash-LOOP detection thresholds: LOOP_N deaths within LOOP_WINDOW seconds
    # means something is killing (or instantly crashing) the server every time
    # we bring it back - the silent restart cycle the auto-restart otherwise
    # hides completely. We still keep restarting (the cause may be transient,
    # e.g. the user is about to start Blender), but the terminal now NAMES the
    # problem: exit code, the child's last stderr lines, and - for a port-bound
    # server - who is squatting the port. Banner re-prints at most every
    # LOOP_WARN_COOLDOWN so the terminal stays readable.
    LOOP_N = 3
    LOOP_WINDOW = 60
    LOOP_WARN_COOLDOWN = 120
    while True:
        await asyncio.sleep(5)
        for sid, client in list(mgr.clients.items()):
            try:
                if not client.is_alive():
                    if getattr(client, "needs_auth", False):
                        # A remote server without a token cannot be fixed by
                        # restarting it - only by a human signing in. Say so
                        # once per cooldown instead of hammering it every 5s.
                        now = time.time()
                        if now - client.loop_warned_at > LOOP_WARN_COOLDOWN:
                            client.loop_warned_at = now
                            log(f"[{sid}] needs a sign-in (no valid token). Run:  "
                                f"python bridge.py --login {sid}", "yl")
                        # The sign-in usually happens in ANOTHER terminal while
                        # this bridge is already running, so pick it up as soon
                        # as a token appears instead of making the user restart.
                        if client.has_token():
                            client.needs_auth = False
                            log(f"[{sid}] sign-in detected - connecting...", "cy")
                            await asyncio.to_thread(client.start)
                            mgr.rebuild_index()
                            await broadcast_status()
                        continue
                    now = time.time()
                    # restart_times holds RESTART ATTEMPTS (appended just before
                    # each start below), never per-poll sightings - appending on
                    # every 5s poll would keep the window full forever and the
                    # "slow down" branch would then block restarts permanently.
                    client.restart_times = [t for t in client.restart_times if now - t < LOOP_WINDOW]
                    looping = len(client.restart_times) >= LOOP_N
                    if looping and now - client.loop_warned_at > LOOP_WARN_COOLDOWN:
                        client.loop_warned_at = now
                        log(f"[{sid}] CRASH LOOP: died {len(client.restart_times)} times in the last "
                            f"{LOOP_WINDOW}s (last exit code: {client.last_exit}). Something is killing it "
                            f"or it cannot start.", "rd")
                        if client.start_error:
                            log(f"[{sid}] {client.start_error}", "rd")
                        elif client.stderr_tail:
                            log(f"[{sid}] last error output (usually the real reason):", "rd")
                            for ln in client.stderr_tail:
                                log(f"[{sid}]   {ln}", "yl")
                        else:
                            log(f"[{sid}] the server printed no error output before dying.", "yl")
                        # Port forensics: name the process squatting a port this
                        # server needs. For the primary Roblox proxy that is
                        # Studio's MCP port; a squatter there (seen live: a
                        # 'ropilot' app) makes the proxy die/misbehave forever.
                        if sid == "roblox":
                            owner = _port_owner(STUDIO_MCP_PORT)
                            if owner:
                                pid, name, path = owner
                                if "roblox" not in (name or "").lower() and "studio" not in (path or "").lower():
                                    log(f"[{sid}] port {STUDIO_MCP_PORT} is held by '{name}' (pid {pid}, {path}) - "
                                        f"close that program, it is squatting Studio's MCP port.", "rd")
                        log(f"[{sid}] common causes: its app is not running (e.g. Blender + addon), a port "
                            f"conflict, an antivirus killing it, or a bad command in config.json. "
                            f"Auto-restart continues in the background.", "yl")
                    if looping and client.restart_times and now - client.restart_times[-1] < 15:
                        # Clearly hopeless right now: drop to a ~15s cadence so a
                        # broken command isn't hammer-spawned every 5 seconds,
                        # while still retrying forever (the cause may clear, e.g.
                        # the user finally opens Blender).
                        continue
                    client.restart_times.append(now)
                    log(f"[{sid}] found dead - auto-restarting...", "yl")
                    await asyncio.to_thread(client.start)
                    mgr.rebuild_index()
                    await broadcast_status()  # tell any connected extension right away
            except Exception as e:
                log(f"[{sid}] auto-restart failed: {e}", "rd")


def _current_studio_exe():
    """The StudioMCP.exe our launcher would currently pick (newest version
    folder paired with a real RobloxStudioBeta.exe), or None. Reused here only
    to detect a Studio update happening mid-session: Roblox's own bug report
    ("Studio MCP turning off after update") says the toggle resets to OFF
    whenever Studio auto-updates - restarting our proxy can't fix that (Studio
    itself refuses the connection while its toggle is off), so the terminal
    should say "re-enable the toggle" instead of "wait for auto-recovery"
    when a version bump coincides with the disconnect."""
    if _studio_scan is None:
        return None
    try:
        return _studio_scan.find_studio_mcp()
    except Exception:
        return None


async def studio_watch(initial_app, initial_place=None):
    """Poll Studio attachment and log transitions, so the terminal confirms in
    GREEN the moment Studio attaches (e.g. after the user toggles its MCP server)
    and warns again if it later drops. Best-effort; never raises.

    Also auto-recovers from a real disconnect: two bugs reported on the Roblox
    devforum leave StudioMCP.exe alive (our client stays "alive" - the process
    never dies, so server_watch's dead-process restart never fires) but stuck
    talking to nothing - (1) StudioMCP keeps a stale named-pipe handle keyed by
    Studio's old PID after Studio is closed and reopened, and never rediscovers
    the new one; (2) MCP silently disconnects every 5-15 minutes on some
    machines. The documented user workaround for both is "toggle Studio's MCP
    server off/on" / "reopen the MCP panel" - which just forces StudioMCP to
    redo its handshake. Restarting OUR proxy process is the equivalent from
    this side (taskkill + fresh launch_studio_mcp.py), so do it automatically
    once a drop looks real (sustained, not a momentary blip) instead of leaving
    the user to notice and toggle it themselves."""
    prev_app = initial_app
    prev_place = initial_place
    disconnected_since = None
    last_auto_restart = 0.0
    empty_since = None       # when the roblox catalogue was first seen empty
    last_reclaim = 0.0       # cooldown for the zombie-StudioMCP port reclaim
    place_transitions = []
    known_studio_exe = await asyncio.to_thread(_current_studio_exe)
    update_suspected = False
    # Only auto-restart a disconnect that follows a real connection (matches
    # the two known bugs above) - never spam-restart while Studio simply isn't
    # open yet at all (prev_app starting False/None is the common cold-start
    # case and restarting there would just be noise every cooldown).
    ever_connected = initial_app is True
    while True:
        await asyncio.sleep(4)
        # If StudioMCP launched while Studio was closed, its catalogue is EMPTY
        # and stays that way: start()'s 12s retry loop has long given up, and
        # nothing else ever re-asks for tools/list (probe_studio can't - the
        # probe tools themselves are part of the missing catalogue, which is
        # why it short-circuits to "not connected" on an empty cache). So a
        # Studio opened AFTER that window was never detected until the user
        # restarted the whole bridge (seen live 2026-07-11). Re-ask here on
        # every poll while the catalogue is empty; the moment Studio attaches,
        # tools appear, the index rebuilds, and the normal probe below flips
        # the state to connected on this same iteration.
        rc0 = mgr.clients.get("roblox")
        if rc0 is not None and rc0.is_alive() and not rc0.tools_cache:
            got = False
            try:
                got = bool(await asyncio.to_thread(rc0.refresh_tools, 3))
            except Exception:
                got = False
            if got:
                mgr.rebuild_index()
                log(f"Roblox Studio's tools appeared ({len(rc0.tools_cache)}) - Studio attached.", "gr")
                empty_since = None
            else:
                now0 = time.time()
                if empty_since is None:
                    empty_since = now0
                # First: a PROVEN port hijack (stderr showed StudioMCP talking to
                # a foreign host, e.g. ropilot). Hard evidence, so recover fast
                # and unconditionally - no need to wait out the sustained-empty
                # window the ambiguous zombie case below uses.
                if (rc0.saw_foreign_ws_host and now0 - last_reclaim > 180):
                    last_reclaim = now0
                    killed, sname = await asyncio.to_thread(_kill_port_squatter)
                    if killed:
                        try:
                            await asyncio.to_thread(mgr.restart, "roblox")
                        except Exception as e:
                            log(f"roblox proxy restart after squatter kill failed: {e}", "rd")
                        _print_squatter_hint(sname)
                        await broadcast_status()
                # Otherwise: catalogue stuck empty WITH a Studio window open often
                # means a zombie StudioMCP.exe (not ours) still owns port 13469 and
                # swallowed Studio's one-shot registration - a state no manual
                # restart combination can escape (see _reclaim_studio_port).
                # Sustained-empty threshold + cooldown so a Studio that is
                # merely slow to boot never triggers a spurious kill.
                elif (now0 - empty_since > 20 and now0 - last_reclaim > 180
                        and await asyncio.to_thread(_roblox_studio_app_running) is True):
                    last_reclaim = now0
                    if await asyncio.to_thread(_reclaim_studio_port, rc0):
                        try:
                            await asyncio.to_thread(mgr.restart, "roblox")
                        except Exception as e:
                            log(f"roblox proxy restart after zombie kill failed: {e}", "rd")
                        _print_reregister_hint()
                        await broadcast_status()
        else:
            empty_since = None
        try:
            st = await asyncio.to_thread(probe_studio)
        except Exception:
            continue
        app, place = st["app"], st["place"]
        if app is not None and app != prev_app:
            if app is True:
                # Roblox-only count, not mgr.list_tools() (sums every server,
                # e.g. + Blender) - this message is specifically about Roblox
                # attaching, so it must not borrow addon tool counts (same
                # class of bug as the startup banner, see roblox_total above).
                rc = mgr.clients.get("roblox")
                roblox_now = len(rc.tools_cache) if rc else 0
                log(f"Roblox Studio connected - {roblox_now} tools ready.", "gr")
                ever_connected = True
                disconnected_since = None
                update_suspected = False
            else:
                cur_exe = await asyncio.to_thread(_current_studio_exe)
                if cur_exe and known_studio_exe and cur_exe != known_studio_exe:
                    # A newer Studio version folder appeared since we last saw
                    # one - restarting the proxy will NOT fix this (Studio
                    # itself refuses the MCP connection while its own toggle
                    # is off), so tell the user the actual fix instead of
                    # letting the generic auto-recovery below spin uselessly.
                    ver = os.path.basename(os.path.dirname(cur_exe))
                    log(f"Roblox Studio appears to have UPDATED (new version: {ver}). "
                        "Studio often turns its MCP toggle back OFF after an update - open "
                        "Roblox Studio > Assistant Settings > MCP Servers and re-enable "
                        "'Enable Studio as MCP server'.", "yl")
                    update_suspected = True
                else:
                    log("Roblox Studio disconnected - re-enable its MCP server (toggle off/on).", "yl")
                    update_suspected = False
                known_studio_exe = cur_exe or known_studio_exe
                if ever_connected and disconnected_since is None:
                    disconnected_since = time.time()
            prev_app = app
            # studio_watch only used to LOG transitions - an extension sitting
            # on the pre-start standby screen (no tool calls happening, so
            # nothing else round-trips to the bridge) never saw Studio connect
            # or disconnect mid-session until it happened to poll for an
            # unrelated reason. Push it immediately instead of leaving that
            # extension staring at a stale snapshot indefinitely.
            await broadcast_status()
        # Set once per iteration: BOTH the app-drop branch and the place-churn
        # block below use it. It used to be assigned only inside the app-drop
        # branch, so any iteration that skipped that branch crashed the whole
        # watcher with UnboundLocalError on the churn line (seen live: the task
        # died right after a successful reconnect, silently ending ALL Studio
        # monitoring and status broadcasts until the bridge was restarted).
        now = time.time()
        if app is False and ever_connected and disconnected_since is not None and not update_suspected:
            # ~20s sustained (5 polls) before treating it as a real drop, not a
            # momentary blip; 90s cooldown between recovery attempts so a
            # Studio that is genuinely closed for a while doesn't get hammered.
            # Skipped entirely when a version bump was the likely cause (see
            # above) - restarting our proxy cannot flip Studio's own toggle
            # back on, so retrying would just be noise every 90s.
            if now - disconnected_since > 20 and now - last_auto_restart > 90:
                last_auto_restart = now
                # Which recovery applies depends on whether a Studio WINDOW is
                # actually running (validated live 2026-07-11, both directions):
                #  - Studio RUNNING but not attached: Studio's MCP plugin only
                #    registers ONCE, at Studio boot or on a toggle flip. It
                #    never retries by itself, and restarting OUR proxy cannot
                #    reach into Studio to re-register it - worse, a restart
                #    that lands while Studio is booting kills the listener at
                #    the exact moment the plugin makes its single attempt,
                #    which is precisely how this state got created. So: do NOT
                #    touch the proxy; tell the user the one action that works.
                #  - No Studio running: a restart is safe (nothing to collide
                #    with) and clears genuinely stuck/stale proxy state.
                if await asyncio.to_thread(_roblox_studio_app_running) is True:
                    log("Roblox Studio is RUNNING but its MCP plugin has not registered with the "
                        "bridge yet.", "yl")
                    log("If Studio is still STARTING UP, give it a minute (its plugin registers "
                        "late in boot).", "yl")
                    log("If Studio is fully loaded and this stays yellow: in Roblox Studio, simply "
                        "OPEN Assistant Settings > MCP Servers - opening that panel makes the "
                        "plugin re-register (validated twice live). If that's not enough, toggle "
                        "'Enable Studio as MCP server' OFF then ON there.", "yl")
                else:
                    log("Roblox Studio proxy looks stuck (known StudioMCP disconnect bug) - "
                        "restarting it to recover.", "yl")
                    try:
                        await asyncio.to_thread(mgr.restart, "roblox")
                        await broadcast_status()
                    except Exception as e:
                        log(f"auto-restart of roblox proxy failed: {e}", "rd")
        # PLACE-level churn: `app` can stay stuck reporting True the whole time
        # (seen live 2026-07-11 - Studio fully closed, list_roblox_studios kept
        # answering with a leftover studio entry for 4+ minutes, so the app-drop
        # trigger above never fires) while `place` flip-flops "loaded"/"closed"
        # every ~10-20s forever. That is not a user opening/closing places that
        # fast - it is the same class of stuck-proxy bug, just visible at the
        # place layer instead of the app layer. A fresh StudioMCP.exe process
        # cannot carry over stale cached state, so the same restart applies.
        place_transitions[:] = [t for t in place_transitions if now - t < 90]
        if len(place_transitions) >= 4 and now - last_auto_restart > 90:
            # Same running-Studio guard as the app-drop recovery above: with a
            # real Studio window up, a proxy restart can only collide with the
            # plugin's one-shot registration; the churn is Studio-side state.
            if await asyncio.to_thread(_roblox_studio_app_running) is not True:
                last_auto_restart = now
                log(f"Roblox Studio's place status flipped {len(place_transitions)} times in the last "
                    "90s (known StudioMCP stuck-proxy bug) - restarting the proxy to recover.", "yl")
                try:
                    await asyncio.to_thread(mgr.restart, "roblox")
                    await broadcast_status()
                except Exception as e:
                    log(f"auto-restart of roblox proxy failed: {e}", "rd")
                place_transitions.clear()
        if place is not None and place != prev_place:
            # Debounce: StudioMCP's binding to Studio blips every few seconds
            # on some machines (self-healing in ~1-4s - seen live as "Bound
            # studio ... disconnected" stderr). A probe landing in that window
            # can misread EITHER direction - most confusingly, reporting
            # "place loaded" from a stale cached response while the place is
            # actually still closed (seen live). Recheck once before trusting
            # a transition, in either direction.
            await asyncio.sleep(1.2)
            try:
                confirm = (await asyncio.to_thread(probe_studio))["place"]
            except Exception:
                confirm = None
            if confirm is None or confirm != place:
                continue  # didn't hold up on recheck - treat as noise, not a real change
            if place is True:
                log("Place loaded in Studio.", "gr")
            else:
                log("Place closed (Studio app still connected).", "yl")
            prev_place = place
            place_transitions.append(time.time())
            await broadcast_status()


async def _supervised(name, coro_factory):
    """Run a watcher coroutine forever, restarting it if it ever raises.

    Both watchers are designed to never raise, but one line proved that wrong
    in practice (an UnboundLocalError killed studio_watch SILENTLY - asyncio
    only prints 'Task exception was never retrieved' at shutdown, so all
    Studio monitoring and status broadcasts just stopped until the user
    restarted the bridge). A crash in a watcher must never be silent or
    permanent: log it loudly, wait a beat, start a fresh instance.
    """
    while True:
        try:
            await coro_factory()
            return  # normal completion (doesn't happen today, but respect it)
        except Exception as e:
            log(f"{name} crashed: {type(e).__name__}: {e} - restarting it in 5s "
                f"(please report this).", "rd")
            await asyncio.sleep(5)


# ══════════════════════════════════════════════════════════════════════════
#  LOCAL HTTP API  (/health /status /tools /call /chat)  -  "still the bridge"
# ══════════════════════════════════════════════════════════════════════════
#  The WebSocket at PORT is what the browser extension uses; nothing here
#  changes that. This is an ADDITIONAL, stdlib-only HTTP JSON API on a second
#  loopback port, so anything else on the device - curl, Tasker, scripts, a
#  headless chat client - can ALSO drive the bridge ("make the bridge an API,
#  but still the bridge"; "let me talk through the API"). Loopback only, no
#  auth: same trust model as the WebSocket (anything running as YOU on this
#  device can already run code here via termux.exec anyway).
#
#  /chat runs a headless agent loop against any OpenAI-compatible chat API
#  (DeepSeek by default), executing the same MCP tools the extension would.
#  It needs an API key (the one thing the browser-chat path avoids): set
#  ZS_LLM_KEY (or DEEPSEEK_API_KEY); provider/model/base are env-tunable.
# ══════════════════════════════════════════════════════════════════════════
API_ENABLED = os.environ.get("ZS_API", "1") != "0"
API_PORT = int(os.environ.get("ZS_API_PORT", "17614"))
# Sharing the API with other devices ("let people curl it", 2026-08-28 user
# request). Default stays loopback-only (only THIS device can call it).
#   ZS_API_HOST=0.0.0.0   - bind on every interface: anyone on the network
#                           (LAN, hotspot, Wi-Fi) can reach the API.
#   ZS_API_TOKEN=...      - password for the API: every route except /health
#                           then needs  Authorization: Bearer <token>
#                           (or X-ZS-Token: <token>, or ?token=<token>).
# POST /call and /chat can run SHELL COMMANDS (termux.exec) and in-game code
# (synapse.*), so an unauthenticated shared API = remote code execution for
# whoever finds it. A non-loopback bind without a token is therefore REFUSED
# (falls back to loopback with a loud warning) unless ZS_API_OPEN=1
# explicitly says "I know - open it anyway" (e.g. a fully trusted LAN).
# Sharing over the INTERNET does not even need ZS_API_HOST: leave the API on
# loopback, set a token, and tunnel it (README: cloudflared quick tunnel).
API_HOST = (os.environ.get("ZS_API_HOST", "") or "").strip()
API_TOKEN = (os.environ.get("ZS_API_TOKEN", "") or "").strip()
API_OPEN = os.environ.get("ZS_API_OPEN", "") == "1"
_API_MAX_BODY = 4 * 1024 * 1024

LLM_BASE = os.environ.get("ZS_LLM_BASE", "https://api.deepseek.com").rstrip("/")
LLM_KEY = os.environ.get("ZS_LLM_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
LLM_MODEL = os.environ.get("ZS_LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = float(os.environ.get("ZS_LLM_TIMEOUT", "150"))
LLM_MAX_ITERS = int(os.environ.get("ZS_LLM_MAX_ITERS", "8"))
_TOOL_RESULT_CAP = 8000       # chars of a single tool result forwarded to the LLM
_SESSION_MEMORY = 30          # messages kept per session (system prompt always stays)

_BOOT_TS = time.time()
_chat_sessions = {}           # session_id -> {"messages": [...], "lock": Lock}
_chat_sessions_lock = threading.Lock()

# Keyless /chat via the browser extension (no LLM key configured). The FREE
# web chat IS the language model - the extension types the message in, runs
# the same agent loop + tools as a panel session, and returns the final reply.
# ZS_CHAT_TIMEOUT covers slow chats with many tool calls (web chat is slower
# than an API). The tab must merely be OPEN: the extension brings it to the
# foreground itself when a turn arrives (a background tab parks the loop by
# design, and a curl caller is never there to un-park it).
EXT_CHAT_TIMEOUT = float(os.environ.get("ZS_CHAT_TIMEOUT", "300"))
MAIN_LOOP = None              # captured in main(): HTTP threads hop onto it


async def _chat_relay_to_extension(user_message, session_id, timeout):
    """Forward one /chat turn to a connected extension that advertised the
    "chat" capability, and wait for its chat_result. One relay at a time across
    the whole bridge (the extension runs one agent loop per tab anyway)."""
    if _ext_chat_waiters:
        return {"ok": False, "transport": "extension", "error":
                "another /chat request is already running through the browser "
                "extension - wait for it to finish"}
    targets = [ws for ws, caps in list(ext_caps.items()) if "chat" in caps]
    if not targets:
        return {"ok": False, "transport": "none", "error":
                "no keyless brain available: no browser extension with chat "
                "support is connected. Open your AI chat tab (e.g. "
                "chat.deepseek.com) with the ZeroScript extension loaded so "
                "/chat can talk through it - or set ZS_LLM_KEY for fully "
                "headless chat without a browser tab."}
    # FRESHEST first. Dict order is insertion order, so targets[0] used to be
    # the OLDEST still-connected extension - and a backgrounded Android
    # browser can keep a service worker half-alive: connected on paper, but
    # never answering (the ping reaper needs ~40s to reap it). Every relay
    # landed on that corpse and burned the whole ZS_CHAT_TIMEOUT - the
    # "keeps timing out with the tab right there" report. The client that
    # most recently SAID something is by far the likeliest to be alive, and
    # the extension the user JUST reloaded is the most talkative one.
    targets.sort(key=lambda ws: ext_seen.get(ws, 0), reverse=True)
    global _ext_chat_seq
    _ext_chat_seq += 1
    rid = f"httpchat-{_ext_chat_seq}"
    err = "no connected extension could run the chat"
    for attempt, target in enumerate(targets):
        fut = asyncio.get_running_loop().create_future()
        _ext_chat_waiters[rid] = (target, fut)
        try:
            await target.send(json.dumps({
                "type": "chat", "id": rid,
                "message": user_message, "session": session_id or "default",
            }))
        except Exception as e:
            _ext_chat_waiters.pop(rid, None)
            err = f"could not reach the extension: {e}"
            log(f"keyless /chat: send to client {attempt + 1}/{len(targets)} "
                f"failed ({e}) - trying next client", "yl")
            continue
        try:
            res = await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            # A live client that simply didn't finish in time. Do NOT retry on
            # another client: that would double-send the message into a second
            # AI tab. Report it.
            _ext_chat_waiters.pop(rid, None)
            return {"ok": False, "transport": "extension", "error":
                    f"the extension did not answer within {int(timeout)}s. The "
                    "extension pops the AI tab to the front by itself - look at "
                    "that tab for where the turn got stuck; if it was plainly "
                    "still working, raise ZS_CHAT_TIMEOUT. (Old extension build? "
                    "Reload the AI tab so it updates.)"}
        except Exception as e:  # socket dropped mid-chat
            # Retry on the next client. A late chat_result from the dropped
            # client is harmless (its waiter is gone, so it is discarded); a
            # duplicated turn in a second tab is a rare crash-only trade-off,
            # strictly better than hanging to timeout.
            _ext_chat_waiters.pop(rid, None)
            err = str(e)
            log(f"keyless /chat: client {attempt + 1}/{len(targets)} dropped "
                f"mid-relay ({err}) - trying next client", "yl")
            continue
        log(f"<- keyless /chat via extension: {(res.get('reply') or res.get('error') or '')[:80]}",
            "gr" if res.get("ok") else "yl")
        return {
            "ok": bool(res.get("ok")),
            "session": session_id or "default",
            "reply": (res.get("reply") or "").strip() or None,
            "error": res.get("error"),
            "transport": "extension",
        }
    return {"ok": False, "transport": "extension", "error": err}


def chat_via_extension(user_message, session_id, timeout=None):
    """HTTP-thread entry point for the keyless path: schedule the relay on the
    bridge's asyncio loop and block this thread until it answers. Never raises;
    returns a JSON-serializable dict."""
    if MAIN_LOOP is None:
        return {"ok": False, "error": "the bridge is still starting - retry in a second"}
    timeout = max(2.0, min(float(timeout or EXT_CHAT_TIMEOUT), 900.0))
    try:
        cf = asyncio.run_coroutine_threadsafe(
            _chat_relay_to_extension(user_message, session_id, timeout), MAIN_LOOP)
        return cf.result(timeout + 15)
    except concurrent.futures.TimeoutError:
        return {"ok": False, "transport": "extension",
                "error": f"chat relay exceeded {int(timeout)}s end-to-end"}
    except Exception as e:
        return {"ok": False, "transport": "extension", "error": f"chat relay broke: {e}"}


def _api_host_is_loopback(host):
    """True when the bind address can only be reached from this device."""
    h = (host or "").strip().lower()
    return (h == "" or h == "localhost" or h == "::1" or h.startswith("127.")
            or h.endswith(".localhost"))


def _api_bind_host():
    """Resolve the address the API actually binds (safety fallback included)."""
    host = API_HOST or "127.0.0.1"
    if not _api_host_is_loopback(host) and not API_TOKEN and not API_OPEN:
        log(f"REFUSED to expose the HTTP API on {host}:{API_PORT} without a token: "
            f"/call and /chat can run shell commands and in-game code - an open "
            f"port would be remote code execution for anyone on the network. "
            f"Set ZS_API_TOKEN (recommended) or ZS_API_OPEN=1 to proceed anyway. "
            f"Binding loopback instead.", "yl")
        return "127.0.0.1"
    return host


def _api_snapshot():
    """Fast status dict for GET /status (never probes MCP servers, so it can
    never hang on a half-dead one)."""
    return {
        "ok": True,
        "version": BRIDGE_VERSION,
        "uptime": int(time.time() - _BOOT_TS),
        "mcp_alive": mgr.any_alive(),
        "studio_capable": _platform_supports_studio_mcp(),
        "servers": mgr.health(),
        "tools": len(mgr.list_tools()),
        "ws_port": PORT,
        "api_port": API_PORT,
        "api_auth": bool(API_TOKEN),
        # Which generations of this API the RUNNING file has. Three copies of the
        # HTTP API are in circulation (ancient = none, old = no streaming, new =
        # SSE), all reporting the same version, so a stale zip used to be
        # indistinguishable from a current one until an app 400'd. Now the
        # answer to "am I running the fixed copy?" is one curl.
        "features": ["http_api", "keyless_chat", "openai_v1", "sse_streaming",
                     "mcp_server", "remote_mcp", "mcp_oauth"],
        # Which brains POST /chat can use right now: "llm" = a provider key is
        # configured (headless); "extension" = a browser extension with chat
        # support is connected (keyless, drives the free web chat).
        "chat": {
            "llm": bool(LLM_KEY),
            "extension": any("chat" in c for c in ext_caps.values()),
            # Zombie-trap diagnostics ("extension: true but every relay
            # times out"): how many chat-capable clients are connected and
            # when the most recent one last said anything. Several clients
            # with a large idle age = reloads left stale connections behind.
            "extension_clients": sum(1 for c in ext_caps.values() if "chat" in c),
            "extension_idle_s": (int(min(time.monotonic() - t for t in ext_seen.values()))
                                 if ext_seen else None),
            # Build tags the connected extensions reported (empty for the old
            # build, which sends none) - the objective answer to "did the
            # user REALLY reload the new extension?".
            "extension_builds": sorted({b for b in ext_build.values() if b}),
        },
    }


# ── headless chat agent loop ──────────────────────────────────────────────
def _openai_name(advertised):
    """OpenAI function names must match ^[a-zA-Z0-9_-]{1,64}$; our advertised
    tool names contain dots (synapse.ping, termux.exec). Map dots to double
    underscores; the reverse map is returned alongside for the call phase."""
    return advertised.replace(".", "__")[:64]


def _chat_system_prompt():
    servers = ", ".join(s["id"] for s in mgr.health()) or "none"
    return (
        "You are the ZeroScript agent running INSIDE the ZeroScript bridge on the "
        "user's own device. You operate it through tools, exactly like the browser "
        "extension would. Tool namespaces by MCP server currently connected: "
        f"{servers}. "
        "'synapse.*' tools run inside the live Roblox game (Delta executor); "
        "'termux.*' tools run shell/files on the device itself; 'roblox' tools "
        "drive Roblox Studio when present. When a tool errors, say what is wrong "
        "in one short sentence and suggest the concrete fix (e.g. start the "
        "relay / open Studio). Be concise; never invent tool results."
    )


def _llm_completion(messages, tools):
    """One OpenAI-compatible chat/completions call. Raises on failure."""
    body = {"model": LLM_MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        LLM_BASE + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("error"):
        raise RuntimeError(f"provider error: {data['error'].get('message', data['error'])}")
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("provider returned no choices")
    msg = choices[0].get("message") or {}
    return msg


def _get_session(session_id):
    session_id = (session_id or "default").strip() or "default"
    with _chat_sessions_lock:
        sess = _chat_sessions.get(session_id)
        if sess is None:
            sess = {"messages": [], "lock": threading.Lock()}
            _chat_sessions[session_id] = sess
    return session_id, sess


def _chat_tools_catalogue():
    """OpenAI function-calling view of every advertised MCP tool, plus the
    reverse map from each sanitised function name back to the real tool name."""
    fname_to_tool = {}
    tools = []
    for t in mgr.list_tools():
        real = t.get("name")
        if not real:
            continue
        fname = _openai_name(real)
        fname_to_tool[fname] = real
        tools.append({
            "type": "function",
            "function": {
                "name": fname,
                "description": (t.get("description") or "")[:1000],
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
            },
        })
    return tools, fname_to_tool


def _agent_loop(messages, tools, fname_to_tool):
    """Run the tool-calling loop over `messages` (MUTATED in place: assistant
    and tool turns are appended). Returns (final reply text or None,
    calls_made). None = the iteration cap was hit without a final answer.
    Raises on provider failure."""
    calls_made = []
    for _ in range(max(1, LLM_MAX_ITERS)):
        msg = _llm_completion(messages, tools)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            reply = (msg.get("content") or "").strip()
            messages.append({"role": "assistant", "content": reply})
            return reply, calls_made
        # Assistant message WITH tool calls must echo back verbatim-ish.
        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": tool_calls,
        })
        for call in tool_calls:
            fn = (call.get("function") or {})
            fname = fn.get("name", "")
            real = fname_to_tool.get(fname)
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            if real is None:
                out = {"ok": False, "error": f"unknown tool: {fname}"}
            else:
                out = safe_call(real, args, 120)
            calls_made.append({"name": real or fname, "ok": bool(out.get("ok"))})
            if out.get("ok"):
                content = out.get("text") or ("[image result - not forwardable in this chat]" if out.get("images") else "(ok)")
            else:
                content = f"ERROR: {out.get('error')}"
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": content[:_TOOL_RESULT_CAP],
            })
    return None, calls_made


def chat_turn(user_message, session_id=None, timeout=None):
    """Run one user turn through the provider + local tools. Never raises;
    returns a JSON-serializable dict."""
    user_message = (user_message or "").strip()
    if not user_message:
        return {"ok": False, "error": "'message' is required"}
    # KEYLESS path ("No key", 2026-08-28 user request): with no LLM key the
    # turn goes through the browser extension, which drives the free web chat
    # with the same tools - ZeroScript's whole reason to exist. With a key we
    # stay fully headless below.
    if not LLM_KEY:
        return chat_via_extension(user_message, session_id, timeout)

    tools, fname_to_tool = _chat_tools_catalogue()
    session_id, sess = _get_session(session_id)
    with sess["lock"]:
        if not sess["messages"]:
            sess["messages"].append({"role": "system", "content": _chat_system_prompt()})
        sess["messages"].append({"role": "user", "content": user_message})
        try:
            reply, calls_made = _agent_loop(sess["messages"], tools, fname_to_tool)
            # Trim memory: keep system + the most recent messages.
            if len(sess["messages"]) > _SESSION_MEMORY + 1:
                sess["messages"] = [sess["messages"][0]] + sess["messages"][-_SESSION_MEMORY:]
            if reply is None:
                return {"ok": False, "error": f"agent hit the iteration cap ({LLM_MAX_ITERS}) without a final answer",
                        "tool_calls": calls_made, "session": session_id}
            return {"ok": True, "session": session_id, "reply": reply,
                    "tool_calls": calls_made}
        except Exception as e:
            return {"ok": False, "error": f"chat failed: {e}", "session": session_id}


def chat_reset(session_id):
    session_id = (session_id or "default").strip() or "default"
    with _chat_sessions_lock:
        _chat_sessions.pop(session_id, None)
    return {"ok": True, "session": session_id}


# ── OpenAI-compatible endpoint (/v1) ──────────────────────────────────────
# Point ANY OpenAI-SDK client at the bridge (MoneyPrinterTurbo asked for
# exactly this, 2026-08-31): base_url http(s)://<host>:<port>/v1 and api key =
# ZS_API_TOKEN - the SDK's "Authorization: Bearer" header IS this API's token
# gate, so one secret covers both worlds. Brain selection is identical to
# /chat: no LLM key -> the keyless browser-extension relay; key configured ->
# the headless agent loop with the same tools.
#
# Two deliberate differences from /chat:
#  • STATELESS. OpenAI clients resend the FULL message list every call, so
#    server-side session memory would double-count history. (The keyless
#    relay is the exception it can't avoid: the web conversation is that
#    side's memory, so only the last user message crosses over.)
#  • Streaming is SSE, but not incremental. The brain finishes the whole turn
#    before it has anything to say (one web-chat relay or one provider call),
#    so stream=true gets the standard chunk sequence in one go - role delta,
#    content delta, finish chunk, [DONE] - which is what streaming-only clients
#    require before they will parse a reply at all. A failed turn still answers
#    as JSON with its HTTP status, as OpenAI does.
OPENAI_MODEL_ID = "zeroscript"


def _openai_error(message, status=400, etype="invalid_request_error", code=None):
    return status, {"error": {"message": message, "type": etype,
                              "code": code, "param": None}}


def _msg_text(m):
    """OpenAI content may be a string or a list of typed parts - flatten to text."""
    c = (m or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(str(p.get("text", "")) for p in c
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def openai_models_payload():
    """GET /v1/models: several clients validate the base URL by listing models."""
    return {"object": "list", "data": [{
        "id": OPENAI_MODEL_ID, "object": "model",
        "created": int(_BOOT_TS), "owned_by": "zeroscript-bridge",
    }]}


# ── Browser-agent relay composition ───────────────────────────────────────
# The keyless path has one channel: a single message typed into the free web
# chat, whose only memory is that conversation. That is fine for curl ("is the
# relay alive?") but it BREAKS a real client app: the web chat would never see
# the app's system prompt or its history, so an app whose protocol lives in its
# system prompt (PrivateAgent's {"action":...} schema, 2026-10-03) can never get
# a usable answer back.
#
# Rule: a client that sends ONE message is relayed VERBATIM (unchanged), while a
# client that sends a system prompt or a transcript gets both carried along in a
# clearly-fenced wrapper that tells the web chat to answer as that app's
# assistant for this one turn - no ZeroScript tool calls, no commentary.
_RELAY_SYS_CAP = 6000         # chars of the client's system prompt kept
_RELAY_HIST_CAP = 12000       # chars of the transcript kept (most recent)
_RELAY_MSG_CAP = 12000        # chars of the client's latest message kept


def _clip_middle(s, cap):
    """Keep both ends of an oversized string - the newest state is usually at
    the end of a screen dump, the instruction at the start - with a marker
    where the middle went. The marker counts against `cap`, so the result is
    never longer than asked (a client's composer limit is a hard number)."""
    s = s or ""
    if len(s) <= cap:
        return s
    marker = f"\n…[{len(s) - cap} chars dropped by the bridge]…\n"
    budget = cap - len(marker)
    if budget < 2:                     # absurdly small cap: hard truncate
        return s[:cap]
    head = budget // 3
    return s[:head] + marker + s[-(budget - head):]


def _relay_message_from_messages(msgs):
    """Build the single string the web chat receives for a keyless request."""
    system_parts, turns = [], []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = _msg_text(m).strip()
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
        elif role in ("user", "assistant"):
            turns.append((role, text))
    if not turns:
        return ""
    # Verbatim (the historical behaviour): one lone message, nothing else to carry.
    if not system_parts and len(turns) == 1:
        return turns[0][1]

    latest = turns[-1][1]
    history = turns[:-1]
    out = [
        "[[ This turn comes from an EXTERNAL APP through the ZeroScript bridge, "
        "not from the Roblox user. Answer it as that app's assistant: follow the "
        "CLIENT INSTRUCTIONS below exactly and reply with ONLY the text the app "
        "expects. Do NOT call any tool, do NOT mention this note, do NOT add "
        "commentary or formatting the instructions did not ask for. ]]",
    ]
    if system_parts:
        out.append("\n--- CLIENT INSTRUCTIONS (system prompt of the app) ---\n"
                   + _clip_middle("\n\n".join(system_parts), _RELAY_SYS_CAP)
                   + "\n--- END CLIENT INSTRUCTIONS ---")
    if history:
        lines = []
        for role, text in history:
            lines.append(f"{role}: {text}")
        joined = "\n".join(lines)
        # Keep the REST of the conversation if it is oversized: an agent loop
        # needs its most recent screen/action pairs, not its first ones.
        if len(joined) > _RELAY_HIST_CAP:
            marker = "…[earlier turns dropped]…\n"
            joined = marker + joined[-(_RELAY_HIST_CAP - len(marker)):]
        out.append("\n--- CONVERSATION SO FAR (the app's own history) ---\n"
                   + joined + "\n--- END CONVERSATION ---")
    out.append("\n--- CLIENT'S NEWEST MESSAGE ---\n"
               + _clip_middle(latest, _RELAY_MSG_CAP))
    return "\n".join(out)


def openai_chat_completions(body):
    """POST /v1/chat/completions -> (http_status, OpenAI-shaped payload)."""
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return _openai_error("'messages' (a non-empty list) is required")
    # NOTE: `stream` is handled by the caller (_APIHandler.do_POST), which turns
    # this completed reply into an SSE delta stream. This function always returns
    # the finished payload.
    req_model = str(body.get("model") or OPENAI_MODEL_ID)
    timeout = body.get("timeout")  # non-standard extra, honoured like /chat

    if not LLM_KEY:
        # KEYLESS relay: the web conversation is the memory, so a plain chat
        # client only needs its newest message. An APP that carries its
        # protocol in a system prompt (PrivateAgent's action schema) additionally
        # needs that prompt and its history - see _relay_message_from_messages.
        relay = _relay_message_from_messages(msgs)
        if not relay.strip():
            return _openai_error("no user message with text content found")
        r = chat_via_extension(relay.strip(), "openai-compat", timeout)
        if not r.get("ok"):
            return _openai_error(r.get("error") or "extension chat failed",
                                 502, "server_error")
        reply = r.get("reply") or "(the web chat produced no readable reply)"
        finish = "stop"
    else:
        # Headless agent loop over the CLIENT's transcript (copied - no
        # request-driven mutation leaks back). The ZeroScript system prompt is
        # prepended only when the client sent none of its own. Client "tool"
        # role messages are dropped: tools are executed HERE, so a stale
        # client-side tool result has no matching tool_call in our loop.
        seed = []
        has_system = any((m or {}).get("role") == "system" and _msg_text(m).strip() for m in msgs)
        if not has_system:
            seed.append({"role": "system", "content": _chat_system_prompt()})
        for m in msgs:
            role = (m or {}).get("role")
            if role in ("system", "user", "assistant"):
                seed.append({"role": role, "content": _msg_text(m)})
        tools, fname_to_tool = _chat_tools_catalogue()
        try:
            reply, _calls = _agent_loop(seed, tools, fname_to_tool)
        except Exception as e:
            return _openai_error(f"provider/agent failure: {e}", 502, "server_error")
        finish = "stop" if reply is not None else "length"
        if reply is None:
            reply = "(the agent stopped at its tool-call cap without a final answer)"

    # usage is all-zero on purpose: we don't tokenize, and several SDKs only
    # require the KEY to exist (a missing usage object crashes some).
    return 200, {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req_model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": finish,
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ══════════════════════════════════════════════════════════════════════════
#  MCP SERVER  (POST /mcp)  -  the bridge as a REMOTE MCP SERVER
# ══════════════════════════════════════════════════════════════════════════
# Every other section makes the bridge an MCP CLIENT: it spawns Roblox Studio's
# server, Synapse, Termux and any addon, and routes tools to them. This section
# is the mirror image - the bridge also SPEAKS MCP over HTTP, so a REMOTE agent
# can drive exactly those same tools.
#
# Motivating client: Replit Agent (2026-10-03 user request). Replit connects
# MCP servers by URL with optional custom headers, so the user pastes their
# tunnel URL and an X-API-Key and Replit's cloud agent can then run Roblox and
# phone tools through this bridge. Any MCP client works the same way (Cursor,
# Claude, another agent framework).
#
# Transport: MCP "streamable HTTP" - one POST per JSON-RPC message, answered
# with application/json (or a single SSE `message` event when the client will
# only accept text/event-stream). The bridge never sends server-initiated
# messages, so GET is answered 405 exactly as the spec permits, and DELETE
# ends the session.
_MCP_PROTOCOL_LATEST = "2025-06-18"
_MCP_PROTOCOL_SUPPORTED = ("2024-10-07", "2024-11-05", "2025-03-26",
                           "2025-06-18")
_MCP_SERVER_NAME = "zeroscript-bridge"
_MCP_TOOL_TIMEOUT = 120.0

# MCP tool names are free-form strings, but the LLM tool-schemas most clients
# build from them are not: Anthropic-style names must match
# ^[a-zA-Z0-9_-]{1,64}$, and our advertised names contain dots
# (synapse.ping, termux.exec). Expose a sanitized name and accept EITHER form
# back, so nothing an existing client already knows breaks.
_mcp_name_map = {}            # advertised -> real (rebuilt with every tools/list)
_mcp_name_lock = threading.Lock()


def _mcp_advertised_name(real):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(real or ""))[:64]
    return safe or "tool"


def _mcp_tool_list():
    """The aggregate catalogue, shaped as MCP tools."""
    out = []
    with _mcp_name_lock:
        _mcp_name_map.clear()
        used = set()
        for t in mgr.list_tools():
            real = t.get("name")
            if not real or real in used:
                continue
            used.add(real)
            adv = _mcp_advertised_name(real)
            # Two real names can sanitize onto one advertised name
            # (a.b and a_b) - keep them distinct.
            if adv in _mcp_name_map and _mcp_name_map[adv] != real:
                adv = f"{adv}_{len(_mcp_name_map)}"
            _mcp_name_map[adv] = real
            schema = t.get("inputSchema") or {"type": "object", "properties": {}}
            desc = t.get("description") or ""
            server = t.get("server")
            if server:
                desc = (desc + f" (via ZeroScript bridge server '{server}')").strip()
            out.append({"name": adv, "description": desc, "inputSchema": schema})
    return out


def _mcp_real_name(name):
    """Map an advertised (or already-real) name back to what the manager knows.

    The map is (re)built by tools/list, but a client may call a tool WITHOUT
    listing first - and every tools/list CLEARS the map, so an unrelated
    client's listing could empty it between a call's name and this lookup.
    Either way, fall back to sanitizing the live catalogue, so `demo_echo`
    reaches the server that advertises `demo.echo` regardless of call order.
    """
    n = str(name or "").strip()
    with _mcp_name_lock:
        if n in _mcp_name_map:
            return _mcp_name_map[n]
    for t in mgr.list_tools():
        real = t.get("name")
        if real and (real == n or _mcp_advertised_name(real) == n):
            return real
    return n


def _mcp_rpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _mcp_rpc_error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _mcp_initialize(msg):
    params = msg.get("params") or {}
    asked = str(params.get("protocolVersion") or "").strip()
    # Echo the client's version when we know it (what the spec asks for),
    # otherwise announce our newest so the client can decide.
    version = asked if asked in _MCP_PROTOCOL_SUPPORTED else _MCP_PROTOCOL_LATEST
    client = (params.get("clientInfo") or {}).get("name")
    if client:
        log(f"[mcp] initialize from {client!r} "
            f"(protocol {asked or 'unspecified'} -> {version})", "dim", terminal=False)
    return _mcp_rpc_result(msg.get("id"), {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": _MCP_SERVER_NAME, "version": BRIDGE_VERSION,
                       "title": "ZeroScript Bridge"},
        "instructions": (
            "ZeroScript bridge tools. Depending on what is connected, these "
            "drive Roblox Studio (scripts, instances, play-testing, assets) "
            "and/or the user's own device (Synapse in-game executor, Termux "
            "shell). Call tools/list for the exact catalogue; a tool name "
            "that is not listed will fail. Studio tools do nothing until "
            "Roblox Studio has a place open with its MCP server enabled."
        ),
    })


def _mcp_call_tool(msg):
    params = msg.get("params") or {}
    name = _mcp_real_name(params.get("name"))
    args = params.get("arguments")
    if not isinstance(args, dict):
        args = {}
    try:
        timeout = float(params.get("timeout"))
    except (TypeError, ValueError):
        timeout = _MCP_TOOL_TIMEOUT
    timeout = max(1.0, min(300.0, timeout))
    log(f"-> /mcp tools/call {name} ({len(json.dumps(args))} bytes of args)", "cy", terminal=False)
    t0 = time.monotonic()
    res = safe_call(name, args, timeout)
    log(f"<- /mcp tools/call {name} {time.monotonic() - t0:.1f}s "
        f"{'ok' if res.get('ok') else 'ERR: ' + str(res.get('error'))[:80]}",
        "gr" if res.get("ok") else "rd")
    content = []
    if res.get("ok"):
        text = res.get("text") or ""
        if text:
            content.append({"type": "text", "text": text})
        for img in res.get("images") or []:
            if img.get("data"):
                content.append({"type": "image", "data": img["data"],
                                "mimeType": img.get("mimeType") or "image/jpeg"})
        if not content:
            content.append({"type": "text", "text": "(the tool returned nothing)"})
    else:
        # A failed tool is a RESULT with isError (not a JSON-RPC error), so the
        # model sees the reason and can adapt instead of the call blowing up.
        content.append({"type": "text",
                        "text": f"ERROR: {res.get('error') or 'tool failed'}"})
    return _mcp_rpc_result(msg.get("id"),
                           {"content": content, "isError": not res.get("ok")})


def _mcp_handle_one(msg):
    """One JSON-RPC message -> one response dict, or None for a notification."""
    if not isinstance(msg, dict):
        return _mcp_rpc_error(None, -32600, "invalid request: not an object")
    method = str(msg.get("method") or "")
    rid = msg.get("id")
    if method.startswith("notifications/"):
        return None                       # initialized / cancelled / progress
    if method == "initialize":
        return _mcp_initialize(msg)
    if method == "ping":
        return _mcp_rpc_result(rid, {})
    if method == "tools/list":
        tools = _mcp_tool_list()
        log(f"[mcp] tools/list -> {len(tools)} tool(s)", "dim", terminal=False)
        return _mcp_rpc_result(rid, {"tools": tools})
    if method == "tools/call":
        if not (msg.get("params") or {}).get("name"):
            return _mcp_rpc_error(rid, -32602, "invalid params: 'name' is required")
        return _mcp_call_tool(msg)
    # Capabilities we do not advertise, but clients probe them anyway.
    if method in ("resources/list", "resources/templates/list"):
        return _mcp_rpc_result(rid, {"resources": []})
    if method == "prompts/list":
        return _mcp_rpc_result(rid, {"prompts": []})
    return _mcp_rpc_error(rid, -32601, f"method not found: {method}")


# ── HTTP plumbing ─────────────────────────────────────────────────────────
class _APIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _authorized(self):
        """Token gate for the shared API (no token configured = loopback mode =
        always allowed). Accepted forms: 'Authorization: Bearer <token>',
        'X-ZS-Token: <token>', or '?token=<token>' (the query form exists so a
        plain browser tab can open /status). compare_digest, never ==, so the
        token does not leak through response timing."""
        if not API_TOKEN:
            return True
        header = (self.headers.get("Authorization") or "").strip()
        tok = ""
        if header[:7].lower() == "bearer ":
            tok = header[7:].strip()
        if not tok:
            tok = (self.headers.get("X-ZS-Token") or "").strip()
        # Replit's custom-header field is documented as X-API-Key, and plenty of
        # other MCP clients default to it, so accept it as a third spelling.
        if not tok:
            tok = (self.headers.get("X-API-Key") or "").strip()
        if not tok:
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                tok = (qs.get("token") or [""])[0]
            except Exception:
                tok = ""
        return hmac.compare_digest(tok.encode("utf-8"), API_TOKEN.encode("utf-8"))

    def _require_auth(self):
        """True when the request may proceed; otherwise a 401 has been sent."""
        if self._authorized():
            return True
        log(f"[api] 401 - bad/missing token from {self.address_string()}", "yl")
        self._send_json({"ok": False, "error": "unauthorized - send "
                         "Authorization: Bearer <token> (ZS_API_TOKEN)"}, 401)
        return False

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-ZS-Token, X-API-Key, Mcp-Session-Id")
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        self.end_headers()
        self.wfile.write(body)

    def _send_mcp(self, payload, session, want_sse=False):
        """Answer a JSON-RPC message in the MCP transport shape.

        A client that accepts application/json gets exactly that. One that will
        ONLY accept text/event-stream (the other half of the streamable-HTTP
        spec) gets the same message wrapped as a single SSE `message` event,
        because refusing that client would make the endpoint unusable for it.
        """
        self.send_response(200)
        if want_sse:
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            body = ("event: message\ndata: " + json.dumps(payload) + "\n\n").encode("utf-8")
        else:
            self.send_header("Content-Type", "application/json")
            body = json.dumps(payload).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", session)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-ZS-Token, X-API-Key, Mcp-Session-Id")
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client hung up - nothing to clean up
        if want_sse:
            self.close_connection = True

    def _handle_mcp(self, body):
        """POST /mcp: one JSON-RPC message (or a batch) in, MCP responses out."""
        accept = self.headers.get("Accept") or ""
        want_sse = ("text/event-stream" in accept
                    and "application/json" not in accept
                    and "*/*" not in accept)
        session = self.headers.get("Mcp-Session-Id") or uuid.uuid4().hex
        batch = isinstance(body, list)
        msgs = body if batch else [body]
        responses = []
        for m in msgs:
            try:
                r = _mcp_handle_one(m)
            except Exception as e:            # never 500 a JSON-RPC endpoint
                rid = m.get("id") if isinstance(m, dict) else None
                r = _mcp_rpc_error(rid, -32603, f"internal error: {e}")
            if r is not None:
                responses.append(r)
        if not responses:
            # Every message was a notification: the spec wants 202, no body.
            self.send_response(202)
            self.send_header("Mcp-Session-Id", session)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = responses if batch else responses[0]
        if isinstance(payload, dict) and "error" in payload:
            log(f"[mcp] {payload['error'].get('message')}", "yl", terminal=False)
        self._send_mcp(payload, session, want_sse=want_sse)

    def _send_sse(self, payload, req_model=None):
        """Stream a COMPLETED /v1 reply as Server-Sent Events.

        Mobile AI apps and most OpenAI SDKs request `stream: true` by default
        and refuse a plain JSON body, so the endpoint has to answer in the SSE
        chunk shape. The brain here finishes the whole turn before it can say
        anything (one web-chat relay or one provider call), so there is nothing
        to dribble out token-by-token: the standard shapes are sent in one go -
        role delta, content delta, finish chunk, then [DONE]. A client that
        renders incrementally just paints the reply in a single frame.
        """
        choice = (payload.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason") or "stop"
        cid = payload.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}"
        model = payload.get("model") or req_model or OPENAI_MODEL_ID
        created = payload.get("created") or int(time.time())

        def chunk(delta, finish_reason=None):
            return {"id": cid, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta,
                                 "finish_reason": finish_reason}]}

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # No Content-Length on an event stream: the client reads until we close.
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-ZS-Token")
        self.end_headers()
        frames = [chunk({"role": "assistant", "content": ""}),
                  chunk({"content": content}),
                  chunk({}, finish)]
        try:
            for f in frames:
                self.wfile.write(f"data: {json.dumps(f)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the caller hung up mid-stream - nothing to clean up
        self.close_connection = True

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > _API_MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt, *args):  # keep the terminal clean; file-only
        log(f"[api] {self.address_string()} {fmt % args}", "dim", terminal=False)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-ZS-Token")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        # /health stays open even with a token: it is the liveness probe
        # (uptime robots, the tunnel health check) and leaks nothing useful.
        if path in ("/", "/health"):
            self._send_json({"ok": True, "version": BRIDGE_VERSION, "uptime": int(time.time() - _BOOT_TS)})
            return
        if not self._require_auth():
            return
        if path == "/status":
            self._send_json(_api_snapshot())
        elif path == "/tools":
            try:
                refresh = "refresh=1" in self.path or "refresh=true" in self.path
                tools = mgr.list_tools(refresh)
                self._send_json({"ok": True, "tools": tools})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif path in ("/v1/models", "/models"):
            # OpenAI-SDK clients list models to validate the base URL.
            self._send_json(openai_models_payload())
        elif path == "/mcp":
            # The spec explicitly allows a server that offers no server-initiated
            # SSE stream to answer GET with 405 - which is exactly this bridge.
            self.send_response(405)
            self.send_header("Allow", "POST, DELETE, OPTIONS")
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"jsonrpc": "2.0", "error": {
                "code": -32000, "message": "this MCP server is POST-only; it "
                "sends no server-initiated messages"}}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({"ok": False, "error": f"unknown route: {path} "
                            "(try /health /status /tools /v1/models, or POST /call /chat /v1/chat/completions /mcp)"}, 404)

    def do_DELETE(self):
        """MCP session termination: we hold no per-session state, so this is a
        clean 204 and the client drops its session id."""
        path = self.path.split("?", 1)[0].rstrip("/")
        if not self._require_auth():
            return
        if path == "/mcp":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send_json({"ok": False, "error": f"unknown route: {path}"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not self._require_auth():
            return
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 413)
            return
        except Exception as e:
            self._send_json({"ok": False, "error": f"bad JSON body: {e}"}, 400)
            return
        if path == "/mcp":
            self._handle_mcp(body)
        elif path == "/call":
            name = str(body.get("name") or "").strip()
            if not name:
                self._send_json({"ok": False, "error": "'name' is required"}, 400)
                return
            args = body.get("arguments") or {}
            try:
                timeout = float(body.get("timeout", 120))
            except (TypeError, ValueError):
                timeout = 120.0
            timeout = max(1.0, min(300.0, timeout))
            res = safe_call(name, args, timeout)
            res["name"] = name
            self._send_json(res)
        elif path == "/chat":
            # Request tracing: a pair of ->/  <- lines per request turns "it
            # keeps timing out" into a visible FACT (request arrived vs never
            # arrived; bridge answered after Ns vs still waiting on the tab).
            log(f"-> /chat  {str(body.get('message') or '')[:60]!r}", "cy", terminal=False)
            t0 = time.monotonic()
            res = chat_turn(body.get("message"), body.get("session"), body.get("timeout"))
            ok = bool(res.get("ok"))
            log(f"<- /chat  {time.monotonic() - t0:.1f}s  "
                f"{'ok' if ok else 'ERR: ' + str(res.get('error'))[:100]}", "gr" if ok else "rd")
            self._send_json(res, 200 if ok else 502)
        elif path in ("/chat/reset", "/chat/clear"):
            self._send_json(chat_reset(body.get("session")))
        elif path in ("/v1/chat/completions", "/chat/completions"):
            _msgs = body.get("messages") or []
            _stream = bool(body.get("stream"))
            log(f"-> /v1/chat/completions  {len(_msgs)} msg(s), "
                f"stream={_stream}, model={body.get('model')!r}", "cy", terminal=False)
            t0 = time.monotonic()
            # Ask for the finished reply either way, then decide how to deliver:
            # SSE when the client asked to stream, plain JSON otherwise.
            status, payload = openai_chat_completions({**body, "stream": False})
            log(f"<- /v1/chat/completions  {time.monotonic() - t0:.1f}s  HTTP {status}"
                + ("  (streamed)" if status == 200 and _stream else "")
                + ("" if status == 200 else f"  {payload.get('error', {}).get('message', '')[:100]}"),
                "gr" if status == 200 else "rd")
            if status == 200 and _stream:
                # A failed turn still answers as JSON (with its HTTP status), the
                # way OpenAI does - clients check the status before parsing SSE.
                self._send_sse(payload, body.get("model"))
            else:
                self._send_json(payload, status)
        else:
            self._send_json({"ok": False, "error": f"unknown route: {path} "
                            "(POST /call, /chat, /chat/reset, /v1/chat/completions)"}, 404)


def start_http_api():
    """Start the HTTP API in a daemon thread (loopback by default; other devices
    only when ZS_API_HOST says so and the token/open guards allow it - see
    _api_bind_host). Best-effort: the bridge (WebSocket) must keep serving the
    extension even if this fails to bind."""
    if not API_ENABLED:
        log("local HTTP API disabled (ZS_API=0)", "dim")
        return
    bind = _api_bind_host()  # logs + falls back to loopback on an unsafe request
    try:
        httpd = ThreadingHTTPServer((bind, API_PORT), _APIHandler)
        httpd.daemon_threads = True
    except OSError as e:
        log(f"HTTP API could not bind {bind}:{API_PORT} ({e}) - the WebSocket bridge "
            f"is unaffected. Pick another port with ZS_API_PORT.", "yl")
        return
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    if _api_host_is_loopback(bind):
        log(f"local HTTP API on http://{bind}:{API_PORT}  (GET /health /status /tools "
            f"/v1/models - POST /call /chat /chat/reset /v1/chat/completions /mcp)", "cy")
        log("    /mcp is an MCP server: point a remote agent (Replit, Cursor, "
            "Claude...) at it after sharing the port", "dim")
    else:
        log(f"SHARED HTTP API on http://{bind}:{API_PORT} - other devices on the "
            f"network can curl it now", "cy")
        if API_TOKEN:
            log("    token auth ON - callers must send  Authorization: Bearer "
                "<your ZS_API_TOKEN>  (/health stays open)", "cy")
        else:
            log("    WARNING: ZS_API_OPEN=1 and no token - ANYONE who can reach "
                "this port can run tools (shell + in-game code)!", "yl")
    if not LLM_KEY:
        log("    /chat is KEYLESS: turns run through the browser extension (keep "
            "the AI chat tab OPEN - the extension brings it to the front "
            "itself). For fully headless /chat set ZS_LLM_KEY (or "
            "DEEPSEEK_API_KEY). Tool endpoints need neither.", "dim")


async def _loop_lag_watch():
    """Bark when the event loop stops ticking. Keyless /chat relays live on
    this loop: ONE synchronous blocking call anywhere freezes every extension
    socket along with them, and the caller only ever sees a timeout with zero
    explanation ("the extension answered but the bridge heard it 15s late").
    """
    t0 = time.monotonic()
    while True:
        await asyncio.sleep(1.0)
        now = time.monotonic()
        lag = now - t0 - 1.0
        t0 = now
        if lag > 2.0:
            log(f"EVENT LOOP BLOCKED for {lag:.1f}s - a synchronous call froze "
                f"the bridge; chats/relays relay nothing meanwhile", "rd")


async def main():
    print(f"\n{C['cy']}  ZeroScript Bridge v{BRIDGE_VERSION}{C['reset']}  {C['dim']}- Roblox Studio - ws://{HOST}:{PORT}{C['reset']}\n")
    log(f"===== BRIDGE START  v{BRIDGE_VERSION}  pid={os.getpid()}  log={LOG_PATH} =====", "cy")
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()  # HTTP API threads schedule keyless /chat relays onto this loop
    asyncio.create_task(_loop_lag_watch())
    await asyncio.to_thread(_kill_orphan_studio_mcp)
    killed_squatter = await asyncio.to_thread(check_studio_port)
    if _host_is_android():
        _ensure_android_local_servers()
    mgr.load_config()

    # The primary 'roblox' server launches StudioMCP, which only exists with
    # desktop Roblox Studio (Windows / macOS). On Android/Termux (or any other
    # host) that child exits instantly with code 1 - and server_watch then
    # restarted it forever, a loud crash loop ('exit code 1' + '[Errno 32]
    # Broken pipe' every ~15s, seen live on Termux) that buried the bridge's
    # real status and could never possibly succeed. Skip it here instead. Only
    # our own default launcher is skipped: a user who pointed the primary at a
    # custom command keeps it.
    rc = mgr.clients.get(PRIMARY_SERVER_ID)
    if (rc is not None and not _platform_supports_studio_mcp()
            and os.path.basename(str(rc.command)) == "launch_studio_mcp.py"):
        del mgr.clients[PRIMARY_SERVER_ID]
        log(f"this platform ({sys.platform}) cannot run Roblox Studio, so the 'roblox' "
            "Studio MCP server is skipped - it would otherwise crash-loop forever.", "yl")
        if _host_is_android():
            log("Android path: the Synapse (in-game) and Termux (shell) servers register "
                "automatically - for in-game control also run  python3 synapse/relay.py .", "cy")

    # Shared "we already told the user the corrective step" flag. Two producers
    # can print the 'toggle Studio's MCP server' action banner: the early
    # _early_studio_guidance task (fast, doesn't wait for start_all's ~48s grace
    # loop) and the post-start_all diagnostic block in _boot_and_diagnose. This
    # flag lets whichever fires first suppress the other, so the user never sees
    # the same instruction twice. Mutable dict so both nested coroutines share it.
    _guidance_shown = {"v": False}

    async def _early_studio_guidance():
        """Print the corrective action banner WITHOUT waiting for start_all()'s
        full ~48s grace loop.

        RobloxClient.start() retries tools/list for up to ~48s to catch a Studio
        that attaches a little late - correct for a Studio that IS coming, but it
        also means a user whose Studio is simply closed or whose MCP toggle is off
        waits ~48s before the terminal tells them what to do (the extension, which
        reads status over the socket, already says it immediately). So after a
        short grace we check independently and, if still not connected, show the
        step now. If Studio then attaches, studio_watch prints the green
        'connected' line - so an early banner is at worst redundant, never wrong
        (the user confirmed a premature toggle hint during boot is harmless).

        Deliberately does NOT try to distinguish 'Studio closed' from 'MCP toggle
        off': probe_studio can't tell them apart (both read app=False, see its
        docstring), and the banner wording already covers both, so there is no
        finer detection to preserve here. A proven port-squatter case is left to
        _boot_and_diagnose / studio_watch, which print their own, more specific
        hint."""
        if PRIMARY_SERVER_ID not in mgr.clients:
            return
        await asyncio.sleep(12)  # give a fast, normal attach the chance to win
        if _guidance_shown["v"]:
            return
        rc = mgr.clients.get(PRIMARY_SERVER_ID)
        if rc is None or getattr(rc, "saw_foreign_ws_host", False):
            return
        if rc.tools_cache:
            # Tools present - either connected, or an attach is mid-flight; defer
            # to the authoritative probe / studio_watch rather than second-guess.
            st = await asyncio.to_thread(probe_studio)
            if st["app"] is not False:
                return
        _guidance_shown["v"] = True
        action_banner([
            "Open your place in Roblox Studio.",
            "Go to: Assistant Settings > MCP Servers",
            "       > 'Enable Studio as MCP server'",
            "It can take up to ~10s; this window will turn green.",
        ])

    async def _boot_and_diagnose():
        """Launch every configured MCP server and print the boot diagnostic
        banner. Runs as a background task AFTER the socket below is already
        listening, so a slow or absent Roblox Studio never delays the
        extension's ability to connect and use OTHER MCP servers (e.g.
        Blender) right away - only the terminal banner and Roblox's own
        auto-recovery loop wait on this. (mgr.start_all() itself also
        launches every server in parallel now, for the same reason.)"""
        try:
            await asyncio.to_thread(mgr.start_all)
        except Exception as e:
            log(f"server startup error: {e}", "rd")
            log("The bridge will keep running; it retries on the first tool call.", "yl")
        total = len(mgr.list_tools())
        # Roblox-only count for the corrective message below: list_tools() sums
        # every configured server (Roblox + addons like Blender), so printing
        # `total` there falsely blamed addon tools on "NO Roblox Studio connected"
        # (seen live: 49 = 27 Roblox + 22 Blender, message only about Roblox).
        roblox_client = mgr.clients.get("roblox")
        roblox_total = len(roblox_client.tools_cache) if roblox_client else 0

        # Port-hijack check (ropilot etc.), done at boot: the child's stderr has
        # by now had its ~12s grace loop to reveal it connected to a foreign host
        # on the MCP port. This is proof the port is squatted even when the
        # one-shot check_studio_port() at startup missed it (a background helper
        # grabbing the port a beat after that check ran - seen live 2026-07-13).
        if (roblox_client is not None and roblox_total == 0
                and roblox_client.saw_foreign_ws_host):
            killed, sname = await asyncio.to_thread(_kill_port_squatter)
            if killed:
                try:
                    await asyncio.to_thread(mgr.restart, "roblox")
                    roblox_total = len(roblox_client.tools_cache)
                    total = len(mgr.list_tools())
                except Exception as e:
                    log(f"roblox proxy restart after squatter kill failed: {e}", "rd")
                _print_squatter_hint(sname)

        # Set True if we kill a leftover StudioMCP zombie below and print the
        # re-register hint - so the diagnostic block further down doesn't ALSO
        # print its own near-identical action banner (the same de-duplication
        # killed_squatter already does for the ropilot-squatter path).
        reclaimed_zombie = False
        # Zombie-port deadlock check, done at boot too (not just studio_watch):
        # with Studio ALREADY open, _kill_orphan_studio_mcp was skipped by its
        # safety guard, so a leftover StudioMCP.exe may still own the port and
        # our fresh proxy just spent its 12s grace loop talking to nothing.
        if (roblox_client is not None and roblox_total == 0
                and await asyncio.to_thread(_roblox_studio_app_running) is True
                and await asyncio.to_thread(_reclaim_studio_port, roblox_client)):
            try:
                await asyncio.to_thread(mgr.restart, "roblox")
                roblox_total = len(roblox_client.tools_cache)
                total = len(mgr.list_tools())
            except Exception as e:
                log(f"roblox proxy restart after zombie kill failed: {e}", "rd")
            reclaimed_zombie = True
            _print_reregister_hint()

        # A tool count alone only proves StudioMCP (the proxy) is up - it advertises
        # its catalogue even with NO Studio attached. The authoritative "a Studio is
        # actually connected" signal is the list_roblox_studios probe. So we probe
        # FIRST and only show the green "ready" line when Studio is really attached;
        # otherwise we show just the corrective step (no misleading green success).
        # Probe even when total == 0: StudioMCP advertises an EMPTY catalogue when
        # Studio's MCP server toggle is off (or no place is open), so 0 tools is the
        # most common "needs a corrective step" state, not a success.
        _st = await asyncio.to_thread(probe_studio)
        # Even when Studio (and its place) were ALREADY open before the bridge
        # started, the freshly-launched StudioMCP proxy needs a moment to (re)bind
        # to Studio's own MCP port - so an instant probe right after launch often
        # reads app=False for a beat before flipping True a few seconds later
        # (studio_watch would catch it, but only after printing a scary yellow
        # "not connected" block first). Give it the same grace period the tools
        # probe already gets before deciding it is a real problem.
        if _st["app"] is False:
            with _Spinner("    waiting for Roblox Studio to attach..."):
                for _ in range(8):
                    await asyncio.sleep(1)
                    _st = await asyncio.to_thread(probe_studio)
                    if _st["app"] is not False:
                        break
        # A single app=True reading can be a STALE positive: StudioMCP.exe can
        # answer list_roblox_studios with a leftover studio entry from a PREVIOUS
        # session even though no Studio window is actually open right now (seen
        # live 2026-07-11: bridge booted with Studio fully closed, still printed
        # "Roblox Studio connected" from the very first probe). studio_watch
        # already distrusts a single reading for PLACE transitions the same way -
        # apply the identical confirm-before-trusting step here for APP, so the
        # boot banner can't announce a connection that isn't really there.
        if _st["app"] is True:
            await asyncio.sleep(1.5)
            confirm = await asyncio.to_thread(probe_studio)
            if confirm["app"] is not True:
                _st = confirm
        if roblox_client is not None and (roblox_total == 0 or _st["app"] is False):
            if killed_squatter or reclaimed_zombie or _guidance_shown["v"]:
                # The action banner was ALREADY shown - either right after a kill
                # (check_studio_port / _print_reregister_hint for a zombie) or by
                # the early _early_studio_guidance task. Repeating the full
                # explanation here in a different color, seconds later, reads as a
                # second unrelated problem to a non-technical user (seen live
                # 2026-07-13: the toggle instruction and this block blurred
                # together). Just confirm we're still waiting, no new instructions.
                log("    still waiting for you to toggle Studio's MCP server "
                    "(see the action box above)...", "yl")
            else:
                _guidance_shown["v"] = True
                # No squatter: Studio is simply closed, or its MCP option is off.
                # Match the exact steps the extension itself tells the user
                # (Assistant Settings > MCP Servers), not a paraphrase - a
                # differently-worded instruction here reads as a second,
                # unrelated problem instead of the same one step.
                if roblox_total > 0:
                    log(f"    {roblox_total} Roblox tools loaded, but NO Roblox Studio is connected yet.", "yl")
                    log("    (This can be a slow attach that clears itself within ~10-15s -", "yl")
                    log("    watch for a green 'Roblox Studio connected' line right after.)", "yl")
                action_banner([
                    "Open your place in Roblox Studio.",
                    "Go to: Assistant Settings > MCP Servers",
                    "       > 'Enable Studio as MCP server'",
                    "It can take up to ~10s; this window will turn green.",
                ])
        elif _st["app"] is True:
            log(f"ready {total} tools available - Roblox Studio connected", "gr")
        else:
            log(f"ready {total} tools available ({len(mgr.clients)} MCP server(s))", "gr")
        # studio_watch only monitors the primary Roblox Studio server - on a
        # platform where that server was skipped (e.g. Android/Termux) there is
        # nothing for it to watch, so don't start it at all.
        if PRIMARY_SERVER_ID in mgr.clients:
            asyncio.create_task(_supervised(
                "studio_watch", lambda: studio_watch(_st["app"], _st["place"])))

    async def _early_status_pushes():
        """A few follow-up status broadcasts shortly after boot.

        mgr.start_all() still doesn't RETURN until every server's thread has
        joined - including Roblox's, which can take up to ~48s (StudioMCP's
        own internal "waiting for tools" retry loop, seen live). So a single
        broadcast placed after start_all() would be just as slow as the old
        blocking behavior for the exact case this is meant to fix: an addon
        server (e.g. Blender) that's ready in 1-13s while Roblox is still
        slowly timing out. Poll-and-broadcast a few times instead, cheaply,
        so any extension that connected during that window self-corrects
        quickly instead of staying stuck on its first, incomplete snapshot.
        """
        for interval in (2, 2, 4, 6, 6):  # cumulative: 2s, 4s, 8s, 14s, 20s after boot
            await asyncio.sleep(interval)
            await broadcast_status()

    # Free our own port from a leftover bridge (double-launch / X-closed window /
    # prior crash) BEFORE binding, so relaunching start.bat "just works" instead
    # of dying on WinError 10048. Only ever kills a proven bridge.py; anything
    # else falls through to the friendly bind-error below.
    if await asyncio.to_thread(_reclaim_bridge_port):
        await asyncio.sleep(0.6)  # let Windows release the socket before we bind

    try:
        server_ctx = await websockets.serve(
            handler, HOST, PORT, ping_interval=20, ping_timeout=20,
            max_size=16 * 1024 * 1024)
    except OSError as e:
        # errno 10048 (Win) / EADDRINUSE: something we could NOT auto-kill still
        # owns the port - another app, or a python whose cmdline we couldn't read.
        if getattr(e, "errno", None) in (98, 10048) or "10048" in str(e):
            owner = await asyncio.to_thread(_port_owner, PORT)
            who = f" by '{owner[1]}' (pid {owner[0]})" if owner else ""
            log(f"could not start: port {PORT} is already in use{who}.", "rd")
            log(f"    A previous bridge may still be running, or another app took "
                f"the port. Close it, then relaunch. To find it:", "yl")
            log(f"      netstat -ano | findstr {PORT}", "yl")
            log(f"      taskkill /F /PID <the pid from the last column>", "yl")
            log(f"    Or set a different port before start.bat:  set ZS_BRIDGE_PORT=17614", "yl")
            return
        raise

    async with server_ctx:
        log(f"listening on ws://{HOST}:{PORT}  - load the extension and open a supported AI chat", "cy")
        start_http_api()
        asyncio.create_task(_supervised("server_watch", server_watch))
        asyncio.create_task(_boot_and_diagnose())
        asyncio.create_task(_early_studio_guidance())
        asyncio.create_task(_early_status_pushes())
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    _cli_rc = mcp_cli(sys.argv[1:])
    if _cli_rc is not None:
        sys.exit(_cli_rc)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("shutting down...", "yl")
        for c in mgr.clients.values():
            c.stop()
    finally:
        log("===== BRIDGE STOP =====", "cy")
