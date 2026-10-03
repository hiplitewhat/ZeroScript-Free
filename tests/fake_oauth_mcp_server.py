#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in for Replit's MCP server, used by tests/mcp-remote.test.cjs.

It copies the SHAPE of https://mcp.replit.com/server/mcp (verified live
2026-10-03):
  * protected-resource metadata at the PATH-SCOPED well-known URL
    (/.well-known/oauth-protected-resource/server/mcp) - Replit answers that
    one, and the origin-root one 404s, so the bridge's fallback order is
    exercised for real
  * authorization_servers pointing at an issuer whose metadata is at
    /<issuer-path>/.well-known/oauth-authorization-server
  * dynamic client registration, S256-only PKCE, refresh tokens
  * a token endpoint that rejects a WRONG code_verifier (so a broken PKCE
    implementation fails the test instead of quietly passing)
  * an MCP endpoint that 401s without a bearer token

Usage:  fake_oauth_mcp_server.py <port> <stats_file> [access_token_ttl]
"""
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
STATS = sys.argv[2]
TTL = float(sys.argv[3]) if len(sys.argv) > 3 else 3600
BASE = f"http://127.0.0.1:{PORT}"

stats = {"registrations": 0, "authorize": 0, "token_grants": 0, "refreshes": 0,
         "unauthorized": 0, "resources_seen": [], "pkce_ok": 0, "pkce_bad": 0,
         "tool_calls": []}
codes = {}          # code -> {"challenge":..., "redirect_uri":..., "resource":...}
tokens = {}         # access_token -> {"refresh":..., "expires_at":...}
refresh_tokens = {}
lock = threading.Lock()

TOOLS = [
    {"name": "list_apps", "description": "List apps you can edit (Replit stand-in)",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "number"}}}},
    {"name": "create_app_from_prompt", "description": "Start a new app (Replit stand-in)",
     "inputSchema": {"type": "object",
                     "properties": {"appDescription": {"type": "string"},
                                    "app_stack": {"type": "string"}},
                     "required": ["appDescription", "app_stack"]}},
    {"name": "publish_app", "description": "Publish an app (Replit stand-in)",
     "inputSchema": {"type": "object", "properties": {"replId": {"type": "string"}},
                     "required": ["replId"]}},
]


def flush_stats():
    with open(STATS, "w") as f:
        json.dump(stats, f, indent=2)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, code=200, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode("utf-8", "replace") if n else ""

    # ── discovery ─────────────────────────────────────────────────────────
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/.well-known/oauth-protected-resource/server/mcp":
            return self._json({
                "resource": f"{BASE}/server/mcp",
                "authorization_servers": [f"{BASE}/oidc"],
                "scopes_supported": ["apps:read", "apps:write", "offline_access"],
                "bearer_methods_supported": ["header"],
                "resource_name": "Fake Replit MCP Server"})
        if path == "/.well-known/oauth-protected-resource":
            return self._json({"error": "not found"}, 404)   # like the real root one
        if path == "/oidc/.well-known/oauth-authorization-server":
            return self._json({
                "issuer": f"{BASE}/oidc",
                "authorization_endpoint": f"{BASE}/oidc/auth",
                "token_endpoint": f"{BASE}/oidc/token",
                "registration_endpoint": f"{BASE}/oidc/reg",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": ["apps:read", "apps:write", "offline_access"]})
        if path == "/stats":
            return self._json(stats)
        return self._json({"error": "not found"}, 404)

    # ── dynamic client registration ───────────────────────────────────────
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/oidc/reg":
            body = json.loads(self._read() or "{}")
            with lock:
                stats["registrations"] += 1
                flush_stats()
            if not body.get("redirect_uris"):
                return self._json({"error": "invalid_client_metadata"}, 400)
            return self._json({"client_id": f"client-{stats['registrations']}",
                               "redirect_uris": body["redirect_uris"],
                               "token_endpoint_auth_method": "none"}, 201)
        if path == "/oidc/token":
            return self._token()
        if path == "/server/mcp":
            return self._mcp()
        return self._json({"error": "not found"}, 404)

    # ── authorization endpoint: auto-approves like a signed-in user ───────
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _token(self):
        form = urllib.parse.parse_qs(self._read())
        got = {k: v[0] for k, v in form.items()}
        with lock:
            stats["resources_seen"].append(got.get("resource"))
        if got.get("grant_type") == "refresh_token":
            info = refresh_tokens.get(got.get("refresh_token"))
            if not info:
                return self._json({"error": "invalid_grant"}, 400)
            with lock:
                stats["refreshes"] += 1
                flush_stats()
            return self._json(self._mint(info["resource"]))
        if got.get("grant_type") != "authorization_code":
            return self._json({"error": "unsupported_grant_type"}, 400)
        info = codes.pop(got.get("code") or "", None)
        if not info:
            return self._json({"error": "invalid_grant"}, 400)
        import hashlib, base64
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256((got.get("code_verifier") or "").encode()).digest()
        ).decode().rstrip("=")
        with lock:
            if challenge != info["challenge"]:
                stats["pkce_bad"] += 1
                flush_stats()
                return self._json({"error": "invalid_grant",
                                   "error_description": "PKCE verification failed"}, 400)
            stats["pkce_ok"] += 1
            stats["token_grants"] += 1
            flush_stats()
        return self._json(self._mint(got.get("resource") or info.get("resource")))

    def _mint(self, resource):
        access = f"at-{int(time.time()*1000)}-{len(tokens)}"
        refresh = f"rt-{int(time.time()*1000)}-{len(tokens)}"
        tokens[access] = {"expires_at": time.time() + TTL, "resource": resource}
        refresh_tokens[refresh] = {"resource": resource}
        return {"access_token": access, "refresh_token": refresh,
                "token_type": "Bearer", "expires_in": int(TTL),
                "scope": "apps:read apps:write offline_access"}

    # ── the MCP endpoint itself ───────────────────────────────────────────
    def _mcp(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            with lock:
                stats["unauthorized"] += 1
                flush_stats()
            return self._json({"error": "unauthorized"}, 401, extra={
                "WWW-Authenticate":
                    f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/server/mcp"'})
        tok = tokens.get(auth[7:])
        if not tok or tok["expires_at"] < time.time():
            with lock:
                stats["unauthorized"] += 1
                flush_stats()
            return self._json({"error": "invalid_token"}, 401)
        msg = json.loads(self._read() or "{}")
        rid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            return self._json({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-replit", "version": "1.0.0"}}},
                extra={"Mcp-Session-Id": "sess-1"})
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "tools/list":
            return self._json({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        if method == "tools/call":
            p = msg.get("params") or {}
            with lock:
                stats["tool_calls"].append(p.get("name"))
                flush_stats()
            return self._json({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": f"replit-stand-in ran {p.get('name')} "
                                         f"with {json.dumps(p.get('arguments') or {})}"}]}})
        if rid is not None:
            return self._json({"jsonrpc": "2.0", "id": rid,
                               "error": {"code": -32601, "message": "not found"}})
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()


class A(H):
    """The /oidc/auth endpoint lives on the same server: 302 back to the client."""

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/oidc/auth":
            q = {k: v[0] for k, v in
                 urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
            with lock:
                stats["authorize"] += 1
                stats["resources_seen"].append(q.get("resource"))
                flush_stats()
            code = f"code-{int(time.time()*1000)}"
            codes[code] = {"challenge": q.get("code_challenge"),
                           "redirect_uri": q.get("redirect_uri"),
                           "resource": q.get("resource")}
            target = q.get("redirect_uri", "") + "?" + urllib.parse.urlencode(
                {"code": code, "state": q.get("state", "")})
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_GET()


if __name__ == "__main__":
    flush_stats()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), A)
    srv.daemon_threads = True
    print(f"fake replit mcp on {BASE}", flush=True)
    srv.serve_forever()
