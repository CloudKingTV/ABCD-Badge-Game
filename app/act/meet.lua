-- A swap went through with badge id: we got snack `got` and gave `gave`
-- (0: none). Adds them to the pack, grows the creature on a new person's
-- first snack, and takes an ABCD member's item.

local id, got, gave = ...
local i, item, was = find(id), member(id), stage()
if not i and #S.ids // 6 < PACK then
  S.ids = S.ids .. id
  S.fl = S.fl .. "0"
  S.ps = S.ps .. (peers[id] and peers[id].s or 1)
  S.nm = S.nm .. who(id) .. "\n"
  i = #S.ids // 6
end
local f, grew = i and S.fl:byte(i) or 48, false
if got > 0 then
  S.lf = sys.time() or S.lf
  if i and f % 2 == 0 then          -- their first snack for us: that's what it grows on
    f, grew = f + 1, true
    S.g = S.g + 1
  end
end
if item and f < 50 then f = f + 2 end
if i then S.fl = S.fl:sub(1, i - 1) .. string.char(f) .. S.fl:sub(i + 1) end
local gift = item and not S.it:find(tostring(item), 1, true)
if gift then
  S.it = S.it .. item
  S.w = tostring(item)
end
save()
glow, glow_c = 6, gift and 0xE8F180 or GLOW[S.sp]
ui.toast(gift and who(id) .. " gave you a " .. ({"crown", "halo", "flower", "bow", "party hat", "star"})[item] .. "!"
  or grew and stage() > was and SPECIES[S.sp] .. " grew up!"
  or grew and "Yum! " .. who(id) .. " joined your pack"
  or got > 0 and "Yum! Thanks, " .. who(id)
  or gave > 0 and "You fed " .. who(id)
  or "No snacks to swap. Go forage!")
return "go", "home"
