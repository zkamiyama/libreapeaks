-- Query REAPER's canonical read/write peak-cache path for one media file.
-- This script never calls PCM_Source_BuildPeaks.
local media = os.getenv("LIBREAPEAKS_MEDIA")
local result = os.getenv("LIBREAPEAKS_RESULT")
assert(media and media ~= "", "LIBREAPEAKS_MEDIA missing")
assert(result and result ~= "", "LIBREAPEAKS_RESULT missing")

local function esc(s)
  s = tostring(s or "")
  return s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r")
end

local function returned_path(a, b)
  if type(b) == "string" and b ~= "" then return b end
  if type(a) == "string" then return a end
  return ""
end

local source = reaper.PCM_Source_CreateFromFile(media)
assert(source, "PCM_Source_CreateFromFile failed")
local source_type = reaper.GetMediaSourceType(source, "") or ""
local r1, r2 = reaper.GetPeakFileNameEx(source, "", false)
local w1, w2 = reaper.GetPeakFileNameEx(source, "", true)
local read_path = returned_path(r1, r2)
local write_path = returned_path(w1, w2)
reaper.PCM_Source_Destroy(source)

local file = assert(io.open(result, "wb"))
file:write('{"media":"' .. esc(media) .. '","read":"' .. esc(read_path)
  .. '","write":"' .. esc(write_path) .. '","source_type":"' .. esc(source_type) .. '"}\n')
file:close()
reaper.Main_OnCommand(40004, 0)
