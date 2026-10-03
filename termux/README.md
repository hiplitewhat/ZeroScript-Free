# Termux shell server

**mcp_termux.py** gives the AI agent a shell and file access on the Android
device itself — next to the in-game Synapse tools. Registered **automatically**
by the bridge on Android/Termux (see `_ensure_android_local_servers()` in
`bridge.py`); no panel step needed.

```
AI chat → ZeroScript extension → ZeroScript bridge → Termux MCP (stdio)
                                                          │
                                                          ▼
                                                   host shell (/bin/sh or cmd.exe)
```

## Tools

| Tool | What it does |
|------|--------------|
| `termux.ping` | Health check: Python/platform info, Termux markers, uptime. |
| `termux.exec` | Run a shell command (`pip install …`, `python3 …`, `git pull`, `am …`, `termux-*` API commands). Returns exit code + stdout + stderr. |
| `termux.read_file` | Read a text file on the device (capped, size reported). |
| `termux.write_file` | Write/append a text file (parents created, byte count reported). |

## Rules of engagement

- Commands run **non-interactively** (stdin closed) via `/bin/sh -c` on
  POSIX/Termux and `cmd.exe /c` (or `%COMSPEC%`) on Windows, as the same
  user as the bridge — exactly what you could type yourself.
- Hard timeout: default 30s, max 120s. Output is capped (tail kept, so the
  error at the end survives).
- Long-lived processes must be detached or they are killed on timeout:
  `termux.exec` → `nohup python3 synapse/relay.py > relay.log 2>&1 &`
- Working directory defaults to the Termux home; override with `cwd`.

## Manual registration (PC / other platforms)

On desktop platforms nothing is auto-registered. To use it there, add it from
the ZeroScript panel: **MCP servers → Add server** — Name: `Termux`,
Command: `termux/mcp_termux.py`. It works anywhere Python runs; the safety
rails above still apply.
