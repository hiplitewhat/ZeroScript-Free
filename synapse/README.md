# Synapse

**Synapse** is the in-game half of the ZeroScript stack. Where ZeroScript drives
the *editor* (Roblox Studio), Synapse drives the *live game*: it runs inside any
Luau executor (Delta and friends) on PC or Android, talks to a local HTTP relay,
and executes commands sent by the AI agent — raw Luau plus a set of high-level,
game-aware operations.

```
AI chat → ZeroScript extension → ZeroScript bridge → Synapse MCP (stdio)
                                                          │
                                                          ▼
                                                    Synapse relay (HTTP)
                                                          │
                                                          ▼
                                             synapse.lua (in-game executor)
```

## Files

| File | What it is |
|------|-----------|
| `relay.py` | Local HTTP relay. Queues commands, serves the executor, collects results. |
| `synapse.lua` | The in-game runtime. Long-polls the relay, executes Luau and semantic commands, reports back. |
| `mcp_synapse.py` | Stdio MCP server. Registers Synapse as an addon MCP server under ZeroScript's local bridge. |
| `controller.py` | Small REPL for driving the executor directly from a terminal. |
| `setup_delta.lua` | One-line bootstrap snippet for Delta/any executor. |
| `screenshot.py` | Local desktop capture used by `synapse.screenshot` and `!ss`. |

## 1. Start the relay

```bash
cd ZeroScript-Free/synapse
python3 relay.py
```

Default port `18081`. Override with `SYNAPSE_RELAY_PORT=18082 python3 relay.py`
(both the relay and the MCP server read the same env var).

## 2. Boot the executor in-game (Android / Delta)

In Delta, inside any Roblox game, execute the contents of
[`setup_delta.lua`](setup_delta.lua) (paste the whole file). It downloads
`synapse.lua` from the relay walking every HTTP transport with a per-attempt
timeout — important because the plain one-liner
`loadstring(game:HttpGet(u.."/synapse"))()` **hangs forever** on executors
whose `game:HttpGet` is broken in live games (seen live on Delta). If you're
on a healthy executor, that one-liner still works.

Keep the game running. The executor polls every 250–500ms.

Health check:

```
curl http://127.0.0.1:18081/health
```

`executor_alive: true` means Synapse is connected.

## 3. Wire it into ZeroScript

> **On Android/Termux nothing here is needed** — the bridge registers the
> Synapse server (and the Termux shell server, see `termux/README.md`)
> automatically on first run (once each; `<id>_auto_added` markers in
> `config.json` record it, so removing one later is respected).
> The steps below are for PC.

In the ZeroScript panel on the AI chat site:

- **MCP servers → Add server**
- Name: `Synapse`
- Command: `synapse/mcp_synapse.py`

The ZeroScript bridge runs addon servers from its own directory (`HERE`), so
a relative `synapse/mcp_synapse.py` resolves against the repo root and is
launched with the same Python interpreter the bridge uses. That works on
Windows and macOS without `python3` being on PATH. The bridge restarts, the
Synapse tools appear alongside the Roblox Studio commands, and the AI can
call:

- `synapse.ping`
- `synapse.diag`
- `synapse.self_test`
- `synapse.screenshot`
- `synapse.execute`
- `synapse.player`
- `synapse.game_tree`
- `synapse.find`
- `synapse.fire_remote`
- `synapse.invoke_remote`
- `synapse.teleport`
- `synapse.auto_click`
- `synapse.anti_kick`
- `synapse.spoof`
- `synapse.env_get` / `env_set` / `env_list`
- `synapse.reset`

## Command protocol

The relay speaks a tiny JSON protocol over HTTP. Every executor job has an id,
so concurrent commands never cross wires.

**Queue + wait (used by the MCP server):**

```
POST /execute
{"cmd":"return game.PlaceId","raw":true,"timeout":30}
→ {"ok":true,"out":"1234567890","err":""}
```

**Queue raw code (curl/controller):**

```
POST /cmd    body: return game.PlaceId
→ {"ok":true,"id":"S-..."}
```

**Executor long-poll:**

```
GET /cmd
→ {"id":"S-...","cmd":"return game.PlaceId"}
```

**Executor result:**

```
POST /result
{"id":"S-...","ok":true,"out":"1234567890","err":""}
```

**Read the last result (non-blocking):**

```
GET /result
```

## Semantic commands

A raw Luau string is executed with `loadstring`. A JSON object with `"op"` is
routed to a built-in operation:

```json
{"op":"player"}
{"op":"tree","max":300}
{"op":"find","name":"Farm","class":"RemoteEvent"}
{"op":"fire","name":"ReplicatedStorage/FarmEvent","args":[1,2]}
{"op":"teleport","x":0,"y":50.5,"z":-20}
{"op":"click","x":400,"y":700,"count":10,"delay":0.05}
{"op":"anti_kick"}
{"op":"spoof","user_id":123456,"name":"Synapse"}
{"op":"env_set","key":"stage","value":"farm_2"}
```

### Why `[synapse] loaded` but the relay still times out

The executor **does not** use `HttpService:GetAsync` in its loop — that API is
server-only inside Roblox Studio and fails silently on a live game client. The
runtime walks transport in this order:

1. `syn.request` (executor-native, bypasses HTTP restrictions)
2. `http_request` / `request` (executor-native)
3. `game:HttpGet` / `game:HttpPost` (the same transport the setup script uses)
4. `HttpService:GetAsync` / `PostAsync` (Studio server contexts only)

On load it prints the chosen transport and runs a one-shot `GET /health` so a
blocked relay is visible in the Developer Console instead of a silent no-op.

**Quick single-line test** (run in your injector, inside the game):

```lua
print(game:HttpGet("http://127.0.0.1:18081/health"))
print(_G.syn and _G.syn.request != nil)  -- is a native request API available?
```

If the first prints `{"ok":true,...}` the relay is reachable. If the second is
`true`, Synapse should already be using the better transport. If the first
errors and the second is `false`, the dev console will show `[synapse] poll
error` lines telling you exactly which request failed.

## Persistence

`_G.SYNAPSE.Env` lives for the whole executor session. Set keys with
`env_set`, read them with `env_get` / `env_list`, clear everything with
`reset`. Raw Luau can use `SYNAPSE.Env` directly, so long-running agent plans
can keep state across commands.

## Screenshot capture (desktop + Android)

`synapse.screenshot` (MCP) and `!ss` (controller) capture the screen of the
machine running the relay — the Delta window, the terminal, the browser —
and hand the PNG to the AI as an attached image. It runs on the Python side
(no executor needed).

The ZeroScript panel also exposes a **Synapse tile** in its "more" menu: a
Capture screen button that fires `synapse.screenshot` through the bridge and
shows the image both in the tile and in the left-hand capture popup. It's
enabled whenever the Synapse MCP server is live.

### Desktop (PC)

Uses `mss` (or Pillow fallback) to capture the desktop:

```bash
pip install mss            # fast, cross-platform, recommended
# or
pip install pillow
```

### Android (Termux + Delta)

Android has no desktop capture. Synapse drives the free
[ScreenshotTile](https://github.com/cvzi/ScreenshotTile) app instead:

1. Install **Screenshot Tile (NoRoot)** and add its quick-settings tile.
2. In the app settings, enable automatic screenshots and set a **broadcast
   secret** (string, anything).
3. Install `am` access from Termux (Termux usually ships it; the app must have
   the same storage access).
4. Run the relay with the secret:

```bash
export SYNAPSE_SCREENSHOT_SECRET="YOUR_SECRET"
python3 relay.py
```

The tool sends `am broadcast ... com.github.cvzi.screenshottile.SCREENSHOT`,
ScreenshotTile saves `Pictures/Screenshots/Screenshot_<timestamp>.png`, and
Synapse picks the newest one and returns it as the image.

Environment knobs:

| Var | Meaning |
|-----|---------|
| `SYNAPSE_SCREENSHOT_SECRET` | Required on Android. The ScreenshotTile broadcast secret. |
| `SYNAPSE_SCREENSHOT_DIR` | Override the screenshot folder (default `Pictures/Screenshots`). |
| `SYNAPSE_SCREENSHOT_PACKAGE` | Target package (default `com.github.cvzi.screenshottile`). |

```bash
pip install mss            # fast, cross-platform, recommended
# or
pip install pillow
```

## Notes

- The executor and relay must be on the **same device**, so `127.0.0.1` in the
  executor points at the local relay.
- `anti_kick` / `spoof` use executor-only functions (`getrawmetatable`,
  `setreadonly`, `newcclosure`, `getnamecallmethod`). They fail gracefully on
  an executor that does not expose them.
- Transport order is `syn.request` / `http_request` / `request` /
  `game:HttpGet` / `HttpService:GetAsync`; the executor picks the strongest one
  available on load and can be forced with `_G.__SYNAPSE_TRANSPORT =
  "syn"|"http_request"|"request"|"game"|"http_service"` before loading.
