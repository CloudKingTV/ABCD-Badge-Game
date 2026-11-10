-- A swap message from another badge: "q" asks, "a" accepts, "n" declines,
-- "b" is busy. A request is repeated until it's answered (main.lua's beat),
-- and an answer is remembered, so a repeat gets the same one without the
-- snack changing hands twice.

local from, m = ...
local t, k = m.t, m.k
k = math.type(k) == "integer" and k >= 1 and k <= 4 and k or 0     -- the snack they give

if t == "q" then
  local a = answered[from]
  if a and a.x == m.x then
    mesh.send(from, a)                  -- our answer got lost: the same again
    return
  end
  if out and out.to == from and from < ME then out = nil end      -- we asked each other: theirs wins
  if inc or out or not S.sp then
    mesh.send(from, {t = "b", x = m.x})
    return
  end
  inc = {from = from, x = m.x, k = k}
  ui.confirm(who(from) .. " wants to swap snacks. Swap?", function(yes) act("answer", yes) end)
else                                    -- an answer to what we asked (main.lua checked it is)
  local gave = out.k
  out = nil
  if t == "a" then
    if gave > 0 then S.bk = S.bk:sub(2) end
    return "act", "meet", from, k, gave
  end
  ui.toast(t == "n" and "Maybe later" or "They're busy. Try again")
end
