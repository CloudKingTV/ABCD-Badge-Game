-- LEFT / RIGHT on the home screen: wear the next item, or nothing. An ABCD
-- member can always wear their own.

local d = ...
local own = member(ME)
local all = (own and not S.it:find(tostring(own), 1, true) and tostring(own) or "") .. S.it
if #all == 0 then
  ui.toast("Meet an ABCD member for something to wear")
  return
end
local i = S.w ~= "" and all:find(S.w, 1, true) or 0
i = (i + d) % (#all + 1)
S.w = i == 0 and "" or all:sub(i, i)
save()
return "go", "home"
