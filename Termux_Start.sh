#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Termux_Start.sh
# -----------------------------------------------------------------------------
#  One-tap Android launcher (the Termux equivalent of start.bat /
#  MacOS_Start.command). Starts both halves of the Android path:
#
#     synapse/relay.py   (HTTP :18081  -  the in-game executor talks to this)
#     bridge.py          (WebSocket    -  the browser extension talks to this)
#
#  First run installs the one Python dependency if it is missing. The bridge
#  runs in the foreground (so you can see its status); Ctrl+C stops it AND
#  the relay. Run it from anywhere - it cds to its own folder first:
#
#     sh Termux_Start.sh        (or:  chmod +x Termux_Start.sh && ./Termux_Start.sh)
# -----------------------------------------------------------------------------

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1

PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "[termux-start] python3 not found - run:  pkg install python"
    exit 1
fi

if ! "$PY" -c "import websockets" >/dev/null 2>&1; then
    echo "[termux-start] installing missing dependency (websockets)..."
    "$PY" -m pip install websockets || {
        echo "[termux-start] pip install failed - run it yourself:  pip install websockets"
        exit 1
    }
fi

mkdir -p logs
RELAY_PID=""

# Relay on :18081 (skipped if one is already running, e.g. from an earlier
# session you left on purpose).
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:18081/health; then
    echo "[termux-start] synapse relay already running on :18081 - leaving it alone"
else
    echo "[termux-start] starting synapse relay (log: logs/relay.log)"
    "$PY" synapse/relay.py >"logs/relay.log" 2>&1 &
    RELAY_PID=$!
fi

# Keep Android from killing Termux while you play (no-op without termux-api).
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
    echo "[termux-start] wake lock held - Android should not kill this session"
fi

cleanup() {
    if [ -n "$RELAY_PID" ]; then
        kill "$RELAY_PID" 2>/dev/null
    fi
    command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock
}
trap cleanup EXIT INT TERM

echo "[termux-start] starting bridge (Ctrl+C stops bridge + relay)"
echo "[termux-start] on Android the bridge SKIPS the 'roblox' Studio server on"
echo "[termux-start] purpose (Studio cannot run on Android) - that yellow line is fine."

# Sharing the HTTP API (ZS_API_HOST set to a non-loopback address): show how
# people on the same network reach it. The bridge enforces ZS_API_TOKEN itself.
case "${ZS_API_HOST:-}" in
    ""|127.0.0.1|localhost|::1) ;;
    *)
        IP="$(ip -4 addr show scope global 2>/dev/null | awk '/inet /{sub(/\/.*$/, "", $2); print $2; exit}')"
        [ -z "$IP" ] && IP="$(ifconfig 2>/dev/null | awk '/inet /{print $2; exit}')"
        echo "[termux-start] HTTP API shared on port ${ZS_API_PORT:-17614} - people on your network can curl:"
        echo "               curl -H 'Authorization: Bearer <ZS_API_TOKEN>' http://${IP:-<this-phone-ip>}:${ZS_API_PORT:-17614}/status"
        if [ -z "${ZS_API_TOKEN:-}" ] && [ "${ZS_API_OPEN:-}" != "1" ]; then
            echo "[termux-start] (no ZS_API_TOKEN set - the bridge will REFUSE the shared bind and stay loopback)"
        fi
        ;;
esac

"$PY" bridge.py
