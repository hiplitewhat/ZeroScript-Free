# SPDX-License-Identifier: GPL-3.0-or-later
# synapse/mcp_synapse.py
# -----------------------------------------------------------------------------
# Synapse MCP server (stdio JSON-RPC). Registers itself as an addon MCP server
# for ZeroScript's local bridge, so the AI can drive the in-game executor:
#
#   bridge.py  --(stdio JSON-RPC)-->  mcp_synapse.py  --(HTTP)-->  relay.py
#                                                                      |
#                                                         in-game executor
#                                                     (synapse.lua in Delta)
#
# Add it from the ZeroScript panel:
#   Name: Synapse
#   Command: synapse/mcp_synapse.py
# (a relative .py path is resolved against the ZeroScript repo root and
#  launched with the bridge's own Python interpreter). Port the relay reaches:
#  SYNAPSE_RELAY_PORT env (default 18081), same as synapse/relay.py.
#
# The server must be started with the relay already running and the executor
# already loaded in the game for actual tool calls to succeed.
# -----------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from typing import Any

import screenshot as _shot
try:
    import android_screenshot as _android_shot
except Exception:
    _android_shot = None

RELAY_HOST = os.environ.get("SYNAPSE_RELAY_HOST", "127.0.0.1")
RELAY_PORT = int(os.environ.get("SYNAPSE_RELAY_PORT", "18081"))
RELAY = f"http://{RELAY_HOST}:{RELAY_PORT}"
PROTOCOL_VERSION = os.environ.get("SYNAPSE_MCP_PROTOCOL", "2024-11-05")
SERVER_NAME = "synapse"
SERVER_VERSION = "1.0.0"


def _relay(method: str, path: str, body: dict | None = None, timeout: float = 40.0) -> dict:
    """Call the local relay; returns the parsed JSON body."""
    url = RELAY + path
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return {"ok": False, "out": "", "err": f"relay unreachable ({exc.reason})"}
    except Exception as exc:  # json error / unexpected
        return {"ok": False, "out": "", "err": str(exc)}


def _send_command(cmd: Any, raw: bool = True, timeout: float = 30.0) -> dict:
    body = {"cmd": cmd, "raw": raw, "timeout": timeout}
    return _relay("POST", "/execute", body, timeout=timeout + 5.0)


# -----------------------------------------------------------------------------
#  MCP TOOLS
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


