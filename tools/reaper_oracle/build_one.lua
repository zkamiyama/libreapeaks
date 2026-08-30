-- Build one media file's peak cache and quit.
--
-- This script is deliberately single-source. Reverse-engineering probes showed
-- that spectral state can leak between PCM_Source_BuildPeaks calls when several
-- sources are processed in the same REAPER process. The Python runner launches
-- a new REAPER process for every media file.

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

append("start=" .. media)
local src = reaper.PCM_Source_CreateFromFile(media)
if not src then
  append("ERR PCM_Source_CreateFromFile")
  reaper.Main_OnCommand(40004, 0)
  return
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
reaper.Main_OnCommand(40004, 0) -- File: Quit REAPER
