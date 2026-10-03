# ZeroScript Free - AI Roblox Studio Agent (DeepSeek, ChatGPT, Gemini, Kimi, GLM, Qwen, Arena, Meta AI)

Control Roblox Studio with AI, for free. ZeroScript turns a normal AI chat (DeepSeek, ChatGPT, Google Gemini, Kimi, GLM, Qwen, Arena, or Meta AI) into an agent that builds and scripts your Roblox game for you: just describe what you want, and it reads/edits scripts, runs Luau, inspects the game tree, and generates assets directly in Roblox Studio. No API key, no terminal, no coding required.

It's a Chrome/Edge browser extension plus a small local bridge that connects the chat to Roblox Studio through the official MCP server. **DeepSeek is the recommended provider.** ChatGPT, Gemini, Kimi, GLM, Qwen, Arena and Meta AI also work but can be less stable: Gemini tends to stop using the Roblox tools in long sessions, and Kimi sometimes reaches for its own native tools instead of the Roblox commands. On Arena, keep the mode dropdown on **Direct** (ZeroScript only supports Direct mode).

## Setup

**Load the extension manually (Edge or Chrome):**
1. Go to `edge://extensions` (Edge) or `chrome://extensions` (Chrome)
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked**
4. Select the `zeroscript-extension` folder
5. The extension is now active

**Then set up the Bridge:**
1. **Download the Bridge** from the [GitHub releases page](https://github.com/sebattfg/ZeroScript-Free)
2. **Open Roblox Studio** and load a Place
3. **Enable the MCP server in Roblox Studio** (first time only): click **Assistant AI** in the top bar, then **...** > **Manage MCP Servers** > **Enable Studio as MCP Server**
4. **Run the Bridge** - double-click `start.bat` (Windows) or `MacOS_Start.command` (macOS); a small window opens, the Bridge is running. On macOS, the first launch shows a Gatekeeper warning (normal for any downloaded script): click **Done**, then **System Settings > Privacy & Security**, scroll down, and click **Open Anyway**.
5. **Go to https://chat.deepseek.com** (recommended), https://chatgpt.com, https://gemini.google.com, https://www.kimi.com, https://chat.z.ai, https://chat.qwen.ai, https://arena.ai, or https://www.meta.ai, open a new chat (only works on these exact addresses; on Arena use Direct mode; on ChatGPT use a regular text chat, not its Agent mode)
6. Type what you want to build and hit send - the agent starts itself on your first message (Automatic send, on by default; menu → Behaviour to switch it off and use the **▶ Start** button instead). **Pace auto-replies** is also on by default (25s + jitter between injected turns, a 45–90s rest every 5th reply, 15-minute cooldown after 24 in an hour) so long sessions on the same DeepSeek/etc. account are less likely to get suspended; raise the interval in Behaviour if you still see rate-limit / suspend warnings. **Show live timers on message chips** (Behaviour, default on) turns the ticking seconds on chips off if you find them distracting.

That's it. On ChatGPT, sign in there if prompted. ChatGPT currently supports text/tool commands but not image attachments from `screen_capture`; the adapter hides vision-only tools rather than silently sending an image-free result.

📺 [Watch the setup tutorial](https://youtu.be/kPKiZLZ9_Ps)

## Architecture (for contributors)

The extension is split between a provider-agnostic core and per-AI-site providers:

```
core/config.js        system prompt, feedback strings, tool categories (global ZS)
core/parser.js        ZeroScript command parsing - pure string logic   (global ZSParse)
core/main.js          agentic loop, UI, camouflage, session state      (uses ZSProvider)
providers/deepseek.js everything DeepSeek-specific: DOM selectors, generation
                      detection, send mechanics, composer modes…       (global ZSProvider)
providers/chatgpt.js  same interface for ChatGPT (classic/new DOM shells,
                      ProseMirror input, command camouflage)           (global ZSProvider)
providers/gemini.js   same interface for Google Gemini (Angular DOM, Quill
                      composer, code-block masking)                    (global ZSProvider)
providers/kimi.js     same interface for Kimi / Moonshot AI (Vue DOM, Lexical
                      composer, segment-code masking)                  (global ZSProvider)
providers/glm.js      same interface for GLM / Z.ai (Svelte DOM, code-block
                      wrapper masking)                                 (global ZSProvider)
providers/qwen.js     same interface for Qwen / chat.qwen.ai (Vue DOM, network-tap
                      SSE stream, Monaco disposal guard)               (global ZSProvider)
providers/qwen-net.js MAIN-world fetch tap for Qwen SSE stream        (injected by manifest)
providers/arena.js    same interface for Arena / arena.ai (React DOM, multi-model
                      playground, A/B-comparison auto-commit, Direct-mode gate) (global ZSProvider)
providers/meta.js     same interface for Meta AI / meta.ai (React DOM, textarea
                      composer, JSON-viewer + code-collapse masking)   (global ZSProvider)
background.js         WebSocket to the local bridge (provider-agnostic)
```

`core/main.js` never touches the host site's DOM directly - it only calls the
`ZSProvider` interface. To integrate another AI site: write a new
`providers/<site>.js` exporting the same interface, then add its URL pattern to
`manifest.json` (`content_scripts` + `host_permissions`) and to
`PROVIDER_URLS` in `background.js`. No core change required.

Run `node test-parser.js` to smoke-test the command parser.

## Support

☕ [Ko-fi](https://ko-fi.com/sebattfg) - Robux tip passes available in the extension panel
