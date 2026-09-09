-- Cross-process race driver. Two independent REAPER processes wait on the same
-- filesystem barrier, then rebuild the same media/cache using different peak
-- profiles. No direct extension build API is called.
local root=assert(os.getenv('LRPK_CASE'))
local media=assert(os.getenv('LRPK_MEDIA'))
local op=os.getenv('LRPK_ACTION') or 'manual'
local ready=assert(os.getenv('LRPK_BARRIER_READY'))
local go=assert(os.getenv('LRPK_BARRIER_GO'))
local f=assert(io.open(root..'/result.txt','w'))
local closed=false
local function log(k,v) if not closed then f:write(k,'=',tostring(v),'\n');f:flush() end end
local function quit(err)
  if closed then return end
  if err then log('error',err) end
  pcall(reaper.Main_SaveProjectEx,0,root..'/saved.rpp',0)
  log('finished',true);f:close();closed=true;reaper.Main_OnCommand(40004,0)
end
local function normalize(s) return tostring(s or ''):gsub('%z',''):gsub('%s+',' '):match('^%s*(.-)%s*$'):lower() end
local function actions()
  local by_name={}
  for i=0,65535 do
    local id=reaper.kbd_enumerateActions(0,i)
    if not id or id<=0 then break end
    local name=reaper.kbd_getTextFromCmd(id,0) or ''
    by_name[normalize(name)]=id
  end
  return by_name
end
local function main()
  log('version',reaper.GetAppVersion());log('resource',reaper.GetResourcePath())
  log('plugin',reaper.APIExists('RPKX_Status'));log('operation',op)
  if not reaper.APIExists('RPKX_Status') then error('reference extension API missing') end
  local by_name=actions()
  local function action(names,candidates)
    local id
    for _,name in ipairs(names) do if by_name[normalize(name)] then id=by_name[normalize(name)];break end end
    if not id then
      for _,candidate in ipairs(candidates or {}) do
        local actual=normalize(reaper.kbd_getTextFromCmd(candidate,0))
        for _,name in ipairs(names) do if actual==normalize(name) then id=candidate;break end end
        if id then break end
      end
    end
    if not id then error('Missing host action: '..table.concat(names,' / ')) end
    log('action',reaper.kbd_getTextFromCmd(id,0));log('action_id',id)
    reaper.Main_OnCommand(id,0);log('action_returned',id)
  end
  local function source()
    local item=reaper.GetMediaItem(0,0);if not item then return nil end
    local take=reaper.GetActiveTake(item);if not take then return nil end
    local src=reaper.GetMediaItemTake_Source(take)
    for i=1,16 do local p=reaper.GetMediaSourceParent(src);if not p then break end;src=p end
    return src
  end
  log('phase','import_begin');reaper.InsertMedia(media,0);log('phase','import_returned')
  local initial=reaper.time_precise();local acted=false;local action_time=initial;local last=-999
  local tick
  tick=function()
    local ok,err=xpcall(function()
      local src=source()
      if not src then if reaper.time_precise()-initial>8 then error('No imported source') end;reaper.defer(tick);return end
      local st=reaper.RPKX_Status(src)
      if st~=last then log('status',st);last=st end
      local now=reaper.time_precise();local age=now-initial
      if not acted and age>1 and (st==2 or st==0 or st==-1) then
        acted=true;log('pre_action_status',st)
        if op=='manual' then action({'Peaks: Rebuild all peaks','Peaks: Rebuild peaks'},{41101,40048})
        elseif op=='spectrogram' then action({'Peaks: Toggle spectrogram'},{42073,42294})
        else error('unsupported race operation: '..op) end
        action_time=reaper.time_precise();reaper.defer(tick);return
      end
      reaper.UpdateArrange()
      if acted and now-action_time>3 and (st==2 or st==-1) then
        log('final_status',st);log('peak_read',reaper.GetPeakFileNameEx(media,'',false));log('peak_write',reaper.GetPeakFileNameEx(media,'',true))
        local arr=reaper.new_array(64);local n=reaper.PCM_Source_GetPeaks(src,100,0,2,16,0,arr);log('peak_count',n & 0xfffff)
        quit();return
      end
      if age>55 then error('cross-process host timeout status='..st) end
      reaper.defer(tick)
    end,debug.traceback)
    if not ok then quit(err) end
  end
  reaper.defer(tick)
end
local function wait_for_barrier()
  local rf=assert(io.open(ready,'w'));rf:write('ready\n');rf:close();log('barrier_ready',true)
  local started=reaper.time_precise()
  local poll
  poll=function()
    local gf=io.open(go,'r')
    if gf then gf:close();log('barrier_go',true);local ok,err=xpcall(main,debug.traceback);if not ok then quit(err) end;return end
    -- macOS host startup is UI-mediated and a second independent REAPER can
    -- legitimately need more than 30 seconds to reach its own ready marker.
    -- This timeout only bounds the pre-GO idle wait; it does not change when
    -- either process begins the shared-cache mutation.
    if reaper.time_precise()-started>60 then quit('barrier timeout');return end
    reaper.defer(poll)
  end
  reaper.defer(poll)
end
local ok,err=xpcall(wait_for_barrier,debug.traceback)
if not ok then quit(err) end