TOOLS: list[dict] = [
    _tool(
        "synapse.ping",
        "Check that the Synapse relay and the in-game executor are alive. Returns relay status, executor/session info and the current player.",
        {"timeout": {"type": "number", "description": "Seconds to wait (default 10)."}},
        [],
    ),
    _tool(
        "synapse.diag",
        "Report the executor's HTTP transport mode, polling/error counters and the last transport error. Use when the relay loads but never receives commands.",
        {"timeout": {"type": "number", "description": "Seconds to wait (default 10)."}},
        [],
    ),
    _tool(
        "synapse.self_test",
        "Run a one-shot GET /health through the executor's chosen HTTP transport and report whether the relay is reachable.",
        {"timeout": {"type": "number", "description": "Seconds to wait (default 10)."}},
        [],
    ),
    _tool(
        "synapse.screenshot",
        "Capture the local desktop/monitor (the Delta executor, relay terminal, browser, etc.) and return it as a PNG image. Needs Python 'mss' or Pillow on the machine running the relay.",
        {
            "monitor": {"type": "number", "description": "Monitor index for mss. Omit for the whole desktop union. 1 is usually the primary monitor."},
            "timeout": {"type": "number", "description": "Seconds to wait (default 10)."},
        },
        [],
    ),
    _tool(
        "synapse.execute",
        "Run arbitrary Luau code inside the live Roblox game (Delta executor). Use `return` to capture a value; tables and scalars are JSON-encoded.",
        {
            "code": {"type": "string", "description": "Luau source to execute."},
            "timeout": {"type": "number", "description": "Seconds to wait (default 30, max 60)."},
        },
        ["code"],
    ),
    _tool(
        "synapse.player",
        "Read the local player + character state: name, user id, health, position, team, place/job ids.",
        {"timeout": {"type": "number", "description": "Seconds to wait (default 15)."}},
        [],
    ),
    _tool(
        "synapse.game_tree",
        "Walk the game tree and return up to N instances (name, class, path, child count). Useful to understand the world before scripting.",
        {
            "max": {"type": "number", "description": "Max instances to return (default 250)."},
            "timeout": {"type": "number", "description": "Seconds to wait (default 15)."},
        },
        [],
    ),
    _tool(
        "synapse.find",
        "Look up instances by name/substring and/or class across Workspace, ReplicatedStorage, ServerStorage, ServerScriptService and Players.",
        {
            "name": {"type": "string", "description": "Instance name or substring to match."},
            "class": {"type": "string", "description": "Instance class filter, e.g. RemoteEvent, Script, Model."},
            "max": {"type": "number", "description": "Max results (default 50)."},
            "timeout": {"type": "number", "description": "Seconds to wait (default 15)."},
        },
        [],
    ),
    _tool(
        "synapse.fire_remote",
        "Fire a RemoteEvent. Accepts a name (searched as a path) or a full path like Workspace/Game/Farm or ReplicatedStorage/FarmEvent.",
        {
            "name": {"type": "string", "description": "RemoteEvent name or full path."},
            "args": {"description": "Arguments to pass. Accepts a JSON array, a JSON-encoded array string, or a single value.", "oneOf": [{"type": "array", "items": {}}, {"type": "string"}]},
            "timeout": {"type": "number", "description": "Seconds to wait (default 15)."},
        },
        ["name"],
    ),
    _tool(
        "synapse.invoke_remote",
        "Invoke a RemoteFunction and return its result. The call runs on a background thread with a timeout.",
        {
            "name": {"type": "string", "description": "RemoteFunction name or full path."},
            "args": {"description": "Arguments to pass. Accepts a JSON array, a JSON-encoded array string, or a single value.", "oneOf": [{"type": "array", "items": {}}, {"type": "string"}]},
            "timeout": {"type": "number", "description": "Seconds to wait for the function (default 8)."},
            "wait": {"type": "number", "description": "Seconds to wait for the whole call (default 15)."},
        },
        ["name"],
    ),
    _tool(
        "synapse.teleport",
        "Move the local player's HumanoidRootPart to a world position.",
        {
            "x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"},
            "timeout": {"type": "number", "description": "Seconds to wait (default 15)."},
        },
        ["x", "y", "z"],
    ),
    _tool(
        "synapse.auto_click",
        "Send synthetic left-click mouse events at a screen coordinate via VirtualInputManager.",
        {
            "x": {"type": "number"}, "y": {"type": "number"},
            "count": {"type": "number", "description": "Number of clicks (default 1, max 100)."},
            "delay": {"type": "number", "description": "Seconds between clicks (default 0.05)."},
            "timeout": {"type": "number", "description": "Seconds to wait (default 15)."},
        },
        ["x", "y"],
    ),
    _tool(
        "synapse.anti_kick",
        "Install a metatable hook on the local player that swallows Kick calls. Idempotent (safe to call repeatedly).",
        {"timeout": {"type": "number", "description": "Seconds to wait (default 15)."}},
        [],
    ),
    _tool(
        "synapse.spoof",
        "Spoof the local player's UserId and/or Name via a __index metatable hook. Pass at least one of user_id / name.",
        {
            "user_id": {"type": "number", "description": "Fake UserId to report."},
            "name": {"type": "string", "description": "Fake Name to report."},
            "timeout": {"type": "number", "description": "Seconds to wait (default 15)."},
        },
        [],
    ),
    _tool(
        "synapse.env_get",
        "Read a key from the executor's persistent session environment (survives across commands).",
        {"key": {"type": "string"}, "timeout": {"type": "number", "description": "Seconds to wait (default 15)."}},
        ["key"],
    ),
    _tool(
        "synapse.env_set",
        "Write a value into the executor's persistent session environment.",
        {"key": {"type": "string"}, "value": {}, "timeout": {"type": "number", "description": "Seconds to wait (default 15)."}},
        ["key"],
    ),
    _tool(
        "synapse.env_list",
        "List every key currently held in the executor session environment.",
        {"timeout": {"type": "number", "description": "Seconds to wait (default 15)."}},
        [],
    ),
    _tool(
        "synapse.reset",
        "Clear the executor's session environment and installed hooks.",
        {"timeout": {"type": "number", "description": "Seconds to wait (default 15)."}},
        [],
    ),
]


