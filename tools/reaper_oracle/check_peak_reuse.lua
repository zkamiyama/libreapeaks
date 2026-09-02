-- Check whether REAPER considers an existing peak cache reusable.
--
-- PCM_Source_BuildPeaks(src, 0) is the public PeaksBuild_Begin entrypoint. The
-- REAPER API contract says a zero return means no further build work is needed.
-- We record that decision before doing anything else. If Begin requests work,
-- the probe then completes the build normally so the PCM_source is not destroyed
-- with an unfinished peak builder. A matching/reused cache is never rebuilt.

local media = os.getenv("REAPEAKS_MEDIA")
local result = os.getenv("REAPEAKS_RESULT")
assert(media and media ~= "", "REAPEAKS_MEDIA missing")
assert(result and result ~= "", "REAPEAKS_RESULT missing")

local function returned_path(a, b)
  if type(b) == "string" and b ~= "" then return b end
  if type(a) == "string" then return a end
  return ""
end

local f = assert(io.open(result, "wb"))
local src = reaper.PCM_Source_CreateFromFile(media)
if not src then
  f:write("ERR create\n")
  f:close()
  reaper.Main_OnCommand(40004, 0)
  return
end

local r1, r2 = reaper.GetPeakFileNameEx(src, "", false)
local w1, w2 = reaper.GetPeakFileNameEx(src, "", true)
f:write("PEAK_READ=" .. returned_path(r1, r2) .. "\n")
f:write("PEAK_WRITE=" .. returned_path(w1, w2) .. "\n")

local begin_result = reaper.PCM_Source_BuildPeaks(src, 0)
f:write("BEGIN=" .. tostring(begin_result) .. "\n")
f:write(begin_result == 0 and "REUSE=1\n" or "REUSE=0\n")

if begin_result ~= 0 then
  local remaining = begin_result
  local loops = 0
  while remaining ~= 0 and loops < 100000 do
    remaining = reaper.PCM_Source_BuildPeaks(src, 1)
    loops = loops + 1
  end
  if remaining == 0 then
    reaper.PCM_Source_BuildPeaks(src, 2)
    f:write("REBUILD_FINISHED=1\n")
  else
    f:write("ERR rebuild did not finish loops=" .. tostring(loops) .. "\n")
  end
end

f:close()
reaper.PCM_Source_Destroy(src)
reaper.Main_OnCommand(40004, 0)
