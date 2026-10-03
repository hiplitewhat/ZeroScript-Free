# The Brain — how to use the bridge's HTTP API

The bridge does two jobs at once:

| | what it is | who talks to it |
|---|---|---|
| **WebSocket** `ws://127.0.0.1:17613` | the normal extension ↔ Roblox link (unchanged) | the ZeroScript extension |
| **HTTP API** `http://127.0.0.1:17614` | the "brain" — send a message, get a reply; tools included | curl, Tasker, scripts, other apps |

Two ways to think, picked automatically:

- **Keyless** (default): your **free web chat is the model**. The extension types your message into the logged-in DeepSeek/Gemini/… tab, runs the agent loop with tools, and returns the reply. No API key, no cost.
- **Headless** (`ZS_LLM_KEY` set): the bridge itself calls any OpenAI-compatible API. No browser needed.

Everything below was run against this repo's bridge; the commands are copy-pasteable.

---

## 1. Start the bridge

**Windows** — double-click `start.bat`. **macOS** — double-click `MacOS_Start.command`.

**Android (Termux)** — starts the relay *and* the bridge, holds a wake lock, installs `websockets` if missing:

```bash
sh Termux_Start.sh
```

You want to see this line at startup:

```
local HTTP API on http://127.0.0.1:17614  (GET /health /status /tools /v1/models - POST /call /chat /chat/reset /v1/chat/completions)
```

## 2. Check it's alive

```bash
curl http://127.0.0.1:17614/health     # {"ok": true, "version": ..., "uptime": ...}
curl http://127.0.0.1:17614/status     # servers, tool counts, and which brains are live
```

`/status` tells you which brain you have before you try to use it:

```json
"chat": { "llm": false, "extension": false, "extension_clients": 0 }
```

- `extension: true` → a ZeroScript tab is connected, keyless chat will work.
- `llm: true` → `ZS_LLM_KEY` is set, headless chat will work.
- both `false` → `/chat` will fail; fix that first (step 3 or 4).

## 3. Keyless: make the free web chat do the work

1. Keep your AI chat tab **open** (`chat.deepseek.com` etc.) with the ZeroScript extension loaded.
2. Ask:

```bash
curl -X POST http://127.0.0.1:17614/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"is the relay alive? ping the game if it is","session":"main"}'
```

```json
{"ok": true, "session": "main", "reply": "...", "tool_calls": [{"name": "synapse.ping", "ok": true}]}
```

What happens under the hood: the bridge relays the turn to the extension, the extension **brings the AI tab to the front by itself** (background tabs park the agent by design), types the message, runs the real agent loop with tools, and returns the last assistant reply. The tab jumping to the front is expected — a curl caller is never there to un-park it.

- One `/chat` at a time bridge-wide; a second one gets `another /chat request is already running`.
- `session` is just a label for the caller — the web conversation itself is the memory.
- Slow turn? Raise the cap: `export ZS_CHAT_TIMEOUT=600` (default 300 s).

## 4. Headless: no browser at all

```bash
export ZS_LLM_KEY=sk-...                    # or DEEPSEEK_API_KEY
export ZS_LLM_MODEL=deepseek-chat           # optional
export ZS_LLM_BASE=https://api.deepseek.com # optional; any OpenAI-compatible base
sh Termux_Start.sh                          # restart the bridge so it picks these up
```

Same `/chat` call, but now the bridge runs the agent loop itself. Memory is per `session` id, in RAM (30 messages, system prompt pinned):

```bash
curl -X POST http://127.0.0.1:17614/chat/reset -H 'Content-Type: application/json' -d '{"session":"main"}'
```

## 5. Plug it into any app (OpenAI-compatible `/v1`)

Anything with a "custom OpenAI" setting can use this — MoneyPrinterTurbo, a local script, an SDK:

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:17614/v1` (or `http://<phone-ip>:17614/v1`, or your tunnel URL) |
| API key | your `ZS_API_TOKEN` — any non-empty string when no token is set |
| Model | anything; `zeroscript` is listed by `GET /v1/models` |

```bash
curl -X POST http://127.0.0.1:17614/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"zeroscript","messages":[{"role":"user","content":"say hi"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:17614/v1", api_key="not-needed")
print(client.chat.completions.create(model="zeroscript",
      messages=[{"role": "user", "content": "say hi"}]).choices[0].message.content)
```

Notes: **streaming is supported** — `stream: true` returns SSE (`text/event-stream`): one `role` delta, one `content` delta holding the reply, the `finish` chunk, then `[DONE]`. The brain completes the whole turn before it has anything to say (a web-chat relay or a provider call), so nothing is dribbled token-by-token — clients that render incrementally simply paint the reply in one frame, and streaming-only apps (most mobile AI clients) accept it. A failed turn still answers as JSON with its HTTP status, the way OpenAI does.

