-- Build one source's peak cache, report the media-source type, then keep the
-- source alive until an external controller captures the REAPER process maps.
-- This lets CI prove which decoder libraries are loaded after real decoding.
local media = os.getenv("REAPEAKS_MEDIA")
local result = os.getenv("REAPEAKS_RESULT")
local ready = os.getenv("REAPEAKS_READY")
local release = os.getenv("REAPEAKS_RELEASE")

local function append(line)
  if not result or result == "" then return end
  local f = assert(io.open(result, "a"))
  f:write(line, "\n")
  f:close()
end

local function touch(path)
  if not path or path == "" then return end
  local f = assert(io.open(path, "w"))
  f:write("ready\n")
  f:close()
end

local function exists(path)
  if not path or path == "" then return false end
  local f = io.open(path, "r")
  if not f then return false end
  f:close()
  return true
end

if not media or media == "" then
  append("ERR no REAPEAKS_MEDIA")
  reaper.Main_OnCommand(40004, 0)
  return
end

local src = reaper.PCM_Source_CreateFromFile(media)
if not src then
  append("ERR PCM_Source_CreateFromFile")
  reaper.Main_OnCommand(40004, 0)
  return
end

local source_type = reaper.GetMediaSourceType(src, "")
append("TYPE=" .. tostring(source_type or ""))

local length, is_qn = reaper.GetMediaSourceLength(src)
append("LENGTH=" .. tostring(length or "") .. ";QN=" .. tostring(is_qn or false))

local r = reaper.PCM_Source_BuildPeaks(src, 0)
local loops = 0
while r ~= 0 and loops < 100000 do
  r = reaper.PCM_Source_BuildPeaks(src, 1)
  loops = loops + 1
end

if r ~= 0 then
  append("ERR build did not finish loops=" .. tostring(loops))
  reaper.PCM_Source_Destroy(src)
  reaper.Main_OnCommand(40004, 0)
  return
end

reaper.PCM_Source_BuildPeaks(src, 2)
append("OK loops=" .. tostring(loops))
touch(ready)

local function finish_after_capture()
  if exists(release) then
    reaper.PCM_Source_Destroy(src)
    reaper.Main_OnCommand(40004, 0)
    return
  end
  reaper.defer(finish_after_capture)
end

finish_after_capture()
