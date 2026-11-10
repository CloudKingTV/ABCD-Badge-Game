-- The home screen: the creature, how it's doing, the basket and the pack.
-- Loaded by go("home"); returns what main.lua calls while it shows.

local s = stage()
if shown ~= s then                  -- the creature's pixels, kept until it changes shape
  shown = s
  local a = art("s" .. S.sp .. "_" .. s)
  body, hx, hy = ui.load(a.rows, {palette = {d = 0x0B8F5A, r = 0xD9482B, n = 0x5B2A99, s = 0x3BA7C4}}), a.hx, a.hy
end
if hat_no ~= S.w then               -- and what it wears
  hat_no = S.w
  hat = tonumber(S.w) and ui.load(art("hat" .. S.w)) or nil
end

local sc = ({4, 5, 6})[s]
ui.label(8, 4, SPECIES[S.sp] .. " the " .. ({"Hatchling", "Junior", "Grown"})[s]
  .. (member(ME) and "  [ABCD]" or ""), {size = 14})
local near = ui.label(200, 5, "", {size = 12, color = "dim", w = 112, align = "right"})
local bodyi = ui.image(0, 0, body, {scale = sc})
local hati, hw, hh
if hat then
  hati = ui.image(0, 0, hat, {scale = sc})
  hw, hh = hat:size()
end
local zz = ui.label(0, 0, "z Z z", {size = 14, color = "dim"})
ui.label(172, 26, "FED BY", {size = 12, color = "dim"})
local count = ui.label(172, 38, "", {size = 32, color = "green"})
local bar = ui.bar(172, 80, 140, 8, 0)
local goal = ui.label(172, 92, "", {size = 12, color = "dim"})
local bk = ui.label(172, 112, "", {size = 12, color = "dim"})
local snacks = ui.canvas(172, 126, 42, 6, {scale = 3})
local status = ui.label(172, 148, "", {size = 12, w = 142})
local n = #S.ids // 6
ui.label(8, 163, "PACK " .. n, {size = 12, color = "dim"})
ui.hint("SELECT swap  UP pack  DOWN help")

-- The pack, newest first: one mini each, crowned if they're an ABCD member.
local minis = {}
for k = 1, math.min(n, 14) do
  local i = n - k + 1
  minis[k] = {ui.image(0, 0, MINI[tonumber(S.ps:sub(i, i))] or MINI[1], {scale = 2}),
              S.fl:byte(i) >= 50 and ui.image(0, 0, CROWN, {scale = 2}) or nil}
end

local function place()
  local x, y = 84 - 8 * sc, 158 - 16 * sc + (asleep() and 0 or beat % 2 * 2)
  bodyi:pos(x, y)
  zz:pos(x + 13 * sc, y + hy * sc - 14)
  if hati then hati:pos(x + hx * sc - hw * sc // 2, y + (hy - hh) * sc) end
  for k, m in ipairs(minis) do
    local mx, my = k * 22 - 14, 183 + (k + beat) % 2 * 2
    m[1]:pos(mx, my)
    if m[2] then m[2]:pos(mx + 2, my - 5) end
  end
end

local function tell()
  near:text(npeers > 0 and npeers .. " nearby" or "")
  zz:show(asleep())
  status:text(asleep() and "Asleep. A snack wakes it up."
    or #S.bk == 0 and "No snacks. Walk around to forage."
    or npeers > 0 and npeers .. " nearby. SELECT to swap snacks."
    or "Find someone to swap snacks with.")
end

local function refresh()
  count:text(tostring(S.g))
  local last, next = 0, nil
  for _, g in ipairs(GOALS) do
    if S.g < g then next = g break end
    last = g
  end
  bar:set(next and (S.g - last) * 100 // (next - last) or 100)
  goal:text(next and (next - S.g) .. " more to " .. (s < 3 and "grow" or "level up") or "A legend")
  bk:text("SNACKS " .. #S.bk .. "/" .. BASKET)
  snacks:clear()
  for i = 1, #S.bk do
    snacks:image(SNACK[tonumber(S.bk:sub(i, i))] or SNACK[1], (i - 1) * 7, 0)
  end
  tell()
end

refresh()
place()

return {
  refresh = refresh,
  beat = function()
    tell()
    place()
  end,
  key = function(k)
    if k == "select" then go("share")
    elseif k == "up" then go("pack")
    elseif k == "down" then go("help")
    elseif k == "left" then act("wear", -1)
    elseif k == "right" then act("wear", 1)
    else return false end
    return true
  end,
}
