-- TIMING DRIVER ONLY. Unlike host_actions.lua this intentionally drives the
-- build API directly to exclude scripted UI waits from the measured interval.
-- It is NOT evidence that every normal host action reaches the extension.
local root=assert(os.getenv('LRPK_CASE'))
local media=assert(os.getenv('LRPK_MEDIA'))
local f=assert(io.open(root..'/result.txt','w'))
local src
local function log(k,v) f:write(k,'=',tostring(v),'\n');f:flush() end
local ok,err=xpcall(function()
  log('version',reaper.GetAppVersion())
  local plugin=reaper.APIExists('RPKX_Status')
  log('plugin',plugin)
  src=assert(reaper.PCM_Source_CreateFromFile(media),'Cannot create source')
  local started=reaper.time_precise()
  local begin=reaper.PCM_Source_BuildPeaks(src,0)
  local more=begin
  local loops=0
  while more~=0 do
    more=reaper.PCM_Source_BuildPeaks(src,1);loops=loops+1
    if loops>100000 or reaper.time_precise()-started>120 then error('Build did not complete') end
  end
  if begin~=0 then reaper.PCM_Source_BuildPeaks(src,2) end
  local peak_ready=reaper.time_precise()
  log('build_s',string.format('%.9f',peak_ready-started))
  log('begin',begin);log('loops',loops)
  if plugin then
    -- Canonical waveform jobs may return peak-ready while the stronger WAL/fsync
    -- durability work continues. Keep the source alive and independently prove
    -- that persistence reaches the same durable-ready status before teardown.
    local status=reaper.RPKX_Status(src)
    local spins=0
    while status==1 and reaper.time_precise()-peak_ready<120 do
      status=reaper.RPKX_Status(src);spins=spins+1
    end
    log('settle_s',string.format('%.9f',reaper.time_precise()-peak_ready))
    log('settle_spins',spins)
    log('status',status)
  else
    log('settle_s','0.000000000')
  end
  log('peak_read',reaper.GetPeakFileNameEx(media,'',false))
  log('peak_write',reaper.GetPeakFileNameEx(media,'',true))
end,debug.traceback)
if not ok then log('error',err) end
if src then reaper.PCM_Source_Destroy(src) end
log('finished',true);f:close()
reaper.Main_OnCommand(40004,0)
