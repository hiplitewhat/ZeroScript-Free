# Changelog

All notable changes to ZeroScript Free are documented here.

## [Unreleased]

### Added
- **ChatGPT (chatgpt.com) as a text/tool-command provider.** The extension
  handles both the classic transcript and the September 2026 search-unit /
  ProseMirror layout, detects Send/Stop from the composer, preserves code blocks
  for the tool parser, and offers the same Start / Automatic send flow as the
  other providers. Vision-only tools remain hidden until image upload is
  verified on ChatGPT. Run `npm run test:chatgpt` for DOM regression coverage.
- **`termux.exec` works on Windows.** The Termux MCP server no longer
  always spawns `/bin/sh` (WinError 2: file not found on Windows-2025Server /
  GitHub Actions / a PC bridge). It uses `cmd.exe /c` (or `%COMSPEC%`) on
  Windows and `/bin/sh -c` on POSIX. `termux.ping` now reports `host`,
  `shell`, and a clear note that this is the PC — not Android — so the agent
  can actually check `where blender` instead of claiming the shell is dead.
- **Pace interval for auto-replies (anti-suspend).** Behaviour menu: toggle
  **Pace auto-replies** (on by default) plus an interval in seconds (default 25,
  range 5–180) with random jitter (×1.2–1.6). The agent waits that long before
  injecting each tool-result turn into DeepSeek / Gemini / Kimi / GLM / Qwen /
  Arena / Meta AI, so a long session on the same account is less likely to look
  like a bot and get suspended. On top of the interval: every 5th auto-reply
  rests 45–90s, and after 24 in an hour it cools down for 15 minutes. The bar
  shows `pace Ns` (and a countdown while waiting).
  Turn it off if you want the old full-speed send.
