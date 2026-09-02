-- Build one media file's peak cache and quit.
--
-- This script is deliberately single-source. Reverse-engineering probes showed
-- that spectral state can leak between PCM_Source_BuildPeaks calls when several
-- sources are processed in the same REAPER process. The Python runner launches
-- a new REAPER process for every media file.
--
-- The initial PCM_Source_BuildPeaks(src, 0) return value is also recorded. A
-- zero value means REAPER considers the existing cache reusable and no Run or
-- Finish call is necessary. This lets cache-sharing oracles test reuse with the
-- same process lifecycle used by the established fresh-build corpus.

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

append("start=" .. media)
local src = reaper.PCM_Source_CreateFromFile(media)
if not src then
  append("ERR PCM_Source_CreateFromFile")
  reaper.Main_OnCommand(40004, 0)
  return
end

local ok_type, source_type = pcall(function()
  return reaper.GetMediaSourceType(src, "")
end)
if ok_type then append("TYPE=" .. tostring(source_type or "")) end

local begin_result = reaper.PCM_Source_BuildPeaks(src, 0)
append("BEGIN=" .. tostring(begin_result))
append(begin_result == 0 and "REUSE=1" or "REUSE=0")

local r = begin_result
local loops = 0
while r ~= 0 and loops < 100000 do
  r = reaper.PCM_Source_BuildPeaks(src, 1)
  loops = loops + 1
end

if r ~= 0 then
  append("ERR build did not finish loops=" .. tostring(loops))
else
  if begin_result ~= 0 then
    reaper.PCM_Source_BuildPeaks(src, 2)
  end
  append("OK loops=" .. tostring(loops))
end

reaper.PCM_Source_Destroy(src)
reaper.Main_OnCommand(40004, 0) -- File: Quit REAPER
