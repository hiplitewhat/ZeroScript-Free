// SPDX-License-Identifier: GPL-3.0-or-later
// Regression tests for the bridge's MCP SERVER side (POST /mcp).
// Spins up a throwaway copy of bridge.py next to a fake stdio MCP server, then
// speaks raw JSON-RPC to it the way Replit Agent / Cursor / any MCP client does.
// Run: npm run test:mcp        (needs `pip install websockets`; override the
//      interpreter with ZS_TEST_PYTHON=/path/to/python if it is not python3)
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const REPO = path.join(__dirname, '..');
const TOKEN = 'mcp-test-token';

// A tool name with a DOT on purpose: LLM tool-schemas reject dots, so the
// bridge must advertise it sanitized and still accept both spellings.
const FAKE_SERVER = `#!/usr/bin/env python3
import json, sys
TOOLS = [{"name": "demo.echo",
          "description": "Echo a message back (test tool)",
          "inputSchema": {"type": "object",
                          "properties": {"message": {"type": "string"}},
                          "required": ["message"]}}]
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: msg = json.loads(line)
    except Exception: continue
    m, rid = msg.get("method"), msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0.0.1"}}})
    elif m == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif m == "tools/call":
        p = msg.get("params") or {}
        if p.get("name") in ("demo.echo", "demo_echo"):
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": "echo: " + str((p.get("arguments") or {}).get("message", ""))}]}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {"isError": True, "content": [
                {"type": "text", "text": "unknown tool"}]}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not found"}})
`;

function findPython() {
  const candidates = [process.env.ZS_TEST_PYTHON, 'python3', 'python'].filter(Boolean);
  for (const py of candidates) {
    try {
      execFileSync(py, ['-c', 'import websockets'], { stdio: 'ignore' });
      return py;
    } catch { /* try the next one */ }
  }
  return null;
}
const PYTHON = findPython();

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

let bridge = null, tmp = null, base = null;

test.before(async () => {
  if (!PYTHON) return; // the tests below are skipped
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'zs-mcp-test-'));
  for (const f of ['bridge.py', 'launch_studio_mcp.py']) {
    if (fs.existsSync(path.join(REPO, f))) fs.copyFileSync(path.join(REPO, f), path.join(tmp, f));
  }
  fs.writeFileSync(path.join(tmp, 'fake_mcp_server.py'), FAKE_SERVER);
  fs.writeFileSync(path.join(tmp, 'config.json'), JSON.stringify({
    mcpServers: { fake: { command: path.join(tmp, 'fake_mcp_server.py'), args: [] } },
  }, null, 2));

  const port = await freePort();
  const wsPort = await freePort(); // also unique, so the tests run next to a real bridge
  base = `http://127.0.0.1:${port}`;
  bridge = spawn(PYTHON, ['bridge.py'], {
    cwd: tmp,
    env: { ...process.env, ZS_API_HOST: '127.0.0.1', ZS_API_PORT: String(port),
      ZS_BRIDGE_PORT: String(wsPort), ZS_API_TOKEN: TOKEN },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  bridge.stdout.on('data', () => {}); // drain
  bridge.stderr.on('data', () => {});

  const deadline = Date.now() + 30_000;
  for (;;) {
    if (bridge.exitCode !== null) throw new Error(`bridge exited early (code ${bridge.exitCode})`);
    try {
      const r = await fetch(`${base}/health`);
      if (r.ok) break;
    } catch { /* not up yet */ }
    if (Date.now() > deadline) throw new Error('bridge did not come up within 30s');
    await new Promise((r) => setTimeout(r, 250));
  }
});

test.after(() => {
  if (bridge && bridge.exitCode === null) bridge.kill('SIGKILL');
  if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
});

const opts = (extra = {}) => ({ ...extra, headers: { 'X-API-Key': TOKEN, 'Content-Type': 'application/json', ...(extra.headers || {}) } });

async function rpc(body, extra = {}) {
  const r = await fetch(`${base}/mcp`, opts({ method: 'POST', body: JSON.stringify(body), ...extra }));
  const text = await r.text();
  return { status: r.status, headers: r.headers, text, json: (() => { try { return JSON.parse(text); } catch { return null; } })() };
}
const skip = PYTHON ? false : 'no python with websockets found (pip install websockets)';

