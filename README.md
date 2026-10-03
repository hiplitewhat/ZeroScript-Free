# ZeroScript - Free AI Agent for Roblox (PC + Android)

![GitHub stars](https://img.shields.io/github/stars/sebattfg/ZeroScript-Free?style=social)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Android-lightgrey)
![License](https://img.shields.io/badge/license-GPL--3.0-blue)

**ZeroScript** is a free browser extension that turns AI chats (DeepSeek, ChatGPT, Gemini, Kimi, GLM, Qwen, Arena, Meta AI) into a Roblox AI agent.

Control Roblox from any AI chat — read/edit scripts, run Luau, generate assets, auto-farm games. Works on PC (Roblox Studio) AND Android (Delta executor + in-game Roblox). No API key, no terminal, no coding needed.

> 🌐 **Website: [zeroscript-five.vercel.app](https://zeroscript-five.vercel.app)**

Eight AI providers supported: **DeepSeek** (recommended), **ChatGPT**, **Gemini**, **Kimi**, **GLM**, **Qwen**, **Arena**, **Meta AI**.

> 💬 **Discord: [discord.gg/9aNyZsMWcb](https://discord.gg/9aNyZsMWcb)**

---

## ⚠️ ZeroScript is Free — Beware of Paid Copycats

ZeroScript is 100% free and open-source. No official paid version, no subscription, no sign-in. If you see a site using the ZeroScript name that asks for payment, it is **not** this project.

---

## How it works

```
AI chat (in your browser) -> ZeroScript Extension -> Bridge (your device) -> Roblox
```

**PC path:** Bridge -> Roblox Studio MCP server
**Android path:** Bridge -> HTTP relay -> Delta executor -> Roblox in-game

---

## Setup — PC (Roblox Studio)

> 📺 **[Setup tutorial on YouTube](https://youtu.be/kPKiZLZ9_Ps)**

### 1. Download & install extension

Download the latest zip from **Releases**, extract it.

- Go to `edge://extensions` or `chrome://extensions`
- Enable **Developer mode**
- Click **Load unpacked**
- Select the `zeroscript-extension` folder

### 2. Enable Studio MCP

- Open Roblox Studio, load a Place
- Click **Assistant AI** → **...** → **Manage MCP Servers**
- Click **Enable Studio as MCP Server**

### 3. Run the Bridge

- **Windows:** double-click `start.bat`
- **macOS:** double-click `MacOS_Start.command` (first time: System Settings → Privacy & Security → Open Anyway)

### 4. Start chatting

Go to https://chat.deepseek.com (recommended), https://chatgpt.com, or any supported AI chat. The ZeroScript bar appears above the input box. **Just type and send** — with the bridge connected, your first message on a fresh chat starts the agent automatically and goes straight to it (Automatic send, on by default; toggle it in the ZeroScript menu → Behaviour). Prefer the old way? Turn it off and click **▶ Start Roblox agent** first. **Pace auto-replies** (Behaviour, default on, 25s + jitter, with a 45–90s rest every 5th reply and a 15-minute cooldown after 24 in an hour) waits between injected tool turns so the same DeepSeek/Gemini/Kimi account is less likely to get suspended on long sessions. Behaviour also has a **Show live timers on message chips** toggle if the ticking seconds bother you.

On ChatGPT, use a normal text chat (not its built-in Agent mode). The ChatGPT adapter handles text and tool-command replies; image attachments for `screen_capture` are not supported yet, so vision-only tools are hidden there. Sign in to ChatGPT if the site prompts you.

---

## Setup — Android (Delta Executor + In-Game Roblox)

Control Roblox on your phone while playing ANY game. The AI can execute Lua in real-time, auto-farm, teleport, fire remotes, and bypass anti-cheat.

### 1. Install apps

- **Termux** (from F-Droid, NOT Play Store)
- **Delta executor** APK
- A Chromium browser with extension support (Edge Canary, Quetta, or Lemur)

### 2. One-time Termux setup

```bash
pkg update && pkg install -y python git
cd /sdcard/Download
git clone -b arena/019f97f5-potential-waddle https://github.com/hipliteidk-glitch/potential-waddle nnnn
cd nnnn/vendor/ZeroScript-Free
pip install websockets
```

### 3. Start the bridge + relay

Every time you want to use it, run the one-command launcher:

```bash
cd /sdcard/Download/<your ZeroScript folder>
sh Termux_Start.sh
```

That starts `synapse/relay.py` and `bridge.py` together (installs `websockets` if missing, holds the wake lock, Ctrl+C stops both). Manual equivalent, if you prefer:

```bash
python3 synapse/relay.py &
python3 bridge.py &
```

> On Android the bridge deliberately **skips** the `roblox` Studio server (Roblox Studio cannot run on Android). A one-time notice pointing at `synapse/relay.py` is expected — it is not an error.

### 4. Install the extension

Same as PC — load the `zeroscript-extension` folder unpacked in your browser.

### 5. In Delta executor, load the in-game runtime

While in a Roblox game, execute the contents of [`synapse/setup_delta.lua`](synapse/setup_delta.lua) in Delta (paste the whole file). It fetches the runtime from the relay trying executor-native HTTP first, with a timeout on every transport — the plain `game:HttpGet` one-liner hangs forever on some executor/game combos. Quick version, if you insist on one:

```lua
local u = "http://127.0.0.1:18081/synapse"
local f = (syn and syn.request) or http_request or request
local b = f and (function(r) return r and (r.Body or r.body) end)(f({Url = u, Method = "GET"}))
loadstring(b or game:HttpGet(u, true))()
```

Health check from Termux: `curl http://127.0.0.1:18081/health` — `executor_alive: true` means the game is connected.

The **Synapse and Termux MCP servers are registered automatically** the first time the bridge runs on Android (no panel step needed — Synapse appears with 18 in-game tools, Termux with shell/file tools, once `Termux_Start.sh` is up). Manual add only matters on PC: ZeroScript panel → **MCP servers → Add server** — Name: `Synapse`, Command: `synapse/mcp_synapse.py`. Full details: [synapse/README.md](synapse/README.md), [termux/README.md](termux/README.md).

### 6. Start chatting

Open DeepSeek (or any supported AI) in your browser. The extension connects automatically and **your first sent message starts the agent by itself** (Automatic send — default on; the ▶ Start button remains as a manual alternative). You can now send Luau commands from chat that execute in real-time in your game.

**What the AI can do on Android:**
- Fire RemoteEvents/RemoteFunctions to farm cash, XP, items
- Use VirtualInputManager for auto-clicking
- Teleport between areas
- Spoof UserId/Name to bypass blacklists
- Block Kick with metatable hooks
- Auto-equip tools and interact with game objects
- Read PlayerGui to track cash, level, inventory
- **Run shell commands and Python in Termux** (`termux.exec` — pip install, git, curl, am/termux-api…)
- **Read/write files on the device** (`termux.read_file` / `termux.write_file`)

---

## Local HTTP API + headless chat (optional)

Next to the WebSocket the extension uses, the bridge exposes a loopback HTTP JSON API on `http://127.0.0.1:17614` — only your own device can reach it. The extension flow is untouched; this is for curl, Tasker, scripts, bots. Change the port with `ZS_API_PORT`, disable with `ZS_API=0`.

```bash
curl http://127.0.0.1:17614/health          # liveness
curl http://127.0.0.1:17614/status          # servers, tool counts, studio_capable
curl http://127.0.0.1:17614/tools           # full aggregate tool catalogue
curl -X POST http://127.0.0.1:17614/call \
  -H 'Content-Type: application/json' \
  -d '{"name":"termux.exec","arguments":{"command":"uname -a"}}'
```

**Chat** — talk to the agent from anything that can curl: `/chat` takes a message, runs the agent loop with the same MCP tools the extension uses, and returns the final reply:

```bash
curl -X POST http://127.0.0.1:17614/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"is the relay alive? ping the game if it is","session":"main"}'
```

Two brains, picked automatically:

- **No API key (default, keyless): the browser extension does the thinking.** `/chat` is relayed to a connected ZeroScript extension, which drives the *free* web chat (DeepSeek, Gemini…) exactly as if you had typed the message — tools included. Keep the AI chat tab **open**; the extension brings it to the foreground **by itself** when a request arrives (background tabs pause the agent, so the tab pops to the front — that's expected). The web conversation is the memory; one request at a time.
- **With a key (fully headless, no browser needed):** `export ZS_LLM_KEY=sk-...` (or `DEEPSEEK_API_KEY`; optional `ZS_LLM_BASE` / `ZS_LLM_MODEL` — any OpenAI-compatible API, DeepSeek by default). Headless replies keep short-term memory per `session` id (in-memory only); `POST /chat/reset {"session":"main"}` clears it.

`GET /status` reports which brains are live under `chat.llm` / `chat.extension`. Tool endpoints (`/call`, `/tools`) always need neither.

> 📖 **Step-by-step guide: [BRAIN.md](BRAIN.md)** — start the bridge, run a keyless and a headless chat, point an OpenAI client at `/v1`, share the API over LAN/internet, and fix the common errors.

### OpenAI-compatible endpoint (`/v1`) — plug into MoneyPrinterTurbo & co

The bridge also speaks the standard OpenAI API, so any tool with a "custom OpenAI" setting can use the phone as its LLM — brain stays **keyless** (your logged-in DeepSeek tab) unless `ZS_LLM_KEY` is set (then fully headless):

| Setting | Value |
|---|---|
| Base URL | `http://<phone-ip>:17614/v1` (or `https://<your-tunnel>/v1`) |
| API key | your `ZS_API_TOKEN` (any string when no token is set) |
| Model | anything — echoed back; `zeroscript` is listed under `GET /v1/models` |

**The bridge is also an MCP server** (`POST /mcp`), so the AI you already use can drive those same Roblox/phone tools. Give it the URL `http://127.0.0.1:17614/mcp` (or your `cloudflared` URL off-device) plus `X-API-Key: <ZS_API_TOKEN>` — or, for AI apps that can only launch a command instead of calling a URL, point them at [`mcp_stdio_bridge.py`](mcp_stdio_bridge.py), which speaks stdio MCP and forwards to the bridge. Stdlib only; no installs. Tool names are sanitized for LLM schemas (`synapse.ping` → `synapse_ping`) and `tools/call` accepts either form. See [BRAIN.md §7](BRAIN.md).

Routes: `POST /v1/chat/completions` (also `/chat/completions`), `GET /v1/models`. Stateless from the bridge's side (the client owns history): a lone message is relayed to the web chat verbatim, while a client that sends a **system prompt or a transcript** gets both carried along inside a clearly-fenced wrapper — that is what lets an app whose protocol lives in its system prompt (PrivateAgent's `{"action":…}` schema) work through the free web chat at all. Carried text is bounded (6k system / 12k history / 12k newest message), keeping the newest turns. **Streaming works** (`stream:true` gets real SSE — the reply is sent as one content delta followed by the finish chunk and `[DONE]`, because the brain finishes the whole turn before it has anything to say; a failed turn answers as JSON with its HTTP status, as OpenAI does). The keyed path runs the same tool-calling agent loop as `/chat`, so clients can trigger real MCP tools. Tested with both brains incl. a live `termux.ping` round-trip through the function-calling shape, and with the real `openai` Python SDK in streaming and non-streaming mode.

### Share the API — let other people curl it

Something not working? Run the built-in checker **in the same shell you started the bridge in** — it pinpoints the broken stage (old files, bridge dead, token mismatch, sharing refused, dead tunnel URL, no extension tab, stale/zombie extension clients) and finishes with a **live end-to-end chat probe** that runs one tiny turn and prints the bridge's own diagnosis if it fails:

```bash
sh Termux_Doctor.sh [https://your-tunnel-url]
```

The API is loopback-only by default. Two safe ways to share it (`POST /call` and `/chat` can run shell commands and in-game code, so a token is enforced — a non-loopback bind without one is refused):

**Same network (LAN / hotspot / Wi-Fi):**

```bash
export ZS_API_HOST=0.0.0.0
export ZS_API_TOKEN=choose-a-long-random-string
sh Termux_Start.sh            # bridge logs "SHARED HTTP API ... token auth ON"
# your device IP is in:  ip addr   (or the bridge's own hint at startup)
curl -H "Authorization: Bearer $ZS_API_TOKEN" http://<device-ip>:17614/status
curl -X POST -H "Authorization: Bearer $ZS_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"synapse.ping"}' http://<device-ip>:17614/call
```

**Over the internet** (no `ZS_API_HOST` needed — the tunnel reaches the loopback API; set the token anyway):

```bash
export ZS_API_TOKEN=choose-a-long-random-string
pkg install cloudflared
cloudflared tunnel --url http://127.0.0.1:17614   # prints a public https://*.trycloudflare.com URL
curl -H "Authorization: Bearer $ZS_API_TOKEN" https://<your-tunnel>.trycloudflare.com/status
```

Every route except `/health` requires the token (`Authorization: Bearer …`, `X-ZS-Token: …`, or `?token=…` for a plain browser tab). Trusted-LAN-only override without a token: `ZS_API_OPEN=1` (the bridge warns loudly at startup).

---

## Panel status

| Dot | Meaning |
|-----|---------|
| Green | Bridge + Studio/Delta ready |
| Yellow | Bridge OK, Studio not usable yet |
| Grey | Bridge offline — restart it |

---

## Relay script (for Android setup)

The relay ships with the repo: [`synapse/relay.py`](synapse/relay.py). Run it on the Android device with `python3 synapse/relay.py` (default port `18081`, override with `SYNAPSE_RELAY_PORT`). For the full protocol, the in-game runtime, and troubleshooting see [`synapse/README.md`](synapse/README.md).

---

## Game script examples (Android)

**Anti-kick bypass:**
```lua
local mt=getrawmetatable(game:GetService("Players").LocalPlayer)
setreadonly(mt,false)
local old=mt.__namecall
mt.__namecall=newcclosure(function(self,...)
    if getnamecallmethod()=="Kick"then return end
    return old(self,...)
end)
```

**Fire all remotes (find working ones):**
```lua
local rs=game:GetService("ReplicatedStorage")
for _,v in ipairs(rs:GetDescendants())do
    if v:IsA("RemoteEvent")then
        pcall(function()v:FireServer()end)
    end
end
```

**Auto-click with VirtualInputManager:**
```lua
local vim=game:GetService("VirtualInputManager")
vim:SendMouseButtonEvent(x,y,0,true,game,1) -- press
vim:SendMouseButtonEvent(x,y,0,false,game,1) -- release
```

---

## What the AI can do (PC)

- Read and edit scripts
- Run Luau code directly in Studio
- Inspect the game tree and instances
- Generate meshes, materials, and models
- Browse and insert from the Creator Store
- Control play-testing
- Persistent project memory saved inside your place

---

## Requirements

- **PC:** Windows or macOS, Roblox Studio, Edge or Chrome, Python 3.9+
- **Android:** Termux (F-Droid), Delta executor, Chromium browser with extension support, Python 3.9+

---

## Support

ZeroScript is free. If it saves you time: [Ko-fi](https://ko-fi.com/sebattfg)

---

Credit: MCP server connection idea from [javnpa](https://github.com/javnpa). macOS/Linux support from [archivealf](https://github.com/archivealf).
# Trigger re-run
# Trigger fresh run
