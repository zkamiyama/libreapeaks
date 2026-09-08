-- Dedicated source-change race observer. Import starts the production job; once
-- that specific job reports failure, exit immediately so a later stable-source
-- REAPER recheck cannot mask the raced job's no-write atomicity.
local root=assert(os.getenv('LRPK_CASE'))
local media=assert(os.getenv('LRPK_MEDIA'))
local f=assert(io.open(root..'/result.txt','w'))
local closed=false
local function log(k,v) if not closed then f:write(k,'=',tostring(v),'\n');f:flush() end end
local function quit(err)
  if closed then return end
  if err then log('error',err) end
  log('finished',true);f:close();closed=true;reaper.Main_OnCommand(40004,0)
end
local function source()
  local item=reaper.GetMediaItem(0,0);if not item then return nil end
  local take=reaper.GetActiveTake(item);if not take then return nil end
  local src=reaper.GetMediaItemTake_Source(take)
  for i=1,16 do local p=reaper.GetMediaSourceParent(src);if not p then break end;src=p end
  return src
end
local function main()
  log('version',reaper.GetAppVersion());log('resource',reaper.GetResourcePath())
  log('plugin',reaper.APIExists('RPKX_Status'))
  if not reaper.APIExists('RPKX_Status') then error('reference extension API missing') end
  -- Preserve the unwrapped-pointer safety probe used by the normal host driver.
  local midi=root..'/unwrapped.mid'
  local uf=assert(io.open(midi,'wb'))
  uf:write('MThd',string.char(0,0,0,6,0,0,0,1,0,96),'MTrk',string.char(0,0,0,4,0,255,47,0));uf:close()
  local native=reaper.PCM_Source_CreateFromFile(midi)
  if not native then error('Could not create native unwrapped MIDI source') end
  log('unwrapped_type',reaper.GetMediaSourceType(native,'') or '')
  log('unwrapped_status',reaper.RPKX_Status(native));log('unwrapped_force',reaper.RPKX_ForceBuild(native))
  reaper.PCM_Source_Destroy(native)

  log('phase','import_begin');reaper.InsertMedia(media,0);log('phase','import_returned')
  local started=reaper.time_precise();local last=-999
  local tick
  tick=function()
    local ok,err=xpcall(function()
      local src=source()
      if not src then if reaper.time_precise()-started>8 then error('No imported source') end;reaper.defer(tick);return end
      local st=reaper.RPKX_Status(src)
      if st~=last then log('status',st);last=st end
      if st==-1 then
        log('failure_observed',true);log('final_status',st)
        log('peak_read',reaper.GetPeakFileNameEx(media,'',false));log('peak_write',reaper.GetPeakFileNameEx(media,'',true))
        quit();return
      end
      if reaper.time_precise()-started>60 then error('source-race timeout status='..st) end
      reaper.defer(tick)
    end,debug.traceback)
    if not ok then quit(err) end
  end
  reaper.defer(tick)
end
local ok,err=xpcall(main,debug.traceback)
if not ok then quit(err) end
