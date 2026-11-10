-- Everyone you've met, newest first (the newest 40: a list this size is all
-- the memory there is to spare).

local n, rows, k = #S.ids // 6, {}, 0
for name in S.nm:gmatch("[^\n]+") do
  k = k + 1
  if k > n - 40 then
    local f = S.fl:byte(k) or 48
    table.insert(rows, 1, {title = name, right = f >= 50 and "ABCD" or "",
      sub = (SPECIES[tonumber(S.ps:sub(k, k))] or "") .. (f % 2 == 1 and ", fed you" or "")})
  end
end
ui.label(8, 4, n == 0 and "Your pack is empty" or "Your pack: " .. n .. (n > 40 and " (newest 40)" or ""), {size = 14})
ui.list(0, 24, 320, 178, rows, {})
ui.hint("CANCEL back")
