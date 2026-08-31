-- Build one media source peak cache, record REAPER's actual source type/paths, and quit.
-- Intended for compatibility/oracle automation. One REAPER process per source.
local media = os.getenv("REAPEAKS_MEDIA")
local result = os.getenv("REAPEAKS_RESULT")

local function append(line)
  if not result or result == "" then return end
  local f = assert(io.open(result, "a"))
  f:write(line, "\n")
  f:close()
end

if not media or media == "" then
  append("ERR no REAPEAKS_MEDIA")
  reaper.Main_OnCommand(40004, 0)
  return
end

local ok_read_path, read_path = pcall(function()
  return reaper.GetPeakFileNameEx(media, "", false)
end)
if ok_read_path then append("PEAK_READ=" .. tostring(read_path or "")) end

local ok_write_path, write_path = pcall(function()
  return reaper.GetPeakFileNameEx(media, "", true)
end)
if ok_write_path then append("PEAK_WRITE=" .. tostring(write_path or "")) end

local src = reaper.PCM_Source_CreateFromFile(media)
if not src then
  append("ERR PCM_Source_CreateFromFile")
  reaper.Main_OnCommand(40004, 0)
  return
end

local ok_type, source_type = pcall(function()
  return reaper.GetMediaSourceType(src, "")
end)
if ok_type then
  append("TYPE=" .. tostring(source_type or ""))
else
  append("TYPE_ERR=" .. tostring(source_type))
end

local ok_len, length, is_qn = pcall(function()
  return reaper.GetMediaSourceLength(src)
end)
if ok_len then
  append("LENGTH=" .. tostring(length or "") .. ";QN=" .. tostring(is_qn or false))
end

local r = reaper.PCM_Source_BuildPeaks(src, 0)
local loops = 0
while r ~= 0 and loops < 100000 do
  r = reaper.PCM_Source_BuildPeaks(src, 1)
  loops = loops + 1
end

if r ~= 0 then
  append("ERR build did not finish loops=" .. tostring(loops))
else
  reaper.PCM_Source_BuildPeaks(src, 2)
  append("OK loops=" .. tostring(loops))
end

reaper.PCM_Source_Destroy(src)
reaper.Main_OnCommand(40004, 0)