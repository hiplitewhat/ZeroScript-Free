// SPDX-License-Identifier: GPL-3.0-or-later
// Remote MCP servers ({"url": ...}) end to end: OAuth discovery, dynamic client
// registration, PKCE sign-in over a loopback callback, token storage, refresh,
// and the bridge picking up a sign-in that happened in another terminal.
// The mock copies the shape of https://mcp.replit.com/server/mcp.
// Run: npm run test:mcp:remote      (needs `pip install websockets`)
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const REPO = path.join(__dirname, '..');
const TOKEN_TTL = 2; // seconds - short on purpose, so refresh is exercised

function findPython() {
  for (const py of [process.env.ZS_TEST_PYTHON, 'python3', 'python'].filter(Boolean)) {
    try {
      execFileSync(py, ['-c', 'import websockets'], { stdio: 'ignore' });
      return py;
    } catch { /* next */ }
  }
  return null;
}
const PYTHON = findPython();
const skip = PYTHON ? false : 'no python with websockets found (pip install websockets)';

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.on('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let tmp, mock, mockPort, statsFile, bridge, bridgePort, callbackPort;
const callbacks = [];

async function startBridge() {
  bridgePort = await freePort();
  const child = spawn(PYTHON, ['bridge.py'], {
    cwd: tmp,
    env: {
      ...process.env, ZS_API_HOST: '127.0.0.1', ZS_API_PORT: String(bridgePort),
      ZS_BRIDGE_PORT: String(await freePort()), ZS_API_TOKEN: 'secret',
      ZS_MCP_CALLBACK_PORT: String(callbackPort), ZS_MCP_NO_BROWSER: '1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '';
  child.stdout.on('data', (d) => { log += d; });
  child.stderr.on('data', (d) => { log += d; });
  child.getLog = () => log;
  const deadline = Date.now() + 30_000;
  for (;;) {
    if (child.exitCode !== null) throw new Error(`bridge exited (${child.exitCode}): ${log}`);
    try {
      if ((await fetch(`http://127.0.0.1:${bridgePort}/health`)).ok) break;
    } catch { /* not up yet */ }
    if (Date.now() > deadline) throw new Error(`bridge never came up: ${log}`);
    await sleep(250);
  }
  return child;
}

const status = async () => (await fetch(`http://127.0.0.1:${bridgePort}/status`,
  { headers: { Authorization: 'Bearer secret' } })).json();
const stats = () => JSON.parse(fs.readFileSync(statsFile, 'utf8'));
async function rpc(body) {
  const r = await fetch(`http://127.0.0.1:${bridgePort}/mcp`, {
    method: 'POST',
    headers: { 'X-API-Key': 'secret', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}
async function tools() {
  return (await rpc({ jsonrpc: '2.0', id: 1, method: 'tools/list' })).result.tools || [];
}

test.before(async () => {
  if (!PYTHON) return;
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'zs-remote-mcp-'));
  for (const f of ['bridge.py', 'launch_studio_mcp.py']) {
    if (fs.existsSync(path.join(REPO, f))) fs.copyFileSync(path.join(REPO, f), path.join(tmp, f));
  }
  statsFile = path.join(tmp, 'oauth_stats.json');
  mockPort = await freePort();
  callbackPort = await freePort();
  mock = spawn(PYTHON, [path.join(REPO, 'tests', 'fake_oauth_mcp_server.py'),
    String(mockPort), statsFile, String(TOKEN_TTL)], { stdio: ['ignore', 'pipe', 'pipe'] });
  mock.stdout.on('data', () => {});
  mock.stderr.on('data', (d) => process.stderr.write(d));
  const deadline = Date.now() + 20_000;
  while (!fs.existsSync(statsFile)) {
    if (Date.now() > deadline) throw new Error('mock OAuth MCP server never started');
    await sleep(150);
  }
  fs.writeFileSync(path.join(tmp, 'config.json'), JSON.stringify({
    mcpServers: {
      replit: { url: `http://127.0.0.1:${mockPort}/server/mcp` },
    },
  }, null, 2));
});

test.after(() => {
  for (const c of [...callbacks, bridge, mock]) {
    if (c && c.exitCode === null) c.kill('SIGKILL');
  }
  if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
});

test('without a sign-in the server is reported as needing auth, not retried forever', { skip }, async () => {
  bridge = await startBridge();
  callbacks.push(bridge);
  // The bridge never blocks boot on a remote server, so the first connect
  // attempt happens in the background - poll for its verdict.
  const deadline = Date.now() + 25_000;
  let row;
  for (;;) {
    row = (await status()).servers.find((s) => s.id === 'replit');
    if (row && row.needs_auth) break;
    if (Date.now() > deadline) break;
    await sleep(300);
  }
  assert.ok(row, 'the remote server must appear in /status');
  assert.equal(row.remote, true);
  assert.equal(row.needs_auth, true);
  assert.equal(row.alive, false);
  assert.match(bridge.getLog(), /--login replit/, 'the log must name the fix');
  assert.deepEqual(await tools(), [], 'no remote tools before signing in');
});

// Stand in for the human: run the login, read the URL it prints, then "browse"
// to it so the fake AS redirects back to the bridge's loopback callback.
async function loginWithFakeBrowser() {
  const child = spawn(PYTHON, ['bridge.py', '--login', 'replit'], {
    cwd: tmp,
    env: { ...process.env, ZS_MCP_CALLBACK_PORT: String(callbackPort), ZS_MCP_NO_BROWSER: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let out = '';
  const authUrl = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`--login printed no URL:\n${out}`)), 40_000);
    const onData = (d) => {
      out += d;
      const m = out.match(/https?:\/\/\S*\/oidc\/auth\?\S+/);
      if (m) {
        clearTimeout(timer);
        resolve(m[0].replace(/\u001b\[[0-9;]*m/g, ''));
      }
    };
    child.stdout.on('data', onData);
    child.stderr.on('data', onData);
    child.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`--login exited early (${code}):\n${out}`));
    });
  });
  const page = await fetch(authUrl);              // the "browser"
  assert.match(await page.text(), /Signed in/);
  const code = await new Promise((resolve) => child.on('exit', resolve));
  return { code, out, authUrl };
}