test('initialize echoes the client protocol version and returns server info', { skip }, async () => {
  const { status, json, headers } = await rpc({ jsonrpc: '2.0', id: 1, method: 'initialize',
    params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'test', version: '1' } } });
  assert.equal(status, 200);
  assert.equal(json.result.protocolVersion, '2024-11-05', 'must echo a version it knows');
  assert.equal(json.result.serverInfo.name, 'zeroscript-bridge');
  assert.ok(json.result.capabilities.tools);
  assert.ok(headers.get('mcp-session-id'), 'session id header');
});

test('initialize negotiates the newest protocol when the client sends gibberish', { skip }, async () => {
  const { json } = await rpc({ jsonrpc: '2.0', id: 2, method: 'initialize',
    params: { protocolVersion: '1999-01-01' } });
  assert.equal(json.result.protocolVersion, '2025-06-18');
});

test('a notification-only POST answers 202 with an empty body', { skip }, async () => {
  const r = await fetch(`${base}/mcp`, opts({ method: 'POST',
    body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) }));
  assert.equal(r.status, 202);
  assert.equal(await r.text(), '');
});

// IMPORTANT: declared BEFORE the tools/list test on purpose. tools/list is what
// populates the advertised->real name map (and each list clears it), so this
// proves a client that calls straight after initialize - without listing - still
// reaches `demo.echo`. Regression: it used to answer "unknown tool 'demo_echo'".
test('tools/call works even when the client never called tools/list', { skip }, async () => {
  const deadline = Date.now() + 25_000;
  for (;;) {
    const { json } = await rpc({ jsonrpc: '2.0', id: 99, method: 'tools/call',
      params: { name: 'demo_echo', arguments: { message: 'no list first' } } });
    if (json.result && json.result.isError !== true) {
      assert.equal(json.result.content[0].text, 'echo: no list first');
      return;
    }
    if (Date.now() > deadline) assert.fail(`never resolved: ${JSON.stringify(json)}`);
    await new Promise((r) => setTimeout(r, 500)); // stdio child still handshaking
  }
});

test('tools/list advertises bridge tools with sanitized names and a schema', { skip }, async () => {
  const deadline = Date.now() + 25_000;
  let tool;
  for (;;) {
    const { json } = await rpc({ jsonrpc: '2.0', id: 3, method: 'tools/list' });
    tool = (json.result.tools || []).find((t) => t.name.startsWith('demo'));
    if (tool || Date.now() > deadline) break;
    await new Promise((r) => setTimeout(r, 500)); // stdio child still handshaking
  }
  assert.ok(tool, 'fake MCP server tool should surface');
  assert.equal(tool.name, 'demo_echo', 'dots must be sanitized for LLM schemas');
  assert.equal(tool.inputSchema.properties.message.type, 'string');
  assert.match(tool.description, /via ZeroScript bridge/, 'provenance is visible to the agent');
});

test('tools/call works with the sanitized name AND the original dotted name', { skip }, async () => {
  for (const name of ['demo_echo', 'demo.echo']) {
    const { status, json } = await rpc({ jsonrpc: '2.0', id: 4, method: 'tools/call',
      params: { name, arguments: { message: `hi ${name}` } } });
    assert.equal(status, 200);
    assert.equal(json.result.content[0].text, `echo: hi ${name}`);
    assert.notEqual(json.result.isError, true);
  }
});

test('a failed tool is a RESULT with isError, not a transport error', { skip }, async () => {
  const { status, json } = await rpc({ jsonrpc: '2.0', id: 5, method: 'tools/call',
    params: { name: 'demo.nope', arguments: {} } });
  assert.equal(status, 200, 'never a 500');
  assert.equal(json.error, undefined);
  assert.equal(json.result.isError, true);
  assert.match(json.result.content[0].text, /unknown tool|nope/);
});

test('unknown method -> -32601, missing tool name -> -32602', { skip }, async () => {
  const a = await rpc({ jsonrpc: '2.0', id: 6, method: 'totally/unknown' });
  assert.equal(a.json.error.code, -32601);
  const b = await rpc({ jsonrpc: '2.0', id: 7, method: 'tools/call', params: {} });
  assert.equal(b.json.error.code, -32602);
});

test('batched JSON-RPC returns an array in request order', { skip }, async () => {
  const { status, json } = await rpc([
    { jsonrpc: '2.0', id: 8, method: 'ping' },
    { jsonrpc: '2.0', id: 9, method: 'tools/list' },
  ]);
  assert.equal(status, 200);
  assert.ok(Array.isArray(json));
  assert.equal(json[0].id, 8);
  assert.equal(json[1].id, 9);
});

