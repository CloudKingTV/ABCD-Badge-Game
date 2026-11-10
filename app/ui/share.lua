-- Pick someone nearby to swap snacks with. People with the app open come
-- first; badges without it are listed too, so you know who to tell about it.

local ids = {}

local function rows()
  local r, known = {}, {}
  ids = {}
  for id in pairs(peers) do known[#known + 1] = id end
  table.sort(known)
  for _, id in ipairs(known) do
    local p = peers[id]
    r[#r + 1] = {title = p.n, sub = SPECIES[p.s] .. ", fed by " .. p.g, right = find(id) and "friend" or "new"}
    ids[#r] = id
  end
  for _, b in ipairs(mesh.nearby()) do
    if b.kind == "badge" and not peers[b.id] and #r < 24 then
      r[#r + 1] = {title = clean(b.name, b.id), sub = "", right = "no app yet"}
      ids[#r] = false
    end
  end
  return r
end

local head = ui.label(8, 4, "Swap snacks with...", {size = 14})
local list = ui.list(0, 24, 320, 178, rows(), {on_select = function(i)
  local id = ids[i]
  if id == nil or out then return end
  if not id then
    ui.toast("They need Kinlings from the App Store")
    return
  end
  local k = tonumber(S.bk:sub(1, 1)) or 0
  out = {to = id, x = math.random(1, 2000000000), k = k, t = sys.millis(), n = 1}
  mesh.send(id, {t = "q", x = out.x, k = k})
  head:text("Asking " .. who(id) .. "...")
end})
ui.hint("SELECT swap  CANCEL back")

return {beat = function()
  head:text(out and "Asking " .. who(out.to) .. "..." or "Swap snacks with...")
  if beat % 4 == 0 and not out then list:items(rows()) end
end}
