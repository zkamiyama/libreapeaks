-- Read waveform/spectral peaks from an existing cache without rebuilding it.
-- Intended only for fresh-process compatibility oracles.

local media = os.getenv("REAPEAKS_MEDIA")
local result = os.getenv("REAPEAKS_RESULT")
assert(media and media ~= "", "REAPEAKS_MEDIA missing")
assert(result and result ~= "", "REAPEAKS_RESULT missing")

local function append(line)
  local f = assert(io.open(result, "a"))
  f:write(line, "\n")
  f:close()
end

local src = reaper.PCM_Source_CreateFromFile(media)
if not src then
  append("ERR create")
  reaper.Main_OnCommand(40004, 0)
  return
end

local begin_result = reaper.PCM_Source_BuildPeaks(src, 0)
append("BEGIN=" .. tostring(begin_result))

local rates = {300.0, 10.0, 1.0}
local extras = {0, 115}
for _, rate in ipairs(rates) do
  for _, extra in ipairs(extras) do
    local sample_count = 16
    local buf = reaper.new_array(sample_count * 3)
    local retval = reaper.PCM_Source_GetPeaks(
      src, rate, 0.125, 1, sample_count, extra, buf
    )
    local values = buf.table(1, sample_count * 3)
    local encoded = {}
    for index = 1, #values do
      encoded[index] = string.format("%.17g", values[index])
    end
    append(
      "PEAK rate=" .. string.format("%.0f", rate)
      .. " extra=" .. tostring(extra)
      .. " ret=" .. tostring(retval)
      .. " values=" .. table.concat(encoded, ",")
    )
  end
end

append("READ_OK=1")
reaper.PCM_Source_Destroy(src)
reaper.Main_OnCommand(40004, 0)