**Stateless**, and the keyless relay adapts to what you send:

- **One message** (curl, a simple bot) → relayed verbatim into the web chat. Unchanged behaviour.
- **A system prompt and/or a transcript** (a real app) → the web chat has no idea those exist, so the bridge carries them inside a fenced wrapper that says: answer as this app's assistant, follow the client instructions, reply with only the text the app expects, do not call tools. Bounds: 6 000 chars of system prompt, 12 000 of history (keeping the newest turns), 12 000 of the newest message — oversized text keeps both ends with a `…[N chars dropped by the bridge]…` marker.

Caveat worth knowing: the web chat is still a ZeroScript session, so its own agent prompt sits above the wrapper. The wrapper asks the model to ignore that for the turn, and in practice it does — but a client whose protocol is extremely rigid may prefer the headless brain (`ZS_LLM_KEY`), where the client's own system prompt is used as-is. The keyed path replays your transcript and can run real MCP tools mid-request. `usage` is zeroed on purpose (no tokenizer here), but the object exists because some SDKs require it.

## 6. Use it from another device

**Same Wi-Fi / hotspot —** share the port and set a password:

```bash
export ZS_API_HOST=0.0.0.0
export ZS_API_TOKEN=choose-a-long-random-string
sh Termux_Start.sh
# bridge prints:  SHARED HTTP API on http://0.0.0.0:17614 ... token auth ON
curl -H "Authorization: Bearer $ZS_API_TOKEN" http://<device-ip>:17614/status
```

**Over the internet —** leave the API on loopback, tunnel it (still set the token):

```bash
pkg install cloudflared
cloudflared tunnel --url http://127.0.0.1:17614     # prints a fresh https://*.trycloudflare.com URL
```

Every new cloudflared run prints a **new** URL; the old one is dead.

Token is accepted as `Authorization: Bearer <token>`, `X-ZS-Token: <token>`, or `?token=<token>` (that last one so a plain browser tab can open `/status`). `/health` stays open. `/call` and `/chat` can run shell commands (`termux.exec`) and in-game code (`synapse.*`), which is why a **non-loopback bind without a token is refused** — the bridge logs the refusal and stays on 127.0.0.1. Trusted LAN only, no token: `ZS_API_OPEN=1`.

## 7. Make your AI use the bridge (MCP)

Everything above makes the bridge an **MCP client** — it spawns Roblox Studio's
server, Synapse, Termux and addons. The bridge is also an **MCP server** at
`POST /mcp`, so the AI **you** already use can drive those same tools. Give the
AI a token (step 6) and pick the shape it wants:

### a) AI that takes a URL (cloud agents, VS Code, LibreChat, Open WebUI, Replit…)

Three fields, wherever the app keeps MCP servers:

- **URL:** `http://127.0.0.1:17614/mcp` — or `https://<your-tunnel>.trycloudflare.com/mcp`
  for anything running off-device (a cloud agent cannot reach `127.0.0.1`; see
  step 6 for the tunnel).
- **Header:** `X-API-Key` = your `ZS_API_TOKEN` (Replit's *Integrations → MCP
  Servers → + Add MCP server* hides this under **Advanced settings**; Cursor and
  VS Code take `"headers"` in JSON).
- Then ask for a tool by name, e.g. *"use the ZeroScript MCP server to list the
  commands Roblox Studio has"*.

### b) AI that launches a COMMAND (Claude Desktop, Cursor, Cline, Windsurf, Cherry Studio, LM Studio, 5ire…)

Most AI **apps** cannot do HTTP MCP at all — they configure a command to launch
and speak MCP over its stdin/stdout. `mcp_stdio_bridge.py` **is** that command:
stdio on the AI's side, the bridge's `/mcp` on the other. Stdlib only, so plain
python3 runs it. Paste this into the app's MCP config file:

```json
{
  "mcpServers": {
    "zeroscript": {
      "command": "python3",
      "args": ["/home/user/ZeroScript-Free/mcp_stdio_bridge.py"],
      "env": { "ZS_API_TOKEN": "your-token" }
    }
  }
}
```

Use the real path to `mcp_stdio_bridge.py`; drop `env` when the bridge has no
token, and add `"--url", "https://<tunnel>.trycloudflare.com/mcp"` to `args`
for a bridge on another device. It reports bridge-down and bad-token as normal
JSON-RPC errors (`-32001`), so the app shows you *why* instead of spinning.

### c) Any client, by hand (official MCP SDK)

```bash
# official MCP SDK (python) — same URL/header
python - <<'PY'
import asyncio, httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
async def main():
    c = httpx2.AsyncClient(headers={"X-API-Key": "your-token"}, timeout=60)
    async with streamable_http_client("http://127.0.0.1:17614/mcp", http_client=c) as s:
        async with ClientSession(s[0], s[1]) as sess:
            print((await sess.initialize()).server_info)
            print([t.name for t in (await sess.list_tools()).tools])
asyncio.run(main())
PY
```