test('an SSE-only client gets exactly one message frame', { skip }, async () => {
  const r = await fetch(`${base}/mcp`, opts({ method: 'POST',
    headers: { Accept: 'text/event-stream' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 10, method: 'ping' }) }));
  assert.equal(r.status, 200);
  assert.match(r.headers.get('content-type') || '', /text\/event-stream/);
  const text = await r.text();
  assert.match(text, /^event: message\ndata: \{.*\}\n\n$/s);
  assert.equal(JSON.parse(text.split('data: ')[1]).id, 10);
});

// Most AI APPS (Claude Desktop, Cursor, Cline, Windsurf, Cherry Studio, LM
// Studio...) cannot speak HTTP MCP at all - they configure a COMMAND to launch
// and talk MCP over its stdin/stdout. mcp_stdio_bridge.py is that command, so
// this is the "my AI uses my MCP" path for them.
async function stdioShim(url, token = TOKEN) {
  const child = spawn(PYTHON, [path.join(REPO, 'mcp_stdio_bridge.py'), '--url', url, '--token', token],
    { stdio: ['pipe', 'pipe', 'pipe'] });
  child.stderr.on('data', () => {});
  let buf = '';
  const pending = [];
  child.stdout.on('data', (d) => {
    buf += d;
    for (let i; (i = buf.indexOf('\n')) >= 0;) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) pending.push(JSON.parse(line));
    }
  });
  const rpc = async (msg, id) => {
    child.stdin.write(`${JSON.stringify(msg)}\n`);
    const deadline = Date.now() + 25_000;
    for (;;) {
      const at = pending.findIndex((m) => m.id === id);
      if (at >= 0) return pending.splice(at, 1)[0];
      if (Date.now() > deadline) assert.fail(`no reply for id ${id} from the stdio shim`);
      await new Promise((r) => setTimeout(r, 100));
    }
  };
  return { child, rpc };
}

test('a command-launching AI app reaches the bridge through mcp_stdio_bridge.py', { skip }, async () => {
  const { child, rpc } = await stdioShim(`${base}/mcp`);
  try {
    const init = await rpc({ jsonrpc: '2.0', id: 1, method: 'initialize',
      params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'stdio-app', version: '1' } } }, 1);
    assert.equal(init.result.serverInfo.name, 'zeroscript-bridge');
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })}\n`);
    const list = await rpc({ jsonrpc: '2.0', id: 2, method: 'tools/list' }, 2);
    assert.ok(list.result.tools.some((t) => t.name === 'demo_echo'));
    const call = await rpc({ jsonrpc: '2.0', id: 3, method: 'tools/call',
      params: { name: 'demo_echo', arguments: { message: 'stdio app' } } }, 3);
    assert.equal(call.result.content[0].text, 'echo: stdio app');
  } finally { child.kill('SIGKILL'); }
});

test('the shim answers instead of hanging when the bridge is down or the token is wrong', { skip }, async () => {
  const deadPort = await freePort(); // never bound -> connection refused
  const dead = await stdioShim(`http://127.0.0.1:${deadPort}/mcp`);
  try {
    const r = await dead.rpc({ jsonrpc: '2.0', id: 1, method: 'tools/list' }, 1);
    assert.equal(r.error.code, -32001);
    assert.match(r.error.message, /unreachable/);
  } finally { dead.child.kill('SIGKILL'); }

  const bad = await stdioShim(`${base}/mcp`, 'wrong-token'); // bridge up, bad token: 401
  try {
    const r = await bad.rpc({ jsonrpc: '2.0', id: 7, method: 'tools/list' }, 7);
    assert.equal(r.id, 7, 'the client must get its id back, or it shows a spinner forever');
    assert.equal(r.error.code, -32001);
    assert.match(r.error.message, /401|unauthorized/i);
  } finally { bad.child.kill('SIGKILL'); }
});

test('GET /mcp is 405 (no server-initiated stream), DELETE ends the session', { skip }, async () => {
  const g = await fetch(`${base}/mcp`, { headers: { 'X-API-Key': TOKEN } });
  assert.equal(g.status, 405);
  assert.match(g.headers.get('allow') || '', /POST/);
  const d = await fetch(`${base}/mcp`, { method: 'DELETE', headers: { 'X-API-Key': TOKEN } });
  assert.equal(d.status, 204);
});

test('the endpoint is token-gated like the rest of the API', { skip }, async () => {
  const r = await fetch(`${base}/mcp`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 11, method: 'tools/list' }) });
  assert.equal(r.status, 401, 'a public tunnel URL must not be an open shell');
});
