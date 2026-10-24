-- tmux-voice: hold a key to talk, release to transcribe.
-- Text goes to the active tmux pane when a terminal is frontmost, otherwise it is typed into the frontmost app.
-- Any other key pressed while holding cancels the recording.
--
-- Usage in ~/.hammerspoon/init.lua:
--   tmux_voice = dofile("/path/to/tmux-voice/hammerspoon/tmux_voice.lua").start({ root = "/path/to/tmux-voice" })

local M = {}

-- keycode and device-specific flag mask for each supported hold key
local KEYS = {
  rightalt   = { code = 61, mask = 0x40 },
  rightcmd   = { code = 54, mask = 0x10 },
  rightctrl  = { code = 62, mask = 0x2000 },
  rightshift = { code = 60, mask = 0x04 },
  fn         = { code = 63, mask = 0x800000 },
}

function M.start(opts)
  opts = opts or {}
  assert(opts.root, "tmux_voice.start: opts.root (path to the tmux-voice checkout) is required")
  local port = opts.port or 8766
  local key = KEYS[opts.key or "rightalt"] or error("tmux_voice: unknown key " .. tostring(opts.key))
  local minHold = opts.min_hold or 0.3
  local url = "http://127.0.0.1:" .. port
  local stateDir = os.getenv("HOME") .. "/.local/state/tmux-voice"

  local held, pressedAt, cancelled = false, 0, false
  local menubar = opts.menubar ~= false and hs.menubar.new() or nil
  local function title(t) if menubar then menubar:setTitle(t) end end
  title("mic")

  local function post(path, body, cb)
    hs.http.asyncPost(url .. path, hs.json.encode(body or {}), { ["Content-Type"] = "application/json" },
      function(status, resp) if cb then cb(status, resp) end end)
  end

  -- Daemon supervisor: a login shell finds uv on the user's PATH.
  local restarts = 0
  local function startDaemon()
    local cmd = "mkdir -p '" .. stateDir .. "'; lsof -ti tcp:" .. port .. " | xargs kill 2>/dev/null; cd '" .. opts.root ..
      "' && exec uv run --quiet tmux-voice run >> '" .. stateDir .. "/daemon.log' 2>&1"
    M.task = hs.task.new("/bin/zsh", function()
      restarts = restarts + 1
      if restarts <= 5 then hs.timer.doAfter(2 * restarts, startDaemon)
      else hs.alert.show("tmux-voice daemon keeps crashing, see " .. stateDir .. "/daemon.log") end
    end, { "-lc", cmd })
    M.task:start()
  end
  startDaemon()
  M.resetTimer = hs.timer.doEvery(300, function() restarts = 0 end)

  -- Watchdog: if the daemon stops answering, kill it; the task exit callback restarts it.
  local pendingPing = false
  M.watchdog = hs.timer.doEvery(15, function()
    if held then return end
    if pendingPing then
      print("tmux-voice: daemon not responding, restarting")
      hs.execute("lsof -ti tcp:" .. port .. " | xargs kill -9", true)
      pendingPing = false
      title("mic")
      return
    end
    pendingPing = true
    hs.http.asyncGet(url .. "/status", nil, function(status)
      if status == 200 then pendingPing = false end
    end)
  end)

  local function onDown()
    held, pressedAt, cancelled = true, hs.timer.secondsSinceEpoch(), false
    title("● REC")
    post("/start")
  end

  local function onUp()
    held = false
    if cancelled then return end
    if hs.timer.secondsSinceEpoch() - pressedAt < minHold then
      post("/cancel"); title("mic"); return
    end
    title("…")
    local app = hs.application.frontmostApplication()
    post("/stop", { app = app and app:bundleID() or "" }, function(status, resp)
      title("mic")
      if status ~= 200 then hs.alert.show("tmux-voice: daemon error " .. tostring(status)); return end
      local r = hs.json.decode(resp) or {}
      if r.error then hs.alert.show("tmux-voice: " .. r.error) end
      if not r.inserted and r.text and r.text ~= "" then hs.eventtap.keyStrokes(r.text) end
    end)
  end

  M.flagsTap = hs.eventtap.new({ hs.eventtap.event.types.flagsChanged }, function(e)
    if e:getKeyCode() ~= key.code then return false end
    local down = (e:rawFlags() & key.mask) ~= 0
    if down and not held then onDown() elseif not down and held then onUp() end
    return false
  end)

  M.keyTap = hs.eventtap.new({ hs.eventtap.event.types.keyDown }, function()
    if held and not cancelled then
      cancelled = true
      post("/cancel")
      title("mic")
    end
    return false
  end)

  M.flagsTap:start()
  M.keyTap:start()
  -- macOS disables event taps that stall; re-enable them.
  M.tapTimer = hs.timer.doEvery(5, function()
    if not M.flagsTap:isEnabled() then M.flagsTap:start() end
    if not M.keyTap:isEnabled() then M.keyTap:start() end
  end)

  if not hs.accessibilityState() then
    hs.alert.show("tmux-voice: grant Hammerspoon Accessibility, then quit and reopen Hammerspoon", 8)
  end
  return M
end

return M
