-- SPDX-License-Identifier: GPL-3.0-or-later
-- synapse/synapse.lua
-- =============================================================================
-- Synapse - an in-game Luau executor + agent runtime for Roblox.
--
-- Run this inside any executor (Delta, Synapse-family, whatever you use) with:
--
--   local u = "http://127.0.0.1:18081"
--   loadstring(game:HttpGet(u .. "/synapse"))()
--
-- ...or paste synapse/setup_delta.lua instead - same result, but it walks every
-- HTTP transport with a per-attempt timeout, which matters on executors whose
-- game:HttpGet hangs forever in live games (seen live on Delta).
--
-- It long-polls the local Synapse relay, executes each queued command, and
-- reports the result back. It accepts:
--   * RAW Luau code  (evaluated with loadstring)
--   * Semantic JSON commands   {"op":"fire","name":"Farm","args":{...}}
--
-- Semantic commands are dispatched to the SYNAPSE table below, which is also
-- exposed as _G.SYNAPSE so your own injected code can call it directly.
-- The environment table (_G.SYNAPSE.Env) persists for the whole session.
-- =============================================================================

local Http       = game:GetService("HttpService")
local Players    = game:GetService("Players")
local Replicated = game:GetService("ReplicatedStorage")
local Workspace  = game:GetService("Workspace")
local LocalPlayer = Players.LocalPlayer
local RunService = game:GetService("RunService")

local RELAY = (_G.__SYNAPSE_RELAY or "http://127.0.0.1:18081"):gsub("/+$", "")

local SYNAPSE = _G.SYNAPSE or {}
_G.SYNAPSE = SYNAPSE
SYNAPSE.Relay = RELAY
SYNAPSE.Env = SYNAPSE.Env or {}
SYNAPSE.Hooks = SYNAPSE.Hooks or {}
SYNAPSE.Started = SYNAPSE.Started or false
SYNAPSE.Version = "1.1.0"
SYNAPSE.LastError = ""
SYNAPSE.LastStatus = nil
SYNAPSE.Stats = { polls = 0, errors = 0, mode = "unknown" }

-- -----------------------------------------------------------------------------
--  HTTP TRANSPORT
-- -----------------------------------------------------------------------------
-- CRITICAL: the loop must NOT use HttpService:GetAsync / PostAsync. Those are
-- SERVER-ONLY in Roblox: inside a live game, on the client, they fail silently
-- (the script appears "loaded" but never pulls a command). The setup script
-- loads via game:HttpGet, so that transport demonstrably reaches the relay.
--
-- Preference order:
--   1. executor-native  syn.request      (bypasses HttpService restrictions)
--   2. executor-native  http_request / request
--   3. Roblox client API game:HttpGet / game:HttpPost   (what setup_delta uses)
--   4. HttpService server APIs            (only works in Studio server context)
-- Every call records SYNAPSE.LastError / LastStatus so the failure is visible
-- in the Developer Console instead of silently swallowed.
-- -----------------------------------------------------------------------------

local function _has_global(name)
    local g = _G
    return type(g[name]) == "function"
end

local function _executor_request_with(fn)
    if not fn then return nil end
    return function(method, url, body)
        local call = {
            Url = url,
            Method = method,
            Headers = { ["Content-Type"] = "application/json" },
        }
        if body then call.Body = body end
        local ok, res = pcall(fn, call)
        if not ok or not res then return nil, nil, tostring(res or "request failed") end
        local status = res.StatusCode or res.statusCode or res.Status or res.status
        local response = res.Body or res.body or res.Content or res.content
        return status, response, nil
    end
end

local function _game_request()
    return function(method, url, body)
        local ok, out
        if method == "GET" then
            ok, out = pcall(function() return game:HttpGet(url, true) end)
        else
            ok, out = pcall(function() return game:HttpPost(url, body or "", "application/json") end)
        end
        if ok then return 200, out, nil end
        return nil, nil, tostring(out)
    end
end

local function _http_service_request()
    return function(method, url, body)
        local ok, out
        if method == "GET" then
            ok, out = pcall(function() return Http:GetAsync(url, true) end)
        else
            ok, out = pcall(function() return Http:PostAsync(url, body or "", "application/json") end)
        end
        if ok then return 200, out, nil end
        return nil, nil, tostring(out)
    end
