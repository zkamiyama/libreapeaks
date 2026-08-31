-- Export GetPeakFileNameEx() read/write results for exact source-name strings.
--
-- Environment:
--   REAPEAKS_MANIFEST  UTF-8 text file, one source identity per line
--   REAPEAKS_RESULT    destination JSON file
--
-- The source strings are intentionally not normalized: REAPER's alternate
-- cache SHA-1 key includes dot components, separator spelling and non-ASCII
-- bytes exactly as supplied to GetPeakFileNameEx().

local manifest = os.getenv("REAPEAKS_MANIFEST")
local result = os.getenv("REAPEAKS_RESULT")

local function json_escape(value)
  value = tostring(value or "")
  value = value:gsub("\\", "\\\\")
  value = value:gsub('"', '\\"')
  value = value:gsub("\b", "\\b")
  value = value:gsub("\f", "\\f")
  value = value:gsub("\n", "\\n")
  value = value:gsub("\r", "\\r")
  value = value:gsub("\t", "\\t")
  value = value:gsub("[%z\1-\31]", function(c)
    return string.format("\\u%04x", string.byte(c))
  end)
  return '"' .. value .. '"'
end

local function fail(message)
  if result and result ~= "" then
    local out = io.open(result, "w")
    if out then
      out:write('{"version":1,"error":', json_escape(message), '}\n')
      out:close()
    end
  end
  reaper.Main_OnCommand(40004, 0)
end

if not manifest or manifest == "" then
  fail("REAPEAKS_MANIFEST is not set")
  return
end
if not result or result == "" then
  reaper.Main_OnCommand(40004, 0)
  return
end

local input, input_error = io.open(manifest, "r")
if not input then
  fail("cannot open manifest: " .. tostring(input_error))
  return
end
local rows = {}
for source in input:lines() do
  if source ~= "" then
    rows[#rows + 1] = {
      source = source,
      read = reaper.GetPeakFileNameEx(source, "", false),
      write = reaper.GetPeakFileNameEx(source, "", true),
    }
  end
end
input:close()

local out, output_error = io.open(result, "w")
if not out then
  reaper.ShowConsoleMsg("cannot create peak-cache map: " .. tostring(output_error) .. "\n")
  reaper.Main_OnCommand(40004, 0)
  return
end
out:write('{"version":1,"resource_path":', json_escape(reaper.GetResourcePath()), ',"entries":{')
for index, row in ipairs(rows) do
  if index > 1 then out:write(',') end
  out:write(
    json_escape(row.source),
    ':{"read":', json_escape(row.read),
    ',"write":', json_escape(row.write),
    '}'
  )
end
out:write('}}\n')
out:close()
reaper.Main_OnCommand(40004, 0)
