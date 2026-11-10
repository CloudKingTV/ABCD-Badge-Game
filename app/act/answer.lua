-- SELECT or CANCEL on "... wants to swap snacks. Swap?": answer, and on a
-- yes hand over the first snack in the basket and take theirs.

local yes = ...
local q = inc
inc = nil
if not answered[q.from] then
  nanswered = nanswered + 1
  if nanswered > 24 then answered, nanswered = {}, 1 end
end
local k = yes and tonumber(S.bk:sub(1, 1)) or 0
if k > 0 then S.bk = S.bk:sub(2) end
local a = {t = yes and "a" or "n", x = q.x, k = k}
answered[q.from] = a
mesh.send(q.from, a)
if yes then return "act", "meet", q.from, q.k, k end