def _call_tool(name: str, arguments: dict) -> dict:
    args = arguments or {}
    timeout = min(60.0, max(1.0, float(args.get("timeout", 15) or 15)))

    if name == "synapse.ping":
        resp = _send_command({"op": "ping"}, raw=False, timeout=timeout)
    elif name == "synapse.diag":
        resp = _send_command({"op": "diag"}, raw=False, timeout=timeout)
    elif name == "synapse.self_test":
        resp = _send_command({"op": "self_test"}, raw=False, timeout=timeout)
    elif name == "synapse.screenshot":
        on_android = bool(_android_shot and _android_shot.is_android())
        data, err = _shot.capture_png_b64(args.get("monitor"))
        if err:
            return {"content": [{"type": "text", "text": f"ERROR: {err}"}], "isError": True}
        caption = ("Phone screen captured via ScreenshotTile." if on_android
                   else "Desktop captured.")
        return {
            "content": [
                {"type": "text", "text": f"{caption} The image is attached to this message - look at it and describe what you see."},
                {"type": "image", "data": data, "mimeType": "image/png"},
            ]
        }
    elif name == "synapse.execute":
        code = args.get("code", "")
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "out": "", "err": "code is required"}
        resp = _send_command(code, raw=True, timeout=timeout)
    elif name == "synapse.player":
        resp = _send_command({"op": "player"}, raw=False, timeout=timeout)
    elif name == "synapse.game_tree":
        resp = _send_command({"op": "tree", "max": args.get("max", 250)}, raw=False, timeout=timeout)
    elif name == "synapse.find":
        resp = _send_command({"op": "find", "name": args.get("name"), "class": args.get("class"),
                              "max": args.get("max", 50)}, raw=False, timeout=timeout)
    elif name == "synapse.fire_remote":
        resp = _send_command({"op": "fire", "name": args.get("name"),
                              "args": args.get("args")}, raw=False, timeout=timeout)
    elif name == "synapse.invoke_remote":
        resp = _send_command({"op": "invoke", "name": args.get("name"),
                              "args": args.get("args"),
                              "timeout": args.get("wait", 8)}, raw=False, timeout=timeout)
    elif name == "synapse.teleport":
        resp = _send_command({"op": "teleport", "x": args.get("x"), "y": args.get("y"),
                              "z": args.get("z")}, raw=False, timeout=timeout)
    elif name == "synapse.auto_click":
        resp = _send_command({"op": "click", "x": args.get("x"), "y": args.get("y"),
                              "count": args.get("count", 1), "delay": args.get("delay", 0.05)},
                             raw=False, timeout=timeout)
    elif name == "synapse.anti_kick":
        resp = _send_command({"op": "anti_kick"}, raw=False, timeout=timeout)
    elif name == "synapse.spoof":
        resp = _send_command({"op": "spoof", "user_id": args.get("user_id"),
                              "name": args.get("name")}, raw=False, timeout=timeout)
    elif name == "synapse.env_get":
        resp = _send_command({"op": "env_get", "key": args.get("key")}, raw=False, timeout=timeout)
    elif name == "synapse.env_set":
        resp = _send_command({"op": "env_set", "key": args.get("key"),
                              "value": args.get("value")}, raw=False, timeout=timeout)
    elif name == "synapse.env_list":
        resp = _send_command({"op": "env_list"}, raw=False, timeout=timeout)
    elif name == "synapse.reset":
        resp = _send_command({"op": "reset"}, raw=False, timeout=timeout)
    else:
        return {"ok": False, "out": "", "err": f"unknown tool: {name}"}

    text = ""
    if resp.get("ok"):
        out = resp.get("out", "")
        if isinstance(out, (dict, list)):
            text = json.dumps(out, ensure_ascii=False)
        elif isinstance(out, str):
            try:
                text = json.dumps(json.loads(out), ensure_ascii=False)
            except Exception:
                text = out
        else:
            text = json.dumps(out, ensure_ascii=False)
    else:
        text = "ERROR: " + str(resp.get("err", "unknown"))
        return {"content": [{"type": "text", "text": text}], "isError": True}
    return {"content": [{"type": "text", "text": text}]}


def _respond(msg_id: Any, result: dict | None = None, error: dict | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _notify(method: str, params: dict | None = None) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
    sys.stdout.flush()


def _tools_result() -> dict:
    return {"tools": TOOLS}


def main() -> int:
    # Keep errors out of the stdio protocol stream; the bridge echoes stderr.
    sys.stderr.write(f"[synapse] MCP server '{SERVER_NAME}' v{SERVER_VERSION}\n")
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
