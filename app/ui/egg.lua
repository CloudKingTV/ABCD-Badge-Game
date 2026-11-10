-- The first launch: an egg to warm until it hatches. The species comes from
-- the badge's id, so a badge always hatches the same one.

local warm = 0
ui.label(0, 12, "An egg!", {size = 20, w = 320, align = "center"})
local egg = ui.image(112, 44, art("egg"), {scale = 6})
local msg = ui.label(0, 150, "Keep it warm: press SELECT", {size = 14, w = 320, align = "center", color = "dim"})
ui.hint("SELECT warm it  CANCEL exit")

return {key = function(k)
  if k ~= "select" then return false end
  warm = warm + 1
  egg:pos(112 + (warm % 2 * 2 - 1) * 3, 44)
  msg:text(warm < 3 and "It's wobbling..." or "Almost there...")
  if warm >= 5 then
    S.sp = ME == "------" and math.random(4) or hash(ME) % 4 + 1
    S.bk = tostring(math.random(4)) .. math.random(4)   -- two snacks to give away
    S.lf = sys.time()
    save()
    glow, glow_c = 8, GLOW[S.sp]
    ui.toast("It's a " .. SPECIES[S.sp] .. "!")
    go("home")
  end
  return true
end}