test('--login runs discovery + DCR + PKCE and stores a token', { skip }, async () => {
  const { code, out, authUrl } = await loginWithFakeBrowser();
  assert.equal(code, 0, `login must exit cleanly:\n${out}`);
  assert.match(out, /signed in to replit/i);
  assert.match(authUrl, /code_challenge_method=S256/, 'PKCE must be in the URL');
  assert.match(authUrl, /resource=http/, 'RFC 8707 resource parameter must be in the URL');
  const s = stats();
  assert.equal(s.registrations, 1, 'dynamic client registration must have happened');
  assert.equal(s.authorize, 1);
  assert.equal(s.token_grants, 1);
  assert.equal(s.pkce_ok, 1, 'the code_verifier must match the S256 challenge');
  assert.equal(s.pkce_bad, 0);
  assert.ok(s.resources_seen.includes(`http://127.0.0.1:${mockPort}/server/mcp`),
    'RFC 8707 resource parameter must be sent');

  const store = JSON.parse(fs.readFileSync(path.join(tmp, '.mcp_tokens.json'), 'utf8'));
  assert.ok(store.replit.access_token);
  assert.ok(store.replit.refresh_token, 'offline_access -> a refresh token');
  assert.equal(fs.statSync(path.join(tmp, '.mcp_tokens.json')).mode & 0o777, 0o600,
    'tokens are credentials: 0600');
});

test('the running bridge notices the sign-in and exposes the Replit tools', { skip }, async () => {
  const deadline = Date.now() + 30_000;
  let list = [];
  while (Date.now() < deadline) {
    list = await tools();
    if (list.length) break;
    await sleep(500); // server_watch polls every 5s
  }
  const names = list.map((t) => t.name);
  assert.ok(names.includes('list_apps'), `expected Replit's tools, got ${names}`);
  assert.ok(names.includes('create_app_from_prompt'));
  const row = (await status()).servers.find((s) => s.id === 'replit');
  assert.equal(row.needs_auth, false);
  assert.equal(row.alive, true);
});

