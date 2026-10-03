#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Termux_Doctor.sh - "curl doesn't work": find WHICH stage is broken, in order.
# Run it in the ZeroScript folder, in the SAME shell you exported settings in:
#     sh Termux_Doctor.sh  [https://your-tunnel-url]   (tunnel URL optional)
# It only READS (never starts/stops anything) and prints ok/BAD per stage.
# Share the output when asking for help - it replaces ten questions.

PORT="${ZS_API_PORT:-17614}"
TOKEN="${ZS_API_TOKEN:-}"
HOST_SHARE="${ZS_API_HOST:-}"
FAIL=0

say() { printf '%s\n' "$*"; }
hdr() { printf '\n[%s] %s\n' "$1" "$2"; }
ok()  { say "  ok   - $*"; }
bad() { say "  BAD  - $*"; FAIL=1; }
inf() { say "  note - $*"; }

# curl exists? (Termux: pkg install curl)
if ! command -v curl >/dev/null 2>&1; then
    say "curl is not installed - run: pkg install curl"; exit 1
fi
GET() { curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$@" 2>/dev/null; }

# ── 1. are these the new files? ─────────────────────────────────────────────
# TWO distinct generations are checked, because "has the HTTP API" is NOT the
# same as "current": the branch the doctor used to link (01a02d7b) already had
# sharing + keyless chat but still answered `stream:true` with a 400, so a user
# on it read "bridge.py is the new one" and then watched every streaming app
# fail. The doc link below points at the branch that carries the SSE fix.
NEW_ZIP="https://github.com/hipliteidk-glitch/ZeroScript-Free/archive/refs/heads/arena/01a1003e-zeroscript-free.zip"
hdr 1 "files (sharing + keyless chat + SSE streaming live in the new zip)"
if ! grep -q "SHARED HTTP API" bridge.py 2>/dev/null; then
    bad "OLD bridge.py (no HTTP API at all) - redownload and start from THAT folder:"
    inf "$NEW_ZIP"
elif ! grep -q "_send_sse" bridge.py 2>/dev/null; then
    bad "bridge.py has the HTTP API but PREDATES SSE streaming - apps that stream get HTTP 400 'streaming is not supported'. Redownload and restart:"
    inf "$NEW_ZIP"
    inf "(the old string 'streaming is not supported' must be GONE:  grep -c '_send_sse' bridge.py)"
else
    ok "bridge.py is current (HTTP API + SSE streaming)"
fi

# ── 2. is anything even running? ────────────────────────────────────────────
hdr 2 "processes"
if pgrep -f "bridge.py" >/dev/null 2>&1; then
    ok "bridge.py running:  $(pgrep -fa bridge.py | head -1)"
else
    bad "bridge.py NOT running (Android may have killed it - start: sh Termux_Start.sh)"
fi
if pgrep -f "relay.py" >/dev/null 2>&1; then
    ok "relay.py running (in-game path)"
else
    inf "relay.py NOT running - only needed for in-game Roblox control, not for /chat"
fi

# ── 3. settings in THIS shell ───────────────────────────────────────────────
hdr 3 "settings in THIS shell (exports die when the session closes)"
[ -n "$TOKEN" ] && ok "ZS_API_TOKEN is set" || inf "ZS_API_TOKEN empty (ok for phone-local use; REQUIRED for sharing)"
if [ -n "$HOST_SHARE" ]; then
    ok "ZS_API_HOST=$HOST_SHARE (LAN sharing requested)"
    if [ -z "$TOKEN" ] && [ "${ZS_API_OPEN:-}" != "1" ]; then
        bad "...but no ZS_API_TOKEN and no ZS_API_OPEN=1: the bridge REFUSES this and stays loopback"
    fi
else
    inf "ZS_API_HOST not set (loopback only - fine when you go through a tunnel)"
fi

# ── 4. the API on the phone itself ──────────────────────────────────────────
hdr 4 "API on this phone"
code=$(GET "http://127.0.0.1:$PORT/health")
if [ "$code" = "200" ]; then
    ok "/health answers on 127.0.0.1:$PORT"
else
    bad "nothing on 127.0.0.1:$PORT (curl code $code, 000=refused) - read the bridge's own first lines in its window"
fi
if [ "$code" = "200" ]; then
    if [ -n "$TOKEN" ]; then
        st=$(GET -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/status")
    else
        st=$(GET "http://127.0.0.1:$PORT/status")
    fi
    case "$st" in
        200) ok "token accepted on /status" ;;
        401) bad "token REJECTED (401) - the bridge was started with a DIFFERENT ZS_API_TOKEN than this shell has; restart both consistently" ;;
        *)   bad "/status answered $st (unexpected)" ;;
    esac