end

local function _available_transports()
    local out = {}
    if type(_G.syn) == "table" and type(_G.syn.request) == "function" then
        out["syn.request"] = _executor_request_with(_G.syn.request)
    end
    if _has_global("http_request") then
        out["http_request"] = _executor_request_with(_G.http_request)
    end
    if _has_global("request") then
        out["request"] = _executor_request_with(_G.request)
    end
    out["game:HttpGet"] = _game_request()
    out["HttpService"] = _http_service_request()
    return out
end

local AVAILABLE = _available_transports()
local ORDER = { "syn.request", "http_request", "request", "game:HttpGet", "HttpService" }
local Transport, HTTP_MODE
local force = tostring(_G.__SYNAPSE_TRANSPORT or ""):lower()
if force ~= "" then
    local ALIAS = {
        syn = "syn.request", syn_x = "syn.request",
        http_request = "http_request", httprequest = "http_request",
        request = "request",
        game = "game:HttpGet", gamehttpget = "game:HttpGet",
        http_service = "HttpService", httpservice = "HttpService",
    }
    local target = AVAILABLE[force] or AVAILABLE[ALIAS[force]]
    if target then
        Transport = target
        HTTP_MODE = AVAILABLE[force] and force or ALIAS[force]
    else
        print("[synapse] unknown forced transport: " .. force)
    end
end
if not Transport then
    for _, name in ipairs(ORDER) do
        if AVAILABLE[name] then
            Transport = AVAILABLE[name]
            HTTP_MODE = name
            break
        end
    end
end
SYNAPSE.Stats.mode = HTTP_MODE or "none"

local function request(method, url, body)
    SYNAPSE.Stats.polls = SYNAPSE.Stats.polls + 1
    if not Transport then
        SYNAPSE.Stats.errors = SYNAPSE.Stats.errors + 1
        SYNAPSE.LastError = "no HTTP transport available"
        return nil, nil, SYNAPSE.LastError
    end
    local status, out, err = Transport(method, url, body)
    SYNAPSE.LastStatus = status
    if not status then
        SYNAPSE.Stats.errors = SYNAPSE.Stats.errors + 1
        SYNAPSE.LastError = err or ("HTTP " .. method .. " failed")
    else
        SYNAPSE.LastError = ""
    end
    return status, out, err
end

local function get(url)
    local status, body = request("GET", url)
    return body
end

local function post(url, data)
    local status, body, err = request("POST", url, data)
    return not not status, body, err
end

local function enc(s) return Http:UrlEncode(tostring(s or "")) end
local function dec(s)
    local ok, v = pcall(function() return Http:JSONDecode(s) end)
    return ok and v or nil
end

local function report(job_id, resp)
    local fields = {
        id  = job_id or "",
        ok  = resp and resp.ok or false,
        out = resp and resp.out or "",
        err = resp and resp.err or "",
    }
    local okenc, payload = pcall(function() return Http:JSONEncode(fields) end)
    if not okenc then
        -- A result field was not JSON-serialisable (an Instance, a function in a
        -- returned table, ...). Fall back to a string form so the executor never
        -- crashes the loop trying to hand a value back over the HTTP pipe.
        fields.out = tostring(fields.out)
        fields.err = tostring(fields.err)
        payload = Http:JSONEncode(fields)
    end
    local okp, _, perr = post(RELAY .. "/result", payload)
    if not okp then
        SYNAPSE.LastError = "result post failed: " .. tostring(perr or "unknown")
        SYNAPSE.Stats.errors = SYNAPSE.Stats.errors + 1
        print("[synapse] " .. SYNAPSE.LastError)
    end
end

local function ok(out)
    return { ok = true, out = out, err = "" }
end

local function fail(err)
    return { ok = false, out = "", err = tostring(err or "error") }
end

local function char_of()
    local c = LocalPlayer and LocalPlayer.Character
    return c, c and c:FindFirstChild("HumanoidRootPart"), c and c:FindFirstChildOfClass("Humanoid")
end

