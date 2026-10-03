# SPDX-License-Identifier: GPL-3.0-or-later
# synapse/controller.py
# -----------------------------------------------------------------------------
# Direct command-line driver for the Synapse executor. Handy when you don't
# want the whole MCP stack: start relay.py, load synapse.lua in the game, then
# drive it from here.
#
#   python3 synapse/controller.py
#   > return game.PlaceId
#   > !player
#   > !click 400 700 5
#   > quit
#
# Line starting with `!` is a semantic op; everything else is raw Luau.
# -----------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

RELAY_HOST = os.environ.get("SYNAPSE_RELAY_HOST", "127.0.0.1")
RELAY_PORT = int(os.environ.get("SYNAPSE_RELAY_PORT", "18081"))
RELAY = f"http://{RELAY_HOST}:{RELAY_PORT}"

SEMANTIC = {
    "player": {"op": "player"},
    "ping": {"op": "ping"},
    "antikick": {"op": "anti_kick"},
    "reset": {"op": "reset"},
    "env": {"op": "env_list"},
}


def _screenshot(save=None):
    import screenshot as _shot
    import android_screenshot as _android_shot
    import tempfile
    try:
        if _android_shot.is_android():
            data, err = _android_shot.capture_png_b64()
        else:
            data, err = _shot.capture_png_b64()
    except Exception as exc:
        return {"ok": False, "out": "", "err": f"screenshot import failed: {exc}"}
    if err:
        return {"ok": False, "out": "", "err": err}
    path = save or os.path.join(tempfile.gettempdir(), "synapse_screenshot.png")
    import base64
    with open(path, "wb") as f:
        f.write(base64.b64decode(data))
    return {"ok": True, "out": path, "err": ""}


def _execute(cmd, raw=True, timeout=30):
    payload = json.dumps({"cmd": cmd, "raw": raw, "timeout": timeout}).encode("utf-8")
    req = urllib.request.Request(RELAY + "/execute", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return {"ok": False, "out": "", "err": f"relay unreachable ({exc.reason})"}


def main() -> int:
    print(f"[synapse] controller -> {RELAY}")
    print("type  !player  |  !click x y count  |  !tell x y z  |  !fire RemotePath arg..  (or raw Luau)")
    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if raw.lower() in ("quit", "exit"):
            break

        if raw.startswith("!"):
            parts = raw[1:].split()
            if not parts:
                continue
            name = parts.pop(0)
            if name == "player":
                cmd = {"op": "player"}
            elif name == "ping":
                cmd = {"op": "ping"}
            elif name == "antikick":
                cmd = {"op": "anti_kick"}
            elif name == "reset":
                cmd = {"op": "reset"}
            elif name == "env":
                cmd = {"op": "env_list"}
            elif name in ("ss", "shot", "screenshot"):
                print(_screenshot().get("out") or _screenshot().get("err", "failed"))
                continue
            elif name in ("tell", "teleport"):
                if len(parts) < 3:
                    print("usage: !tell x y z")
                    continue
                cmd = {"op": "teleport", "x": float(parts[0]), "y": float(parts[1]), "z": float(parts[2])}
            elif name == "click":
                if len(parts) < 2:
                    print("usage: !click x y [count] [delay]")
                    continue
                cmd = {"op": "click", "x": float(parts[0]), "y": float(parts[1]),
                       "count": int(parts[2]) if len(parts) > 2 else 1,
                       "delay": float(parts[3]) if len(parts) > 3 else 0.05}
            elif name == "fire":
                if len(parts) < 1:
                    print("usage: !fire RemotePath [arg1 ...]")
                    continue
                args = []
                for a in parts[1:]:
                    try:
                        args.append(json.loads(a))
                    except Exception:
                        args.append(a)
                cmd = {"op": "fire", "name": parts[0], "args": args}
            else:
                print(f"unknown op: {name}")
                continue
            result = _execute(cmd, raw=False)
        else:
            result = _execute(raw, raw=True)

        if result.get("ok"):
            print(result.get("out", "") or "OK")
        else:
            print("ERROR:", result.get("err", "unknown"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