fi

# ── 5. same-Wi-Fi sharing ───────────────────────────────────────────────────
hdr 5 "LAN sharing (friends on the same Wi-Fi)"
IP="$(ip -4 addr show scope global 2>/dev/null | awk '/inet /{sub(/\/.*$/, "", $2); print $2; exit}')"
[ -z "$IP" ] && IP="$(ifconfig 2>/dev/null | awk '/inet /{print $2; exit}')"
if [ -n "$HOST_SHARE" ] && [ "$HOST_SHARE" != "127.0.0.1" ] && [ "$HOST_SHARE" != "localhost" ]; then
    lan=$(GET "http://${IP:-127.0.0.1}:$PORT/health")
    if [ "$lan" = "200" ]; then
        ok "shared - people on this Wi-Fi curl:  curl -H 'Authorization: Bearer \$ZS_API_TOKEN' http://$IP:$PORT/status"
    else
        bad "not reachable on the phone's own LAN IP ($IP) - sharing never engaged (check the bridge log for 'SHARED HTTP API' vs 'REFUSED')"
    fi
else
    inf "off (ZS_API_HOST unset) - skip; use a tunnel to reach the phone instead"
fi

# ── 6. tunnel check (optional arg) ──────────────────────────────────────────
hdr 6 "tunnel (public URL)"
if [ -n "$1" ]; then
    turl="${1%/}"
    hc=$(GET "$turl/health")
    if [ "$hc" = "200" ]; then
        ok "tunnel reaches the phone"
        if [ -n "$TOKEN" ]; then
            st=$(GET -H "Authorization: Bearer $TOKEN" "$turl/status")
        else
            st=$(GET "$turl/status")
        fi
        case "$st" in
            200) ok "token accepted through the tunnel - you're done:  curl -X POST '$turl/chat' -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{\"message\":\"say hi\"}'" ;;
            401) bad "401 through tunnel - this shell's ZS_API_TOKEN differs from the bridge's" ;;
            *)   bad "tunnel /status answered $st" ;;
        esac
    elif [ "$hc" = "000" ]; then
        bad "tunnel URL dead/wrong (curl 000). Every new ssh/cloudflared run prints a NEW URL - use the FRESH one"
    else
        bad "tunnel /health answered $hc - wrong URL (or their browser warning page - curl should pass, though)"
    fi
else
    inf "not tested - re-run with your public URL:  sh Termux_Doctor.sh https://your-url"
fi