-- -----------------------------------------------------------------------------
--  HIGH-LEVEL SEMANTIC COMMANDS
-- -----------------------------------------------------------------------------

function SYNAPSE.ping()
    return ok({ relay = RELAY, version = SYNAPSE.Version, executor = true,
                player = LocalPlayer and LocalPlayer.Name or "none" })
end

function SYNAPSE.player_info()
    local char, hrp, hum = char_of()
    return ok({
        name         = LocalPlayer and LocalPlayer.Name,
        display_name = LocalPlayer and LocalPlayer.DisplayName,
        user_id      = LocalPlayer and LocalPlayer.UserId,
        health       = hum and hum.Health,
        max_health   = hum and hum.MaxHealth,
        position     = hrp and { x = hrp.Position.X, y = hrp.Position.Y, z = hrp.Position.Z },
        team         = LocalPlayer and LocalPlayer.Team and LocalPlayer.Team.Name,
        game         = game.PlaceId,
        job          = game.JobId,
    })
end

function SYNAPSE.game_tree(max)
    if type(max) == "table" then max = max.max end
    max = tonumber(max) or 250
    local root, count = Workspace, 0
    local stack = { root }
    local out = {}
    local paths = { [root] = "Workspace" }
    while #stack > 0 and count < max do
        local inst = table.remove(stack)
        count = count + 1
        local path = paths[inst] or inst.Name
        out[#out + 1] = {
            name     = inst.Name,
            class    = inst.ClassName,
            path     = path,
            children = #inst:GetChildren(),
        }
        local kids = inst:GetChildren()
        for i = #kids, 1, -1 do
            local k = kids[i]
            paths[k] = path .. "/" .. k.Name
            stack[#stack + 1] = k
        end
    end
    return ok({ count = count, truncated = count >= max, nodes = out })
end

local function resolve_ref(ref)
    if not ref or ref == "" then return nil end
    if ref:find("^%d+$") then return nil end
    local current = nil
    if ref:sub(1, 1) == "/" then
        current = Workspace
        ref = ref:sub(2)
    end
    local parts = {}
    for part in ref:gmatch("[^/%.]+") do parts[#parts + 1] = part end
    for i = 1, #parts do
        local name = parts[i]
        -- Absolute "Workspace/..." paths: the root label already set current.
        if current == Workspace and i == 1 and name == "Workspace" then
            -- skip root label, continue with its children
        elseif current == nil then
            for _, s in ipairs({ Workspace, Replicated, game:GetService("ServerStorage"),
                                 game:GetService("ServerScriptService"), game:GetService("Players") }) do
                local found = s:FindFirstChild(name)
                if found then current = found break end
            end
            if current == nil then return nil end
        else
            local found = current:FindFirstChild(name)
            if not found then return nil end
            current = found
        end
    end
    return current
end

function SYNAPSE.find(p)
    local want_name = p.name
    local want_class = p.class
    local max = tonumber(p.max) or 50
    local out = {}
    local roots = { Workspace, Replicated, game:GetService("ServerStorage"),
                    game:GetService("ServerScriptService"), game:GetService("Players") }
    local function scan(inst)
        if #out >= max then return end
        if (not want_name or inst.Name:lower() == want_name:lower() or
            inst.Name:lower():find(want_name:lower(), 1, true)) and
           (not want_class or inst:IsA(want_class)) then
            out[#out + 1] = { name = inst.Name, class = inst.ClassName,
                              path = inst:GetFullName() }
        end
        for _, c in ipairs(inst:GetChildren()) do scan(c) end
    end
    for _, r in ipairs(roots) do scan(r) end
    return ok({ count = #out, results = out })
end

local function args_of(p)
    local a = p.args
    if a == nil then return {} end
    if type(a) == "table" then return a end
    if type(a) == "string" then
        local decoded = dec(a)
        if type(decoded) == "table" then return decoded end
    end
    return { a }
end

function SYNAPSE.fire(p)
    local inst = resolve_ref(p.path or p.name)
    if not inst then return fail("remote not found: " .. tostring(p.path or p.name)) end
    if not inst:IsA("RemoteEvent") then return fail(inst.Name .. " is not a RemoteEvent (" .. inst.ClassName .. ")") end
    local args = args_of(p)
    pcall(function() inst:FireServer(unpack(args)) end)
    return ok({ fired = inst:GetFullName(), args = args })
end

function SYNAPSE.invoke(p)
    local inst = resolve_ref(p.path or p.name)
    if not inst then return fail("remote not found: " .. tostring(p.path or p.name)) end
    if not inst:IsA("RemoteFunction") then return fail(inst.Name .. " is not a RemoteFunction (" .. inst.ClassName .. ")") end
    local args = args_of(p)
    local result
    local done = false
    task.spawn(function()
        local okv, r = pcall(function() return inst:InvokeServer(unpack(args)) end)
        result = okv and r or r
        done = true
    end)
    local t0 = os.clock()
    local timeout = tonumber(p.timeout) or 8
    while not done and os.clock() - t0 < timeout do task.wait(0.05) end
    if not done then return fail("InvokeServer timed out after " .. timeout .. "s") end
    return ok({ invoked = inst:GetFullName(), result = result })
end

function SYNAPSE.teleport(p)
    local _, hrp = char_of()
    if not hrp then return fail("no character") end
    local x, y, z = tonumber(p.x) or 0, tonumber(p.y) or 0, tonumber(p.z) or 0
    hrp.CFrame = CFrame.new(x, y, z)
    return ok({ teleported = true, to = { x = x, y = y, z = z } })
end

function SYNAPSE.click(p)
    local vim = game:GetService("VirtualInputManager")
    local x = tonumber(p.x) or 0
    local y = tonumber(p.y) or 0
    local count = math.min(tonumber(p.count) or 1, 100)
    local delay = tonumber(p.delay) or 0.05
    for i = 1, count do
        vim:SendMouseButtonEvent(x, y, 0, true, game, 1)
        task.wait(0.02)
        vim:SendMouseButtonEvent(x, y, 0, false, game, 1)
        task.wait(delay)
    end
    return ok({ clicks = count, x = x, y = y })
end

function SYNAPSE.anti_kick(p)
    if SYNAPSE.Hooks.antiKick then
        return ok({ hooked = true, already = true })
    end
    local mt = getrawmetatable(LocalPlayer)
    setreadonly(mt, false)
    local old = mt.__namecall
    mt.__namecall = newcclosure(function(self, ...)
        if getnamecallmethod() == "Kick" then
            return
        end
        return old(self, ...)
    end)
    setreadonly(mt, true)
    SYNAPSE.Hooks.antiKick = true
    return ok({ hooked = true })
end

function SYNAPSE.spoof(p)
    local mt = getrawmetatable(LocalPlayer)
    setreadonly(mt, false)
    local old = mt.__index
    local user_id = tonumber(p.user_id)
    local name = p.name
    mt.__index = newcclosure(function(self, key)
        if self == LocalPlayer then
            if user_id and key == "UserId" then return user_id end
            if name and key == "Name" then return name end
        end
        return old(self, key)
    end)
    setreadonly(mt, true)
    SYNAPSE.Hooks.spoof = true
    return ok({ hooked = true, user_id = user_id, name = name })
end

function SYNAPSE.env_set(p)
    local k = tostring(p.key or "")
    if k == "" then return fail("key required") end
    SYNAPSE.Env[k] = p.value
    return ok({ key = k, value = p.value })
end

function SYNAPSE.env_get(p)
    local k = tostring(p.key or "")
    return ok({ key = k, value = SYNAPSE.Env[k] })
end

function SYNAPSE.env_list()
    local out = {}
    for k, v in pairs(SYNAPSE.Env) do out[#out + 1] = { key = tostring(k), value = v } end
    return ok(out)
end

function SYNAPSE.reset()
    for k in pairs(SYNAPSE.Env) do SYNAPSE.Env[k] = nil end
    for k in pairs(SYNAPSE.Hooks) do SYNAPSE.Hooks[k] = nil end
    return ok({ reset = true })
end

-- -----------------------------------------------------------------------------
--  DISPATCH
-- -----------------------------------------------------------------------------

local DISPATCH = {
    ping       = SYNAPSE.ping,
    diag       = SYNAPSE.diag,
    self_test  = SYNAPSE.self_test,
    player     = SYNAPSE.player_info,
    playerinfo = SYNAPSE.player_info,
    tree       = SYNAPSE.game_tree,
    game_tree  = SYNAPSE.game_tree,
    find       = SYNAPSE.find,
    fire       = SYNAPSE.fire,
    invoke     = SYNAPSE.invoke,
    teleport   = SYNAPSE.teleport,
    click      = SYNAPSE.click,
    antikick   = SYNAPSE.anti_kick,
    anti_kick  = SYNAPSE.anti_kick,
    spoof      = SYNAPSE.spoof,
    env_set    = SYNAPSE.env_set,
    env_get    = SYNAPSE.env_get,
    env_list   = SYNAPSE.env_list,
    reset      = SYNAPSE.reset,
}

function SYNAPSE.dispatch(p)
    local op = p and p.op
    if not op then return fail("missing op") end
    local fn = DISPATCH[op]
    if not fn then return fail("unknown op: " .. tostring(op)) end
    local okv, resp = pcall(fn, p)
    if not okv then return fail(resp) end
    return resp
end

function SYNAPSE.run_lua(code)
    if type(code) ~= "string" or code == "" then return fail("empty code") end
    local f
    local ok_load, loader = pcall(function() return loadstring(code) end)
    if not ok_load or not loader then
        return fail(ok_load and "loadstring returned nil" or tostring(loader))
    end
    local ok_run, result = pcall(loader)
    if not ok_run then
        return fail(tostring(result))
    end
    if result == nil then return ok("OK") end
    if type(result) == "table" or type(result) == "boolean" or type(result) == "number" then
        local okenc, s = pcall(function() return Http:JSONEncode(result) end)
        if okenc then return ok(s) end
    end
    return ok(tostring(result))
end

function SYNAPSE.diag()
    return ok({
        mode       = HTTP_MODE,
        relay      = RELAY,
        version    = SYNAPSE.Version,
        polls      = SYNAPSE.Stats.polls,
        errors     = SYNAPSE.Stats.errors,
        last_error = SYNAPSE.LastError,
        last_status = SYNAPSE.LastStatus,
        http_service_enabled = (function()
            local okp, val = pcall(function() return Http.Enabled end)
            return okp and val
        end)(),
    })
end

function SYNAPSE.self_test()
    local status, body, err = request("GET", RELAY .. "/health")
    if status and body then
        return ok({
            mode = HTTP_MODE,
            relay = RELAY,
            http_status = status,
            body = body,
            reachable = true,
        })
    end
    return fail("relay not reachable via " .. HTTP_MODE .. ": " .. (err or SYNAPSE.LastError))
end

function SYNAPSE.start()
    if SYNAPSE.Started then return SYNAPSE.Started end
    SYNAPSE.Started = true
    print("[synapse] loaded - relay " .. RELAY .. " - transport " .. HTTP_MODE)
    local _, health, herr = request("GET", RELAY .. "/health")
    if health then
        print("[synapse] relay health: " .. tostring(health))
    elseif herr then
        print("[synapse] relay health FAILED: " .. tostring(herr))
    end
    task.spawn(function()
        local idle = 0.5
        local lastErrPrinted = -1
        while true do
            local status, body, err = request("GET", RELAY .. "/cmd")
            if status and body and body ~= "" then
                local job = dec(body)
                if job and job.id and job.id ~= "" then
                    local resp
                    local payload = job.cmd or ""
                    local semantic = dec(payload)
                    if type(semantic) == "table" and (semantic.op or semantic.command) then
                        semantic.op = semantic.op or semantic.command
                        semantic.command = semantic.op
                        resp = SYNAPSE.dispatch(semantic)
                    else
                        resp = SYNAPSE.run_lua(payload)
                    end
                    report(job.id, resp)
                    idle = 0.25
                end
            elseif err and SYNAPSE.Stats.polls - lastErrPrinted > 8 then
                lastErrPrinted = SYNAPSE.Stats.polls
                print("[synapse] poll error: " .. tostring(err))
            end
            task.wait(idle)
        end
    end)
    return true
end

SYNAPSE.start()
