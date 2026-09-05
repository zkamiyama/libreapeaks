-- Normal host actions only: never call PCM_Source_BuildPeaks or ForceBuild.
local root=assert(os.getenv('LRPK_CASE'))
local media=assert(os.getenv('LRPK_MEDIA'))
local op=os.getenv('LRPK_ACTION') or 'import'
local plugin=os.getenv('LRPK_EXPECT_PLUGIN')=='1'
local f=assert(io.open(root..'/result.txt','w'))
local closed=false
local function log(k,v) if not closed then f:write(k,'=',tostring(v),'\n');f:flush() end end
local function quit(err)
  if closed then return end
  if err then log('error',err) end
  local ok,e=pcall(reaper.Main_SaveProjectEx,0,root..'/saved.rpp',0)
  if not ok then log('save_error',e) end
  log('finished',true);f:close();closed=true;reaper.Main_OnCommand(40004,0)
end
local function main()
  log('version',reaper.GetAppVersion());log('resource',reaper.GetResourcePath())
  log('plugin',reaper.APIExists('RPKX_Status'))
  local by_id,by_name={},{}
  local function normalize(s) return tostring(s or ''):gsub('%z',''):gsub('%s+',' '):match('^%s*(.-)%s*$'):lower() end
  for i=0,65535 do
    local id=reaper.kbd_enumerateActions(0,i)
    if not id or id<=0 then break end
    -- Read each description from its ID immediately. Avoid depending on the
    -- enumeration wrapper's optional output-string behaviour.
    local name=reaper.kbd_getTextFromCmd(id,0) or ''
    by_id[id]=name;by_name[normalize(name)]=id
  end
  local af=assert(io.open(root..'/actions.txt','w'))
  for id,name in pairs(by_id) do if normalize(name):find('peak') or normalize(name):find('offline') or normalize(name):find('online') or normalize(name):find('reverse') then af:write(id,'\t',name,'\n') end end
  af:close()
  local function action(names,candidates)
    local id
    for _,name in ipairs(names) do if by_name[normalize(name)] then id=by_name[normalize(name)];break end end
    if not id then
      -- Numeric fallbacks are accepted only when the runtime description
      -- matches, not merely because a remembered command ID exists.
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
  local function rebuild() action({'Peaks: Rebuild all peaks','Peaks: Rebuild peaks'},{41101,40048}) end
  local function source()
    local item=reaper.GetMediaItem(0,0);if not item then return nil end
    local take=reaper.GetActiveTake(item);if not take then return nil end
    local src=reaper.GetMediaItemTake_Source(take)
    for i=1,16 do local p=reaper.GetMediaSourceParent(src);if not p then break end;src=p end
    return src
  end
  local initial=reaper.time_precise();local acted=false;local action_time=initial
  log('phase','import_begin')
  if op=='project' then reaper.Main_openProject(root..'/input.rpp') else reaper.InsertMedia(media,0) end
  log('phase','import_returned')
  local last=-999
  local tick
  tick=function()
    local ok,err=xpcall(function()
      local src=source()
      if not src then if reaper.time_precise()-initial>5 then error('No imported source') end;reaper.defer(tick);return end
      local st=plugin and reaper.APIExists('RPKX_Status') and reaper.RPKX_Status(src) or 0
      if st~=last then log('status',st);last=st end
      local now=reaper.time_precise();local age=now-initial
      if not acted and age>1 and (not plugin or st==2 or st==0 or st==-1 or st==-2) then
        acted=true;log('pre_action_status',st)
        if op=='manual' then rebuild()
        elseif op=='selected' then action({'Peaks: Rebuild peaks for selected items'},{40441})
        elseif op=='spectrogram' then action({'Peaks: Toggle spectrogram'},{42073})
        elseif op=='reverse' then action({'Item properties: Reverse active take','Item properties: Toggle take reverse'},{40912});rebuild()
        elseif op=='online' then action({'Item: Set all media offline'},{40100});action({'Item: Set all media online'},{40101}) end
        action_time=reaper.time_precise();reaper.defer(tick);return
      end
      reaper.UpdateArrange()
      if now-action_time>2 and acted and (not plugin or st==2 or st==-1 or st==-2) then
        log('type',reaper.GetMediaSourceType(src,''));log('final_status',st)
        log('peak_read',reaper.GetPeakFileNameEx(media,'',false));log('peak_write',reaper.GetPeakFileNameEx(media,'',true))
        local arr=reaper.new_array(64)
        local n=reaper.PCM_Source_GetPeaks(src,100,0,2,16,0,arr)
        log('peak_count',n & 0xfffff);quit();return
      end
      if age>40 then error('Host scheduler timeout status='..st) end
      reaper.defer(tick)
    end,debug.traceback)
    if not ok then quit(err) end
  end
  reaper.defer(tick)
end
local ok,err=xpcall(main,debug.traceback)
if not ok then quit(err) end
