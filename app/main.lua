-- Kinlings: raise a creature that only eats what other people give it.
-- Alone, you forage snacks from the badges around you. Together, you swap
-- them. Each new person who feeds your creature helps it grow and joins your
-- pack, and ABCD members hand out something for it to wear.
--
-- A badge app has 48 KB of memory, code included, so this file holds only
-- what must always be there: the saved state, the radio and the half-second
-- beat. Each screen is a file in ui/ that is loaded when it's shown and
-- dropped when you leave it; one-off actions (a swap's messages, what a swap
-- does) are in act/, loaded when they happen. They share the globals set here.

SPECIES = {"Sprig", "Ember", "Nimbu", "Glim"}
GOALS = {2, 6, 12, 25, 50, 100}     -- people who fed it; the first two change its shape
GLOW = {0x14F195, 0xFF754A, 0x80ECFF, 0x9945FF}
BASKET, PACK = 6, 150                -- snacks carried, people remembered

-- The saved state. A big pack must stay small in memory, so people are kept
-- as a few strings: ids has 6 characters a person, fl (flags: "1" fed us, "2"
-- an ABCD member, "3" both) and ps (their species) one each, nm a line each.
-- sp: our species, g: how many people fed us, bk: the basket, one digit a
-- snack, it: hats collected, w: the one worn, fo: badges foraged from lately,
-- lf: when we last ate.
S = storage.get("s") or {}
for k, v in pairs({g = 0, bk = "", it = "", ids = "", fl = "", ps = "", nm = "", fo = ""}) do
  if S[k] == nil then S[k] = v end
end

ME = badge.id() or "------"
peers, npeers = {}, 0               -- badges nearby with the app open: id -> {n, s, g, t}
beat, glow, glow_c = 0, 0, 0
view = {}                           -- the screen showing: its key, beat and refresh functions
answered, nanswered = {}, 0         -- what we answered to whom, in case they ask again
-- out: the swap we asked for {to, x, k, t, n}; inc: the one we're being asked {from, x, k}
local lean = 0

function save()
  storage.set("s", S)
end

-- Text from another badge is shown on screen and saved: printable ASCII only.
function clean(s, alt)
  if type(s) ~= "string" then return alt end
  s = (s:gsub("[^\32-\126]", "")):sub(1, 12)
  return #s > 0 and s or alt
end

NAME = clean(badge.owner(), ME)

-- ABCD members: "\n<badge id><item number>" for each (data/members.txt).
local MEM = "\n" .. (fs.read("data/members.txt") or "")
MEM = (MEM:gsub("#[^\n]*", ""))
MEM = (MEM:gsub("\n(%w%w%w%w%w%w%d)[^\n]*", "\n%1"))

function member(id)
  local i = MEM:find("\n" .. id, 1, true)
  return i and tonumber(MEM:sub(i + 7, i + 7))
end

if S.w == nil then S.w = member(ME) and tostring(member(ME)) or "" end

function hash(s)
  local h = 7
  for i = 1, #s do h = (h * 31 + s:byte(i)) % 65521 end
  return h
end

-- Where a person is in the pack (1 = the first met), or nil.
function find(id)
  local from = 1
  while true do
    local i = S.ids:find(id, from, true)
    if not i then return nil end
    if i % 6 == 1 then return i // 6 + 1 end
    from = i + 1
  end
end

function stage()
  return S.g < GOALS[1] and 1 or S.g < GOALS[2] and 2 or 3
end

function asleep()
  local now = sys.time()
  return S.lf ~= nil and now ~= nil and now - S.lf > 4 * 3600
end

function who(id)
  return peers[id] and peers[id].n or id
end

-- Art is a file of rows of characters: loaded, turned into pixels, dropped.
function art(name)
  return assert(fs.load("art/" .. name .. ".lua"))()
end

function go(name, ...)
  ui.clear()
  view, screen = {}, name
  view = assert(fs.load("ui/" .. name .. ".lua"))(...) or {}
end

