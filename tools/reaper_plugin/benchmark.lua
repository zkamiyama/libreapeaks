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
  log('plugin',reaper.APIExists('RPKX_Status'))
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
  log('build_s',string.format('%.9f',reaper.time_precise()-started))
  log('begin',begin);log('loops',loops)
  if reaper.APIExists('RPKX_Status') then log('status',reaper.RPKX_Status(src)) end
  log('peak_read',reaper.GetPeakFileNameEx(media,'',false))
  log('peak_write',reaper.GetPeakFileNameEx(media,'',true))
end,debug.traceback)
if not ok then log('error',err) end
if src then reaper.PCM_Source_Destroy(src) end
log('finished',true);f:close()
reaper.Main_OnCommand(40004,0)
