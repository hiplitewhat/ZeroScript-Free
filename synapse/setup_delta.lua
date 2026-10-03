-- SPDX-License-Identifier: GPL-3.0-or-later
-- synapse/setup_delta.lua
-- -----------------------------------------------------------------------------
--  Bootstrap: fetch the in-game runtime (synapse.lua) from the local relay and
--  start it. Paste this WHOLE file into Delta (or any executor), in-game.
--
--  Why not the old one-liner `loadstring(game:HttpGet(u.."/synapse"))()`?
--  Because game:HttpGet HANGS FOREVER on some executor/game combinations
--  (seen live: Delta in a live game - every bootstrap "timed out after 120s"
--  because the fetch never returned). synapse.lua itself already walks a
--  transport ladder once it is running; this loader now applies the SAME
--  ladder to the bootstrap download, and every attempt is capped with a
--  watchdog so a hanging transport (HttpGet above all) can never wedge the
--  loader again.
--
--  Transport order (same as synapse.lua):
--    1. syn.request      (executor-native, bypasses HttpService restrictions)
--    2. http_request
--    3. request
--    4. game:HttpGet     (last resort, hard-capped - this is the one that hangs)
--
--  Relay override (only if you changed SYNAPSE_RELAY_PORT / host):
--    _G.__SYNAPSE_RELAY = "http://127.0.0.1:18082"  -- set BEFORE running this
-- -----------------------------------------------------------------------------
local RELAY = _G.__SYNAPSE_RELAY or "http://127.0.0.1:18081"
local URL = RELAY .. "/synapse"
local ATTEMPT_CAP = 5  -- seconds per transport attempt

-- Call fn(url) in a separate thread and give up after ATTEMPT_CAP seconds.
-- A hanging call (HttpGet on a bad executor) leaks one dead thread instead of
-- freezing the whole bootstrap - the whole point of this file.
local function try_fetch(fn, url)
    local done, body = false, nil
    task.spawn(function()
        local ok, res = pcall(fn, url)
        if ok and res and res ~= "" then
            body = res
        end
        done = true
    end)
    local deadline = os.clock() + ATTEMPT_CAP
    while not done and os.clock() < deadline do
        task.wait(0.05)
    end
    return body  -- nil on error OR timeout
end

local function executor_fetch(fn)
    return function(url)
        local res = fn({ Url = url, Method = "GET" })
        return res and (res.Body or res.body)
    end
end

local attempts = {}
if type(syn) == "table" and type(syn.request) == "function" then
    table.insert(attempts, { "syn.request", executor_fetch(syn.request) })
end
if type(http_request) == "function" then
    table.insert(attempts, { "http_request", executor_fetch(http_request) })
end
if type(request) == "function" then
    table.insert(attempts, { "request", executor_fetch(request) })
end
table.insert(attempts, { "game:HttpGet", function(url) return game:HttpGet(url, true) end })

local code, used
for _, attempt in ipairs(attempts) do
    code = try_fetch(attempt[2], URL)
    if code then
        used = attempt[1]
        break
    end
    print("[synapse] bootstrap: " .. attempt[1] .. " failed/timeout, trying next transport")
end

if not code then
    warn("[synapse] bootstrap FAILED: no transport could reach " .. RELAY ..
        " - is the relay running? (Termux: sh Termux_Start.sh / python3 synapse/relay.py; " ..
        "check with: curl " .. RELAY .. "/health)")
    return
end

print("[synapse] bootstrap via " .. used .. " - starting runtime")
loadstring(code)()
print("[synapse] executor loaded")
