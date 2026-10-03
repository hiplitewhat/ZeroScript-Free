#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# mcp_stdio_bridge.py
# ──────────────────────────────────────────────────────────────────────────
#  Use the ZeroScript bridge from an AI that only speaks STDIO MCP.
#
#  The bridge answers MCP over HTTP (POST /mcp), which a cloud agent (Replit)
#  can call directly. Most AI APPS, though - Claude Desktop, Cursor, Cline,
#  Windsurf, Cherry Studio, 5ire, LM Studio... - configure MCP servers as a
#  COMMAND they launch and talk to over stdin/stdout. This script is that
#  command: it speaks stdio MCP on one side and forwards every message to the
#  bridge's HTTP endpoint on the other, so any of them can drive the same
#  Roblox / Synapse / Termux tools.
#
#      AI app  --stdio JSON-RPC-->  mcp_stdio_bridge.py  --HTTP-->  bridge :17614/mcp
#
#  Config (either form):
#      --url http://127.0.0.1:17614/mcp     or  ZS_MCP_URL
#      --token <ZS_API_TOKEN>               or  ZS_API_TOKEN
#      --timeout 120                        or  ZS_MCP_TIMEOUT (seconds)
#
#  Stdlib only, no pip installs: the AI app launches it with plain python3.
#  stdout carries JSON-RPC ONLY - every diagnostic goes to stderr, because a
#  stray print() on stdout breaks the protocol and the client sees "server
#  disconnected" with no clue why.
# ──────────────────────────────────────────────────────────────────────────
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:17614/mcp"


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _payloads(raw, ctype):
    """Every JSON-RPC message in an HTTP body: bare JSON, or SSE `data:` lines."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if "text/event-stream" in (ctype or ""):
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        out.append(json.loads(chunk))
                    except ValueError:
                        pass
        return out
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [parsed] if isinstance(parsed, dict) else list(parsed)


def main():
    ap = argparse.ArgumentParser(description="stdio MCP <-> ZeroScript bridge /mcp")
    ap.add_argument("--url", default=os.environ.get("ZS_MCP_URL", DEFAULT_URL),
                    help=f"bridge MCP endpoint (default {DEFAULT_URL})")
    ap.add_argument("--token", default=os.environ.get("ZS_API_TOKEN", ""),
                    help="ZS_API_TOKEN, if the bridge was started with one")
    ap.add_argument("--timeout", type=float,
                    default=float(os.environ.get("ZS_MCP_TIMEOUT", "120") or "120"),
                    help="seconds to wait for a tool result (default 120)")
    args = ap.parse_args()

    session_id = None
    print(f"[mcp-stdio] forwarding stdio MCP -> {args.url}", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as e:
            _emit(_err(None, -32700, f"parse error: {e}"))
            continue

        req = urllib.request.Request(args.url, data=json.dumps(msg).encode("utf-8"),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        # Ask for JSON but accept SSE: an endpoint is allowed to answer either.
        req.add_header("Accept", "application/json, text/event-stream")
        if args.token:
            req.add_header("X-API-Key", args.token)
        if session_id:
            req.add_header("Mcp-Session-Id", session_id)

        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                status = resp.status
                if resp.headers.get("Mcp-Session-Id"):
                    session_id = resp.headers["Mcp-Session-Id"]
                raw = resp.read().decode("utf-8", "replace")
                ctype = resp.headers.get("Content-Type") or ""
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read().decode("utf-8", "replace")
            ctype = e.headers.get("Content-Type") or ""
        except Exception as e:  # connection refused, DNS, timeout - bridge down?
            _emit(_err(msg.get("id"), -32001,
                       f"bridge unreachable at {args.url}: {e}"))
            continue

        got = _payloads(raw, ctype)
        # The bridge's own error bodies (401 unauthorized, 404 route, ...) are
        # JSON but NOT JSON-RPC. Forwarding one as-is leaves the client with no
        # reply for its request id - it just hangs - so translate those into a
        # protocol error the client can actually show the user.
        # Strictly `jsonrpc`: the bridge's 401 body is {"ok": false, "error": ...},
        # which a looser "has an error key" test would happily mis-forward.
        rpc = [p for p in got if isinstance(p, dict) and p.get("jsonrpc") == "2.0"]
        if status >= 400 and not rpc:
            detail = raw.strip()[:300] or f"HTTP {status}"
            _emit(_err(msg.get("id"), -32001,
                       f"bridge answered HTTP {status} for {args.url}: {detail}"))
            continue

        # A notification gets an empty 202 body: nothing to send back upstream.
        for payload in (rpc or got):
            _emit(payload)


if __name__ == "__main__":
    main()