# ── 7. the keyless brain (for /chat without an LLM key) ─────────────────────
hdr 7 "keyless /chat brain"
code=$(GET "http://127.0.0.1:$PORT/status")
if [ "$code" = "401" ] && [ -n "$TOKEN" ]; then
    code=$(GET -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/status")
fi
if [ "$code" = "200" ]; then
    st="$(curl -s --max-time 6 -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/status" 2>/dev/null)"
    case "$st" in
        *'"extension":true'*|*'"extension": true'*) ok "extension connected - /chat works WITHOUT an LLM key (the extension brings the AI tab forward itself)"
            n=$(printf '%s' "$st" | sed -n 's/.*"extension_clients": *\([0-9]*\).*/\1/p')
            idle=$(printf '%s' "$st" | sed -n 's/.*"extension_idle_s": *\([0-9]*\).*/\1/p')
            [ -n "$n" ] && inf "chat-capable extension clients: $n (reloads leave stale ones; the bridge now picks the freshest)"
            # Screenshot-driven (2026-09-10): six stacked ZeroScript bars AND two
            # different bar layouts in ONE tab = more than one copy of the
            # extension is loaded at once (old + new unpacked folder both enabled,
            # or a second browser running it). The copies fight over the AI tab's
            # composer (mode toggles flip back and forth -> the "mode not ready"
            # banner, swallowed sends, relayed /chat landing on a half-dead
            # worker). One extension = one websocket client, period.
            if [ -n "$n" ] && [ "$n" -gt 1 ] 2>/dev/null; then
                bad "MULTIPLE ZeroScript clients are connected ($n) - you have duplicate extension copies or a second browser running ZeroScript; in chrome://extensions REMOVE every ZeroScript entry, load unpacked ONCE from THIS zip's zeroscript-extension/ folder, and close the second browser"
            fi
            [ -n "$idle" ] && inf "freshest client last spoke ${idle}s ago"
            want=$(sed -n 's/.*EXT_BUILD = "\([^"]*\)".*/\1/p' zeroscript-extension/background.js 2>/dev/null | head -1)
            got=$(printf '%s' "$st" | sed -n 's/.*"extension_builds": *\[\([^]]*\)\].*/\1/p')
            if [ -z "$got" ]; then
                bad "connected extension sends NO build tag = OLD build from a stale zip folder - in chrome://extensions REMOVE ZeroScript, then load unpacked from THIS zip's zeroscript-extension/ folder and reload the AI tab"
            elif [ -n "$want" ]; then
                case "$got" in
                    *"$want"*) ok "extension build matches this zip ($want)" ;;
                    *) bad "extension build is [$got] but this zip is [$want] - you reloaded an OLD folder; load THIS zip's zeroscript-extension/ and reload the AI tab" ;;
                esac
            else
                inf "extension build tag(s): $got"
            fi
            ;;
        *) bad "no extension connected to the bridge - open the AI chat tab with the ZeroScript extension, or set ZS_LLM_KEY" ;;
    esac
else
    inf "skipped (API unreachable above)"
fi

# ── 8. live keyless probe: run ONE tiny chat turn and watch it answer ───────
# Every other stage can be green while /chat still fails (stale extension
# socket, backgrounded tab, busy agent). This is the end-to-end truth.
# (Still read-only: a chat turn is exactly what your own curls already do.)
hdr 8 "live keyless /chat probe (one tiny turn, 40s cap)"
if [ "$code" = "200" ]; then
    inf "asking the bridge 'Reply with exactly: PONG' - the AI tab may pop to the front on its own; that is expected"
    pr="$(curl -s --max-time 40 -X POST "http://127.0.0.1:$PORT/chat" \
        -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
        -d '{"message":"Reply with exactly: PONG","timeout":35}' 2>/dev/null)"
    case "$pr" in
        *'"ok":true'*|*'"ok": true'*)
            ok "a real chat turn ANSWERED: $(printf '%s' "$pr" | cut -c1-180)"
            ;;
        *'"ok":false'*|*'"ok": false'*)
            bad "the turn FAILED - the bridge's own error IS the diagnosis:"
            inf "$(printf '%s' "$pr" | cut -c1-240)"
            ;;
        *)
            bad "NO ANSWER even after 40s (curl gave up) - the turn is still running somewhere:"
            inf "watch the bridge window: no '-> /chat' = request never arrived; '->' without '<-' = still inside the web chat (slow model / stuck tab); a '<- Ns ERR' line names the cause"
            ;;
    esac
else
    inf "skipped (API unreachable above)"
fi

printf '\n── result: %s ──\n' "$( [ $FAIL -eq 0 ] && echo 'all good (green ok everywhere)' || echo 'fix the BAD lines above, then re-run')"
exit $FAIL