test('calling a remote tool goes through the OAuth bearer token', { skip }, async () => {
  const r = await rpc({ jsonrpc: '2.0', id: 2, method: 'tools/call',
    params: { name: 'list_apps', arguments: { limit: 5 } } });
  assert.match(r.result.content[0].text, /replit-stand-in ran list_apps/);
  assert.match(r.result.content[0].text, /"limit": ?5/);
  assert.deepEqual(stats().tool_calls, ['list_apps']);
});

test('an expired access token refreshes itself instead of failing the call', { skip }, async () => {
  await sleep((TOKEN_TTL + 1) * 1000);
  const before = stats().refreshes;
  const r = await rpc({ jsonrpc: '2.0', id: 3, method: 'tools/call',
    params: { name: 'publish_app', arguments: { replId: 'abc' } } });
  assert.match(r.result.content[0].text, /publish_app/);
  assert.ok(stats().refreshes > before, 'a refresh_token grant must have been used');
  assert.equal((await status()).servers.find((s) => s.id === 'replit').needs_auth, false);
});

test('servers can be added and removed from the CLI - no file editing', { skip }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'zs-add-server-'));
  try {
    fs.copyFileSync(path.join(REPO, 'bridge.py'), path.join(dir, 'bridge.py'));
    fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify({
      mcpServers: { roblox: { command: 'launch_studio_mcp.py', args: [] } },
    }));
    // `replit` alone uses the documented URL - the phone-friendly shortcut.
    const out = execFileSync(PYTHON, ['bridge.py', '--add-server', 'replit'], {
      cwd: dir, encoding: 'utf8', timeout: 30_000 });
    assert.match(out, /added 'replit'/);
    assert.match(out, /--login replit/, 'it must say what to do next');
    let cfg = JSON.parse(fs.readFileSync(path.join(dir, 'config.json'), 'utf8'));
    assert.equal(cfg.mcpServers.replit.url, 'https://mcp.replit.com/server/mcp');
    assert.deepEqual(cfg.mcpServers.roblox, { command: 'launch_studio_mcp.py', args: [] },
      'the existing server must survive untouched');

    execFileSync(PYTHON, ['bridge.py', '--add-server', 'zapier',
      'https://mcp.zapier.com/api/v1/connect', '--header', 'X-API-Key=abc'],
    { cwd: dir, encoding: 'utf8', timeout: 30_000 });
    cfg = JSON.parse(fs.readFileSync(path.join(dir, 'config.json'), 'utf8'));
    assert.deepEqual(cfg.mcpServers.zapier, {
      url: 'https://mcp.zapier.com/api/v1/connect', headers: { 'X-API-Key': 'abc' } });

    const status = execFileSync(PYTHON, ['bridge.py', '--mcp-status'], {
      cwd: dir, encoding: 'utf8', timeout: 30_000 });
    assert.match(status, /replit.*not signed in/s);
    assert.match(status, /zapier.*header auth/s, 'a header server is not "not signed in"');

    execFileSync(PYTHON, ['bridge.py', '--remove-server', 'zapier'], {
      cwd: dir, encoding: 'utf8', timeout: 30_000 });
    cfg = JSON.parse(fs.readFileSync(path.join(dir, 'config.json'), 'utf8'));
    assert.deepEqual(Object.keys(cfg.mcpServers), ['roblox', 'replit']);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('a remote server that stays 401 is explained, never looped', { skip }, async () => {
  // Break the token and let the bridge notice on its next call: it must come
  // back as a clear "sign in" error rather than a hang or a restart storm.
  const store = JSON.parse(fs.readFileSync(path.join(tmp, '.mcp_tokens.json'), 'utf8'));
  store.replit.access_token = 'expired';
  store.replit.refresh_token = 'bogus';
  fs.writeFileSync(path.join(tmp, '.mcp_tokens.json'), JSON.stringify(store));
  const r = await rpc({ jsonrpc: '2.0', id: 4, method: 'tools/call',
    params: { name: 'list_apps', arguments: {} } });
  assert.equal(r.result.isError, true);
  assert.match(r.result.content[0].text, /sign-in|401|unauthorized/i);
});