- **The bridge can now CONSUME a hosted MCP server - Replit's own.**
  `config.json` accepts `{"replit": {"url": "https://mcp.replit.com/server/mcp"}}`
  (2026-10-03 user request: "my bridge uses REPLIT's MCP"), and the bridge speaks
  streamable HTTP to it: 401 -> RFC 9728 protected-resource discovery -> RFC 8414
  authorization-server metadata -> dynamic client registration -> PKCE (S256)
  sign-in over a loopback callback -> token exchange, with the RFC 8707
  `resource` parameter. Replit's tools (`list_apps`, `create_app_from_prompt`,
  `update_app_using_prompt`, `publish_app`, `get_publish_status`, `ask_question`,
  `search_apps`, `resolve_app_by_name`) then appear in the same aggregated
  catalogue as Roblox/Synapse/Termux, so the extension's AI can build and publish
  Replit apps. Sign-in is a command - `python bridge.py --login replit` - never a
  startup step, because boot must not block on a browser; a bridge that is
  already running notices the sign-in within ~5s. Access tokens refresh
  themselves (`offline_access`), a 401 that cannot be refreshed is reported as
  `needs_auth` in `/status` with the exact command to fix it (and is NOT
  auto-restart-looped), and tokens live in `.mcp_tokens.json` (0600, gitignored).
  Static-key servers work too: `{"url": ..., "headers": {"X-API-Key": ...}}`.
  Added without editing files on a phone: `python bridge.py --add-server replit`
  (fills in Replit's URL, merges into config.json, leaves other servers alone),
  `--add-server <id> <url> [--header K=V]` for any other hosted server, and
  `--remove-server <id>` to undo. On Termux the sign-in URL is also copied to
  the clipboard, since tapping a long link in a terminal is fiddly.
  Verified against a Replit-shaped mock (18 checks, `npm run test:mcp:remote`):
  discovery at the path-scoped well-known URL, DCR, PKCE verified server-side,
  resource parameter, token storage mode, refresh-on-expiry, sign-in pickup by a
  running bridge, and the no-token path. `/status` gains `remote_mcp` +
  `mcp_oauth` features and `remote`/`needs_auth` per server.
- **Any AI app can now drive the bridge, not just HTTP-speaking ones.**
  `mcp_stdio_bridge.py` (new, stdlib only) is a stdio MCP server that forwards
  every message to the bridge's `POST /mcp`, so an AI app that can only launch a
  command - Claude Desktop, Cursor, Cline, Windsurf, Cherry Studio, LM Studio -
  drives the same Roblox / Synapse / Termux tools as a URL-configured agent.
  Bridge-down and bad-token are reported as JSON-RPC `-32001` errors rather
  than leaving the app spinning, and stdout carries protocol messages only
  (diagnostics go to stderr). Verified with the official MCP SDK's stdio client
  and with two committed tests (`npm run test:mcp` now covers both client
  shapes, 14 tests).
- **The bridge is now an MCP SERVER too (`POST /mcp`).** Every other part of
  the bridge consumes MCP (Roblox Studio, Synapse, Termux, addons); this is the
  mirror image, so a REMOTE agent can drive those same tools. Replit Agent is
  the motivating client (2026-10-03 user request): it connects MCP servers by
  URL plus custom headers, so a tunnel URL and `X-API-Key: <ZS_API_TOKEN>`
  is the whole setup. Transport is MCP "streamable HTTP" — `initialize`, `ping`,
  `tools/list`, `tools/call`, batched JSON-RPC, answers as `application/json`
  or a single SSE `message` event for clients that only accept
  `text/event-stream`; `GET /mcp` is 405 (no server-initiated stream, as the
  spec allows) and `DELETE /mcp` ends a session. Tool names are sanitized for
  LLM tool-schemas (`synapse.ping` → `synapse_ping`) and `tools/call` accepts
  either spelling (resolving a sanitized name even when the client never called
  `tools/list` first, or another client's listing cleared the map); a failed
  tool returns `isError: true` as a RESULT so the agent can read the reason
  instead of the call blowing up. `X-API-Key` is now
  accepted as a token spelling everywhere (that is what Replit's custom-header
  field documents). Verified with the official `mcp` Python SDK 2.3 (initialize
  + tools/list + tools/call) and with 28 raw JSON-RPC checks, including the
  SSE-only, batch, 405, 204, and 401-without-token paths. Security note:
  `/mcp` reaches `termux.exec` and `synapse.*`, so a public tunnel URL without a
  token is remote code execution — set `ZS_API_TOKEN`.
- **Keyless relay now carries an app's system prompt and history.** The web
  chat is one message wide with no memory of your client, so a relayed app used
  to arrive stripped of the only thing that defines its protocol: PrivateAgent
  (Android screen agent, 2026-10-03) sends a 2 175-char system prompt that
  specifies `{"action": …, "params": …, "response": …}` as the ONLY acceptable
  reply, plus up to 20 turns of screen/action history — and the bridge forwarded
  the newest message alone, so no valid action could ever come back. A client
  that sends a system prompt or a transcript now gets both carried into the web
  chat inside a fenced wrapper ("answer as that app's assistant, follow the
  client instructions, reply with only the text the app expects, do not call
  tools"). A client that sends ONE message is still relayed verbatim, unchanged.
  Carried text is bounded — 6 000 chars of system prompt, 12 000 of history
  (newest turns kept), 12 000 of the newest message, oversized text clipped at
  both ends with a visible `…[N chars dropped by the bridge]…` marker, the
  marker counting against the cap.
- **The `/v1` endpoint now streams.** A client asking for `stream: true` gets
  real SSE (`text/event-stream`) instead of the old 400 — role delta, one
  content delta with the reply, the finish chunk, then `[DONE]`. This unblocks
  the many mobile AI apps and OpenAI SDKs that request streaming by default and
  refuse a plain JSON body (a user's Android app pointed at
  `http://127.0.0.1:17614/v1` was the report). The delta is not incremental:
  the brain finishes the whole turn before it has anything to say, so the reply
  arrives as a single content frame — correct per the chunk protocol, just not
  word-by-word. A FAILED turn still answers as JSON with its HTTP status, the
  way OpenAI does, so a client raises its normal error instead of hanging.
  Verified with the real `openai` Python SDK 3.24 (streaming + non-streaming)
  and with curl, including the no-brain-available failure path.
- **Show live timers on message chips (toggle, default ON).** Behaviour menu:
  turn off the ticking seconds on chips — the `execute_luau · 12.0s` while a
  tool runs and the `~1.2k tokens · 8s` while a reply streams. Purely
  cosmetic; the elapsed anchors keep being stamped while it is off, so
  switching it back on mid-turn shows the TRUE elapsed time rather than
  restarting at 0s. Persisted as `zsShowTimers`.
- **The doctor now calls out DUPLICATE extension copies.** Stage 7 turns red
  when more than one extension client holds a bridge websocket — one
  extension = one client, so >1 means an old+new unpacked folder are both
  enabled (or a second browser runs ZeroScript). That's the configuration
  behind the 2026-09-10 screenshot: six stacked bars, two bar layouts in one
  tab, and the "DeepSeek mode not ready" banner the copies caused by
  fighting over the composer toggles. The message names the fix
  (chrome://extensions → remove ALL ZeroScript entries → load unpacked once).
- **Arena Agent Mode provider hardened with the MoatazCode build's proven
  agent machinery (la porter — the whole file, Arena branding kept).**
  `providers/arena-agent.js` gains, verbatim in behavior:
  the task-complete panel auto-resume ("Was this task successful?" → the
  composer UNMOUNTS at every turn end; we now detect the tag-agnostic
  "Keep working" row, click it with the full pointer/mouse sequence, and
  poll on a 700 ms safety timer so even a parked waitForResponse
  auto-resumes — before, one panel killed the whole session);
  stable turn anchoring on `[data-agent-transcript-message]` +
  `data-user-message-body-row` (class-based turns kept as fallback) with
  document-order sorting and WeakMap per-node `lastAssistantId`;
  `readAssistant` now returns the `{present, reply, thinking, item}` object
  the core actually reads (the old string return made every reply invisible
  to the watcher);
  a real six-key `timings` with agent-width windows (`RESPONSE_TIMEOUT_MS`
  15 min for coding-agent runs) — the old `{NO_TURN_GRACE, IDLE_MS}` left
  every key the core reads undefined;
  send CONFIRMATION in `typeAndSend` (click, watch the composer clear,
  re-click / Enter-fallback up to 6 tries) plus the panel pre-dismiss;
  the stop button scoped to the composer card (stale stops no longer read
  as "still generating");
  a locked-vs-absent editor distinction with a 12 s remount grace in
  `scanError` (no more "input box disappeared" while WE hold the input
  lock; also no more "any toast text = fatal error");
  a real `contenteditable=false` input lock with the standard placeholder;
  and a 250 ms editor-resolution cache for the 60 fps bar loop. Verified:
  29 jsdom assertions (turn order/roles, identity ids, lock reach-through,
  stop scoping, error grace, HITL never clicks Yes/No, send confirm, throw-
  when-truly-gone, timings keys).
- **The status bar now prints the build tag (`ZeroScript v1.5.2 ·0907a`) —
  every screenshot self-identifies the code on the phone.** "Did the fix
  even reach the phone?" was unanswerable from a photo of the tab: the
  manifest version is the maintainer's and never moves with arena fixes.
  `background.js` now rides `EXT_BUILD` on every `zs-status` push/poll and
  the bar's version span shows the short tag (`zs-2026-09-07a` → `·0907a`).
- **A command interrupted mid-flight is now told apart from one that never
  left — fixing the "ping answered, the very next command 'failed'
  instantly" loop seen in the 2026-09-07 screenshot.** The bridge-facing
  send path now stamps every `disconnected` result: `sent:true` when the
  frame was already on the wire (socket dropped while the bridge may be
  RUNNING the command) and `sent:false` when nothing left the device.
  `runTool` acts on the difference: `sent:true` returns the new
  `FEEDBACK.linkDropped` ("it may ALREADY have run — verify with a
  read-only command before re-running, never blind-repeat a write");
  never-sent results first re-check live status and, if the socket is
  already back, return the new `FEEDBACK.linkHiccup` ("the browser just
  suspended the extension — resend the SAME command once, immediately").
  Only a still-dead socket earns the old `bridgeOffline` verdict. No more
  double-runs, no more "bridge is offline" declared over a one-second
  Android doze blip.
- **The extension now reports its build to the bridge — the objective
  answer to "Still [broken]" when fixes never reach the phone.** Manifest
  versioning is the maintainer's and doesn't move with arena fixes, while
  Android reloads silently keep serving the OLD unpacked folder — so a
  user can re-load an extension and run months-old code without any sign.
  `background.js` stamps `EXT_BUILD` (dated tag, bumped with every
  extension fix) into the `hello` handshake; the bridge logs it at connect
  (`extension hello: build zs-2026-09-06b, caps ['chat']`), exposes
  `chat.extension_builds` in `/status`, and `Termux_Doctor.sh` stage 7
  compares it against the zip's own `background.js` — a mismatch (or no
  tag at all = pre-tag build) says exactly which folder to load. Verified
  live: 6 end-to-end checks (old/new client mix, status fields, doctor
  verdicts, bridge log lines) + 7 assertions on the extracted real
  handshake.
- **Request tracing for chat routes.** Every `POST /chat` and
  `/v1/chat/completions` now logs a `-> ...` arrival line (file-only) and a
  `<- ... Ns ok/ERR ...` completion line in the bridge terminal, turning "it
  keeps timing out" into a visible fact: no arrival line = the request never
  reached the bridge (URL/tunnel issue); arrival but no completion = the
  request is still in flight inside the web chat (tab backgrounded/slow);
  a fast completion with an error = read the error, the "timeout" is the
  CLIENT giving up before a slow success (raise its timeout).
- **OpenAI-compatible endpoint: `POST /v1/chat/completions` + `GET /v1/models`.**
  Any OpenAI-SDK tool (MoneyPrinterTurbo was the direct ask) can now point
  its "custom OpenAI" config at the bridge: base URL
  `http(s)://<host>:<port>/v1`, api key = the same `ZS_API_TOKEN` (the SDK's
  `Authorization: Bearer` header IS the API token gate). Brain = keyless
  extension relay, or the headless agent loop when `ZS_LLM_KEY` is set -
  same `/chat` machinery refactored into `_agent_loop`/`_chat_tools_catalogue`
  and reused verbatim. Stateless by design (OpenAI clients resend full
  history); keyless relays only the last user message; content-parts input
  flattened; client `tool` messages dropped; no SSE (`stream:true` ->
  OpenAI-shaped 400); `finish_reason` and a zero `usage` object keep picky
  SDKs happy. Verified live: 20 checks across both brains - token 401,
  models list, last-user-only, parts flattening, stream/empty 400s, and a
  real `termux.ping` function-call round-trip through the mock provider.
- **`Termux_Doctor.sh`: one-command "curl doesn't work" checker.** READ-ONLY;
  run it in the same shell the bridge was started from (optionally with the
  tunnel URL). It prints ok/BAD per stage so remote debugging stops being
  guesswork: files too old (no sharing code), bridge/relay processes dead,
  env vars of THIS shell (exports die per session), API answering on
  loopback, token accepted vs 401 mismatch, LAN sharing actually bound vs
  refused, tunnel URL live vs stale (a new ssh/cloudflared run = a new URL),
  and whether the keyless /chat brain (extension tab) is connected.
- **Keyless `POST /chat`: no LLM API key needed ("No key").** When no
  `ZS_LLM_KEY`/`DEEPSEEK_API_KEY` is configured, `/chat` no longer errors -
  the bridge relays the turn to a connected browser extension over the
  existing WebSocket, which types it into the FREE web chat (DeepSeek,
  Gemini...) and runs the normal agent loop with the same tools, then
  returns the final reply to the curl caller (`transport:"extension"`).
  Handshake: background.js advertises `caps:["chat"]` in a new `hello`
  message; relays pick only capable sockets, wait on per-id futures
  (`chat_result`), fail on a mid-chat disconnect, honour `ZS_CHAT_TIMEOUT`
  (default 300 s, per-request `timeout` too), and serialize (a concurrent
  second call gets a clear "busy" error). The content script gains
  `zs_remote_chat`: it reuses the Start-button bootstrap on an empty chat,
  refuses to hijack an existing conversation, and requires the AI tab open
  + foreground (a background tab pauses the agent, by design); the web
  conversation IS the memory (session ids advisory on this path). With a
  key configured, the headless provider loop is used unchanged.
  `GET /status` now reports which brains are live (`chat.llm`,
  `chat.extension`). Verified live: full curl → bridge → mock-extension →
  reply round-trip, plus timeout, busy, disconnect-mid-chat, and
  no-extension error paths.
- **Share the HTTP API: other people can curl your bridge.** The API was
  loopback-only; `ZS_API_HOST=0.0.0.0` now binds it on every interface so
  friends/devices on the same LAN/hotspot can `curl` it, and tunnelling
  (`cloudflared tunnel --url http://127.0.0.1:17614`) shares it over the
  internet without touching the bind. Shared access is gated by
  `ZS_API_TOKEN` on every route except `/health` (`Authorization: Bearer`,
  `X-ZS-Token`, or `?token=` for a plain browser tab; constant-time
  compare). Because `/call` and `/chat` execute shell and in-game code, a
  non-loopback bind without a token is REFUSED (loopback fallback + loud
  warning) unless `ZS_API_OPEN=1` explicitly opens a trusted LAN.
  `Termux_Start.sh` prints the share URL with the phone's LAN IP when
  sharing is configured; `/status` now reports `api_auth`. Verified on all
  four modes: loopback default, refused-token-less share, token auth
  (401/200 matrix incl. wrong token), and forced-open.
- **Automatic send: the agent starts on your first message.** On a fresh
  chat, typing a message and hitting send while the bridge is healthy now
  bootstraps the agent session by itself and delivers that message as the
  first agent turn - the "▶ Start Roblox agent" click is no longer a
  required first step (the panel used to just nudge while the message went
  out as plain, tool-less chat). The takeover only happens when ALL hold:
  the new toggle is on (menu → Behaviour → "Automatic send", default ON),
  the chat is brand new and empty, no session/loop/injection is in flight,
  the same gate that enables the Start button passes (bridge + tools +
  provider mode), and no auto-start failed in the last 20 s (a failed
  bootstrap leaves the typed text in the composer and the next send goes
  out as plain chat). Every provider (DeepSeek, Gemini, Kimi, GLM, Qwen,
  Arena, Arena Agent, Meta AI) intercepts both Enter and the send button;
  an existing conversation is never hijacked. The standby bar line now
  reads "Just send a message - the agent starts itself." while active.
  Verified: 8 scenario matrix (60+ assertions) against the real provider
  hook and the real `tryAutoSend` extracted from `core/main.js`.
- **Local HTTP API on the bridge (still the bridge).** A stdlib-only,
  loopback-only JSON API now runs next to the WebSocket on
  `http://127.0.0.1:17614` (`ZS_API_PORT` to move it, `ZS_API=0` to disable):
  `GET /health` `/status` `/tools(?refresh=1)` and `POST /call
  {name,arguments,timeout}` - curl/Tasker/scripts can drive every MCP tool
  (Synapse in-game, Termux shell, Studio on PC) without the browser.
- **Headless chat through the API: `POST /chat`.** The bridge runs an agent
  loop against any OpenAI-compatible chat API (DeepSeek default;
  `ZS_LLM_KEY`/`DEEPSEEK_API_KEY`, `ZS_LLM_BASE`, `ZS_LLM_MODEL`,
  `ZS_LLM_MAX_ITERS`), executing the same MCP tools the extension would -
  tool names are sanitised for the OpenAI function-name charset
  (`synapse.ping` -> `synapse__ping` and back), tool results are capped, and
  sessions keep short in-memory history per id (`POST /chat/reset` clears).
  Verified end-to-end with a mock provider driving a real `termux.exec`
  round-trip. Needs a provider key; the no-key path stays the browser
  extension.
- **`Termux_Start.sh`: one-command Android launcher** (the Termux equivalent
  of `start.bat` / `MacOS_Start.command`). It starts `synapse/relay.py` and
  `bridge.py` together, installs `websockets` if missing, holds the wake
  lock so Android does not kill the session, and Ctrl+C stops everything it
  started.
- **Bridge status now reports `studio_capable`** in every snapshot: `false`
  on hosts that can never run Roblox Studio (Android/Termux, Linux - where
  the `roblox` server is skipped), so the extension can stop demanding
  Studio there. Older bridges simply don't send the field and the extension
  keeps its desktop behavior.
- **Android: the bridge auto-registers its local servers.** No more
  "MCP servers > Add server" dance on a phone screen: on the first run on
  Android/Termux, `synapse/mcp_synapse.py` (18 in-game tools) and now
  `termux/mcp_termux.py` (4 shell/file tools) are written into `config.json`
  by the bridge itself - each with its own done-once marker
  (`<id>_auto_added`), so a deliberate later removal is respected and new
  auto-servers land independently for existing installs. Seen live: a user
  HAD added Synapse and the panel still sat on the bare-bridge guidance
  with Start disabled.
- **`termux/*`: the AI can run code on the device itself.** New stdio MCP
  server (`termux/mcp_termux.py`, auto-registered on Android):
  `termux.exec` runs shell commands with timeout/output caps (pip install,
  python3, git, curl, `am`/`termux-*` API calls), `termux.read_file` /
  `termux.write_file` handle device files (parent folders auto-created,
  append mode, size/byte reporting), `termux.ping` health-checks. Commands
  run non-interactively (stdin closed) as the same Termux user; long-lived
  work goes through `nohup ... &`. Also addable manually on PC.

### Fixed
- **The reskin's ICON-ONLY DeepThink/Search chips are controllable again.**
  DeepSeek's "Instant, Expert, and Vision — now unified and upgraded" update
  (their own promo card, screenshot 2026-09-10) replaced the text chips with
  atom/globe icon buttons — no `.ds-toggle-button`, no text — so the
  text-matching toggle finder went blind and **Search stayed silently ON**
  ("it derails the agent"), while the gate (previous fix) let the session
  start. `findToggleBy` now also matches `aria-label`/`title` and, for the
  icon UI, searches ONLY inside the composer toolbar (a "Search
  conversations" sidebar button must never be clicked). DeepSeek's unified
  model means there is nothing to pick for the user: one model does
  everyday chat, image understanding and reasoning. 7 jsdom assertions
  (globe found + clicked off exactly once, sidebar untouched, no-toggle and
  legacy regressions).
- **"DeepSeek mode not ready" on the reskinned UI — you don't need any model
  tabs; the gate no longer asks for them.** DeepSeek dropped the
  Instant/Expert/Vision radiogroup in a 2026 reskin (verified live
  2026-09-10: a fresh chat shows only the DeepThink + Search chips — "It was
  not there"), and the start gate hard-required finding one of those radios,
  so the banner fired over a perfectly good composer. `ensureComposerReady`
  now passes when NO model control exists at all (nothing to switch; the
  model is whatever DeepSeek serves), still requires Search off, and still
  refuses when a picker IS present but nothing could be switched on. 5
  assertions on the extracted real gate (no-tabs/expert-on/none-on/search-
  late/partial-picker).
- **One tab can no longer stack five status bars — and double-run every
  tool call behind them.** The 2026-09-07 screenshot showed five stacked
  "Agent active" bars in a single DeepSeek tab: the content script had no
  same-document execution guard and `build()` never removed older roots,
  so every re-execution mounted another bar (and another reply-scraper
  that fires the SAME commands). The module now latches
  `window.__zsMainInstance` and refuses to run twice in one document, and
  `build()` wipes every earlier `#zs-root` before mounting — including any
  left by an older copy of the extension still injected in the page
  (isolated worlds share the DOM even though they can't see each other's
  flags).
- **A doze-wake tool call gets three chances, not two — and only the
  safe-to-retry kind.** `callToolWithWakeRetry` used to retry any
  `disconnected` once after a flat 2.5 s, which both missed short Android
  doze windows (worker respawn can outlast it) and would have re-sent even
  a command that was already on the bridge when the socket died. Retries
  now escalate (1.2 s, then 2.5 s) and stop the moment a result says
  `sent:true`, so a possibly-running command is never duplicated while a
  never-sent one rides out the wake window. Verified with 22 assertions
  including re-runs of the extracted real retry loop (sent/unsent/heal/
  dead/timeout/falsy cases).
- **The "bridge is offline" guard no longer poisons the conversation.**
  The `bridgeOffline` note told the model to stop until the user confirms
  — and then said NOTHING about what to do after, so a chat that ever hit
  one transient failure kept answering "the bridge is offline, let me know
  when it's ready" forever, without ever re-testing the bridge. The
  feedback now instructs the model: on any user "it's back / retry",
  quietly verify with ONE cheap command (`list_mcp_servers`, or
  `termux.ping` on Android) — continue the task if it answers, and never
  claim the bridge is offline without a just-seen failure. Fresh sessions
  were the old workaround; this makes recovery part of the protocol.
- **Tool calls no longer die as "bridge unreachable" while Android wakes
  the extension worker mid-call — the real "But I did [start the
  bridge]".** A long-running tool (`ffmpeg` was the repro) starts fine,
  then the phone dozes, Chrome suspends the MV3 service worker, and when
  the agent's next call lands the worker is still warming its bridge
  reconnect: the call resolves `disconnected`, which is fed to the model
  as *"the local ZeroScript bridge is unreachable"* — whose own handling
  text tells the model to STOP sending commands entirely. Bridge running
  the whole time; every retry dies the same way. The extension now retries
  the `disconnected` class (and a dead-worker no-reply) exactly once after
  a short beat — a warm worker answers, a truly-down bridge fails fast
  again, and Stop still latches off the retry. Verified: 11 extraction
  assertions on the real `callToolWithWakeRetry` (wake-retry succeeds,
  down-bridge fails after exactly 2 attempts, healthy call untouched,
  timeout/error classes never double-run).
- **Keyless `/chat` on a stuck-background tab now fails in ~4 s with
  instructions instead of hanging for minutes — the real "from timeout".**
  A relayed turn can never run while the tab is hidden (send/watch park on
  `waitVisible()` with no cap, and a remote caller is never there to un-park
  it): the turn used to hang to the 270 s backstop and surface only as a
  client-side timeout. The content script now waits a moment for the
  background page's tab-raise to land, then refuses fast, explaining that on
  Android an extension cannot pull the whole browser app over another app
  (Termux) — foreground or split-screen the browser and retry.
- **Keyless relay hardened and made self-explaining — chased down with a
  live end-to-end rig (real bridge + mock extensions + Termux_Doctor).**
  After the user pushed back that the tab was visible ("Not brain"), the
  relay itself was proven sound (`<- /chat 0.1s ok` round-trips), and the
  remaining failure classes were fixed or instrumented: relay now targets
  the FRESHEST chat-capable extension client (dict order used to pick the
  oldest — a reloaded Android service worker could sit at the front of the
  line, half-alive, eating every relay into a socket that never answers);
  a send failure or mid-chat disconnect retries the next client (logged
  `client i/N dropped mid-relay - trying next`), while a plain slow turn
  never double-sends into a second AI tab; timed-out relays log whether
  the late `chat_result` arrived with no waiter (late/duplicate) — the
  classic "extension answered but nobody heard it" class is now one log
  line instead of a guess; a 1 Hz event-loop-lag watchdog barks if any
  synchronous call ever freezes the bridge; `ZS_WS_DEBUG=1` adds per-frame
  relay tracing; `/status` reports `extension_clients` and the freshest
  client's idle age (zombie-trap diagnostics); the AI TAB itself toasts
  every remote turn and every failure, so the person holding the phone
  sees what the curl caller sees; and `Termux_Doctor.sh` gained stage 8,
  a live end-to-end probe that runs one tiny chat turn and prints the
  bridge's own diagnosis when it fails. Verified: 19 live end-to-end
  checks (freshest-pick, stalest-untouched, drop-retry, timeout-no-
  double-send, late-answer-discard, status fields, doctor green) plus 12
  extraction assertions on the real `remoteChatTurn` (toast mirroring,
  focus race, gates).
- **Keyless `/chat`: the extension brings its AI tab to the front itself —
  the real "make the extension send".** The content script parks every
  submit/watch while the tab is hidden (`waitVisible()` has no cap;
  background-tab reads are unreliable by design), and a curl caller is in
  Termux / MoneyPrinterTurbo / another machine — never looking at the tab —
  so nothing ever un-parked it and the request burned the whole chat
  timeout. `background.js` now activates the relayed tab AND raises its
  window before handing over the turn, so a hidden/backgrounded AI tab
  answers instead of hanging. The stale "bring the AI tab to the
  foreground" error hint was reworded to match.
- **Automatic send no longer silently eats your message when auto-start
  fails.** The takeover swallowed the Enter/click, then the bootstrap
  prompt OVERWROTE the composer's copy — if startup then failed (bridge
  down, mode not ready, site derped), the typed message existed only
  inside an async closure and was lost; the old comment claiming "the text
  never left their composer" was only true before bootstrap ran. All 8
  providers now expose `setDraft()` (type text without sending) and the
  failure paths restore the captured message into the input with a toast
  ("press Enter to send it as a normal chat"); the existing 20 s cooldown
  already routes that next Enter to the site's native send. Verified: 35
  assertions against the extracted real code (relay focus/ordering/fall-
  through, un-swallow restore on bootstrap-fail and submit-throw, gate
  matrix).
- **`termux.exec` no longer stalls a whole timeout cap on commands that
  background children — the real "it keeps timing out when it's running
  cmd".** `subprocess.run(capture_output=True, timeout=…)` waits for pipe
  EOF, which only arrives when EVERY descendant exits: `cmd &`, dev
  servers, npm/pip build chains all burned the full 30 s cap and were then
  misreported as TIMEOUT (verified live: `echo hi; sleep 300 &` took the
  full cap). The shell is now watched for EXIT via a single-threaded
  non-blocking `select` pump, output drained with a 1.5 s post-exit grace
  — the same call now returns in ~1.7 s with exit code 0 (an earlier
  threaded attempt deadlocked: `file.close()` blocks while a reader thread
  is mid-read). Timeouts now kill the whole PROCESS GROUP, so backgrounded
  children can't leak on the phone. 11-check matrix: bg-child, blocked
  group-kill (no leaked sleep), plain timeout, nohup daemon, grandchild
  pipes, streams/exit codes, memory cap, closed-stdin EOF, rc=127.
- **Android: the panel no longer "forces Roblox Studio".** With the bridge
  skipping the Studio server as designed, the extension still gated its
  status dot, its Start button, the MCP-servers menu and its corrective
  banners on the absent `roblox` server: a bare Android bridge read "Bridge
  OK, open Roblox Studio", showed "Open Roblox Studio for the tools" with
  Start disabled, claimed "Roblox Studio - primary - always connected" in
  the menu, and fed "open your place in Roblox Studio" back to the AI. On
  `studio_capable:false` the gate now follows the other servers (a live
  Synapse = green dot, Start enabled normally), and every nag points at the
  Synapse in-game path (`synapse/relay.py` + the `synapse/mcp_synapse.py`
  addon + the `/synapse` executor loader) instead.
- **Android/Termux: the Bridge no longer crash-loops the `roblox` Studio MCP
  server forever.** The primary server launches `StudioMCP`, which only ships
  with desktop Roblox Studio (Windows/macOS); on Termux it exited with
  code 1 instantly and the auto-restart then looped it every ~15s
  (`stdout closed (process ended, exit code 1)` followed by
  `auto-restart failed: [Errno 32] Broken pipe`), burying the Bridge's real
  status under noise that could never succeed. The Bridge now skips the
  default `launch_studio_mcp.py` primary on platforms that cannot host
  Studio, prints a one-time note pointing at the Synapse in-game path
  (`synapse/relay.py` + the `synapse/mcp_synapse.py` addon server), and keeps
  serving the extension normally. A `roblox` server pointed at a different,
  custom command is left untouched.
- **Dead-on-arrival MCP servers now explain themselves.** Writing the MCP
  handshake to a child that had already exited surfaced as a bare
  `[Errno 32] Broken pipe`; the Bridge now fails that launch / restart with
  `server '<id>' died before it could answer (stdin write failed: ...)` and
  the crash-loop banner prints the child's actual stderr line as the reason.
- **No more `Exception ignored while finalizing file <_io.TextIOWrapper ...>`
  garbage on the terminal.** Dead child pipes are now closed explicitly when
  EOF is detected (and on stop), so the garbage collector never finalizes a
  broken pipe mid-spinner / mid-banner.

- **`synapse/setup_delta.lua` can no longer hang forever on executors with a
  broken `game:HttpGet`.** The bootstrap used to be exactly the naive
  `loadstring(game:HttpGet(..."/synapse"))()` one-liner - on executors where
  `game:HttpGet` never returns in a live game (seen live on Delta: every
  bootstrap attempt "timed out after 120 seconds"), nothing ever connected.
  The loader now walks the same transport ladder `synapse.lua` itself uses
  (`syn.request` / `http_request` / `request` / `game:HttpGet` last), each
  attempt run in a spawned thread with a hard 5s cap so a hanging transport
  leaks one dead thread instead of wedging the loader, prints which transport
  worked, and warns with the relay health check when nothing can reach the
  relay. README + synapse docs updated to match.

### Changed
- **Android guidance everywhere now says the servers self-register** - the
  bar's bare-bridge hint, the startup banner, the `list_commands` /
  `list_mcp_servers` fallbacks and the menu note no longer tell users to add
  the Synapse server through the panel (it is automatic since the
  auto-registration above), and point at `sh Termux_Start.sh` + the
  `/synapse` executor loader instead.
- **README: the Android setup steps now match the shipped Synapse stack** -
  `python3 synapse/relay.py` replaces the copy-pasted `relay.py` snippet, and
  the Delta step uses the `http://127.0.0.1:18081/synapse` loader plus the
  one-time "Add server" registration of `synapse/mcp_synapse.py`, instead of
  the stale raw-polling loop that spoke the old relay protocol.
- **`launch_studio_mcp.py` is platform-accurate when StudioMCP cannot
  exist.** On non-Windows/macOS hosts it says so plainly (and on
  Android/Termux it points at the Synapse path) instead of telling the user
  to "Open Roblox Studio and enable 'Studio as MCP server'" - an app that
  does not exist on their device.

## [1.4.9] - 2026-07-24

### Added
- **Popup: new Settings button.** Opens the same Switch AI / support panel
  as the in-page bar, without needing an already-started conversation. The
  footer text no longer singles out chat.deepseek.com - it now points to
  "a supported AI" since seven providers are supported.
- **Bridge: auto-recovers its own port on relaunch.** Relaunching `start.bat`
  while a previous Bridge was still holding port 17613 (window closed with
  the X, a crash, a double launch) used to crash with a cryptic, sometimes
  localized `OSError [WinError 10048]`. The Bridge now detects and kills a
  leftover Bridge process it can positively identify (by command line, never
  by process name alone) before binding, and falls through to a clear,
  actionable message - with the exact `netstat`/`taskkill` commands and the
  `ZS_BRIDGE_PORT` override - if the port is held by something else.

### Fixed
- **The agent could parse/execute commands while its AI tab was backgrounded
  or the window minimized.** Background tabs throttle rendering and timers,
  which made DOM reads unreliable and could send duplicate feedback or run a
  tool blind (observed live: GLM kept running `execute_luau` while minimized).
  The agent loop, the tool-dispatch step, and the auto-resume watchdog now
  all gate on `document.visibilityState` and park - with no time limit -
  until the AI tab is the foreground tab again, then resume exactly where
  they left off. Working with Roblox Studio focused while the AI tab stays
  the active tab in its own window is unaffected; this only pauses execution
  while that tab is truly backgrounded or its window minimized.

## [1.4.8] - 2026-07-22

### Added
- **macOS and Linux support for the Bridge.** A new self-contained
  `MacOS_Start.command` launcher (double-click in Finder - no Terminal
  knowledge needed) finds Python 3.9+, installs `websockets` if missing,
  frees a previous Bridge still holding the port, and runs `bridge.py`,
  mirroring what `start.bat` already does on Windows. `launch_studio_mcp.py`
  now also locates Roblox Studio's MCP binary inside the macOS app bundle
  (`RobloxStudio.app/Contents/MacOS/StudioMCP`), with a `ZS_STUDIO_MCP_PATH`
  override for non-standard installs.
- **DeepSeek: outgoing messages are now truncated to fit its input limit.**
  DeepSeek's composer silently refuses to send past 163840 characters
  (validated live), which could wedge the agent in the input box after a
  large tool result (a big `http_get` / `get_page_text` / Luau dump). Long
  results are now truncated to a safe margin below that limit, keeping both
  the start and the end of the content, the same approach already used for
  Qwen and Arena.

## [1.4.7] - 2026-07-21

### Fixed
- **Qwen: a tool could show a green "done" check while it never ran and returned
  no result** (seen rarely with repeated `multi_edit` / `execute_luau` calls, with
  no Stop or regenerate involved). Qwen virtualizes its message list, so the
  off-DOM "already executed" record was keyed on the positional turn index, and
  two turns that shared the same 60-character command prefix could collide on the
  same index. That false positive made the auto-resume watchdog skip the fresh
  command (so it never ran, no result was injected) while the chip was still
  painted a green check. The dedupe now keys on Qwen's stable per-turn id
  (`chat-response-message-<uuid>`, exposed as `itemKey`) instead of the index, so
  the collision cannot happen.
- **Qwen: the ZeroScript bar covered the "Expand more models" submenu.** That
  fly-out is a separate body-portalled `.ant-dropdown` at a low z-index, not the
  main model dropdown, so the bar drew on top of it. Raised just that dropdown
  above the bar (scoped so other Ant menus and tooltips are untouched).

### Added
- **Per-model image support on Qwen.** Qwen offers both multimodal and text-only
  models, switchable mid-conversation, and image input only works on the
  multimodal ones. `screen_capture` and image input are now enabled only on a
  vision-capable model (Qwen3.7-Plus, Qwen3.6-Plus, Qwen3.6-27B, Qwen3.8-Max-Preview)
  and correctly withheld on a text-only one (Qwen3.7-Max, Qwen3.6-Max-Preview),
  read from the selected model and updated when you switch models.
- **Image support on DeepSeek's Vision model.** DeepSeek forces its Expert model
  for the agent, but if you choose the Vision tab that choice is now respected and
  `screen_capture` plus image input are enabled for it. Selecting Vision is
  detected reliably, including after switching conversations. Image attachment was
  also fixed: it used to stage the same image multiple times and never send,
  because the upload went through a paste that only made a local preview and never
  uploaded the file. It now uses DeepSeek's real file upload and sends once the
  upload completes.

## [1.4.6] - 2026-07-19

### Fixed
- **Kimi's login and "priority queue" popups were covered by the ZeroScript
  bar**: both render as full-screen fixed masks (`.login-modal-mask` and
  `.modal-mask`) rather than a standard `[role="dialog"]`, so the generic
  overlay probe used by other providers never caught them. The anchored bar
  (a full-width fixed element hugging the composer) and the "unstable"
  warning pill sat on top of the mask and could intercept clicks meant for
  its buttons (e.g. "Continue with Google"). Added a Kimi-specific
  `overlayBlocking()` that detects both mask classes by real visibility; the
  core already hides the whole bar while it reports true, and restores it the
  instant the mask clears.

### Added
- **Kimi now defaults fresh chats to K2.6**: Kimi lands new chats on K3,
  which is flagged unstable here and easy to miss switching away from. A
  brand new or emptied chat now picks K2.6 automatically, once; a deliberate
  manual switch to K3 on that same chat is left alone.

## [1.4.5] - 2026-07-18

### Fixed
- **DeepSeek re-executed old tool commands when scrolling up in a long
  conversation**: DeepSeek virtualizes its message list, so scrolling up makes
  an OLD command turn the last *rendered* assistant turn - its injected result
  sits below the fold (unrendered), the in-memory "already executed" record is
  empty after a page reload, and the node change makes generation detection
  flicker true, refreshing the auto-resume watchdog's freshness clock. The
  watchdog then re-fired the historical tool. Three-layer fix (validated live):
  - The off-DOM executed/halted dedupe maps now key on a virtualization-stable
    per-turn id (`itemKey`, DeepSeek's `data-virtual-list-item-key`) instead of
    the positional assistant index, which collides across scroll windows.
  - The watchdog skips any command turn whose stable id is below the session's
    high-water mark (`A.maxTurnId`) - a scrolled-back old turn can never resume,
    even right after a reload (`resume.skipOld` in the diag ring).
  - The watchdog also skips a command turn whose injected result is rendered
    right below it (settled history), a provider-generic guard.
- **Gemini stranded a tool result in the composer ("Message could not be
  sent")**: after a generation ends, Gemini's action button can stay WEDGED on
  the stop icon instead of reverting to the send arrow. The loop's generation
  *detection* already tolerates this (WEDGE_MS), so the tool ran, but the *send*
  waited for an `arrow_upward` button that never appeared - four retries failed
  and the injected result sat unsent in the composer. `typeAndSend` now resets a
  frozen stop button (clicking it, guarded by the same not-actually-generating
  check) so the send button reappears, then sends (validated live). The native
  stop-click hook now ignores non-trusted (programmatic) clicks, so this
  un-wedge click is never mistaken for the user halting the agent - otherwise
  the next legitimate command was wrongly marked "stopped".

## [1.4.4] - 2026-07-16

### Fixed
- **Qwen fired tool commands mid-stream ("Bad JSON" while Qwen was still
  writing)**: Qwen's frontend update (fe 0.2.73) now emits `status:"finished"`
  in its SSE stream ~12s before the stream actually closes. The network tap
  treated that as the turn's end, so a still-incomplete command (e.g. an
  unclosed `###LUA###` block) was extracted and sent, and the loop's premature
  "unclosed" feedback was injected while the model kept writing. Fixed by no
  longer treating `status:"finished"` as done, and by having generation
  detection check the DOM stop button first (validated live: it now tracks the
  real stream end closely, unlike its old ~6s lag).

### Changed
- **Removed the "⚠ unstable" badge on Qwen's Auto/Think modes**: those modes
  used to make Qwen claim a tool "does not exist" without even trying it. The
  extension never force-switches Qwen's mode, so Auto (Qwen's own default) is
  left untouched.

## [1.4.3] - 2026-07-15

Adds a seventh AI provider (Meta AI) and fixes a Qwen tool-turn regression, plus
further Studio-port recovery hardening and a friendlier system prompt.

### Added
- **Meta AI (www.meta.ai) as a provider**: full ZeroScript support on Meta AI -
  new `providers/meta.js`, manifest content script + host permissions, and the
  provider switcher entry. Handles Meta's React DOM: reasoning ("Réflexion")
  chain-of-thought is excluded from the read text, the interactive JSON viewer
  and collapsible code blocks are masked so a streamed command never flashes, and
  the composer card is fully covered while typing. Meta AI accepts very large
  prompts, so no Qwen-style send cap is needed.

### Fixed
- **Qwen tool result took ~30s to inject on every tool turn**: Qwen dropped the
  assistant turn's own `id`, so `lastAssistantId()` returned null and the core
  fell back to the virtualized flat count, waiting the full ~30s NO_TURN_GRACE
  each turn. It now reads the stable `chat-response-message-<uuid>` descendant
  (with the old id kept as a fallback).
- **Qwen refused oversized messages**: a large tool result past Qwen's 131072
  character composer cap silently wedged the loop in the input box. Outgoing text
  is now truncated to a safe margin, keeping the head and tail and marking the gap
  so the model does not re-run the command.

### Changed
- **Friendlier, less restrictive system prompt**: the "do not use native tools"
  wording is reframed as a technical note (the site's own sandbox cannot reach the
  user's Studio) rather than a hard prohibition, with an explicit "you can act
  directly in the user's project" section. Reduces provider refusals.
- **Studio-port recovery hardening**: PID-based reclaim of leftover `StudioMCP`
  zombies and clearer, de-duplicated action banners on top of the 1.4.2 port
  hijack recovery.

## [1.4.2] - 2026-07-13

Follow-up robustness fixes for the Studio-connection failures the 1.4.1 work
did not cover: a rare "0 tools that survives every restart" deadlock, and a
third-party app silently hijacking Studio's MCP port.

### Fixed
- **A third-party app (e.g. ropilot) hijacking Studio's MCP port**: whichever
  program binds Studio's MCP port (13469) FIRST wins it, and if that is not
  Studio, `StudioMCP.exe` connects to the wrong host - the handshake succeeds
  but no tools ever appear. A PC reboot never helps because the offending app
  restarts with Windows and can grab the port before Studio again. The existing
  one-shot port check at boot could miss it. The bridge now detects the hijack
  from an unmistakable, timing-independent signal - `StudioMCP.exe` reporting it
  cannot parse the host's messages on that port - then kills the offending
  process (by port owner, with a fallback that kills the known squatter by
  name), restarts the proxy, and tells the user which app to uninstall or remove
  from Windows startup so it stops coming back. It never stays silent: if it
  cannot identify or kill the squatter it prints how to find it by hand.
- **`_port_owner` was IPv4-only**: the internal port-owner probe ran
  `netstat -p TCP`, so a squatter listening on IPv6 loopback was invisible to
  it; it now scans TCP and TCPv6.
- **A missing custom-MCP command (e.g. `uvx` not installed) looked like an
  endless silent restart loop**: when a configured server's command could not
  be found on PATH, the process never started, so there was no exit code and no
  stderr, and the crash-loop banner printed "the server printed no error output
  before dying". The bridge now catches the launch failure and names the real
  cause ("command not found: 'uvx' ...") both on the first attempt and in the
  crash-loop banner, while auto-restart keeps retrying in case the dependency
  is installed later.

### Changed
- After killing a port squatter, the "toggle Studio's MCP server OFF/ON"
  instruction now prints IMMEDIATELY (right after the kill) instead of only
  after the ~48s server-launch grace loop - so the user acts within seconds
  instead of staring at a seemingly-idle terminal for a minute. Toggling early
  also lets the grace loop pick up the tools and go green right away.
- **0 tools that no restart could fix**: if a `StudioMCP.exe` from a crashed
  session kept listening on Studio's MCP port (13469), reopening Studio made
  its MCP plugin do its one-shot registration against that *zombie* process.
  Because a Studio window was now running, both existing cleanups skipped it
  (the orphan-killer only acts when no Studio runs; the port check treats any
  Roblox-path owner as legitimate), so our fresh proxy could never own the
  port - 0 tools forever, unfixable by restarting Studio or the bridge in any
  order. The bridge now identifies the port owner by process ID: a
  `StudioMCP.exe` holding the port that this bridge did not launch (outside our
  own process tree) is a leftover by definition, so it is killed and the proxy
  restarted - at boot and again in the live watcher if the catalogue stays
  empty with Studio open. It then tells the user the one action that finishes
  recovery: open Assistant Settings > MCP Servers so Studio re-registers. If
  the process tree can't be read, nothing is killed (a healthy connection is
  never put at risk).
- The extension now tells non-technical users to "Run start.bat" instead of
  "Run python bridge.py" / "Run the ZeroScript bridge" in the offline panel,
  popup, and startup banner, matching the one-click launcher the README ships.

## [1.4.1] - 2026-07-11

Robustness release focused on the Roblox Studio connection lifecycle. Every
fix below was reproduced and validated live against a real Studio + Blender
setup, including the Roblox-side bugs reported on the devforum (StudioMCP
stale-pipe disconnects, MCP toggle turning off after a Studio update).

### Fixed
- **Phantom "Studio connected" state**: leftover `StudioMCP.exe` processes
  from a previous session or a Studio crash kept answering the bridge as if a
  Studio were attached, so the terminal and the extension showed green with
  Studio fully closed. The bridge now kills orphaned `StudioMCP.exe` at boot
  (only when no real Studio window exists, so a live connection can never be
  hit), and the boot banner re-confirms a positive probe before announcing a
  connection.
- **Status dot stuck green with Studio closed**: when StudioMCP advertised an
  empty tool catalogue (Studio closed at launch), the connectivity probe
  returned "unknown" instead of "disconnected", and the extension's
  don't-degrade-on-unknown rule kept the dot green forever. An alive Roblox
  proxy with an empty catalogue is now an authoritative "not connected".
- **Studio opened after the bridge was never detected** (yellow until a full
  bridge restart): two combined causes. (1) Nothing ever re-asked for the
  tool catalogue once the launch-time retry window expired - the watcher now
  re-polls `tools/list` while the catalogue is empty, so a late-attaching
  Studio is picked up within seconds. (2) Studio's MCP plugin registers with
  the MCP channel exactly ONCE (late in Studio's boot, or when the Assistant
  Settings > MCP Servers panel is opened/toggled) and never retries; the
  bridge's own recovery restarts could kill the MCP listener at that exact
  moment, permanently orphaning the plugin. The bridge no longer restarts the
  Roblox proxy while a Studio window is running, and both the terminal and
  the extension now say the one thing that actually fixes an orphaned
  plugin: open Assistant Settings > MCP Servers in Studio (validated three
  times live; a proxy-side restart provably cannot repair it).
- **Watcher crash silently disabling all Studio monitoring**: an unbound
  variable in the place-churn detector could kill the background watcher
  right after a reconnect, silently stopping every status update until the
  next bridge restart. Fixed, and both watchers are now supervised: a crash
  is logged in red and the watcher restarts itself in 5 seconds.
- Boot/connection messages no longer blame the merged multi-server tool count
  on Roblox ("49 tools loaded but NO Roblox Studio connected" when 22 of
  those were Blender's): every Roblox-specific message now uses the
  Roblox-only count.

### Added
- **Fast startup with addon servers**: MCP servers now launch in parallel and
  the extension-facing socket opens immediately, so a slow or absent Roblox
  Studio no longer delays Blender (or any addon) by up to a minute. The
  Roblox diagnostic continues in the background and the bridge pushes status
  updates to already-connected extensions as servers come up - previously an
  extension that connected early could keep a stale "addon offline" snapshot
  forever (greyed Start button instead of the orange degraded start).
- **Self-healing for Roblox's own disconnect bugs**: sustained loss of the
  Studio connection (stale named-pipe state, periodic silent disconnects)
  now auto-restarts the Roblox proxy - but only when no Studio window is
  running, where it is safe and effective.
- **Studio-update detection**: when a disconnect coincides with a new Studio
  version folder appearing, the terminal says Studio likely turned its MCP
  toggle off after updating (a known Roblox bug) and points at the exact
  setting, instead of retrying a recovery that cannot work.
- Extension messages distinguish "Roblox Studio is not running" from "Studio
  is running but not connected" (new `studio_proc` status field), each with
  its own corrective step.
- Terminal spinner during slow startup phases (server launch, Studio
  attach), so the console never looks frozen; only one spinner animates at a
  time.
- start.bat hardening: refuses to run from an unextracted ZIP, handles
  missing winget, rescans install folders after a winget install (PATH not
  refreshed), prints the Python version and the bridge's exit code on
  screen, and logs the Windows build - so a single screenshot of the
  terminal carries enough context for support.

## [1.4.0] - 2026-07-08

### Added
- Multi-MCP addon servers (experimental): a new "MCP servers" section in the
  panel menu lets you add or remove additional MCP servers (Blender,
  Sketchfab, or any local MCP command) alongside the always-primary Roblox
  Studio connection. The bridge rewrites `config.json` and restarts itself to
  load a change; Roblox stays protected from edits/removal and its status dot
  is scoped to Roblox alone so an addon going down never misrepresents the
  primary connection. New `list_mcp_servers` command and a `server` param on
  `list_commands` let the model discover and use addon tool sets on demand.
  When Roblox is down but an addon server is alive, the panel now offers a
  degraded start instead of refusing to start at all.
- Vision support (screen_capture / other tool-returned images) enabled for
  Arena, Gemini, GLM, Kimi and Qwen, each with a real "upload finished" signal
  before sending instead of trusting the first local preview, fixing several
  silent-attachment-drop and duplicate-attachment-on-retry bugs. A tool from
  any connected server that returns an image now gets the camera chip and is
  remembered for future calls, even for a custom MCP server whose name gives
  no hint it returns images.
- Parser: a JSON command cut off by the model's own output limit, missing
  only its trailing closing brackets, is now auto-completed and executed
  instead of failing with a parse error and forcing a full retry turn.
  Strictly refuses to salvage anything where real content (not just closers)
  was cut off.
- Per-reason parse-error feedback (cut off, bad JSON, missing ###LUA###
  opener, wrong envelope) instead of one generic "bad JSON" message, so the
  model fixes the actual problem instead of guessing.

### Fixed
- DeepSeek: a command's chip could show green "done" while DeepSeek was still
  streaming the reply, on back-to-back calls to the same tool. Caused by
  DeepSeek's list virtualization defeating the turn-count identity guard;
  fixed with a stable per-turn id.
- GLM: new "scroll to bottom" buttons were mistaken for the Stop button and
  permanently latched generation state to "busy." Raw command JSON could leak
  into the visible reply when nested inside a paragraph. An image filename
  could corrupt result-chip detection.
- Kimi: added detection of Kimi's own native "Agent" mode, which conflicts
  with ZeroScript's command protocol; Start is disabled with a warning until
  it's turned off. Fixed the hidden file-upload input not existing until the
  "+" menu is opened, raw command text leaking when nested/oversized, and
  normal model prose containing "try again" being misread as a site error.
- Qwen: same "try again" false-busy fix as Kimi. A/B "carousel" comparison
  turns (where the composer disappears mid-carousel) now auto-resolve to
  Response 1 once both candidates finish, instead of stalling or misreading a
  candidate as a truncated command.
- Arena: send is now confirmed until the composer actually clears instead of
  trusting a single click, preventing stranded messages/attachments; the chip
  now anchors below the reply text instead of floating above it.
- A command turn abandoned mid-stream (reload, or superseded by a
  regenerate) no longer shows a false green checkmark; it now shows a
  neutral "not run" state instead.
- A tool's own in-body error (e.g. "Output of '...': Error executing code...")
  now settles the chip red instead of green, even when the tool didn't use
  ZeroScript's own ERROR wrapper.
- Regenerating a stopped command no longer briefly re-shows the old call's
  chip before the new one streams in.

### Changed
- The version number next to the ZeroScript name in the panel is now small,
  plain text instead of a bordered green badge.
- System prompt updated to cover multiple MCP servers: the model must call
  `list_mcp_servers` before assuming something outside Roblox is unsupported,
  and the tool list is no longer inlined in the prompt (fetched on demand via
  `list_commands`).

## [1.3.9] - 2026-07-04

### Fixed
- Bridge: kill the full process tree on restart instead of just the wrapper
  process, which used to leave orphaned StudioMCP.exe instances behind that
  fought the next launch and caused seemingly random "Studio looks connected
  but nothing responds" failures.
- Bridge: a dead MCP server is now auto-restarted by a background watchdog
  instead of waiting for the next tool call to notice.
- Bridge: a tool call that hits one of Studio's own brief connection blips now
  retries once instead of surfacing a spurious "Studio not connected" error.
- Extension: the status bar no longer shows a falsely healthy "N tools" label
  when the agent is active but Studio, the place, or the bridge itself isn't
  actually usable, it now names the real blocker (open a place / enable the
  MCP server / bridge offline).
- Cross-provider: DeepSeek, Gemini, Kimi, GLM and Qwen composer menus, model
  pickers and tooltips (including GLM's search hover card and Kimi's model
  popover) no longer render clipped or hidden behind ZeroScript's own
  bar/pill/cover.
- Cross-provider: a thinking model quoting command JSON in its own reasoning
  area no longer makes the tool chip flap between done/run/done (Gemini, Kimi,
  GLM and Qwen).
- The "Agent is working" composer cover now blocks clicks into the composer
  underneath it instead of letting them through, and can no longer balloon
  past the composer's visible band or drag itself off position when a site
  recreates its editor node mid-session (seen on Kimi).
- A command chip could briefly flash or restart its spinner when revisiting a
  past turn; it now settles to done correctly instead.
- DeepSeek: the raw system-prompt turn no longer flashes for a frame before
  being hidden.
- Gemini: "New chat" no longer gets stuck on "Agent active" from a reused
  previous conversation URL.
- Kimi: reasoning is read separately from the actual reply, so a command
  drafted while the model is still "thinking" is no longer detected or
  executed; input can no longer be typed mid-run after the editor node is
  recreated.
- Arena: unsupported-mode gate now also covers Web Search and Generate Image,
  and chip alignment is fixed when a command turn renders as an A/B
  model-comparison carousel.
- Bridge: a long-running tool call no longer starves the connection's ping
  handling and trips the half-open-socket watchdog.

### Changed
- Bridge and installer logs moved to `logs/bridge_debug.log` and
  `logs/start.log`; the console now only shows what a user actually needs to
  read, full detail still lands in the log files.
- `start.bat` now detects and explains a double launch instead of silently
  replacing the previous instance, and warns clearly if port 17613 stays held
  after trying to free it.
- Removed remaining em dashes from user-visible strings.
- Removed remaining em dashes from user-visible strings.

## [1.3.3] - 2026-06-24

### Fixed
- Bridge no longer depends on Roblox's `mcp.bat`, which hard-coded a single
  Studio version path and broke (0 tools / "Bridge or Studio offline") once
  Studio auto-updated and that version folder was removed. A new
  `launch_studio_mcp.py` finds the newest installed `StudioMCP.exe` and launches
  it directly.
- `bridge.py` now runs a `.py` MCP command with the same Python interpreter as
  the bridge, so it works on installs where only the `py` launcher exists.

## [1.0.0] - 2026-06-09

### Added
- Initial public release of ZeroScript Free
- Browser extension for Chrome and Edge (DeepSeek chat integration)
- Local Python bridge (`bridge.py` + `start.bat`) for Roblox Studio communication
- Built-in MCP server support (no plugin required - activate directly in Roblox Studio)
- Read and edit Luau scripts directly from DeepSeek chat
- Run Luau code in real time inside Roblox Studio
- Inspect game tree and instances
- Generate meshes, materials, and models
- Browse and insert assets from the Creator Store
- Control play-testing from chat
- Panel status indicator (green / yellow / grey)
- Auto kill port 17613 on start to avoid conflicts
- Ko-fi support link with Robux tip passes in the extension panel
- Setup tutorial video on YouTube