-- A one-off action: its file runs and is dropped. It may hand over to another
-- action ("act", name, ...) or end on a screen ("go", name), which happens
-- once it has returned, so that two files' code is never held at once.
function act(name, a, b, c)
  local what
  repeat
    what, name, a, b, c = assert(fs.load("act/" .. name .. ".lua"))(a, b, c)
  until what ~= "act"
  if what == "go" then go(name) end
end

MINI, SNACK = {}, {}
do
  local a = art("common")
  for i = 1, 4 do
    MINI[i] = ui.load(a.mini[i])
    SNACK[i] = ui.load(a.food[i])
  end
  CROWN = ui.load(a.crown)
end

-- ---------------------------------------------------------------------------
-- The radio. Every few seconds each badge says hello ("h") with its species,
-- fed-by count and name, which is how badges with the app open find each
-- other. A swap's messages are rarer and handled in act/radio.lua.

function on_mesh(from, m)
  if m.t == "h" then
    if not peers[from] then
      if npeers >= 16 then return end
      npeers = npeers + 1
    end
    local s, g = m.s, m.g
    peers[from] = {n = clean(m.n, from), t = sys.millis(),
      s = math.type(s) == "integer" and s >= 1 and s <= 4 and s or 1,
      g = math.type(g) == "integer" and g > 0 and g or 0}
  elseif type(m.x) == "number" and (m.t == "q" or out and from == out.to and m.x == out.x) then
    act("radio", from, m)
  end
end

-- ---------------------------------------------------------------------------
-- Foraging: a snack from each new badge around, one every three seconds,
-- with a flash of the LEDs so you notice without looking at the screen

local function found(k, where)
  S.bk = S.bk .. k
  save()
  ui.toast("Found a " .. ({"berry", "leaf", "starfruit", "dewdrop"})[k] .. " " .. where)
  glow, glow_c = 4, ({0xEB54BC, 0xACFF9B, 0xE8F180, 0x80ECFF})[k]    -- two blinks in the snack's colour
  if view.refresh then view.refresh() end
end

local function forage()
  if #S.bk >= BASKET then return end
  for _, b in ipairs(mesh.nearby()) do
    if b.kind == "badge" and not S.fo:find(b.id, 1, true) then
      S.fo = (S.fo .. b.id):sub(-240)
      found(hash(b.id) % 4 + 1, "near " .. clean(b.name, b.id))
      return
    end
  end
  lean = lean + 1
  if #S.bk == 0 and lean >= 40 then     -- nobody new for two minutes, and nothing to give
    lean = 0
    found(math.random(4), "on the floor")
  end
end

-- ---------------------------------------------------------------------------
-- Twice a second: the radio's upkeep, LEDs, and the screen's own animation

timer.every(500, function()
  beat = beat + 1
  local now = sys.millis()
  for id, p in pairs(peers) do
    if now - p.t > 10000 then
      peers[id] = nil
      npeers = npeers - 1
    end
  end
  if S.sp then
    if beat % 6 == 1 then mesh.send("*", {t = "h", s = S.sp, g = S.g, n = NAME}) end
    if beat % 6 == 4 then forage() end
  end
  if out and now - out.t > 1500 then
    if out.n >= 4 then
      out = nil
      ui.toast("No answer. Stand closer?")
    else
      out.n, out.t = out.n + 1, now
      mesh.send(out.to, {t = "q", x = out.x, k = out.k})
    end
  end
  -- LEDs: a flash after a swap or a find, else one lit per friend in range
  if glow > 0 then glow = glow - 1 end
  local lit = glow % 2 == 1 and 8 or glow == 0 and S.sp and not asleep() and math.min(npeers, 8) or 0
  local c = glow > 0 and glow_c or GLOW[S.sp or 1]
  for i = 1, 8 do led.hex(i, i <= lit and c or 0) end
  if view.beat then view.beat() end
end)

function on_key(k)
  if view.key and view.key(k) then return true end
  if k == "back" and screen ~= "home" and screen ~= "egg" then
    go("home")
    return true
  end
  return false
end

function on_exit()
  if S.sp then save() end
end

go(S.sp and "home" or "egg")