What the server exposes: `initialize`, `ping`, `tools/list`, `tools/call`,
`resources/list`, `prompts/list` (empty), and it accepts batched JSON-RPC.
Transport is MCP "streamable HTTP": `application/json` normally, a single SSE
`message` event when the client will only accept `text/event-stream`. The bridge
never sends server-initiated messages, so `GET /mcp` answers **405** (allowed by
the spec) and `DELETE /mcp` ends a session with 204.

Two details worth knowing:

- **Tool names are sanitized.** Our real names contain dots (`synapse.ping`,
  `termux.exec`), which breaks the `^[a-zA-Z0-9_-]{1,64}$` rule that LLM
  tool-schemas enforce, so `tools/list` advertises `synapse_ping` etc. and
  `tools/call` accepts **either** spelling. A failed tool returns a normal result
  with `isError: true` (not a JSON-RPC error) so the agent can read the reason.
- **Auth is the same token** as the rest of the API. That matters: `/mcp`
  reaches `termux.exec` and `synapse.*`, i.e. shell and in-game code. A public
  tunnel URL without a token is remote code execution for anyone who finds it —
  the bridge still refuses a non-loopback bind without `ZS_API_TOKEN`, but a
  tunnel reaches loopback, so **set the token**.

## 8. Let the bridge use a HOSTED MCP server (Replit's own)

Everything above is about the bridge *serving* MCP. It also *consumes* it — that
used to mean spawning a local child, but a hosted server like
**Replit's** (`https://mcp.replit.com/server/mcp`) speaks MCP over HTTP and
authenticates with OAuth. Add it by URL and its tools land in the same
aggregated catalogue as Roblox/Synapse/Termux, so the AI in the extension can
build, search, update and publish Replit apps.

1. Add the server — one command, no file editing (it merges into
   `config.json` and leaves your other servers alone):

   ```bash
   python bridge.py --add-server replit          # fills in Replit's URL for you
   ```

   …or edit `config.json` by hand (same result):

   ```json
   { "mcpServers": {
       "roblox": { "command": "launch_studio_mcp.py", "args": [] },
       "replit": { "url": "https://mcp.replit.com/server/mcp" } } }
   ```

   Undo with `python bridge.py --remove-server replit`.

2. Sign in once — this is a command, not part of startup, because it needs a
   browser and boot must never block on a human:

   ```bash
   python bridge.py --login replit
   ```

   It prints the Replit URL (and opens your browser), you approve the workspace,
   and the token is stored in `.mcp_tokens.json` (chmod 600, gitignored). On
   Termux the printed link is usually the easiest way to open it.
   `ZS_MCP_CALLBACK_PORT` (default 17615) picks the loopback port Replit
   redirects back to.

3. Check it: `python bridge.py --mcp-status` shows the token state, and
   `GET /status` lists the server with `"remote": true`. If the bridge was
   already running when you signed in, it notices within ~5s and connects on its
   own — no restart.

Replit tools you now have: `list_apps`, `search_apps`, `resolve_app_by_name`,
`create_app_from_prompt`, `ask_question`, `update_app_using_prompt`,
`publish_app`, `get_publish_status`.

Details worth knowing:

- **Any hosted MCP server works**, not just Replit. For a server that wants a
  static key instead of OAuth, put the header in config:
  `{"url": "https://mcp.example.com/mcp", "headers": {"X-API-Key": "..."}}`
  (a `headers` block implies header auth; add `"auth": "oauth"` to force OAuth).
- **Sign-in state is visible, never hidden.** A remote server with no valid
  token shows up as `needs_auth` in `/status` and the log names the exact
  command to fix it; the auto-restart loop deliberately leaves it alone instead
  of hammering a 401 every 5 seconds.
- **Access tokens refresh themselves** (`offline_access` → a refresh token). A
  call that lands on an expired token refreshes and retries once.
- **Treat `.mcp_tokens.json` like a password.** That token can create, update
  and publish apps on your Replit account. It is gitignored and chmod 600; do
  not paste it into a chat or a screenshot.
- **Replit is the client elsewhere.** Running your own bridge AS an MCP server
  (so Replit Agent consumes *your* tools) is section 7 — this section is the
  opposite direction, and they can both be on at once.

## 9. When it doesn't work

Run the checker first, in the **same shell** you started the bridge in (exports die with the session):

```bash
sh Termux_Doctor.sh [https://your-tunnel-url]
```

8 ordered stages — files current, processes, shell settings, local API, LAN sharing, tunnel, keyless brain (incl. duplicate/stale extension copies), then a live 40 s chat probe. Then match the error:

| Message you got | What it means / fix |
|---|---|
| `no keyless brain available: no browser extension with chat support is connected` | No AI tab, or an old extension build. Open `chat.deepseek.com` and reload the tab; or set `ZS_LLM_KEY`. |
| `the extension did not answer within Ns` | The tab was live but the turn didn't finish. Look at the AI tab — the extension raises it for you. Still working? raise `ZS_CHAT_TIMEOUT`. |
| `no AI chat tab is open - open chat.deepseek.com ...` | Extension is connected, but no provider tab matched the supported URLs. |
| `the AI tab is stuck in the background` | Chrome refused to raise it (common on Android behind Termux). Bring the browser to the front / split-screen, retry. |
| `this conversation has messages but no ZeroScript session` | You're in an old chat the agent never started. Open a **new** chat tab. |
| `the agent is busy in this tab - retry in a few seconds` | A panel session is running. Wait or stop it. |
| `another /chat request is already running` | Serialize your calls — one relay at a time. |
| `401 unauthorized` | `ZS_API_TOKEN` in your shell ≠ the bridge's. Restart the bridge from the same shell. |
| MCP client says "no tools" / 405 on connect | The client used the wrong URL — it needs `/mcp` on the end, and a POST-capable client (browsers can't). |
| `replit` shows `needs_auth` in `/status` | Nobody signed in, or the token was revoked: `python bridge.py --login replit`. |
| MCP client connects but every call fails | No MCP server connected (`0 tools available` in `/status`), or the tool name isn't one from `tools/list`. |
| `'messages' (a non-empty list) is required` | The app sent no messages (some clients probe with an empty body first — harmless). |
| Reply arrives all at once, not word-by-word | Expected: the brain finishes the turn before it can answer, so the SSE stream carries one content delta. |
| Model shows only `zeroscript` after Fetch | That is the only id advertised. Any model name is accepted and echoed back, so type the one the app wants. |
| `0 tools available` in `/status` | No MCP server connected (Studio closed / relay down). `/chat` still answers; tool calls will fail. |

## 10. Reference

**Env vars**

| Var | Default | Meaning |
|---|---|---|
| `ZS_API` | `1` | `0` disables the HTTP API entirely |
| `ZS_API_PORT` | `17614` | HTTP API port |
| `ZS_API_HOST` | loopback | Bind address; non-loopback requires a token |
| `ZS_API_TOKEN` | — | Bearer token for every route except `/health` |
| `ZS_API_OPEN` | — | `1` = allow an unauthenticated non-loopback bind |
| `ZS_LLM_KEY` / `DEEPSEEK_API_KEY` | — | Enables the headless brain |
| `ZS_LLM_BASE` / `ZS_LLM_MODEL` | `https://api.deepseek.com` / `deepseek-chat` | Headless provider |
| `ZS_LLM_TIMEOUT` / `ZS_LLM_MAX_ITERS` | `150` / `8` | Per-call timeout, tool-loop cap |
| `ZS_CHAT_TIMEOUT` | `300` | Keyless relay timeout (seconds) |
| `ZS_BRIDGE_PORT` | `17613` | WebSocket port (extension side) |

**Routes**

| Route | Purpose |
|---|---|
| `GET /health` | liveness (never needs a token) |
| `GET /status` | servers, tool count, which brains are live |
| `GET /tools` | aggregate catalogue of every connected MCP tool |
| `POST /call` | run one tool directly: `{"name":"termux.exec","arguments":{"command":"uname -a"}}` |
| `POST /chat` | one agent turn (keyless or headless) |
| `POST /chat/reset` | clear a headless session's memory |
| `POST /v1/chat/completions`, `GET /v1/models` | OpenAI-compatible surface |
| `POST /mcp` | **MCP server** (streamable HTTP) for any AI that takes a URL — Cursor, VS Code, cloud agents |
| `mcp_stdio_bridge.py` | stdio MCP server (`--url`, `--token`) for AI apps that launch a command — Claude Desktop, Cline, LM Studio |
| `python bridge.py --login <server>` | Sign in to a hosted MCP server (Replit) once; tokens live in `.mcp_tokens.json` |
| `python bridge.py --add-server <id> [url] [--header K=V]` | Add a hosted MCP server to `config.json` without editing it (`replit` fills in the URL) |
| `python bridge.py --remove-server <id>` | Remove a server from `config.json` |
| `python bridge.py --mcp-status` | Which remote MCP servers are signed in, and how long their tokens last |
| `GET /mcp` / `DELETE /mcp` | 405 (no server-initiated stream) / 204 (end session) |

**Where the code lives** — `bridge.py` (HTTP API, relay, agent loop, `/v1`), `zeroscript-extension/background.js` (capability handshake, picks + raises the AI tab), `zeroscript-extension/core/main.js` (`remoteChatTurn`, the real loop).
